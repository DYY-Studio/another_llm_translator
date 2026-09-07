from __future__ import annotations

from copy import deepcopy

import pytest

from app.summary_provenance import (
    DEPENDENCY_CHANGED,
    PROVENANCE_UNAVAILABLE,
    SOURCE_CHANGED,
    STALE_STATUS,
    assess_full_summary,
    build_provenance,
    digest,
    full_summary_context_usable,
)


def _segment(segment_id: str, source: str, model_text: str | None = None) -> dict[str, object]:
    return {
        "segment_id": segment_id,
        "file_id": "F0001",
        "part_id": "document",
        "line_index": int(segment_id.rsplit("-", 1)[-1]),
        "source": source,
        "model_source": model_text,
        "is_empty": False,
    }


def _source_range(*segments: dict[str, object]) -> dict[str, object]:
    values = []
    for segment in segments:
        source = str(segment["source"])
        model_text = str(segment.get("model_source") or source)
        segment_id = str(segment["segment_id"])
        values.append(
            {
                "segment_id": segment_id,
                "original_segment_id": segment_id,
                "slice_id": f"{segment_id}#slice-0000",
                "slice_index": 0,
                "source": source,
                "source_digest": digest(source),
                "original_source_digest": digest(source),
                "model_text": model_text,
                "model_text_digest": digest(model_text),
                "original_model_text_digest": digest(model_text),
            }
        )
    return {
        "file_id": "F0001",
        "part_id": "document",
        "segment_ids": [str(item["segment_id"]) for item in segments],
        "segments": values,
    }


def _fragment(
    segment_range: dict[str, object],
    *,
    record_id: str,
    text: str,
    status: str = "completed",
    file_id: str = "F0001",
    part_id: str = "document",
) -> dict[str, object]:
    return {
        "record_id": record_id,
        "kind": "fragment",
        "file_id": file_id,
        "part_id": part_id,
        "status": status,
        "source_changed": False,
        "text": text,
        "source_range": segment_range,
        "source_digest": digest(segment_range["segments"]),
        "input_digest": "sha256:fragment-input",
    }


def _full(
    segments: list[dict[str, object]],
    children: list[dict[str, object]],
    *,
    record_id: str = "FULL-1",
    origin: str = "llm",
    status: str = "completed",
) -> dict[str, object]:
    provenance, input_digest = build_provenance(origin, children)
    return {
        "record_id": record_id,
        "kind": "full",
        "file_id": "F0001",
        "part_id": "document",
        "status": status,
        "source_changed": False,
        "text": "完整概括",
        "source_range": _source_range(*segments),
        "source_digest": digest(_source_range(*segments)["segments"]),
        "input_digest": input_digest,
        "provenance": provenance,
    }


def _assessment(artifact: dict[str, object], current: list[dict[str, object]], artifacts: list[dict[str, object]]):
    return assess_full_summary(artifact, current, artifacts)


def test_build_provenance_uses_ordered_dependency_digests_for_adopted_full() -> None:
    segment = _segment("F0001-S-1", "Alice")
    child = _fragment(_source_range(segment), record_id="FRAGMENT-1", text="片段")

    provenance, input_digest = build_provenance("adopted_fragment", [child])

    assert provenance == {
        "origin": "adopted_fragment",
        "artifact_ids": ["FRAGMENT-1"],
        "source_ranges": [child["source_range"]],
        "dependencies": [
            {
                "record_id": "FRAGMENT-1",
                "kind": "fragment",
                "text_digest": digest("片段"),
                "source_digest": child["source_digest"],
            }
        ],
    }
    assert input_digest == digest(provenance["dependencies"])


def test_normal_full_and_recursive_reduction_are_valid() -> None:
    first = _segment("F0001-S-1", "Alice")
    second = _segment("F0001-S-2", "Bob")
    fragment_one = _fragment(_source_range(first), record_id="FRAGMENT-1", text="甲")
    fragment_two = _fragment(_source_range(second), record_id="FRAGMENT-2", text="乙")
    reduction_provenance, reduction_input = build_provenance(
        "llm", [fragment_one, fragment_two]
    )
    reduction = {
        "record_id": "REDUCTION-1",
        "kind": "reduction",
        "file_id": "F0001",
        "part_id": "document",
        "status": "completed",
        "source_changed": False,
        "text": "甲乙",
        "source_range": _source_range(first, second),
        "source_digest": digest(_source_range(first, second)["segments"]),
        "input_digest": reduction_input,
        "provenance": reduction_provenance,
    }
    full = _full([first, second], [reduction])

    assessment = _assessment(
        full,
        [first, second],
        [full, reduction, fragment_one, fragment_two],
    )

    assert assessment.expired is False
    assert assessment.expiry_reason is None


def test_completed_full_remains_usable_while_fragment_refresh_is_pending() -> None:
    first = _segment("F0001-S-1", "Alice")
    second = _segment("F0001-S-2", "Bob")
    fragment_one = _fragment(_source_range(first), record_id="FRAGMENT-1", text="甲")
    fragment_two = _fragment(_source_range(second), record_id="FRAGMENT-2", text="乙")
    full = _full([first, second], [fragment_one, fragment_two])
    stale_fragment = deepcopy(fragment_one)
    stale_fragment["status"] = "stale"
    artifacts = [full, stale_fragment, fragment_two]

    assessment = _assessment(full, [first, second], artifacts)

    assert assessment.expired is True
    assert assessment.expiry_reason == DEPENDENCY_CHANGED
    assert full_summary_context_usable(full, [first, second]) is True


