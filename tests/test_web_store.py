from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.web_store import WebStore
from app.errors import TermGroupError, UsageError
from app.execution import latest_completed_by_segment, load_stage_history
from app.project import add_project_files, init_project
from app.sqlite_storage import (
    query_segments,
    read_files,
    read_json,
    record_header,
    segment_ids,
    write_json,
)
from app.project_export import export_project
from app.term_library import TermNormalization, load_terms
from app.term_matching import match_terms
from tests.test_foundation import make_app_root


def create_web_store_project(tmp_path: Path, text: str = "one\n\ntwo") -> Path:
    app_root = make_app_root(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text(text, encoding="utf-8-sig")
    project, _ = init_project(
        [str(source)],
        name="web-demo",
        app_root=app_root,
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    return project


def test_term_group_materialize_switch_primary_and_lifecycle(tmp_path: Path) -> None:
    project = create_web_store_project(tmp_path, "Alice and Alicia")
    store = WebStore(project)
    store.save_term(
        {
            "source": "Alice",
            "category": "人物",
            "description": "main",
            "preferred_translation": "爱丽丝",
            "aliases": ["Alicia"],
        }
    )

    materialized = store.materialize_term(
        {"normalized": "alice", "alias": "Alicia"}
    )
    alice = next(item for item in materialized["terms"] if item["normalized"] == "alice")
    alicia = next(item for item in materialized["terms"] if item["normalized"] == "alicia")
    assert alice["aliases"] == []
    assert alicia["group_primary"] == "alice"
    assert alicia["category"] is None
    assert alicia["preferred_translation"] is None

    switched = store.set_term_primary({"normalized": "alicia", "confirm": True})
    alice = next(item for item in switched["terms"] if item["normalized"] == "alice")
    alicia = next(item for item in switched["terms"] if item["normalized"] == "alicia")
    assert alicia["group_primary"] is None
    assert alice["group_primary"] == "alicia"
    with pytest.raises(TermGroupError, match="组主仍有成员"):
        store.remove_terms({"normalized": ["alicia"]})
    removed = store.remove_terms({"normalized": ["alice"]})
    assert removed["removed"] == 1


def test_materialize_alias_restores_removed_entry_and_preserves_term_data(
    tmp_path: Path,
) -> None:
    project = create_web_store_project(tmp_path, "Alice Alicia Alicia Jr")
    store = WebStore(project)
    store.save_term(
        {
            "source": "Alice",
            "preferred_translation": "爱丽丝",
            "category": "人物",
            "description": "主角",
            "aliases": ["Alicia"],
        }
    )
    store.save_term(
        {
            "source": "Alicia",
            "preferred_translation": "艾丽西亚",
            "category": "别名条目",
            "description": "已有人物资料",
            "aliases": ["Alicia Jr"],
        }
    )
    removed = store.remove_terms({"normalized": ["alicia"]})
    before = removed["terms_revision"]
    assert next(item for item in removed["terms"] if item["normalized"] == "alicia")[
        "disabled"
    ]

    restored = store.materialize_term({"normalized": "alice", "alias": "Alicia"})
    rows = {item["normalized"]: item for item in restored["terms"]}
    assert restored["terms_revision"] == before + 1
    assert rows["alice"]["aliases"] == []
    assert rows["alicia"]["source"] == "Alicia"
    assert rows["alicia"]["preferred_translation"] == "艾丽西亚"
    assert rows["alicia"]["category"] == "别名条目"
    assert rows["alicia"]["description"] == "已有人物资料"
    assert rows["alicia"]["aliases"] == ["Alicia Jr"]
    assert rows["alicia"]["group_primary"] == "alice"
    assert rows["alicia"]["disabled"] is False
    matched = match_terms(
        "Alicia arrived",
        load_terms(project),
        10,
        TermNormalization("NFKC", True),
    )
    assert any(
        item["source"] == "Alicia" and item["preferred_translation"] == "艾丽西亚"
        for item in matched
    )


def test_materialize_alias_restores_removed_entry_into_owner_group(
    tmp_path: Path,
) -> None:
    project = create_web_store_project(tmp_path, "Alice Prime Alice Alicia")
    store = WebStore(project)
    store.save_term({"source": "Alice Prime"})
    store.save_term({"source": "Alice", "aliases": ["Alicia"]})
    store.save_term({"source": "Alicia", "preferred_translation": "艾丽西亚"})
    store.group_related_terms(
        {
            "normalized": "alice prime",
            "related_normalized": "alice",
            "primary_normalized": "alice prime",
            "confirm": True,
        }
    )
    store.remove_terms({"normalized": ["alicia"]})

    restored = store.materialize_term({"normalized": "alice", "alias": "Alicia"})
    rows = {item["normalized"]: item for item in restored["terms"]}
    assert rows["alicia"]["group_primary"] == "alice prime"
    assert rows["alicia"]["preferred_translation"] == "艾丽西亚"


def _group_three_terms(store: WebStore) -> None:
    for source in ("John", "Johnny", "John Jr"):
        store.save_term({"source": source})
    store.group_related_terms(
        {
            "normalized": "john",
            "related_normalized": "johnny",
            "primary_normalized": "john",
            "confirm": True,
        }
    )
    store.group_related_terms(
        {
            "normalized": "john",
            "related_normalized": "john jr",
            "primary_normalized": "john",
            "confirm": True,
        }
    )


def test_term_group_primary_and_all_members_can_be_removed_together(
    tmp_path: Path,
) -> None:
    project = create_web_store_project(tmp_path, "John Smith John Johnny")
    store = WebStore(project)
    _group_three_terms(store)

    with pytest.raises(TermGroupError) as partial_error:
        store.remove_terms({"normalized": ["john", "johnny"]})
    assert partial_error.value.params == {
        "reason": "primary_has_members",
        "normalized": "john",
        "members": ["john jr"],
    }

    removed = store.remove_terms(
        {"normalized": ["john", "johnny", "john jr"]}
    )
    assert removed["removed"] == 3
    assert all(item["disabled"] for item in removed["terms"])

    delete_root = tmp_path / "delete"
    delete_root.mkdir()
    project = create_web_store_project(delete_root, "John Johnny John Jr")
    store = WebStore(project)
    _group_three_terms(store)
    deleted = store.delete_terms(
        {"normalized": ["john", "johnny", "john jr"]}
    )
    assert deleted["deleted"] == 3
    assert deleted["terms"] == []


def test_term_group_member_can_leave_without_changing_term_data(tmp_path: Path) -> None:
    project = create_web_store_project(tmp_path, "John Smith John John Jr")
    store = WebStore(project)
    store.save_term(
        {
            "source": "John Smith",
            "preferred_translation": "约翰·史密斯",
            "category": "人物",
            "description": "全名",
        }
    )
    store.save_term(
        {
            "source": "John",
            "preferred_translation": "约翰",
            "category": "姓氏",
            "description": "成员说明",
            "aliases": ["Johnny"],
        }
    )
    store.save_term({"source": "John Jr", "preferred_translation": "小约翰"})
    store.group_related_terms(
        {
            "normalized": "john smith",
            "related_normalized": "john",
            "primary_normalized": "john smith",
            "confirm": True,
        }
    )
    store.group_related_terms(
        {
            "normalized": "john",
            "related_normalized": "john jr",
            "primary_normalized": "john smith",
            "confirm": True,
        }
    )
    before = {
        item["normalized"]: item
        for item in store.terms()["terms"]
        if item["normalized"] in {"john", "john jr"}
    }
    before_library = read_json(project, project / "terminology" / "terms.json")
    left = store.leave_term_group({"normalized": "john", "confirm": True})
    rows = {item["normalized"]: item for item in left["terms"]}
    assert rows["john"]["group_primary"] is None
    assert rows["john jr"]["group_primary"] == "john smith"
    for key in ("source", "aliases", "preferred_translation", "category", "description"):
        assert rows["john"][key] == before["john"][key]
    assert left["terms_revision"] == int(before_library["terms_revision"]) + 1


def test_term_group_member_leave_rejects_primary_and_stale_without_writing(
    tmp_path: Path,
) -> None:
    project = create_web_store_project(tmp_path, "John Smith John")
    store = WebStore(project)
    store.save_term({"source": "John Smith"})
    store.save_term({"source": "John"})
    store.group_related_terms(
        {
            "normalized": "john smith",
            "related_normalized": "john",
            "primary_normalized": "john smith",
            "confirm": True,
        }
    )
    before_library = read_json(project, project / "terminology" / "terms.json")
    before_overrides = read_json(project, project / "terminology" / "overrides.json")
    with pytest.raises(TermGroupError) as primary_error:
        store.leave_term_group({"normalized": "john smith", "confirm": True})
    assert primary_error.value.params["reason"] == "not_group_member"
    assert read_json(project, project / "terminology" / "terms.json") == before_library
    assert read_json(project, project / "terminology" / "overrides.json") == before_overrides
    with pytest.raises(TermGroupError) as stale_error:
        store.leave_term_group({"normalized": "missing", "confirm": True})
    assert stale_error.value.params["reason"] == "stale_member"
    assert read_json(project, project / "terminology" / "terms.json") == before_library
    assert read_json(project, project / "terminology" / "overrides.json") == before_overrides


def test_term_group_member_leave_preserves_independence_and_records_alias_claim(
    tmp_path: Path,
) -> None:
    project = create_web_store_project(tmp_path, "John John Smith")
    store = WebStore(project)
    store.save_term({"source": "John", "aliases": ["John Smith"]})
    store.save_term({"source": "John Smith"})
    grouped = {item["normalized"]: item for item in store.terms()["terms"]}
    assert grouped["john smith"]["group_primary"] == "john"

    left = store.leave_term_group({"normalized": "john smith", "confirm": True})
    rows = {item["normalized"]: item for item in left["terms"]}
    assert rows["john smith"]["group_primary"] is None
    assert any(
        claim["entry"] == "john smith" and claim["alias"] == "John Smith"
        for claim in rows["john smith"]["conflicts"]["group_claims"]
    )

    saved = store.save_term(
        {
            "old_normalized": "john",
            "source": "John",
            "aliases": ["John Smith"],
            "disabled": False,
        }
    )
    assert next(
        item for item in saved["terms"] if item["normalized"] == "john smith"
    )["group_primary"] is None


def test_related_terms_rank_containment_and_group_independent_entries(
    tmp_path: Path,
) -> None:
    project = create_web_store_project(tmp_path, "John Smith John")
    store = WebStore(project)
    store.save_term({"source": "John Smith", "preferred_translation": "约翰·史密斯"})
    store.save_term({"source": "John", "preferred_translation": "约翰"})
    store.save_term({"source": "Smith", "aliases": ["Smitty"]})

    related = store.related_terms("john smith")
    assert {item["normalized"] for item in related["related"]} == {
        "john",
        "smith",
    }
    assert all(item["relation"] == "contained_by_selected" for item in related["related"])
    assert all(item["can_group"] is True for item in related["related"])
    assert all(item["can_convert_alias"] is True for item in related["related"])
    assert all(item["can_remove"] is True for item in related["related"])

    grouped = store.group_related_terms(
        {
            "normalized": "john smith",
            "related_normalized": "john",
            "primary_normalized": "john smith",
            "confirm": True,
        }
    )
    rows = {item["normalized"]: item for item in grouped["terms"]}
    assert rows["john"]["group_primary"] == "john smith"
    assert rows["john smith"]["group_primary"] is None


def test_related_group_can_add_independent_term_to_existing_group(
    tmp_path: Path,
) -> None:
    project = create_web_store_project(tmp_path, "John Smith John Johnny")
    store = WebStore(project)
    store.save_term({"source": "John Smith"})
    store.save_term({"source": "John"})
    store.save_term({"source": "Johnny"})
    store.group_related_terms(
        {
            "normalized": "john",
            "related_normalized": "johnny",
            "primary_normalized": "john",
            "confirm": True,
        }
    )

    related = store.related_terms("john smith")
    john = next(item for item in related["related"] if item["normalized"] == "john")
    assert john["group_root_normalized"] == "john"
    assert john["group_size"] == 2
    assert john["can_group"] is True

    grouped = store.group_related_terms(
        {
            "normalized": "john smith",
            "related_normalized": "john",
            "primary_normalized": "john",
            "confirm": True,
        }
    )
    rows = {item["normalized"]: item for item in grouped["terms"]}
    assert rows["john smith"]["group_primary"] == "john"
    assert rows["johnny"]["group_primary"] == "john"


def test_related_terms_blocks_two_existing_groups(tmp_path: Path) -> None:
    project = create_web_store_project(
        tmp_path, "John Johnny John Smith John Smithson"
    )
    store = WebStore(project)
    for source in ("John", "Johnny", "John Smith", "John Smithson"):
        store.save_term({"source": source})
    store.group_related_terms(
        {
            "normalized": "john",
            "related_normalized": "johnny",
            "primary_normalized": "john",
            "confirm": True,
        }
    )
    store.group_related_terms(
        {
            "normalized": "john smith",
            "related_normalized": "john smithson",
            "primary_normalized": "john smith",
            "confirm": True,
        }
    )

    related = store.related_terms("john")
    john = next(item for item in related["related"] if item["normalized"] == "john smith")
    assert john["blocked_reason"] == "cross_group"
    with pytest.raises(TermGroupError, match="不能快捷加入") as error:
        store.group_related_terms(
            {
                "normalized": "john",
                "related_normalized": "john smith",
                "primary_normalized": "john",
                "confirm": True,
            }
        )
    assert error.value.params["reason"] == "group_collision"


def test_related_conversion_moves_all_forms_and_disables_candidate(
    tmp_path: Path,
) -> None:
    project = create_web_store_project(tmp_path, "John Smith John Johnny")
    store = WebStore(project)
    store.save_term({"source": "John Smith", "preferred_translation": "约翰·史密斯"})
    store.save_term({"source": "John", "preferred_translation": "约翰", "aliases": ["Johnny"]})
    before = load_terms(project)
    converted = store.convert_related_to_alias(
        {
            "normalized": "john smith",
            "related_normalized": "john",
            "confirm": True,
        }
    )
    rows = {item["normalized"]: item for item in converted["terms"]}
    assert set(rows["john smith"]["aliases"]) == {"John", "Johnny"}
    assert rows["john"]["disabled"] is True
    assert converted["aliases_added"] == ["John", "Johnny"]
    assert converted["terms_revision"] == int(before["terms_revision"]) + 1


def test_related_conversion_rejects_external_alias_collision_without_writing(
    tmp_path: Path,
) -> None:
    project = create_web_store_project(tmp_path, "John Smith John Johnny Jack")
    store = WebStore(project)
    store.save_term({"source": "John Smith"})
    store.save_term({"source": "John", "aliases": ["Johnny"]})
    store.save_term({"source": "Jack"})
    library = read_json(project, project / "terminology" / "terms.json")
    next(item for item in library["terms"] if item["normalized"] == "john")["aliases"].append("Jack")
    write_json(project, project / "terminology" / "terms.json", library)
    overrides = read_json(project, project / "terminology" / "overrides.json")
    next(item for item in overrides["overrides"] if item["normalized"] == "john")["aliases"].append("Jack")
    write_json(project, project / "terminology" / "overrides.json", overrides)
    before_library = read_json(project, project / "terminology" / "terms.json")
    before_overrides = read_json(project, project / "terminology" / "overrides.json")
    with pytest.raises(TermGroupError, match="alias") as error:
        store.convert_related_to_alias(
            {
                "normalized": "john smith",
                "related_normalized": "john",
                "confirm": True,
            }
        )
    assert error.value.params["reason"] == "alias_collision"
    assert read_json(project, project / "terminology" / "terms.json") == before_library
    assert read_json(project, project / "terminology" / "overrides.json") == before_overrides


def test_group_member_conversion_moves_forms_to_primary_without_containment(
    tmp_path: Path,
) -> None:
    project = create_web_store_project(tmp_path, "John Smith John Alice A Bob B")
    store = WebStore(project)
    store.save_term({"source": "John Smith"})
    store.save_term({"source": "John", "preferred_translation": "约翰"})
    store.group_related_terms(
        {
            "normalized": "john smith",
            "related_normalized": "john",
            "primary_normalized": "john smith",
            "confirm": True,
        }
    )
    store.save_term(
        {"old_normalized": "john smith", "source": "John Smith", "aliases": ["Bob"]}
    )
    store.materialize_term({"normalized": "john smith", "alias": "Bob"})
    store.save_term(
        {
            "old_normalized": "john",
            "source": "Alice",
            "aliases": ["A"],
            "preferred_translation": "爱丽丝",
            "category": "人物",
            "description": "member-only data",
        }
    )
    store.save_term({"old_normalized": "bob", "source": "Bob", "aliases": ["B"]})
    before = load_terms(project)

    converted = store.convert_related_to_alias(
        {
            "normalized": "bob",
            "related_normalized": "alice",
            "confirm": True,
        }
    )

    rows = {item["normalized"]: item for item in converted["terms"]}
    assert set(rows["john smith"]["aliases"]) == {"Alice", "A"}
    assert rows["alice"]["disabled"] is True
    assert rows["alice"]["group_primary"] is None
    assert converted["terms_revision"] == int(before["terms_revision"]) + 1


def test_related_capabilities_protect_group_primary_from_conversion_and_removal(
    tmp_path: Path,
) -> None:
    project = create_web_store_project(tmp_path, "John Smith John Johnny")
    store = WebStore(project)
    store.save_term({"source": "John Smith"})
    store.save_term({"source": "John"})
    store.save_term({"source": "Johnny"})
    store.group_related_terms(
        {
            "normalized": "john",
            "related_normalized": "johnny",
            "primary_normalized": "john",
            "confirm": True,
        }
    )

    related = store.related_terms("john smith")["related"]
    candidate = next(item for item in related if item["normalized"] == "john")
    assert candidate["can_convert_alias"] is False
    assert candidate["can_remove"] is False


def test_match_terms_counts_a_group_as_one_slot() -> None:
    library = {
        "terms": [
            {
                "source": "Alice",
                "normalized": "alice",
                "preferred_translation": "爱丽丝",
                "aliases": [],
                "group_primary": None,
                "conflicts": {},
            },
            {
                "source": "Alicia",
                "normalized": "alicia",
                "preferred_translation": "艾丽西亚",
                "aliases": ["Ally"],
                "group_primary": "alice",
                "conflicts": {},
            },
            {
                "source": "Bob",
                "normalized": "bob",
                "preferred_translation": "鲍勃",
                "aliases": [],
                "group_primary": None,
                "conflicts": {},
            },
        ]
    }
    matched = match_terms(
        "Alicia met Bob", library, 1, TermNormalization("NFKC", True)
    )
    assert [item["source"] for item in matched] == ["Alice", "Alicia"]
    assert matched[1]["primary_source"] == "Alice"


def test_term_hits_isolate_a_group_member_from_its_primary(tmp_path: Path) -> None:
    project = create_web_store_project(
        tmp_path,
        "Alice only\nAlicia walks\nAlly sings\nAlice and Alicia",
    )
    project_id = str(read_json(project, project / "project.json")["project_id"])
    write_json(
        project,
        project / "terminology" / "terms.json",
        record_header(
            "terminology_library",
            project_id,
            record_id="TERMS-MEMBER-HITS",
            terms_revision=1,
            terms=[
                {
                    "record_id": "TERM-PRIMARY",
                    "source": "Alice",
                    "normalized": "alice",
                    "category": None,
                    "description": None,
                    "preferred_translation": "爱丽丝",
                    "aliases": [],
                    "group_primary": None,
                    "conflicts": {},
                },
                {
                    "record_id": "TERM-MEMBER",
                    "source": "Alicia",
                    "normalized": "alicia",
                    "category": None,
                    "description": None,
                    "preferred_translation": "艾丽西亚",
                    "aliases": ["Ally"],
                    "group_primary": "alice",
                    "conflicts": {},
                },
            ],
        ),
    )

    hits = WebStore(project).term_hits("alicia")

    assert hits["source"] == "Alicia"
    assert hits["total"] == 3
    assert [item["source"] for item in hits["hits"]] == [
        "Alicia walks",
        "Ally sings",
        "Alice and Alicia",
    ]


def test_term_hits_include_matches_found_in_aozora_ruby_reading(
    tmp_path: Path,
) -> None:
    project = create_web_store_project(tmp_path, "｜猫《Aoki》出现")
    store = WebStore(project)
    store.save_term(
        {
            "source": "Aoki",
            "category": "人名",
            "description": None,
            "preferred_translation": "青木",
            "aliases": [],
        }
    )

    hits = store.term_hits("aoki")

    assert hits["total"] == 1
    assert hits["hits"][0]["source"] == "｜猫《Aoki》出现"


def test_terms_keep_group_primary_before_members_even_when_member_conflicts(
    tmp_path: Path,
) -> None:
    project = create_web_store_project(tmp_path)
    project_id = str(read_json(project, project / "project.json")["project_id"])
    write_json(
        project,
        project / "terminology" / "terms.json",
        record_header(
            "terminology_library",
            project_id,
            record_id="TERMS-GROUP-ORDER",
            terms_revision=1,
            terms=[
                {
                    "record_id": "TERM-ZETA",
                    "source": "Zeta",
                    "normalized": "zeta",
                    "category": None,
                    "description": None,
                    "preferred_translation": None,
                    "aliases": [],
                    "group_primary": None,
                    "conflicts": {},
                },
                {
                    "record_id": "TERM-ALPHA",
                    "source": "Alpha",
                    "normalized": "alpha",
                    "category": None,
                    "description": None,
                    "preferred_translation": None,
                    "aliases": [],
                    "group_primary": "zeta",
                    "conflicts": {"categories": ["人物"]},
                },
                {
                    "record_id": "TERM-BETA",
                    "source": "Beta",
                    "normalized": "beta",
                    "category": None,
                    "description": None,
                    "preferred_translation": None,
                    "aliases": [],
                    "group_primary": "zeta",
                    "conflicts": {},
                },
                {
                    "record_id": "TERM-AARDVARK",
                    "source": "Aardvark",
                    "normalized": "aardvark",
                    "category": None,
                    "description": None,
                    "preferred_translation": None,
                    "aliases": [],
                    "group_primary": None,
                    "conflicts": {},
                },
            ],
        ),
    )

    rows = WebStore(project).terms()["terms"]

    assert [item["normalized"] for item in rows] == [
        "zeta",
        "alpha",
        "beta",
        "aardvark",
    ]


def seed_conflicted_terms(project: Path) -> None:
    project_id = str(read_json(project, project / "project.json")["project_id"])
    terms = [
        {
            "record_id": "TERM-000001",
            "source": "Alpha",
            "normalized": "alpha",
            "category": None,
            "description": "category conflict",
            "preferred_translation": "阿尔法",
            "aliases": [],
            "conflicts": {
                "categories": ["人物", "地点"],
                "preferred_translations": [],
            },
        },
        {
            "record_id": "TERM-000002",
            "source": "Beta",
            "normalized": "beta",
            "category": "人物",
            "description": "translation conflict",
            "preferred_translation": None,
            "aliases": [],
            "conflicts": {
                "categories": [],
                "preferred_translations": ["贝塔", "比塔"],
            },
        },
        {
            "record_id": "TERM-000003",
            "source": "Gamma",
            "normalized": "gamma",
            "category": None,
            "description": "both conflicts",
            "preferred_translation": None,
            "aliases": [],
            "conflicts": {
                "categories": ["人物", "组织"],
                "preferred_translations": ["伽马", "加玛"],
            },
        },
    ]
    write_json(
        project,
        project / "terminology" / "terms.json",
        record_header(
            "terminology_library",
            project_id,
            record_id="TERMS-1",
            terms_revision=1,
            published_run_id="RUN-TEST",
            active_task_id="TERM-TASK-TEST",
            terms=terms,
        ),
    )


def test_web_store_overview_excludes_empty_segments_and_preserves_order(
    tmp_path: Path,
) -> None:
    project = create_web_store_project(tmp_path, "first\n\u3000\nsecond")
    store = WebStore(project)
    overview = store.overview()
    assert overview["name"] == "web-demo"
    assert [item["source"] for item in overview["segments"]] == ["first", "second"]
    assert [item["line_index"] for item in overview["segments"]] == [0, 2]
    assert [item["part_id"] for item in overview["segments"]] == [
        "document",
        "document",
    ]
    assert overview["segments"][0]["translation"] is None
    assert store.segment_detail("F0001-S000001")["part_id"] == "document"
    assert overview["segments"][0]["reviews"]["proofreading"] == {
        "base": None,
        "suggestion": None,
        "applied": None,
        "outdated": False,
        "applied_current": False,
    }


def test_web_store_overview_reports_storage_and_source_file_sizes(
    tmp_path: Path,
) -> None:
    project = create_web_store_project(tmp_path, "source text")
    overview = WebStore(project).overview()
    stored = project / "input" / str(read_files(project)[0]["stored_name"])

    total_bytes = sum(
        path.stat().st_size
        for path in project.rglob("*")
        if path.is_file() and not path.is_symlink()
    )

    assert overview["storage"] == {
        "total_bytes": total_bytes,
        "sqlite_bytes": (project / "project.sqlite").stat().st_size,
    }
    assert overview["files"][0]["size_bytes"] == stored.stat().st_size


def test_web_store_filters_segments_by_file_and_part(tmp_path: Path) -> None:
    project = create_web_store_project(tmp_path, "chapter one\nchapter two\nchapter one again")
    connection = sqlite3.connect(project / "project.sqlite")
    try:
        connection.executemany(
            "UPDATE segments SET part_id = ? WHERE segment_id = ?",
            [
                ("chapter-1.xhtml", "F0001-S000001"),
                ("chapter-2.xhtml", "F0001-S000002"),
                ("chapter-1.xhtml", "F0001-S000003"),
            ],
        )
        connection.commit()
    finally:
        connection.close()

    store = WebStore(project)
    store.save_translation(
        {"segment_id": "F0001-S000001", "text": "translated chapter one"}
    )
    overview = store.overview(file_id="F0001", part_id="chapter-1.xhtml")

    assert overview["files"][0]["part_ids"] == [
        "chapter-1.xhtml",
        "chapter-2.xhtml",
    ]
    assert [item["part_id"] for item in overview["segments"]] == [
        "chapter-1.xhtml",
        "chapter-1.xhtml",
    ]
    assert overview["total_segments"] == 2
    assert overview["completed_segments"] == 1
    other = store.overview(file_id="F0001", part_id="chapter-2.xhtml")
    assert other["total_segments"] == 1
    assert other["completed_segments"] == 0
    assert store.segment_index(
        file_id="F0001", part_id="chapter-2.xhtml"
    )["segment_ids"] == ["F0001-S000002"]


def test_segment_windows_follow_file_order_not_file_id(tmp_path: Path) -> None:
    project = create_web_store_project(tmp_path, "first")
    second = tmp_path / "second.txt"
    second.write_text("second", encoding="utf-8")
    add_project_files(project, [str(second)])

    database = sqlite3.connect(project / "project.sqlite")
    try:
        rows = database.execute(
            "SELECT file_id, payload_json FROM files ORDER BY file_id"
        ).fetchall()
        assert [row[0] for row in rows] == ["F0001", "F0002"]
        for file_id, payload_json in rows:
            payload = json.loads(payload_json)
            payload["file_order"] = 20 if file_id == "F0001" else 10
            database.execute(
                "UPDATE files SET file_order = ?, payload_json = ? WHERE file_id = ?",
                (payload["file_order"], json.dumps(payload, ensure_ascii=False), file_id),
            )
        database.commit()
    finally:
        database.close()

    assert [item["source"] for item in query_segments(project)] == ["second", "first"]
    ordered_ids = segment_ids(project)
    assert ordered_ids == ["F0002-S000001", "F0001-S000001"]

    store = WebStore(project)
    index = store.segment_index()
    assert index["segment_ids"] == ordered_ids
    assert index["total"] == len(ordered_ids)
    for offset, limit in ((0, 1), (1, 1), (0, 2)):
        window = store.overview(offset=offset, limit=limit)
        assert [item["segment_id"] for item in window["segments"]] == ordered_ids[
            offset : offset + limit
        ]
        assert window["total_segments"] == len(ordered_ids)


def test_segment_detail_context_uses_unfiltered_source_neighbors(
    tmp_path: Path,
) -> None:
    project = create_web_store_project(
        tmp_path,
        "before\nmatch\nnear after\nfar after",
    )
    store = WebStore(project)
    ordered_ids = segment_ids(project)

    assert store.segment_index(search="match")["segment_ids"] == [ordered_ids[1]]
    store.save_translation({"segment_id": ordered_ids[0], "text": "上文译文"})

    detail = store.segment_detail(ordered_ids[1])
    context = detail["context"]
    assert [item["segment_id"] for item in context["before"]] == [ordered_ids[0]]
    assert [item["segment_id"] for item in context["after"]] == ordered_ids[2:]
    assert context["before"][0]["translation"]["text"] == "上文译文"
    assert all(
        item["file_id"] == detail["file_id"]
        and item["part_id"] == detail["part_id"]
        for item in [*context["before"], *context["after"]]
    )


def test_web_store_translation_appends_results_and_exports_latest(tmp_path: Path) -> None:
    project = create_web_store_project(tmp_path, "one")
    store = WebStore(project)
    before_runs = list((project / "runs").iterdir())
    first = store.save_translation(
        {"segment_id": "F0001-S000001", "text": "第一版"}
    )
    second = store.save_translation(
        {"segment_id": "F0001-S000001", "text": "第二版"}
    )

    history = load_stage_history(project, "translation")
    latest = latest_completed_by_segment(history)["F0001-S000001"]
    assert len(history) == 2
    assert first["record_id"] != second["record_id"]
    assert latest["text"] == "第二版"
    assert latest["origin"] == "web"
    assert latest["run_id"] is None
    assert list((project / "runs").iterdir()) == before_runs

    export_project(project, "translated", bilingual=False, allow_missing=False)
    output = project / "output" / "translated" / "source.txt"
    assert output.read_text(encoding="utf-8-sig") == "第二版"


def test_web_store_reset_masks_translation_until_new_result(tmp_path: Path) -> None:
    project = create_web_store_project(tmp_path, "one")
    store = WebStore(project)
    store.save_translation({"segment_id": "F0001-S000001", "text": "first"})

    reset = store.reset_results(
        {"stage": "translation", "segment_ids": ["F0001-S000001"]}
    )
    assert reset["cleared"] == 1
    assert store.segment_detail("F0001-S000001")["translation"] is None

    store.save_translation({"segment_id": "F0001-S000001", "text": "second"})
    assert store.segment_detail("F0001-S000001")["translation"]["text"] == "second"


def test_web_store_reset_review_also_clears_applied_without_cascade(
    tmp_path: Path,
) -> None:
    project = create_web_store_project(tmp_path, "one")
    store = WebStore(project)
    segment_id = "F0001-S000001"
    store.save_translation({"segment_id": segment_id, "text": "base"})
    store.save_review(
        {
            "stage": "proofreading",
            "segment_id": segment_id,
            "review_status": "suggested",
            "suggested_text": "proofread",
            "reason": None,
            "apply": True,
        }
    )
    store.save_review(
        {
            "stage": "polishing",
            "segment_id": segment_id,
            "review_status": "accepted",
            "apply": False,
        }
    )

    reset = store.reset_results(
        {"stage": "proofreading", "segment_ids": [segment_id]}
    )
    assert reset["cleared"] == 1
    assert reset["reset_records"] == 2
    detail = store.segment_detail(segment_id)
    assert detail["reviews"]["proofreading"]["suggestion"] is None
    assert detail["reviews"]["proofreading"]["applied"] is None
    assert detail["reviews"]["polishing"]["suggestion"] is not None


def test_web_store_translation_records_enabled_validator_warning(tmp_path: Path) -> None:
    project = create_web_store_project(tmp_path, "one")
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "validators = []", 'validators = ["japanese_kana"]'
        ),
        encoding="utf-8",
    )
    result = WebStore(project).save_translation(
        {"segment_id": "F0001-S000001", "text": "残留カナ"}
    )
    assert result["validation_status"] == "warning"
    store = WebStore(project)
    assert store.overview()["segments"][0]["translation"]["validation_status"] == (
        "warning"
    )
    assert store.overview(status="warning")["total_segments"] == 1
    assert store.segment_index(status="warning")["segment_ids"] == [
        "F0001-S000001"
    ]
    record = load_stage_history(project, "translation")[-1]
    assert record["validation_findings"][0]["validator"] == "japanese_kana"


