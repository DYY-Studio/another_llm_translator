from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from app.errors import UsageError
from app.execution import Scope
from app.main import run
from app.project import init_project
from app.stages import run_terminology
from app.term_exchange import export_terms, import_terms
from app.term_library import TermNormalization, load_terms, publish_partial_terms
from app.term_matching import match_terms
from app.sqlite_storage import (
    append_jsonl,
    read_json,
    read_jsonl,
    record_header,
    write_json,
)
from tests.helpers import llm_jsonl
from tests.test_foundation import make_app_root


def make_project(tmp_path: Path, name: str = "terms") -> Path:
    app_root = make_app_root(tmp_path / name)
    source = tmp_path / f"{name}.txt"
    source.write_text("Alpha and Beta", encoding="utf-8")
    project, _ = init_project(
        [str(source)],
        name=name,
        app_root=app_root,
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    return project


def write_exchange(path: Path, terms: list[dict]) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "record_type": "terminology_exchange",
                "terms": terms,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def term(source: str, *, aliases: list[str] | None = None, disabled: bool = False):
    return {
        "source": source,
        "preferred_translation": f"译-{source}",
        "category": "名称",
        "description": f"说明-{source}",
        "aliases": aliases or [],
        "disabled": disabled,
        "conflicts": {"categories": [], "preferred_translations": []},
    }


def test_term_group_exchange_v2_round_trip_and_rejects_dangling_primary(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path, "group-exchange")
    source = tmp_path / "group-v2.json"
    source.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "record_type": "terminology_exchange",
                "terms": [
                    term("Alice"),
                    {**term("Alicia"), "group_primary": "Alice"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    import_terms(project, source, dry_run=False)
    library = load_terms(project)
    assert library is not None
    assert next(
        item for item in library["terms"] if item["source"] == "Alicia"
    )["group_primary"] == "alice"

    exported = tmp_path / "group-export.json"
    export_terms(project, exported, include_disabled=False)
    document = json.loads(exported.read_text(encoding="utf-8-sig"))
    assert document["schema_version"] == 2
    assert next(
        item for item in document["terms"] if item["source"] == "Alicia"
    )["group_primary"] == "Alice"

    invalid = tmp_path / "dangling.json"
    invalid.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "record_type": "terminology_exchange",
                "terms": [{**term("Orphan"), "group_primary": "Missing"}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(UsageError, match="组主不存在"):
        import_terms(project, invalid, dry_run=False)


def test_explicit_standalone_group_relation_blocks_automatic_grouping(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path, "locked-group")
    source = tmp_path / "locked-group.json"
    source.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "record_type": "terminology_exchange",
                "terms": [
                    {**term("Alpha", aliases=["Beta"]), "group_primary": None},
                    {**term("Beta"), "group_primary": None},
                ],
            }
        ),
        encoding="utf-8",
    )
    import_terms(project, source, dry_run=False)
    rows = {item["source"]: item for item in load_terms(project)["terms"]}
    assert rows["Beta"]["group_primary"] is None
    assert rows["Beta"]["conflicts"]["group_claims"][0]["reason"] == "group_collision"


def test_scanned_terms_can_be_exported_and_published_without_complete_task(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path, "recover")
    metadata = read_json(project, project / "project.json")
    task_id = "TERM-TASK-PARTIAL"
    write_json(
        project,
        project / "terminology" / "active_task.json",
        record_header(
            "terminology_task",
            str(metadata["project_id"]),
            record_id=task_id,
            active_task_id=task_id,
            status="active",
            initial_stage_fingerprint="test",
        ),
    )
    append_jsonl(
        project,
        project / "terminology" / "candidates.jsonl",
        record_header(
            "terminology_candidates",
            str(metadata["project_id"]),
            active_task_id=task_id,
            run_id="RUN-PARTIAL",
            terms=[
                {
                    "source": "recover",
                    "category": "名词",
                    "description": "测试候选",
                    "preferred_translation": "恢复",
                    "aliases": [],
                }
            ],
        ),
    )

    output = tmp_path / "scanned.json"
    exported = export_terms(
        project,
        output,
        include_disabled=False,
        source="scanned",
    )
    assert exported["source"] == "scanned"
    assert json.loads(output.read_text(encoding="utf-8"))["terms"][0]["source"] == "recover"
    assert not (project / "terminology" / "terms.json").exists()

    published = publish_partial_terms(project)
    assert published["published"] is True
    assert load_terms(project)["terms"][0]["source"] == "recover"
    assert read_json(project, project / "terminology" / "active_task.json")["status"] == "partial_published"
    assert read_jsonl(project, project / "terminology" / "candidates.jsonl")


def test_terms_json_csv_round_trip_and_disabled_export(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    source = tmp_path / "terms.json"
    write_exchange(source, [term("Alpha"), term("Beta", disabled=True)])

    summary = import_terms(project, source, dry_run=False)
    assert summary["changed"] is True
    assert summary["terms_revision"] == 1
    library = load_terms(project)
    assert library is not None
    assert [item["source"] for item in library["terms"]] == ["Alpha"]

    visible = tmp_path / "visible.csv"
    export_terms(project, visible, include_disabled=False)
    assert "Beta" not in visible.read_text(encoding="utf-8-sig")
    complete = tmp_path / "complete.csv"
    export_terms(project, complete, include_disabled=True)
    assert "Beta" in complete.read_text(encoding="utf-8-sig")

    restored = make_project(tmp_path, "restored")
    imported = import_terms(restored, complete, dry_run=False)
    assert imported["imported"] == 2
    assert [item["source"] for item in load_terms(restored)["terms"]] == ["Alpha"]


def test_import_merge_follows_case_insensitive_setting(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "case_insensitive = true",
            "case_insensitive = false",
        ),
        encoding="utf-8",
    )
    source = tmp_path / "terms.json"
    write_exchange(source, [term("Alice"), term("alice")])
    import_terms(project, source, dry_run=False)
    assert [item["source"] for item in load_terms(project)["terms"]] == [
        "Alice",
        "alice",
    ]


def test_import_merge_follows_unicode_normalization_setting(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'unicode_normalization = "NFKC"',
            'unicode_normalization = ""',
        ),
        encoding="utf-8",
    )
    source = tmp_path / "terms.json"
    write_exchange(source, [term("\uff21\uff22\uff23"), term("ABC")])
    import_terms(project, source, dry_run=False)
    assert [item["source"] for item in load_terms(project)["terms"]] == [
        "ABC",
        "\uff21\uff22\uff23",
    ]


