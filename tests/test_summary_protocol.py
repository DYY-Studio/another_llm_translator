from __future__ import annotations

from app.llm_response import (
    TerminologyResponseMode,
    parse_terminology_response,
    response_record_types,
)


def test_joint_response_keeps_summary_and_terms_in_response_order() -> None:
    response = (
        '{"type":"summary","text":"Alice enters the room.","refs":["1"]}\n'
        '{"type":"term","source":"Alice","category":"person"}\n'
        '{"type":"end"}'
    )

    parsed = parse_terminology_response(
        response,
        mode=TerminologyResponseMode.TERMS_AND_FRAGMENT_SUMMARY,
        source_refs=("1",),
        source_texts=("Alice enters the room.",),
    )

    assert parsed.complete is True
    assert parsed.summary_complete is True
    assert parsed.terms_complete is True
    assert [record["type"] for record in parsed.records] == [
        "summary",
        "term",
    ]
    assert parsed.summaries[0]["refs"] == ["1"]
    assert parsed.terms[0]["source"] == "Alice"
    assert response_record_types(TerminologyResponseMode.TERMS_ONLY) == ("term",)


def test_summary_reference_error_does_not_discard_valid_terms() -> None:
    response = (
        '{"type":"summary","text":"A summary.","refs":["missing"]}\n'
        '{"type":"term","source":"Alice","category":"person"}\n'
        '{"type":"end"}'
    )

    parsed = parse_terminology_response(
        response,
        mode=TerminologyResponseMode.TERMS_AND_FRAGMENT_SUMMARY,
        source_refs=("1",),
        source_texts=("Alice enters.",),
    )

    assert parsed.complete is False
    assert parsed.summary_complete is False
    assert parsed.terms_complete is True
    assert len(parsed.summaries) == 0
    assert len(parsed.terms) == 1
    assert "summary" in parsed.error_codes_by_type
    assert parsed.error_codes_by_type["summary"] == ("invalid_reference",)


def test_global_protocol_error_invalidates_both_classes_but_keeps_rows() -> None:
    response = (
        '{"type":"summary","text":"A summary.","refs":["1"]}\n'
        '{"type":"term","source":"Alice","category":"person"}\n'
        '{"type":"unknown"}\n'
        '{"type":"end"}'
    )

    parsed = parse_terminology_response(
        response,
        mode=TerminologyResponseMode.TERMS_AND_FRAGMENT_SUMMARY,
        source_refs=("1",),
        source_texts=("Alice enters.",),
    )

    assert parsed.complete is False
    assert parsed.summary_complete is False
    assert parsed.terms_complete is False
    assert parsed.global_error_codes == ("unknown_type",)
    assert len(parsed.summaries) == 1
    assert len(parsed.terms) == 1


def test_summary_only_allows_empty_terms_and_requires_summary() -> None:
    parsed = parse_terminology_response(
        '{"type":"summary","text":"No named terms.","refs":["1"]}\n'
        '{"type":"end"}',
        mode=TerminologyResponseMode.SUMMARY_ONLY,
        source_refs=("1",),
        source_texts=("A plain sentence.",),
    )

    assert parsed.complete is True
    assert parsed.summary_complete is True
    assert parsed.terms_complete is True
    assert parsed.terms == ()


def test_summary_without_refs_is_normalized_to_full_source_refs() -> None:
    parsed = parse_terminology_response(
        '{"type":"summary","text":"Covers both inputs."}\n'
        '{"type":"end"}',
        mode=TerminologyResponseMode.SUMMARY_ONLY,
        source_refs=("1", "2"),
        source_texts=("First input.", "Second input."),
    )

    assert parsed.complete is True
    assert parsed.summaries[0]["refs"] == ["1", "2"]


def test_summary_refs_must_cover_all_source_refs() -> None:
    parsed = parse_terminology_response(
        '{"type":"summary","text":"Only one input.","refs":["1"]}\n'
        '{"type":"end"}',
        mode=TerminologyResponseMode.SUMMARY_ONLY,
        source_refs=("1", "2"),
        source_texts=("First input.", "Second input."),
    )

    assert parsed.complete is False
    assert parsed.summary_complete is False
    assert parsed.summaries == ()
    assert parsed.error_codes_by_type["summary"] == ("invalid_reference",)


