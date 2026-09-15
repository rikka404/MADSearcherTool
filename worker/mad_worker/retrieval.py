"""Local query planning and explainable ranking; independent of indexing and AI I/O."""
import bisect
import re
import unicodedata
from dataclasses import dataclass

STRATEGY_VERSION = "named_event_rrf_v1"
SEMANTIC_MIN = 0.22
CONFIDENCE_WEIGHT = {"high": 1.0, "medium": 0.55}
_CONNECTORS = re.compile(r"[\s,，;；、。.!！?？'\"“”‘’()（）+&/]+")
_PERSON_ONLY = re.compile(r"(?:和|与|及|以及|的|出镜|出现|镜头|画面|片段|and|with)*")


def normalize(value):
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _word_boundary(text, start, end, term):
    if len(term) == 1:
        return (start == 0 or not text[start - 1].isalnum()) and (end == len(text) or not text[end].isalnum())
    if re.search(r"[a-z0-9]", term[0]) and start and re.match(r"[a-z0-9_]", text[start - 1]):
        return False
    if re.search(r"[a-z0-9]", term[-1]) and end < len(text) and re.match(r"[a-z0-9_]", text[end]):
        return False
    return True


def _contains_name(text, term):
    start = text.find(term)
    while start >= 0:
        if _word_boundary(text, start, start + len(term), term):
            return True
        start = text.find(term, start + 1)
    return False


@dataclass(frozen=True)
class Mention:
    start: int
    end: int
    text: str
    ids: tuple[str, ...]


@dataclass(frozen=True)
class QueryPlan:
    original: str
    event: str
    appearance: str
    mentions: tuple[Mention, ...]
    groups: tuple[tuple[str, ...], ...]
    person_only: bool

    @property
    def compound(self):
        return bool(self.groups) and not self.person_only

    @property
    def variants(self):
        result = {"original": self.original}
        if self.compound:
            result["event"] = self.event
            if self.appearance:
                result["appearance"] = self.appearance
        return {k: v for k, v in result.items() if v}

    @property
    def embedding_texts(self):
        # Person-only lookup has actual identity evidence; a vector cannot prove identity.
        return [] if self.person_only else list(dict.fromkeys(self.variants.values()))

    def public(self, cards):
        return {"strategy": STRATEGY_VERSION, "mode": "person" if self.person_only else "compound" if self.compound else "ordinary",
                "characters": [{"ids": list(ids), "names": [cards[i].get("name") or next(iter(cards[i].get("aliases", [])), i[:6]) for i in ids],
                                "ambiguous": len(ids) > 1} for ids in self.groups],
                "variants": self.variants}


