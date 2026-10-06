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
    assert response_record_types(TerminologyResponseMode.TERMS_ONLY) == (
        "term",
        "no_terms",
    )


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
    )

    assert parsed.complete is True
    assert parsed.summaries[0]["refs"] == ["1", "2"]


def test_summary_refs_must_cover_all_source_refs() -> None:
    parsed = parse_terminology_response(
        '{"type":"summary","text":"Only one input.","refs":["1"]}\n'
        '{"type":"end"}',
        mode=TerminologyResponseMode.SUMMARY_ONLY,
        source_refs=("1", "2"),
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
    )

    assert parsed.complete is False
    assert parsed.summary_complete is False
    assert parsed.error_codes_by_type["summary"] == ("invalid_reference",)


def test_joint_summary_error_does_not_restrict_term_sources() -> None:
    response = (
        '{"type":"summary","text":"A summary.","refs":["1","1"]}\n'
        '{"type":"term","source":"Not in source","category":"thing"}\n'
        '{"type":"end"}'
    )

    parsed = parse_terminology_response(
        response,
        mode="terms+fragment-summary",
        source_refs=("1",),
    )

    assert parsed.complete is False
    assert parsed.summary_complete is False
    assert parsed.terms_complete is True
    assert parsed.error_codes_by_type["summary"] == ("duplicate_reference",)
    assert parsed.error_codes_by_type["term"] == ()


def test_terms_only_preserves_existing_duplicate_term_behavior() -> None:
    response = (
        '{"type":"term","source":"Alice","category":"person"}\n'
        '{"type":"term","source":"Alice","category":"person"}\n'
        '{"type":"end"}'
    )

    parsed = parse_terminology_response(
        response,
        mode=TerminologyResponseMode.TERMS_ONLY,
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
    )

    assert parsed.complete is False
    assert parsed.summary_complete is False
    assert parsed.terms_complete is False
    assert parsed.global_error_codes == ("missing_end",)


def test_experimental_terms_use_standard_field_validation() -> None:
    from app.stage_terminology import _validate_term_items

    content = (
        '{"type":"term","source":"Alice Smith","category":"person","aliases":["Ally", ""],"extra":"ignored"}\n'
        '{"type":"term","source":"Alice Smith","category":"person"}\n'
        '{"type":"end"}'
    )
    standard, errors, complete = _validate_term_items(content)
    parsed = parse_terminology_response(content, mode="terms-only")
    assert complete and not errors
    assert parsed.complete
    assert list(parsed.terms) == [{**term, "type": "term"} for term in standard]


def test_summary_missing_term_declaration_invalidates_the_whole_response() -> None:
    content = '{"type":"summary","text":"Plain scene.","refs":["1"]}\n{"type":"end"}'
    missing = parse_terminology_response(
        content, mode="terms+fragment-summary", source_refs=("1",)
    )
    assert not missing.terms_complete and not missing.summary_complete
    assert "missing_terms" in missing.global_error_codes
    declared = parse_terminology_response(
        content.replace('{"type":"end"}', '{"type":"no_terms"}\n{"type":"end"}'),
        mode="terms+fragment-summary",
        source_refs=("1",),
    )
    assert declared.complete and not declared.terms
