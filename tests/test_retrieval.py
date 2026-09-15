"""Deferred regression cases for feedback 13; no model/media dependencies required."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
from mad_worker.retrieval import plan_query, candidate_features, rank_candidates, neighbour_roles
from mad_worker.query_vectors import cached_embeddings


CARDS = {
    "soyo": {"name": "长崎素世", "aliases": ["素世"], "appearance": "棕发女孩"},
    "saki": {"name": "丰川祥子", "aliases": ["祥子"], "appearance": "蓝发女孩"},
}


def row(caption="", matches=()):
    return {"caption": caption, "subtitle": "", "character_matches": [
        {"character_id": key, "confidence": confidence} for key, confidence in matches]}


class QueryPlanningTests(unittest.TestCase):
    def test_embedded_names_and_appearance_keep_actor_order(self):
        plan = plan_query("长崎素世请求丰川祥子不要离开", CARDS)
        self.assertEqual(plan.groups, (("soyo",), ("saki",)))
        self.assertEqual(plan.event, "请求 不要离开")
        self.assertEqual(plan.appearance, "（棕发女孩）请求（蓝发女孩）不要离开")
        self.assertEqual(len(plan.embedding_texts), 3)
        self.assertEqual(plan.variants["original"], "长崎素世请求丰川祥子不要离开")

    def test_bare_names_and_english_boundaries(self):
        self.assertTrue(plan_query("素世和祥子", CARDS).person_only)
        self.assertEqual(plan_query("祥子", CARDS).embedding_texts, [])
        cards = {"a": {"name": "Ann", "aliases": ["爱"], "appearance": ""}}
        self.assertEqual(plan_query("Annabelle loves music", cards).groups, ())
        self.assertEqual(plan_query("喜爱音乐", cards).groups, ())
        self.assertEqual(plan_query("ＡＮＮ sings", cards).groups, (("a",),))
        self.assertEqual(plan_query("爱", cards).groups, (("a",),))

    def test_ambiguous_alias_does_not_invent_an_appearance(self):
        cards = {k: v | {"aliases": ["小祥"]} for k, v in CARDS.items()}
        plan = plan_query("小祥正在哭泣", cards)
        self.assertEqual(len(plan.groups[0]), 2)
        self.assertEqual(plan.appearance, "")
        self.assertTrue(plan.public(cards)["characters"][0]["ambiguous"])

    def test_description_budget_does_not_remove_trailing_action(self):
        cards = {k: v | {"appearance": "细节" * 1000} for k, v in CARDS.items()}
        plan = plan_query("素世和祥子" * 50 + "请求不要离开", cards)
        self.assertLessEqual(len(plan.appearance), 2000)
        self.assertTrue(plan.appearance.endswith("请求不要离开"))


class RankingTests(unittest.TestCase):
    def test_character_only_match_cannot_enter_event_results(self):
        plan = plan_query("素世请求祥子不要离开", CARDS)
        features = candidate_features(plan, row("两人在吃饭", [("soyo", "high"), ("saki", "high")]),
            {"original": 0.8, "event": 0}, {"original": 0.2, "event": 0.1}, CARDS, {})
        self.assertIsNone(features)

    def test_strong_event_without_identity_beats_weak_event_with_identity(self):
        plan = plan_query("素世请求祥子不要离开", CARDS)
        candidates = [
            {"_ranking": candidate_features(plan, row(), {"event": 1, "original": 0.5}, {}, CARDS, {})},
            {"_ranking": candidate_features(plan, row(matches=[("soyo", "high"), ("saki", "high")]),
                {"event": 0.3, "original": 0.8}, {}, CARDS, {})},
        ]
        rank_candidates(candidates, plan)
        self.assertGreater(candidates[0]["score"], candidates[1]["score"])
        self.assertEqual(candidates[0]["ranking_details"]["role_direct"], 0)

    def test_person_confidence_and_text_mentions_are_distinct(self):
        plan = plan_query("祥子", CARDS)
        candidates = [{"_ranking": candidate_features(plan, item, {}, {}, CARDS, {})} for item in
            [row(matches=[("saki", "high")]), row(matches=[("saki", "medium")]), row("丰川祥子被台词提及")]]
        rank_candidates(candidates, plan)
        self.assertGreater(candidates[0]["score"], candidates[1]["score"])
        self.assertGreater(candidates[1]["score"], candidates[2]["score"])

    def test_strong_appearance_paraphrase_survives_as_lower_weight_fallback(self):
        plan = plan_query("素世请求祥子不要离开", CARDS)
        detail = candidate_features(plan, row(), {"event": 0}, {"event": 0.15, "appearance": 0.8}, CARDS, {})
        self.assertIsNotNone(detail)
        self.assertEqual(detail["route"], "appearance_fallback")
        self.assertLessEqual(detail["event_strength"], 0.5)

    def test_equal_evidence_has_equal_rrf_rank(self):
        plan = plan_query("祥子哭泣", CARDS)
        candidates = [{"_ranking": candidate_features(plan, row(), {"event": 1, "original": 0.6},
            {"event": 0.8, "original": 0.8, "appearance": 0.8}, CARDS, {})} for _ in range(3)]
        rank_candidates(candidates, plan)
        self.assertEqual(len({c["score"] for c in candidates}), 1)

    def test_crossing_appearance_threshold_keeps_stronger_original_evidence(self):
        plan = plan_query("素世请求祥子不要离开", CARDS)
        details = [candidate_features(plan, row(), {"event": 0},
            {"event": 0.1, "original": 0.9, "appearance": value}, CARDS, {}) for value in (0.39, 0.4)]
        self.assertTrue(all(detail["route"] == "original_fallback" for detail in details))
        self.assertEqual(details[0]["event_strength"], details[1]["event_strength"])

    def test_crossing_event_threshold_keeps_stronger_paraphrase_evidence(self):
        plan = plan_query("素世请求祥子不要离开", CARDS)
        details = [candidate_features(plan, row(), {"event": 0},
            {"event": value, "appearance": 0.8}, CARDS, {}) for value in (0.219, 0.22)]
        self.assertTrue(all(detail["route"] == "appearance_fallback" for detail in details))
        self.assertEqual(details[0]["event_strength"], details[1]["event_strength"])

    def test_neighbour_identity_remains_soft_and_does_not_modify_matches(self):
        rows = [row(matches=[("soyo", "high")]), row(matches=[("saki", "high")])]
        for i, item in enumerate(rows):
            item.update(id=i, video_id="video", shot_id=f"shot_{i}", start=i * 2, end=(i + 1) * 2,
                        metadata={"sample_times": [i * 2 + 1]})
            item["character_matches"][0]["frame_indices"] = [0]
        nearby = neighbour_roles(rows, {"video": [(0, 2), (2, 4)]})
        self.assertEqual(nearby[0], {"saki": 1})
        plan = plan_query("素世请求祥子不要离开", CARDS)
        features = candidate_features(plan, rows[0], {"event": 1}, {}, CARDS, nearby[0])
        self.assertGreater(features["role_support"], features["role_direct"])
        self.assertLess(features["role_support"], 1)
        self.assertEqual(len(rows[0]["character_matches"]), 1)


class QueryCacheTests(unittest.TestCase):
    def test_valid_but_wrong_dimension_cache_is_refreshed(self):
        calls = []
        class Client:
            def embed(self, texts):
                calls.append(list(texts))
                return [[1.0, 0.0] for _ in texts]
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            cached_embeddings(workspace, "fixture", ["query"], Client, expected_dimensions={2})
            path = next((workspace / "cache" / "query_vectors").glob("*.json"))
            data = json.loads(path.read_text())
            data["vector"] = [1, 0, 0]
            path.write_text(json.dumps(data))
            result, info = cached_embeddings(workspace, "fixture", ["query"], Client, expected_dimensions={2})
            self.assertEqual(len(calls), 2)
            self.assertEqual(info["misses"], 1)
            self.assertEqual(len(result["query"]), 2)

    def test_one_batch_then_cache_hit_and_corrupt_file_recovery(self):
        calls = []
        class Client:
            def embed(self, texts):
                calls.append(list(texts))
                return [[1.0, 0.0] for _ in texts]
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            result, first = cached_embeddings(workspace, "fixture-model", ["query-one", "query-two"], Client)
            again, second = cached_embeddings(workspace, "fixture-model", ["query-one", "query-two"], Client)
            self.assertEqual(result, again)
            self.assertEqual(first["misses"], 2)
            self.assertEqual(second["hits"], 2)
            self.assertEqual(len(calls), 1)
            path = next((workspace / "cache" / "query_vectors").glob("*.json"))
            self.assertNotIn("query-one", path.read_text())
            path.write_text(json.dumps({"model": "fixture-model", "vector": [0, 0]}))
            cached_embeddings(workspace, "fixture-model", ["query-one", "query-two"], Client)
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(calls[-1]), 1)


if __name__ == "__main__":
    unittest.main()
