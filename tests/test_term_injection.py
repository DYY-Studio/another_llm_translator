from app.term_library import TermNormalization
from app.term_matching import _TermMatchCache
from app.term_knowledge import source_range


def make_cache(entries):
    segments = [{"segment_id": f"S{i}", "source": "Alpha Beta Gamma", "file_id": "F1", "part_id": "P1"} for i in range(9)]
    originals = {s["segment_id"]: s for s in segments}

    def ranges(indices):
        return source_range([segments[i] for i in indices], originals, {})

    items = [{"text": text, "sources": [ranges(indices)] if indices is not None else [None]}
             for text, indices in entries]
    terms = [{"source": name, "normalized": name.lower(), "aliases": [], "preferred_translation": name,
              "group_primary": "alpha" if name == "Beta" else None, "entity_key": "alpha" if name == "Beta" else name.lower(),
              "knowledge_status": "current", "description_items": items if name != "Gamma" else items[:1]}
             for name in ("Alpha", "Beta", "Gamma")]
    return _TermMatchCache({"terms": terms}, TermNormalization("NFKC", True), 20, segments=segments), segments


def test_relative_relations_rank_within_entity_and_use_entire_request_span():
    cache, segments = make_cache([("later before", [2]), ("earlier before", [0]),
                                  ("later after", [7]), ("earlier after", [6]),
                                  ("mixed", [0, 4, 7]), ("manual", None)])
    # Names need only occur at one end; the full request defines the relative span.
    payload = cache.for_items([segments[3], {**segments[5], "source": "unmatched"}])
    entities = [term["entity_knowledge"] for term in payload if "entity_knowledge" in term]
    assert len(entities) == 2
    alpha = next(e for e in entities if e["entity"] == "alpha")
    assert alpha == {"entity": "alpha", "items": [
        {"text": "later before", "source_relation": ["before 2"], "unsourced": False},
        {"text": "earlier before", "source_relation": ["before 1"], "unsourced": False},
        {"text": "later after", "source_relation": ["after 2"], "unsourced": False},
        {"text": "earlier after", "source_relation": ["after 1"], "unsourced": False},
        {"text": "mixed", "source_relation": ["before 1", "within", "after 2"], "unsourced": False},
        {"text": "manual", "source_relation": [], "unsourced": True},
    ]}
    assert next(e for e in entities if e["entity"] == "gamma")["items"][0]["source_relation"] == ["before 1"]


def test_relative_relations_preserve_slice_boundaries_and_source_gaps():
    cache, segments = make_cache([("gapped", [0]), ("overlapping", [0])])
    term = cache.matcher.by_normalized["alpha"]
    source = term["description_items"][0]["sources"][0][0]
    term["description_items"][0]["sources"] = [[{**source, "start": 0, "end": 2}, {**source, "start": 7, "end": 10}]]
    term["description_items"][1]["sources"] = [[{**source, "start": 1, "end": 8}]]
    payload = cache.for_items([{**segments[0], "segment_id": "S0-slice", "_original_segment_id": "S0",
                               "_source_offset": 2, "source": "Alpha"}])
    items = payload[0]["entity_knowledge"]["items"]
    assert items[0]["source_relation"] == ["before 1", "after 1"]
    assert items[1]["source_relation"] == ["before 2", "within", "after 1"]


def test_source_preparation_is_reused_across_segments_and_candidate_requests(monkeypatch):
    import app.term_knowledge as knowledge
    cache, segments = make_cache([("identity", [0, 2, 6])])
    calls = 0
    original = knowledge.compact_source_ranges

    def counted(*args):
        nonlocal calls
        calls += 1
        return original(*args)

    monkeypatch.setattr(knowledge, "compact_source_ranges", counted)
    for end in range(1, 9):
        payload = cache.for_items(segments[:end])
        assert len([term for term in payload if "entity_knowledge" in term]) == 2
    assert calls == 2