def plan_query(query, cards, character_filter=None):
    text = normalize(query)
    terms = {}
    for key, card in cards.items():
        for value in [card.get("name", ""), *card.get("aliases", [])]:
            term = normalize(value)
            if term:
                terms.setdefault(term, set()).add(key)
    ordered = sorted(terms, key=lambda term: (-len(term), term))
    mentions, cursor = [], 0
    while cursor < len(text):
        term = next((term for term in ordered if text.startswith(term, cursor)
                     and _word_boundary(text, cursor, cursor + len(term), term)), None)
        if term is None:
            cursor += 1
            continue
        mentions.append(Mention(cursor, cursor + len(term), term, tuple(sorted(terms[term]))))
        cursor += len(term)
    groups = list(dict.fromkeys(m.ids for m in mentions))
    if character_filter and not any(character_filter in ids for ids in groups):
        groups.append((character_filter,))
    expandable = sum(len(m.ids) == 1 and bool(cards[m.ids[0]].get("appearance", "").strip()) for m in mentions)
    # Bound descriptions, never truncate the user's action at the end of a sentence.
    per_description = min(180, max(0, (2000 - len(text)) // max(1, expandable) - 2))
    event, appearance, cursor, expanded = [], [], 0, False
    for mention in mentions:
        prefix = text[cursor:mention.start]
        event.extend([prefix, " "])
        description = cards[mention.ids[0]].get("appearance", "").strip()[:per_description] if len(mention.ids) == 1 else ""
        appearance.extend([prefix, ("（" + description + "）") if description else mention.text])
        expanded |= bool(description)
        cursor = mention.end
    event.append(text[cursor:])
    appearance.append(text[cursor:])
    event_text = re.sub(r"\s+", " ", "".join(event)).strip(" ,，;；、。.!！?？")
    person_only = bool(groups) and bool(_PERSON_ONLY.fullmatch(_CONNECTORS.sub("", event_text)))
    return QueryPlan(query, "" if person_only else event_text,
                     "".join(appearance) if expanded and not person_only else "",
                     tuple(mentions), tuple(groups), person_only)


def role_strengths(row):
    strengths = {}
    for match in row.get("character_matches", []):
        key = match["character_id"]
        strengths[key] = max(strengths.get(key, 0), CONFIDENCE_WEIGHT.get(match["confidence"], 0))
    return strengths


def neighbour_roles(rows, shot_ranges):
    """At most six segments in each adjacent shot, within five seconds; soft evidence only."""
    starts = {video: [r[0] for r in ranges] for video, ranges in shot_ranges.items()}
    by_shot, positions = {}, {}
    for row in rows:
        if not row.get("shot_id") or row["video_id"] not in starts:
            continue
        position = bisect.bisect_right(starts[row["video_id"]], row["start"]) - 1
        if position >= 0:
            positions[row["id"]] = position
            by_shot.setdefault((row["video_id"], position), []).append(row)
    result = {}
    for row in rows:
        if row["id"] not in positions:
            continue
        position, strengths = positions[row["id"]], {}
        neighbours = (by_shot.get((row["video_id"], position - 1), [])[-6:]
                      + by_shot.get((row["video_id"], position + 1), [])[:6])
        for other in neighbours:
            if other["end"] < row["start"] - 5 or other["start"] > row["end"] + 5:
                continue
            times = other.get("metadata", {}).get("sample_times", [])
            for match in other.get("character_matches", []):
                evidence_times = [times[i] for i in match.get("frame_indices", []) if type(i) is int and 0 <= i < len(times)]
                if not any(row["start"] - 5 <= t <= row["end"] + 5 for t in evidence_times):
                    continue
                key = match["character_id"]
                strengths[key] = max(strengths.get(key, 0), CONFIDENCE_WEIGHT.get(match["confidence"], 0))
        result[row["id"]] = strengths
    return result


def _group_strength(ids, strengths):
    return max((strengths.get(key, 0) for key in ids), default=0) * (1 if len(ids) == 1 else 0.5)


def candidate_features(plan, row, lexical, semantic, cards, nearby):
    direct = role_strengths(row)
    role = sum(_group_strength(ids, direct) for ids in plan.groups) / len(plan.groups) if plan.groups else 0
    combined = {key: max(direct.get(key, 0), value * 0.35) for key, value in nearby.items()} | direct
    supported = sum(_group_strength(ids, combined) for ids in plan.groups) / len(plan.groups) if plan.groups else 0
    detail = {"strategy": STRATEGY_VERSION, "lexical": lexical, "semantic": semantic,
              "role_direct": role, "role_support": max(role, supported), "rrf": 0.0}
    if plan.person_only:
        text = normalize(row["caption"] + "\n" + row["subtitle"])
        mentioned = sum(any(_contains_name(text, normalize(n)) for key in ids
                            for n in [cards[key].get("name", ""), *cards[key].get("aliases", [])] if normalize(n))
                        for ids in plan.groups) / len(plan.groups)
        coverage = sum(_group_strength(ids, direct) > 0 for ids in plan.groups) / len(plan.groups)
        if not role and not mentioned:
            return None
        detail.update(mode="person", base_score=max(0.9 * role + 0.1 * coverage, 0.35 * mentioned),
                      name_mention=mentioned, event_strength=0.0)
    elif plan.compound:
        event_lexical = lexical.get("event", 0)
        event_semantic = semantic.get("event", -1)
        route = "event"
        strength = None
        if event_lexical > 0 or event_semantic >= SEMANTIC_MIN:
            strength = max(event_lexical, max(0, (event_semantic - SEMANTIC_MIN) / (1 - SEMANTIC_MIN)))
        # Short name-free queries can miss paraphrases. Compare all eligible evidence
        # so crossing a threshold cannot replace stronger evidence with a weaker one.
        appearance, original = semantic.get("appearance", -1), semantic.get("original", -1)
        fallback = [("appearance_fallback", appearance)] if appearance >= 0.4 else []
        if original >= 0.55:
            fallback.append(("original_fallback", original))
        if fallback:
            fallback_route, evidence = max(fallback, key=lambda item: item[1])
            fallback_strength = 0.5 * max(0, (evidence - SEMANTIC_MIN) / (1 - SEMANTIC_MIN))
            if strength is None or fallback_strength > strength:
                route, strength = fallback_route, fallback_strength
        if strength is None:
            return None
        detail.update(mode="compound", route=route, event_strength=min(1, strength), base_score=0.0)
    else:
        lex, cosine = lexical.get("original", 0), semantic.get("original", -1)
        if lex <= 0 and cosine < SEMANTIC_MIN:
            return None
        detail.update(mode="ordinary", event_strength=0.0,
                      base_score=max(lex, 0.8 * max(0, cosine) + 0.2 * lex))
    return detail


def rank_candidates(candidates, plan):
    """Fuse admitted event and paraphrase candidates; return public diagnostics."""
    if plan.compound and candidates:
        channels = [("lexical", "event", 1.0), ("lexical", "original", 0.25)]
        seen = set()
        for name in ("event", "original", "appearance"):
            text = plan.variants.get(name)
            if text and normalize(text) not in seen:
                channels.append(("semantic", name, 1.0 if name == "event" else 0.5))
                seen.add(normalize(text))
        total_weight = 0.0
        for family, name, weight in channels:
            values = [(i, item["_ranking"][family].get(name, -1)) for i, item in enumerate(candidates)]
            values = [(i, value) for i, value in values if value >= SEMANTIC_MIN] if family == "semantic" else [(i, v) for i, v in values if v > 0]
            if not values:
                continue
            total_weight += weight
            values.sort(key=lambda pair: -pair[1])
            last, rank = None, 0
            for position, (index, value) in enumerate(values, 1):
                if last is None or abs(value - last) > 1e-9:
                    rank = position
                candidates[index]["_ranking"]["rrf"] += weight * 61 / (60 + rank)
                last = value
        for item in candidates:
            detail = item["_ranking"]
            detail["rrf"] /= total_weight or 1
            detail["base_score"] = 0.7 * detail["event_strength"] + 0.2 * detail["rrf"] + 0.1 * detail["role_support"]
    for item in candidates:
        detail = item.pop("_ranking")
        item["score"] = round(detail["base_score"], 4)
        if detail["mode"] == "compound":
            item["ranking_summary"] = (f"情节 {detail['event_strength']:.3f} × 70% + 多路排名 {detail['rrf']:.3f} × 20%"
                                       f" + 人物证据 {detail['role_support']:.3f} × 10%；人物分不是概率")
            if detail["route"] != "event":
                item["ranking_summary"] += "；原句/外观的补充语义候选，情节强度减半，请核对具体情节"
        elif detail["mode"] == "person":
            item["ranking_summary"] = "优先角色出镜证据（high高于medium），姓名文字提及单独参与；文字点名不证明出镜"
        else:
            item["ranking_summary"] = "文字与语义相关度；沿用普通查询评分"
        item["ranking_details"] = detail