def test_terms_import_is_atomic_and_noop_does_not_increment_revision(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    source = tmp_path / "terms.json"
    write_exchange(source, [term("Alpha")])
    first = import_terms(project, source, dry_run=False)
    assert first["terms_revision"] == 1
    before = read_json(project, project / "terminology" / "terms.json")

    second = import_terms(project, source, dry_run=False)
    assert second["changed"] is False
    assert second["terms_revision"] == 1

    invalid = tmp_path / "invalid.json"
    write_exchange(invalid, [{"source": "Broken", "aliases": "wrong"}])
    with pytest.raises(UsageError, match="aliases"):
        import_terms(project, invalid, dry_run=False)
    assert read_json(project, project / "terminology" / "terms.json") == before


def test_terms_cli_import_dry_run_and_export(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = make_project(tmp_path)
    source = tmp_path / "terms.json"
    write_exchange(source, [term("Alpha")])

    assert run(["terms-import", str(project), str(source), "--dry-run"]) == 0
    assert load_terms(project) is None
    assert json.loads(capsys.readouterr().out)["dry_run"] is True

    assert run(["terms-import", str(project), str(source)]) == 0
    capsys.readouterr()
    output = tmp_path / "terms.csv"
    assert run(["terms-export", str(project), str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["exported"] == 1
    assert "Alpha" in output.read_text(encoding="utf-8-sig")


def test_alias_primary_conflict_is_reported_and_not_matched_as_alias(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'alias_primary_collision = "merge"',
            'alias_primary_collision = "conflict"',
        ),
        encoding="utf-8",
    )
    source = tmp_path / "terms.json"
    write_exchange(
        source,
        [term("Alpha", aliases=["Beta", "UnmatchedAlias"]), term("Beta")],
    )
    import_terms(project, source, dry_run=False)
    library = load_terms(project)
    assert library is not None
    alpha = next(item for item in library["terms"] if item["source"] == "Alpha")
    assert alpha["conflicts"]["alias_primaries"] == [
        {"alias": "Beta", "primary_source": "Beta", "reason": "policy"}
    ]
    matched = match_terms("Beta", library, 10, TermNormalization("NFKC", True))
    assert [item["source"] for item in matched] == ["Alpha", "Beta"]
    assert matched[0]["aliases"] == ["Beta"]
    assert matched[1]["aliases"] == []
    assert all(item["preferred_translation"] is None for item in matched)
    assert all(item["group_claims"] for item in matched)


@pytest.mark.parametrize(
    ("terms", "expected_aliases"),
    [
        ([term("Alpha", aliases=["Beta"]), term("Beta")], {"Beta"}),
        (
            [
                term("Alpha", aliases=["Beta"]),
                term("Beta", aliases=["Gamma"]),
                term("Gamma"),
            ],
            {"Beta", "Gamma"},
        ),
    ],
)
def test_alias_primary_merge_preserves_group_members(
    tmp_path: Path, terms: list[dict], expected_aliases: set[str]
) -> None:
    project = make_project(tmp_path)
    source = tmp_path / "terms.json"
    write_exchange(source, terms)
    import_terms(project, source, dry_run=False)
    library = load_terms(project)
    assert library is not None
    assert [item["source"] for item in library["terms"]] == [
        item["source"] for item in terms
    ]
    rows = {item["source"]: item for item in library["terms"]}
    assert rows["Alpha"]["group_primary"] is None
    for source in expected_aliases:
        assert rows[source]["group_primary"] == "alpha"
    assert set(rows["Alpha"]["aliases"]) == {"Beta"}


@pytest.mark.parametrize(
    ("terms", "reason"),
    [
        (
            [
                term("Alpha", aliases=["Beta"]),
                term("Beta", aliases=["Alpha"]),
            ],
            "cycle",
        ),
        (
            [
                term("Alpha", aliases=["Gamma"]),
                term("Beta", aliases=["Gamma"]),
                term("Gamma"),
            ],
            "multiple_owners",
        ),
    ],
)
def test_alias_primary_ambiguous_graph_requires_manual_conflict(
    tmp_path: Path,
    terms: list[dict],
    reason: str,
) -> None:
    project = make_project(tmp_path)
    source = tmp_path / "terms.json"
    write_exchange(source, terms)
    import_terms(project, source, dry_run=False)
    library = load_terms(project)
    assert library is not None
    collisions = [
        collision
        for item in library["terms"]
        for collision in item["conflicts"]["alias_primaries"]
    ]
    assert collisions
    assert {collision["reason"] for collision in collisions} == {reason}


@pytest.mark.asyncio
async def test_forced_rescan_merges_with_published_library(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    os.environ["LLM_API_KEY"] = "test"
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        source = "Alpha" if calls == 1 else "Beta"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": llm_jsonl(
                                [
                                    {
                                        "type": "term",
                                        **term(source),
                                    }
                                ]
                            )
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        await run_terminology(project, Scope(), http_client=client)
        await run_terminology(
            project,
            Scope(force=True),
            http_client=client,
        )
    library = read_json(project, project / "terminology" / "terms.json")
    assert library["terms_revision"] == 2
    assert [item["source"] for item in library["terms"]] == ["Alpha", "Beta"]
