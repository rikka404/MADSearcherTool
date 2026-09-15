"""Subtitle retrieval and result fusion, independent of storage and model calls."""
import bisect
import re
import unicodedata

SEMANTIC_MIN = 0.15
STRATEGY = "subtitle_channels_v1"


def normalize(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


def lexical(query, text):
    q, t = normalize(query), normalize(text)
    if not q or not t:
        return 0.0, False
    if q in t:
        return 1.0, len(q) >= 4
    if len(q) < 3:
        return 0.0, False
    grams = {q[i:i + 2] for i in range(len(q) - 1)}
    overlap = sum(g in t for g in grams) / len(grams)
    # Orthographic similarity only; it never gates the semantic channel.
    score = 0.6 * overlap if overlap >= 0.5 else 0.0
    words = re.findall(r"[a-z0-9]+", query.casefold())
    if len(words) > 1:
        tokens = set(re.findall(r"[a-z0-9]+", text.casefold()))
        coverage = sum(w in tokens for w in words) / len(words)
        score = max(score, 0.6 * coverage if coverage >= 0.5 else 0.0)
    return score, False


def fused_ranks(channels):
    """Channels contain (key, score) pairs; equal scores receive equal ranks."""
    result, weights = {}, 0
    for entries in channels:
        if not entries:
            continue
        weights += 1
        previous, rank = None, 0
        for position, (key, score) in enumerate(sorted(entries, key=lambda p: -p[1]), 1):
            if previous is None or abs(previous - score) > 1e-9:
                rank = position
            result[key] = result.get(key, 0) + 61 / (60 + rank)
            previous = score
    return {key: value / weights for key, value in result.items()} if weights else {}


def candidates(units, query, vector_scores, cap):
    features, lex_list, sem_list = {}, [], []
    for unit in units:
        key = unit["id"]
        score, exact = lexical(query, unit["text"])
        cosine = vector_scores.get(key, -1)
        features[key] = {"lexical": score, "semantic": cosine, "exact": exact}
        if score > 0:
            lex_list.append((key, score))
        if cosine >= SEMANTIC_MIN:
            sem_list.append((key, cosine))
    lex_list.sort(key=lambda p: -p[1])
    sem_list.sort(key=lambda p: -p[1])
    # A complete quote must not be dropped at a tied candidate cutoff.
    exact_keys = {key for key, f in features.items() if f["exact"]}
    ranks = fused_ranks([lex_list[:cap], sem_list[:cap]])
    selected = set(ranks) | exact_keys
    output = []
    for unit in units:
        key = unit["id"]
        if key not in selected:
            continue
        detail = features[key]
        strength = max(detail["lexical"], max(0, (detail["semantic"] - SEMANTIC_MIN) / (1 - SEMANTIC_MIN)))
        detail.update(rrf=ranks.get(key, 0), strength=strength, strategy=STRATEGY)
        output.append({**unit, "dialogue_detail": detail, "score": round(0.75 * strength + 0.25 * detail["rrf"], 4)})
    output.sort(key=lambda u: (not u["dialogue_detail"]["exact"], -u["score"], u["kind"] != "single", u["end"] - u["start"], u["start"]))
    return output


def overlap(a, b):
    shared = min(a["end"], b["end"]) - max(a["start"], b["start"])
    return max(0, shared) / max(0.001, min(a["end"] - a["start"], b["end"] - b["start"]))


def filter_character(units, rows, character_id):
    intervals = {}
    for row in rows:
        if any(m["character_id"] == character_id for m in row.get("character_matches", [])):
            intervals.setdefault(row["video_id"], []).append((row["start"], row["end"]))
    starts = {key: [r[0] for r in values] for key, values in intervals.items()}
    result = []
    for unit in units:
        ranges = intervals.get(unit["video_id"], [])
        position = max(0, bisect.bisect_right(starts.get(unit["video_id"], []), unit["start"]) - 1)
        for start, end in ranges[position:]:
            if start >= max(unit["end"], unit["start"] + 0.1):
                break
            if end > unit["start"]:
                result.append(unit)
                break
    return result


def as_hits(selected, videos, configs, rows, shot_ranges, character_filter=None):
    by_video = {}
    for row in rows:
        by_video.setdefault(row["video_id"], []).append(row)
    starts = {key: [r["start"] for r in values] for key, values in by_video.items()}
    shot_starts = {key: [r[0] for r in values] for key, values in shot_ranges.items()}
    hits, by_result = [], {}
    for unit in selected:
        video_id = unit["video_id"]
        video, config, detail = videos[video_id], configs[video_id], unit["dialogue_detail"]
        # A point cue gets a short playable range; preserve its exact source time below.
        end = min(video["duration"], max(unit["end"], unit["start"] + 0.1))
        hit = {"video_id": video_id, "path": video["path"], "name": video["name"], "duration": video["duration"],
               "start": unit["start"], "end": end, "context_start": unit["start"], "context_end": end,
               "shot_id": "", "thumbnail": "", "character_matches": [], "sample_times": [],
               "score": unit["score"], "text": unit["text"], "ranking_details": {"dialogue": detail},
               "match_type": "字幕原文" if detail["exact"] else "字幕文字+语义" if detail["lexical"] and detail["semantic"] >= SEMANTIC_MIN
               else "字幕文字" if detail["lexical"] else "字幕同义候选",
               "dialogue_matches": [{"start": unit["start"], "end": unit["end"], "text": unit["text"], "kind": unit["kind"],
                                     "approximate": config.get("approximate", False), **detail}]}
        semantic_label = f"{detail['semantic']:.3f}" if detail["semantic"] > -1 else "未评分"
        hit["ranking_summary"] = (f"独立字幕：文字 {detail['lexical']:.3f}、语义 {semantic_label}；"
                                  f"相关度 × 75% + 多路排名 × 25%；{'原文命中优先' if detail['exact'] else '语义候选需核对台词'}")
        if config.get("approximate"):
            hit["ranking_summary"] += "；时码来自旧片段范围"
        media_rows = by_video.get(video_id, [])
        position = max(0, bisect.bisect_right(starts.get(video_id, []), unit["start"]) - 1)
        neighbours = []
        for row in media_rows[position:]:
            if row["start"] >= end:
                break
            if row["end"] > unit["start"]:
                neighbours.append(row)
        if character_filter and not any(any(m["character_id"] == character_filter for m in r.get("character_matches", [])) for r in neighbours):
            continue
        if neighbours:
            best = max(neighbours, key=lambda r: overlap(hit, r))
            hit["thumbnail"] = best["thumbnail"]
            hit["sample_times"] = best.get("metadata", {}).get("sample_times", [])
        ranges = shot_ranges.get(video_id, [])
        if ranges:
            position = max(0, bisect.bisect_right(shot_starts[video_id], unit["start"]) - 1)
            last = max(position, bisect.bisect_left(shot_starts[video_id], end) - 1)
            hit.update(shot_id="subtitle", context_start=ranges[max(0, position - 1)][0],
                       context_end=ranges[min(len(ranges) - 1, last + 1)][1])
        # Same-time translations and overlapping context windows share one result.
        duplicates = by_result.setdefault(video_id, [])
        previous = next((h for h in duplicates if overlap(h, hit) >= 0.8), None)
        if previous:
            if len(previous["dialogue_matches"]) < 6:
                previous["dialogue_matches"].extend(hit["dialogue_matches"])
            continue
        duplicates.append(hit)
        hits.append(hit)
    return hits


def combine(scene_hits, dialogue_hits, cap):
    """Fuse result lists, attaching a scene rank only to an overlapping subtitle hit."""
    results = list(dialogue_hits)
    by_video = {}
    for i, hit in enumerate(results):
        by_video.setdefault(hit["video_id"], []).append(i)
    scene_entries, scene_best = [], {}
    for hit in scene_hits:
        choices = [i for i in by_video.get(hit["video_id"], []) if overlap(results[i], hit) >= 0.5]
        if choices:
            key = max(choices, key=lambda i: results[i]["score"])
            if hit["score"] > scene_best.get(key, -1):
                scene_best[key] = hit["score"]
                results[key]["ranking_details"]["scene"] = hit["ranking_details"]
        else:
            key = len(results)
            results.append(hit)
            scene_best[key] = hit["score"]
    scene_entries.extend(scene_best.items())
    dialogue_entries = [(i, hit["score"]) for i, hit in enumerate(dialogue_hits)]
    ranks = fused_ranks([scene_entries, dialogue_entries])
    for key, hit in enumerate(results):
        strength = max(hit["score"], scene_best.get(key, 0))
        hit["score"] = round(0.65 * strength + 0.35 * ranks.get(key, 0), 4)
        hit["ranking_details"]["fusion"] = {"strategy": STRATEGY, "strength": strength, "rrf": ranks.get(key, 0)}
        hit["ranking_summary"] += "；综合：通道相关分 × 65% + 字幕/场景排名融合 × 35%，字幕原文优先"
    results.sort(key=lambda h: (not any(m["exact"] for m in h.get("dialogue_matches", [])), -h["score"], h["name"], h["start"]))
    return results[:cap]