def test_web_store_saves_and_applies_review_results_with_current_lineage(
    tmp_path: Path,
) -> None:
    project = create_web_store_project(tmp_path, "one")
    store = WebStore(project)
    store.save_translation({"segment_id": "F0001-S000001", "text": "译文一"})
    proof = store.save_review(
        {
            "stage": "proofreading",
            "segment_id": "F0001-S000001",
            "review_status": "suggested",
            "suggested_text": "校对文本",
            "reason": "人工调整",
            "apply": True,
        }
    )
    assert proof["applied"]["text"] == "校对文本"
    assert proof["suggestion"]["origin"] == "web"

    store.save_review(
        {
            "stage": "polishing",
            "segment_id": "F0001-S000001",
            "review_status": "suggested",
            "suggested_text": "润色文本",
            "reason": "",
            "apply": True,
        }
    )
    detail = store.segment_detail("F0001-S000001")
    assert detail["reviews"]["proofreading"]["applied"]["text"] == "校对文本"
    assert detail["reviews"]["proofreading"]["applied_current"] is True
    assert detail["reviews"]["polishing"]["base"]["text"] == "校对文本"
    assert detail["reviews"]["polishing"]["applied"]["text"] == "润色文本"
    assert detail["reviews"]["polishing"]["applied_current"] is True

    overview = store.overview()["segments"][0]
    assert overview["translation"]["text"] == "译文一"
    assert overview["reviews"]["proofreading"]["base"]["text"] == "译文一"
    assert overview["reviews"]["proofreading"]["suggestion"]["reason"] == "人工调整"
    assert overview["reviews"]["polishing"]["base"]["text"] == "校对文本"

    store.save_translation({"segment_id": "F0001-S000001", "text": "译文二"})
    outdated = store.segment_detail("F0001-S000001")["reviews"]["proofreading"]
    assert outdated["outdated"]
    assert outdated["applied_current"] is True
    store.save_review(
        {
            "stage": "proofreading",
            "segment_id": "F0001-S000001",
            "review_status": "accepted",
            "suggested_text": "ignored",
            "reason": None,
            "apply": True,
        }
    )
    applied = latest_completed_by_segment(
        load_stage_history(project, "proofreading_applied")
    )["F0001-S000001"]
    assert applied["text"] == "译文二"
    current = store.overview()["segments"][0]["reviews"]["proofreading"]
    assert current["outdated"] is False
    assert current["applied_current"] is True

    store.save_review(
        {
            "stage": "proofreading",
            "segment_id": "F0001-S000001",
            "review_status": "suggested",
            "suggested_text": "尚未应用的新建议",
            "reason": "再次调整",
            "apply": False,
        }
    )
    newer = store.overview()["segments"][0]["reviews"]["proofreading"]
    assert newer["applied"]["text"] == "译文二"
    assert newer["applied_current"] is False