def test_multiple_summary_records_partition_source_refs_and_keep_term_order() -> None:
    response = (
        '{"type":"summary","text":"Alice enters.","refs":["1"]}\n'
        '{"type":"summary","text":"Bob waves.","refs":["2"]}\n'
        '{"type":"term","source":"Alice","category":"person"}\n'
        '{"type":"end"}'
    )

    parsed = parse_terminology_response(
        response,
        mode=TerminologyResponseMode.TERMS_AND_FRAGMENT_SUMMARY,
        source_refs=("1", "2"),
        source_texts=("Alice enters.", "Bob waves."),
    )

    assert parsed.complete is True
    assert [summary["refs"] for summary in parsed.summaries] == [["1"], ["2"]]
    assert [record["type"] for record in parsed.records] == [
        "summary",
        "summary",
        "term",
    ]


def test_multiple_summary_records_require_refs_on_each_record() -> None:
    parsed = parse_terminology_response(
        '{"type":"summary","text":"Alice enters.","refs":["1"]}\n'
        '{"type":"summary","text":"Bob waves."}\n'
        '{"type":"end"}',
        mode=TerminologyResponseMode.SUMMARY_ONLY,
        source_refs=("1", "2"),
        source_texts=("Alice enters.", "Bob waves."),
    )

    assert parsed.complete is False
    assert parsed.summary_complete is False
    assert parsed.error_codes_by_type["summary"] == ("invalid_reference",)


def test_multiple_summary_records_reject_overlapping_refs() -> None:
    parsed = parse_terminology_response(
        '{"type":"summary","text":"Alice enters.","refs":["1"]}\n'
        '{"type":"summary","text":"Alice and Bob.","refs":["1","2"]}\n'
        '{"type":"end"}',
        mode=TerminologyResponseMode.SUMMARY_ONLY,
        source_refs=("1", "2"),
        source_texts=("Alice enters.", "Bob waves."),
    )

    assert parsed.complete is False
    assert parsed.summary_complete is False
    assert parsed.error_codes_by_type["summary"] == ("duplicate_reference",)


def test_multiple_summary_records_reject_incomplete_coverage() -> None:
    parsed = parse_terminology_response(
        '{"type":"summary","text":"Alice enters.","refs":["1"]}\n'
        '{"type":"summary","text":"Bob waves.","refs":["2"]}\n'
        '{"type":"end"}',
        mode=TerminologyResponseMode.SUMMARY_ONLY,
        source_refs=("1", "2", "3"),
        source_texts=("Alice enters.", "Bob waves.", "Carol smiles."),
    )

    assert parsed.complete is False
    assert parsed.summary_complete is False
    assert parsed.error_codes_by_type["summary"] == ("invalid_reference",)


def test_joint_response_rejects_duplicate_or_out_of_source_terms() -> None:
    response = (
        '{"type":"summary","text":"A summary.","refs":["1","1"]}\n'
        '{"type":"term","source":"Not in source","category":"thing"}\n'
        '{"type":"end"}'
    )

    parsed = parse_terminology_response(
        response,
        mode="terms+fragment-summary",
        source_refs=("1",),
        source_texts=("Alice enters.",),
    )

    assert parsed.complete is False
    assert parsed.summary_complete is False
    assert parsed.terms_complete is False
    assert parsed.error_codes_by_type["summary"] == ("duplicate_reference",)
    assert parsed.error_codes_by_type["term"] == ("source_not_found",)


def test_terms_only_preserves_existing_duplicate_term_behavior() -> None:
    response = (
        '{"type":"term","source":"Alice","category":"person"}\n'
        '{"type":"term","source":"Alice","category":"person"}\n'
        '{"type":"end"}'
    )

    parsed = parse_terminology_response(
        response,
        mode=TerminologyResponseMode.TERMS_ONLY,
        source_texts=("Alice enters.",),
    )

    assert parsed.complete is True
    assert parsed.terms_complete is True
    assert parsed.error_codes_by_type["term"] == ()
    assert len(parsed.terms) == 2


def test_missing_end_invalidates_requested_classes() -> None:
    parsed = parse_terminology_response(
        '{"type":"summary","text":"A summary.","refs":["1"]}\n'
        '{"type":"term","source":"Alice","category":"person"}',
        mode="terms+fragment-summary",
        source_refs=("1",),
        source_texts=("Alice enters.",),
    )

    assert parsed.complete is False
    assert parsed.summary_complete is False
    assert parsed.terms_complete is False
    assert parsed.global_error_codes == ("missing_end",)
