from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from app.execution import Scope
from app.project import init_project
from app.stages import (
    export_terms,
    import_terms,
    load_terms,
    match_terms,
    run_terminology,
)
from app.storage import read_json
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


def test_terms_import_is_atomic_and_noop_does_not_increment_revision(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    source = tmp_path / "terms.json"
    write_exchange(source, [term("Alpha")])
    first = import_terms(project, source, dry_run=False)
    assert first["terms_revision"] == 1
    before = (project / "terminology" / "terms.json").read_bytes()

    second = import_terms(project, source, dry_run=False)
    assert second["changed"] is False
    assert second["terms_revision"] == 1

    invalid = tmp_path / "invalid.json"
    write_exchange(invalid, [{"source": "Broken", "aliases": "wrong"}])
    with pytest.raises(Exception, match="aliases"):
        import_terms(project, invalid, dry_run=False)
    assert (project / "terminology" / "terms.json").read_bytes() == before


def test_alias_primary_conflict_is_reported_and_not_matched_as_alias(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    source = tmp_path / "terms.json"
    write_exchange(source, [term("Alpha", aliases=["Beta"]), term("Beta")])
    import_terms(project, source, dry_run=False)
    library = load_terms(project)
    assert library is not None
    alpha = next(item for item in library["terms"] if item["source"] == "Alpha")
    assert alpha["conflicts"]["alias_primaries"] == [
        {"alias": "Beta", "primary_source": "Beta", "reason": "policy"}
    ]
    assert [item["source"] for item in match_terms("Beta", library, 10)] == ["Beta"]


def test_alias_primary_merge_absorbs_the_alias_entry(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    config_path = project / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'alias_primary_collision = "conflict"',
            'alias_primary_collision = "merge"',
        ),
        encoding="utf-8",
    )
    source = tmp_path / "terms.json"
    write_exchange(source, [term("Alpha", aliases=["Beta"]), term("Beta")])
    import_terms(project, source, dry_run=False)
    library = load_terms(project)
    assert library is not None
    assert [item["source"] for item in library["terms"]] == ["Alpha"]
    assert "Beta" in library["terms"][0]["aliases"]


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
    library = read_json(project / "terminology" / "terms.json")
    assert library["terms_revision"] == 2
    assert [item["source"] for item in library["terms"]] == ["Alpha", "Beta"]