def test_web_store_polishing_overview_falls_back_to_translation_base(
    tmp_path: Path,
) -> None:
    project = create_web_store_project(tmp_path, "one")
    store = WebStore(project)
    translation = store.save_translation(
        {"segment_id": "F0001-S000001", "text": "仅有译文"}
    )

    polishing = store.overview()["segments"][0]["reviews"]["polishing"]
    assert polishing["base"]["record_id"] == translation["record_id"]
    assert polishing["base"]["text"] == "仅有译文"
    assert polishing["suggestion"] is None
    assert polishing["applied_current"] is False


def test_web_store_terms_update_library_and_overrides_immediately(tmp_path: Path) -> None:
    project = create_web_store_project(tmp_path)
    store = WebStore(project)
    added = store.save_term(
        {
            "old_normalized": None,
            "source": "Alice",
            "preferred_translation": "爱丽丝",
            "category": "人名",
            "description": "主角",
            "aliases": ["Alice A."],
            "disabled": False,
        }
    )
    assert added["terms_revision"] == 1
    assert match_terms("Alice arrived", load_terms(project), 10, TermNormalization("NFKC", True))[0][
        "preferred_translation"
    ] == "爱丽丝"

    renamed = store.save_term(
        {
            "old_normalized": "alice",
            "source": "Alicia",
            "preferred_translation": "艾丽西亚",
            "category": "人名",
            "description": "主角",
            "aliases": [],
            "disabled": False,
        }
    )
    assert renamed["terms_revision"] == 2
    overrides = read_json(project, project / "terminology" / "overrides.json")["overrides"]
    assert next(item for item in overrides if item["normalized"] == "alice")[
        "disabled"
    ]
    assert load_terms(project)["terms"][0]["normalized"] == "alicia"

    with pytest.raises(UsageError, match="已存在"):
        store.save_term(
            {
                "old_normalized": None,
                "source": "Alicia",
                "aliases": [],
                "disabled": False,
            }
        )
    disabled = store.save_term(
        {
            "old_normalized": "alicia",
            "source": "Alicia",
            "preferred_translation": "艾丽西亚",
            "category": "人名",
            "description": "主角",
            "aliases": [],
            "disabled": True,
        }
    )
    assert disabled["terms_revision"] == 3
    assert load_terms(project)["terms"] == []
    assert match_terms("Alicia arrived", load_terms(project), 10, TermNormalization("NFKC", True)) == []

    restored = store.save_term(
        {
            "old_normalized": "alicia",
            "source": "Alicia",
            "preferred_translation": "艾丽西亚",
            "category": "人名",
            "description": "主角",
            "aliases": [],
            "disabled": False,
        }
    )
    assert restored["terms_revision"] == 4
    assert restored["terms"][0]["disabled"] is False
    assert match_terms("Alicia arrived", load_terms(project), 10, TermNormalization("NFKC", True))[0][
        "preferred_translation"
    ] == "艾丽西亚"