def test_source_changed_full_is_not_usable_context() -> None:
    segment = _segment("F0001-S-1", "Alice")
    fragment = _fragment(_source_range(segment), record_id="FRAGMENT-1", text="甲")
    full = _full([segment], [fragment])
    full["source_changed"] = True

    assert full_summary_context_usable(full, [segment]) is False


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("source", SOURCE_CHANGED),
        ("model", SOURCE_CHANGED),
        ("coverage", SOURCE_CHANGED),
        ("text", DEPENDENCY_CHANGED),
        ("range", DEPENDENCY_CHANGED),
        ("status", DEPENDENCY_CHANGED),
    ],
)
def test_source_and_dependency_changes_expire_full(
    mutation: str, reason: str
) -> None:
    first = _segment("F0001-S-1", "Alice")
    second = _segment("F0001-S-2", "Bob")
    fragment_one = _fragment(_source_range(first), record_id="FRAGMENT-1", text="甲")
    fragment_two = _fragment(_source_range(second), record_id="FRAGMENT-2", text="乙")
    full = _full([first, second], [fragment_one, fragment_two])
    current = [first, second]
    artifacts = [full, fragment_one, fragment_two]

    if mutation == "source":
        current = [first | {"source": "Alice changed"}, second]
    elif mutation == "model":
        current = [first | {"model_source": "Alice model changed"}, second]
    elif mutation == "coverage":
        current = [first, second, _segment("F0001-S-3", "Carol")]
    elif mutation == "text":
        changed = deepcopy(fragment_one)
        changed["text"] = "甲 changed"
        artifacts = [full, changed, fragment_two]
    elif mutation == "range":
        changed = deepcopy(fragment_one)
        changed["source_range"] = _source_range(first, second)
        changed["source_digest"] = digest(changed["source_range"]["segments"])
        artifacts = [full, changed, fragment_two]
    else:
        changed = deepcopy(fragment_one)
        changed["status"] = "stale"
        artifacts = [full, changed, fragment_two]

    assessment = _assessment(full, current, artifacts)
    assert assessment.expired is True
    assert assessment.expiry_reason == reason


@pytest.mark.parametrize("case", ["missing", "malformed", "cycle", "cross_boundary"])
def test_unverifiable_provenance_fails_closed(case: str) -> None:
    first = _segment("F0001-S-1", "Alice")
    fragment = _fragment(_source_range(first), record_id="FRAGMENT-1", text="甲")
    full = _full([first], [fragment])
    artifacts: list[dict[str, object]] = [full, fragment]

    if case == "missing":
        full["provenance"] = {
            **full["provenance"],
            "artifact_ids": ["MISSING"],
            "source_ranges": [fragment["source_range"]],
            "dependencies": [
                {
                    "record_id": "MISSING",
                    "kind": "fragment",
                    "text_digest": digest("甲"),
                    "source_digest": fragment["source_digest"],
                }
            ],
        }
        full["input_digest"] = digest(full["provenance"]["dependencies"])
    elif case == "malformed":
        full["provenance"] = {"origin": "llm", "artifact_ids": ["FRAGMENT-1"]}
    elif case == "cross_boundary":
        other = _fragment(
            _source_range(first),
            record_id="FRAGMENT-OTHER",
            text="甲",
            file_id="F0002",
        )
        other["source_range"] = {**other["source_range"], "file_id": "F0002"}
        full["provenance"], full["input_digest"] = build_provenance("llm", [other])
        artifacts = [full, other]
    else:
        reduction = {
            "record_id": "REDUCTION-1",
            "kind": "reduction",
            "file_id": "F0001",
            "part_id": "document",
            "status": "completed",
            "source_changed": False,
            "text": "甲",
            "source_range": first and _source_range(first),
            "source_digest": fragment["source_digest"],
        }
        reduction["provenance"], reduction["input_digest"] = build_provenance(
            "llm", [reduction]
        )
        full["provenance"], full["input_digest"] = build_provenance("llm", [reduction])
        artifacts = [full, reduction]

    assessment = _assessment(full, [first], artifacts)
    assert assessment.expired is True
    assert assessment.expiry_reason == PROVENANCE_UNAVAILABLE


def test_legacy_adopted_fragment_skips_only_its_historical_input_digest() -> None:
    segment = _segment("F0001-S-1", "Alice")
    fragment = _fragment(_source_range(segment), record_id="FRAGMENT-1", text="片段")
    full = {
        **_full([segment], [fragment], origin="adopted_fragment"),
        "text": fragment["text"],
        "input_digest": fragment["input_digest"],
        "provenance": {
            "origin": "adopted_fragment",
            "artifact_ids": ["FRAGMENT-1"],
            "source_ranges": [fragment["source_range"]],
        },
    }

    assessment = _assessment(full, [segment], [full, fragment])

    assert assessment.expired is False


def test_legacy_adopted_fragment_without_child_is_unverifiable() -> None:
    segment = _segment("F0001-S-1", "Alice")
    fragment = _fragment(_source_range(segment), record_id="FRAGMENT-1", text="片段")
    full = {
        **_full([segment], [fragment], origin="adopted_fragment"),
        "provenance": {
            "origin": "adopted_fragment",
            "artifact_ids": ["DELETED-FRAGMENT"],
            "source_ranges": [fragment["source_range"]],
        },
    }

    assessment = _assessment(full, [segment], [full])

    assert assessment.expired is True
    assert assessment.expiry_reason == PROVENANCE_UNAVAILABLE


def test_stale_full_is_assessed_before_other_reasons() -> None:
    segment = _segment("F0001-S-1", "Alice")
    fragment = _fragment(_source_range(segment), record_id="FRAGMENT-1", text="片段")
    full = _full([segment], [fragment], status="stale")
    current = [segment | {"source": "changed"}]

    assessment = _assessment(full, current, [full, fragment])

    assert assessment.expired is True
    assert assessment.expiry_reason == STALE_STATUS