def test_web_store_exposes_and_resolves_term_conflicts_independently(
    tmp_path: Path,
) -> None:
    project = create_web_store_project(tmp_path)
    seed_conflicted_terms(project)
    store = WebStore(project)

    result = store.terms()
    assert result["conflict_count"] == 3
    assert [item["normalized"] for item in result["terms"]] == [
        "alpha",
        "beta",
        "gamma",
    ]
    assert result["terms"][0]["conflicts"]["categories"] == ["人物", "地点"]
    assert result["terms"][1]["conflicts"]["preferred_translations"] == [
        "贝塔",
        "比塔",
    ]

    with pytest.raises(UsageError, match="类别冲突"):
        store.save_term(
            {
                "old_normalized": "alpha",
                "source": "Alpha",
                "preferred_translation": "阿尔法",
                "category": "",
                "description": "category conflict",
                "aliases": [],
                "disabled": False,
            }
        )
    resolved = store.save_term(
        {
            "old_normalized": "alpha",
            "source": "Alpha",
            "preferred_translation": "阿尔法",
            "category": "自定义类别",
            "description": "category conflict",
            "aliases": [],
            "disabled": False,
        }
    )
    assert resolved["terms_revision"] == 2
    assert resolved["conflict_count"] == 2
    alpha = next(item for item in resolved["terms"] if item["normalized"] == "alpha")
    assert alpha["category"] == "自定义类别"
    assert alpha["has_conflicts"] is False
    assert next(
        item for item in resolved["terms"] if item["normalized"] == "gamma"
    )["has_conflicts"]

    with pytest.raises(UsageError, match="推荐译名冲突"):
        store.save_term(
            {
                "old_normalized": "beta",
                "source": "Beta",
                "preferred_translation": "",
                "category": "人物",
                "description": "translation conflict",
                "aliases": [],
                "disabled": False,
            }
        )


def test_web_store_can_remove_conflicted_term_without_resolving_it(
    tmp_path: Path,
) -> None:
    project = create_web_store_project(tmp_path)
    seed_conflicted_terms(project)
    store = WebStore(project)
    removed = store.save_term(
        {
            "old_normalized": "gamma",
            "source": "Gamma",
            "preferred_translation": "",
            "category": "",
            "description": "both conflicts",
            "aliases": [],
            "disabled": True,
        }
    )
    assert removed["conflict_count"] == 2
    gamma = next(item for item in removed["terms"] if item["normalized"] == "gamma")
    assert gamma["disabled"] is True
    assert gamma["has_conflicts"] is False
    assert all(
        item["normalized"] != "gamma" for item in load_terms(project)["terms"]
    )
    override = next(
        item
        for item in read_json(project, project / "terminology" / "overrides.json")[
            "overrides"
        ]
        if item["normalized"] == "gamma"
    )
    assert override["disabled"] is True


def test_web_store_rejects_unknown_segments_and_invalid_terms(tmp_path: Path) -> None:
    store = WebStore(create_web_store_project(tmp_path))
    with pytest.raises(UsageError, match="未知或空"):
        store.save_translation({"segment_id": "missing", "text": "x"})
    with pytest.raises(UsageError, match="source 不能为空"):
        store.save_term({"source": " ", "aliases": []})
    with pytest.raises(UsageError, match="aliases"):
        store.save_term({"source": "Alice", "aliases": "Alice"})


def test_web_store_keeps_case_distinct_aliases_when_case_insensitive_off(
    tmp_path: Path,
) -> None:
    project = create_web_store_project(tmp_path)
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "case_insensitive = true",
            "case_insensitive = false",
        ),
        encoding="utf-8",
    )
    store = WebStore(project)
    added = store.save_term(
        {
            "old_normalized": None,
            "source": "Alice",
            "preferred_translation": "爱丽丝",
            "category": "人名",
            "description": "主角",
            "aliases": ["alice"],
            "disabled": False,
        }
    )
    assert added["terms_revision"] == 1
    assert added["terms"][0]["aliases"] == ["alice"]
    spec = TermNormalization("NFKC", False)
    assert [item["source"] for item in match_terms("alice arrived", load_terms(project), 10, spec)] == ["Alice"]
