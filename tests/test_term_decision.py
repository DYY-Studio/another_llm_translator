from __future__ import annotations

import asyncio
import json
import os
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import load_config, load_project_config, validate_config
from app.errors import ConfigError, RequestSizeError, StorageError, UsageError
from app.execution import create_run
from app.main import build_parser
from app.project import init_project
from app.sqlite_storage import atomic_write_json, read_json, record_header, write_json
from app.stages import term_normalization
from app.term_decision import (
    CHECKPOINT_FILE,
    _alias_violations,
    _consistency_states,
    _group_violations,
    _make_payload,
    _pack_batches,
    _parse_decisions,
    _recover_invalid_relationship_components,
    _related_anchors,
    apply_decision_draft,
    collect_term_evidence,
    current_decision_draft,
    rollback_decision,
    run_terminology_decision,
    save_decision_rejections,
)
from app.web import create_app
from app.web_store import WebStore
from tests.helpers import llm_jsonl
from tests.test_foundation import make_app_root


def create_decision_project(tmp_path: Path, text: str = "Alice Ally\nBob") -> Path:
    app_root = make_app_root(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text(text, encoding="utf-8-sig")
    project, _ = init_project(
        [str(source)],
        name="decision-demo",
        app_root=app_root,
        projects_root=tmp_path / "projects",
    )
    assert project is not None
    metadata = read_json(project, project / "project.json")
    write_json(
        project,
        project / "terminology" / "terms.json",
        record_header(
            "terminology_library",
            str(metadata["project_id"]),
            record_id="TERMS-1",
            terms_revision=1,
            published_run_id="RUN-TERM",
            active_task_id="TASK-TERM",
            terms=[
                {
                    "record_id": "TERM-000001",
                    "source": "Alice",
                    "normalized": "alice",
                    "category": "人物",
                    "description": "unhelpful",
                    "preferred_translation": None,
                    "aliases": ["Ally"],
                    "group_primary": None,
                    "conflicts": {
                        "categories": [],
                        "preferred_translations": [],
                        "alias_primaries": [],
                        "group_claims": [],
                    },
                },
                {
                    "record_id": "TERM-000002",
                    "source": "Bob",
                    "normalized": "bob",
                    "category": "人物",
                    "description": "",
                    "preferred_translation": "鲍勃",
                    "aliases": [],
                    "group_primary": None,
                    "conflicts": {
                        "categories": [],
                        "preferred_translations": [],
                        "alias_primaries": [],
                        "group_claims": [],
                    },
                },
            ],
        ),
    )
    return project


def decision_response(payload: dict) -> list[dict]:
    values = []
    for term in payload["terms"]:
        if term["normalized"] == "alice" and payload["phase"] == "adjudication":
            values.append(
                {
                    "type": "decision",
                    "normalized": "alice",
                    "action": "update",
                    "reason": "补全译名并清理说明",
                    "category": "女性人名",
                    "description": None,
                    "preferred_translation": "爱丽丝",
                    "aliases": ["Ally"],
                    "group_primary": None,
                }
            )
        else:
            values.append(
                {
                    "type": "decision",
                    "normalized": term["normalized"],
                    "action": "keep",
                    "reason": "保持当前决定",
                }
            )
    return values


def single_term_batches(states: list[dict], **_: object) -> tuple[list, int]:
    return [([state], []) for state in states], len(states)


def create_complete_legacy_group_run(
    project: Path,
) -> tuple[str, Path, bytes, dict[str, object]]:
    config = load_project_config(project, stage="terminology_decision")
    metadata = read_json(project, project / "project.json")
    library = read_json(project, project / "terminology" / "terms.json")
    terms = {item["normalized"]: item for item in library["terms"]}
    run_id, run_dir = create_run(
        project,
        config=config,
        stage="terminology_decision",
        fingerprint="sha256:legacy",
        prompt="legacy prompt",
        selected_count=2,
        requested_count=2,
        reused_count=0,
        details={
            "source_terms_revision": 1,
            "decision_status": "generating",
            "rejected_proposal_ids": [],
            "prompt_language": "zh-CN",
        },
    )

    def state(normalized: str) -> dict[str, object]:
        term = terms[normalized]
        return {
            "normalized": normalized,
            "source": term["source"],
            "category": term["category"],
            "description": term["description"] or None,
            "preferred_translation": term["preferred_translation"],
            "aliases": term["aliases"],
            "group_primary": term["group_primary"],
            "disabled": False,
        }

    def checkpoint_record(decision: dict[str, object]) -> dict[str, object]:
        return {
            "decision": decision,
            "decision_fingerprint": "sha256:legacy-decision",
            "model_fingerprint": "sha256:legacy-model",
            "prompt_fingerprint": "sha256:legacy-prompt",
        }

    checkpoint_path = run_dir / CHECKPOINT_FILE
    atomic_write_json(
        checkpoint_path,
        record_header(
            "terminology_decision_checkpoint",
            str(metadata["project_id"]),
            run_id=run_id,
            source_terms_revision=1,
            phases={
                "adjudication": {
                    key: checkpoint_record({"action": "keep", "reason": "保持"})
                    for key in ("alice", "bob")
                },
                "consistency": {
                    "alice": checkpoint_record(
                        {
                            "action": "update",
                            "reason": "旧模型错误地设为自身组主",
                            "after": {**state("alice"), "group_primary": "alice"},
                        }
                    ),
                    "bob": checkpoint_record(
                        {
                            "action": "update",
                            "reason": "补全译名",
                            "after": {
                                **state("bob"),
                                "preferred_translation": "罗伯特",
                            },
                        }
                    ),
                },
            },
        ),
    )
    observed_usage: dict[str, object] = {
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
        "available": True,
        "partial": False,
    }
    manifest = read_json(project, run_dir / "manifest.json")
    manifest.update(
        completed_segment_count=4,
        failed_segment_count=0,
        usage=observed_usage,
        usage_invocation_count=1,
    )
    write_json(project, run_dir / "manifest.json", manifest)
    return run_id, run_dir, checkpoint_path.read_bytes(), observed_usage


def test_decision_config_migrates_defaults_and_cli_contract(tmp_path: Path) -> None:
    config_path = make_app_root(tmp_path) / "config" / "config.toml"
    source = config_path.read_text(encoding="utf-8")
    source = "\n".join(
        line
        for line in source.splitlines()
        if not line.startswith("preset_terminology_decision")
        and not line.startswith("temperature_terminology_decision")
    )
    config_path.write_text(source + "\n", encoding="utf-8")

    config = load_config(config_path)
    assert config["llm"]["preset_terminology_decision"] == ""
    assert config["llm"]["temperature_terminology_decision"] == 0.1
    assert config["terminology_decision"] == {
        "allow_soft_target_overflow": True,
        "anchor_overflow_mode": "error",
    }
    config["terminology_decision"] = "invalid"
    with pytest.raises(ConfigError, match="配置节必须是表"):
        validate_config(config)

    parser = build_parser()
    generated = parser.parse_args(["terms-decide", "demo", "--replace-draft"])
    assert generated.command == "terms-decide"
    assert generated.replace_draft is True
    resumed = parser.parse_args(["terms-decide", "demo", "--resume-run"])
    assert resumed.resume_run is True
    forced = parser.parse_args(["terms-decide", "demo", "--force"])
    assert forced.force is True
    applied = parser.parse_args(
        ["terms-decide-apply", "demo", "--all", "--reject", "TDP-1"]
    )
    assert applied.rejected_proposal_ids == ["TDP-1"]


def _batch_state(normalized: str, source: str) -> dict[str, object]:
    return {
        "normalized": normalized,
        "source": source,
        "category": "人物",
        "description": "",
        "preferred_translation": None,
        "aliases": [],
        "group_primary": None,
        "disabled": False,
    }


def _batch_evidence(*states: dict[str, object]) -> dict[str, dict[str, object]]:
    return {
        str(state["normalized"]): {
            "hit_count": 1,
            "source_hit_count": 1,
            "alias_hit_counts": {},
            "samples": [{"file_id": "F", "segment_id": "S", "source": "sample"}] * 5,
        }
        for state in states
    }


def test_consistency_states_overlay_completed_checkpoint_actions() -> None:
    original = {
        key: _batch_state(key, key.title())
        for key in ("keep", "update", "disable", "review")
    }
    tentative = {
        key: {**state, "preferred_translation": f"phase-one-{key}"}
        for key, state in original.items()
    }
    updated = {
        **tentative["update"],
        "preferred_translation": "phase-two-update",
    }
    result = _consistency_states(
        original,
        tentative,
        {
            "keep": {"action": "keep", "reason": "保持"},
            "update": {"action": "update", "reason": "更新", "after": updated},
            "disable": {"action": "disable", "reason": "禁用"},
            "review": {"action": "needs_review", "reason": "人工复核"},
        },
    )

    assert result["keep"]["preferred_translation"] == "phase-one-keep"
    assert result["update"]["preferred_translation"] == "phase-two-update"
    assert result["disable"]["disabled"] is True
    assert result["review"] == original["review"]
    assert tentative["disable"]["disabled"] is False


def test_decision_payload_exposes_read_only_disabled_state() -> None:
    focus = {**_batch_state("focus", "Focus"), "disabled": True}
    anchor = _batch_state("anchor", "Anchor")
    payload = _make_payload(
        phase="consistency",
        target_language="简体中文",
        focus=[focus],
        anchors=[anchor],
        evidence=_batch_evidence(focus, anchor),
    )

    assert payload["terms"][0]["disabled"] is True
    assert payload["anchors"][0]["disabled"] is False
    phase_one = _make_payload(
        phase="adjudication",
        target_language="简体中文",
        focus=[focus],
        anchors=[anchor],
        evidence=_batch_evidence(focus, anchor),
    )
    assert "disabled" not in phase_one["terms"][0]
    assert "disabled" not in phase_one["anchors"][0]


def test_related_anchors_prioritize_effective_group_dependencies(
    tmp_path: Path,
) -> None:
    project = create_decision_project(tmp_path)
    spec = term_normalization(load_project_config(project))
    focus = {
        **_batch_state("thunder", "轟雷"),
        "aliases": ["モナークスプライト"],
    }
    dependency = {
        **_batch_state("princess", "雷鳴公主"),
        "group_primary": "モナークスプライト",
    }
    lexical = _batch_state("thunder-name", "轟雷ちゃん")

    related = _related_anchors([focus], [lexical, dependency], spec)

    assert [item["normalized"] for item in related] == [
        "princess",
        "thunder-name",
    ]


def test_decision_batch_overflow_policy_controls_local_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_decision_project(tmp_path)
    config = load_project_config(project, stage="terminology_decision")
    config["chunking"]["target_chunk_input_tokens"] = 50
    config["llm"]["context_window_tokens"] = 120
    config["llm"]["context_safety_margin_tokens"] = 0
    config["execution"]["input_tokens_per_minute"] = 0
    config["execution"]["token_safety_factor"] = 1
    focus = _batch_state("alice", "Alice")
    first_anchor = _batch_state("alice smith", "Alice Smith")
    second_anchor = _batch_state("alice johnson", "Alice Johnson")
    evidence = _batch_evidence(focus, first_anchor, second_anchor)

    def estimate(messages: list[dict[str, str]], _: float) -> int:
        payload = json.loads(messages[1]["content"])
        anchors = payload["anchors"]
        samples = sum(len(item["evidence"]["samples"]) for item in anchors)
        return 31 + 20 * len(payload["terms"]) + 15 * len(anchors) + 4 * samples

    monkeypatch.setattr("app.term_decision.estimate_messages", estimate)
    spec = term_normalization(config)

    config["terminology_decision"]["anchor_overflow_mode"] = "error"
    with pytest.raises(RequestSizeError, match="Anchor 超限策略为 error") as error:
        _pack_batches(
            [focus],
            phase="consistency",
            target_language="简体中文",
            anchors=[first_anchor, second_anchor],
            evidence=evidence,
            prompt="prompt",
            config=config,
            spec=spec,
        )
    assert error.value.reason == "context"

    config["terminology_decision"]["anchor_overflow_mode"] = "trim"
    batches, _ = _pack_batches(
        [focus],
        phase="consistency",
        target_language="简体中文",
        anchors=[first_anchor, second_anchor],
        evidence=evidence,
        prompt="prompt",
        config=config,
        spec=spec,
    )
    assert [item["source"] for item in batches[0][1]] == ["Alice Smith"]

    config["terminology_decision"]["anchor_overflow_mode"] = "compact"
    batches, _ = _pack_batches(
        [focus],
        phase="consistency",
        target_language="简体中文",
        anchors=[first_anchor, second_anchor],
        evidence=evidence,
        prompt="prompt",
        config=config,
        spec=spec,
    )
    assert len(batches[0][1]) == 2
    assert all(item.get("_compact_evidence") for item in batches[0][1])

    config["terminology_decision"]["anchor_overflow_mode"] = "error"
    config["terminology_decision"]["allow_soft_target_overflow"] = False
    with pytest.raises(RequestSizeError, match="超过软目标") as error:
        _pack_batches(
            [focus],
            phase="consistency",
            target_language="简体中文",
            anchors=[],
            evidence=evidence,
            prompt="prompt",
            config=config,
            spec=spec,
        )
    assert error.value.reason == "context"

    config["terminology_decision"]["allow_soft_target_overflow"] = True
    config["execution"]["input_tokens_per_minute"] = 40
    with pytest.raises(RequestSizeError, match="限制 40 tokens") as error:
        _pack_batches(
            [focus],
            phase="consistency",
            target_language="简体中文",
            anchors=[],
            evidence=evidence,
            prompt="prompt",
            config=config,
            spec=spec,
        )
    assert error.value.reason == "itpm"


def test_decision_parser_keeps_unknown_terms_strict() -> None:
    focus = [_batch_state("target", "Target")]
    content = llm_jsonl(
        [
            {
                "type": "decision",
                "normalized": "target",
                "action": "keep",
                "reason": "保持",
            },
            {
                "type": "decision",
                "normalized": "not-an-anchor",
                "action": "keep",
                "reason": "越界",
            },
        ]
    )
    with pytest.raises(UsageError, match="未知术语决策 normalized：not-an-anchor"):
        _parse_decisions(
            content,
            focus,
            all_forms={"Target"},
            known_states={"target": focus[0]},
            read_only_terms={"known-anchor"},
        )


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (
            {
                "type": "decision",
                "normalized": "target",
                "action": "update",
                "reason": "补全译名",
                "category": "人物",
                "description": None,
                "preferred_translation": "目标",
                "aliases": [],
            },
            "update 决策字段无效：target（缺少字段 group_primary）",
        ),
        (
            {
                "type": "decision",
                "normalized": "target",
                "action": "keep",
                "reason": "保持",
                "category": "人物",
            },
            "keep 决策字段无效：target（禁止字段 category）",
        ),
    ],
)
def test_decision_parser_reports_exact_field_mismatch(
    record: dict[str, object], message: str
) -> None:
    focus = [_batch_state("target", "Target")]
    with pytest.raises(UsageError) as error:
        _parse_decisions(
            llm_jsonl([record]),
            focus,
            all_forms={"Target"},
            known_states={"target": focus[0]},
            read_only_terms=set(),
        )
    assert message in str(error.value)


def _update_decision(normalized: str, primary: str | None) -> dict[str, object]:
    return {
        "type": "decision",
        "normalized": normalized,
        "action": "update",
        "reason": "调整组关系",
        "category": "人物",
        "description": None,
        "preferred_translation": None,
        "aliases": [],
        "group_primary": primary,
    }


def test_completed_consistency_state_prevents_stale_member_chain_repair() -> None:
    original = {
        "princess": _batch_state("princess", "雷鳴公主"),
        "thunder": _batch_state("thunder", "轟雷"),
        "monarch": _batch_state("monarch", "モナークスプライト"),
    }
    tentative = {
        **original,
        "princess": {**original["princess"], "group_primary": "thunder"},
    }
    effective = _consistency_states(
        original,
        tentative,
        {
            "princess": {
                "action": "update",
                "reason": "改为直接指向根术语",
                "after": {
                    **original["princess"],
                    "group_primary": "monarch",
                },
            }
        },
    )

    decisions, _ = _parse_decisions(
        llm_jsonl([_update_decision("thunder", "monarch")]),
        [tentative["thunder"]],
        all_forms={"雷鳴公主", "轟雷", "モナークスプライト"},
        known_states=effective,
        read_only_terms={"princess", "monarch"},
        review_states=original,
    )

    assert decisions["thunder"]["after"]["group_primary"] == "monarch"


@pytest.mark.parametrize(
    ("focus", "known_states", "records", "message"),
    [
        (
            [_batch_state("alice", "Alice")],
            {"alice": _batch_state("alice", "Alice")},
            [_update_decision("alice", "alice")],
            "术语组主自指：alice -> alice",
        ),
        (
            [_batch_state("alice", "Alice")],
            {
                "alice": _batch_state("alice", "Alice"),
                "bob": {**_batch_state("bob", "Bob"), "disabled": True},
            },
            [_update_decision("alice", "bob")],
            "术语组主已禁用：alice -> bob",
        ),
        (
            [_batch_state("alice", "Alice")],
            {
                "alice": _batch_state("alice", "Alice"),
                "bob": {**_batch_state("bob", "Bob"), "group_primary": "carol"},
                "carol": _batch_state("carol", "Carol"),
            },
            [_update_decision("alice", "bob")],
            "术语组成员指向另一成员：alice -> bob",
        ),
        (
            [_batch_state("alice", "Alice"), _batch_state("bob", "Bob")],
            {
                "alice": _batch_state("alice", "Alice"),
                "bob": _batch_state("bob", "Bob"),
            },
            [
                _update_decision("alice", "bob"),
                _update_decision("bob", "alice"),
            ],
            "术语组关系循环：alice -> bob -> alice",
        ),
    ],
)
def test_decision_parser_rejects_provable_group_violations(
    focus: list[dict[str, object]],
    known_states: dict[str, dict[str, object]],
    records: list[dict[str, object]],
    message: str,
) -> None:
    with pytest.raises(UsageError, match=message):
        _parse_decisions(
            llm_jsonl(records),
            focus,
            all_forms={str(state["source"]) for state in known_states.values()},
            known_states=known_states,
            read_only_terms=set(),
        )


def test_decision_parser_accepts_direct_member_to_enabled_root() -> None:
    focus = [_batch_state("alice", "Alice")]
    states = {
        "alice": focus[0],
        "bob": _batch_state("bob", "Bob"),
    }
    decisions, _ = _parse_decisions(
        llm_jsonl([_update_decision("alice", "bob")]),
        focus,
        all_forms={"Alice", "Bob"},
        known_states=states,
        read_only_terms=set(),
    )
    assert decisions["alice"]["after"]["group_primary"] == "bob"


def test_decision_parser_allows_review_to_restore_tentative_group_state() -> None:
    original = _batch_state("alice", "Alice")
    tentative = {**original, "group_primary": "alice"}
    decisions, _ = _parse_decisions(
        llm_jsonl(
            [
                {
                    "type": "decision",
                    "normalized": "alice",
                    "action": "needs_review",
                    "reason": "无法确定合法组主",
                }
            ]
        ),
        [tentative],
        all_forms={"Alice"},
        known_states={"alice": tentative},
        read_only_terms=set(),
        review_states={"alice": original},
    )
    assert decisions["alice"]["action"] == "needs_review"


def test_group_validation_collects_every_illegal_relationship_shape() -> None:
    states = {
        key: _batch_state(key, key.title())
        for key in (
            "self",
            "missing-source",
            "disabled-member",
            "disabled-root",
            "chain-member",
            "chain-parent",
            "root",
            "cycle-a",
            "cycle-b",
            "cycle-c",
        )
    }
    states["self"]["group_primary"] = "self"
    states["missing-source"]["group_primary"] = "absent"
    states["disabled-member"]["group_primary"] = "disabled-root"
    states["disabled-root"]["disabled"] = True
    states["chain-member"]["group_primary"] = "chain-parent"
    states["chain-parent"]["group_primary"] = "root"
    states["cycle-a"]["group_primary"] = "cycle-b"
    states["cycle-b"]["group_primary"] = "cycle-c"
    states["cycle-c"]["group_primary"] = "cycle-a"

    violations = set(_group_violations(states))
    assert ("self", ("self", "self")) in violations
    assert ("missing", ("missing-source", "absent")) in violations
    assert ("disabled", ("disabled-member", "disabled-root")) in violations
    assert ("member", ("chain-member", "chain-parent")) in violations
    assert ("cycle", ("cycle-a", "cycle-b", "cycle-c")) in violations


def test_invalid_relationship_recovery_restores_alias_dependency_component() -> None:
    original = {
        "alice": {
            **_batch_state("alice", "Alice"),
            "aliases": ["Ally"],
        },
        "bob": _batch_state("bob", "Bob"),
        "carol": _batch_state("carol", "Carol"),
    }
    final = {
        "alice": {
            **original["alice"],
            "aliases": [],
            "group_primary": "alice",
        },
        "bob": {**original["bob"], "aliases": ["Ally"]},
        "carol": {**original["carol"], "preferred_translation": "卡萝尔"},
    }
    decisions = {
        key: {"action": "update", "reason": "模型修改", "after": state}
        for key, state in final.items()
    }

    _recover_invalid_relationship_components(
        original=original,
        final=final,
        decisions=decisions,
        language="zh-CN",
        spec=term_normalization(
            {
                "terminology": {
                    "unicode_normalization": "NFKC",
                    "case_insensitive": False,
                }
            }
        ),
    )

    assert final["alice"] == original["alice"]
    assert final["bob"] == original["bob"]
    assert decisions["alice"]["action"] == "needs_review"
    assert decisions["bob"]["action"] == "needs_review"
    assert "alice -> alice" in decisions["alice"]["reason"]
    assert final["carol"]["preferred_translation"] == "卡萝尔"
    assert decisions["carol"]["action"] == "update"


@pytest.mark.asyncio
async def test_cross_batch_group_cycle_becomes_manual_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_decision_project(tmp_path)
    monkeypatch.setattr("app.term_decision._pack_batches", single_term_batches)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        term = payload["terms"][0]
        if payload["phase"] == "adjudication":
            records = [
                {
                    "type": "decision",
                    "normalized": term["normalized"],
                    "action": "keep",
                    "reason": "保持",
                }
            ]
        else:
            primary = "bob" if term["normalized"] == "alice" else "alice"
            records = [_update_decision(term["normalized"], primary)]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    os.environ["LLM_API_KEY"] = "test"
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            summary = await run_terminology_decision(project, http_client=client)
    finally:
        del os.environ["LLM_API_KEY"]

    assert summary["proposals"] == 0
    assert summary["needs_review"] == 2
    draft = current_decision_draft(project)
    assert draft is not None
    assert {item["normalized"] for item in draft["needs_review"]} == {
        "alice",
        "bob",
    }
    assert all("alice -> bob -> alice" in item["reason"] for item in draft["needs_review"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "marker"),
    [
        ("zh-CN", "术语组主自指：alice -> alice"),
        ("en", "self-referencing group pointer: alice -> alice"),
    ],
)
async def test_decision_group_violation_enters_localized_format_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    marker: str,
) -> None:
    project = create_decision_project(tmp_path)
    monkeypatch.setattr("app.term_decision._pack_batches", single_term_batches)
    sent_invalid = False
    repairs = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent_invalid, repairs
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        if "format_correction" in payload:
            repairs += 1
            assert marker in payload["format_correction"]
            assert "group_primary" in payload["format_correction"]
            content = llm_jsonl(decision_response(payload))
        elif not sent_invalid and payload["terms"][0]["normalized"] == "alice":
            sent_invalid = True
            content = llm_jsonl([_update_decision("alice", "alice")])
        else:
            content = llm_jsonl(decision_response(payload))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )

    os.environ["LLM_API_KEY"] = "test"
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            summary = await run_terminology_decision(
                project, prompt_language=language, http_client=client
            )
    finally:
        del os.environ["LLM_API_KEY"]

    assert summary["proposals"] == 1
    assert repairs == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "marker"),
    [("zh-CN", "本批唯一允许输出"), ("en", "The only allowed decision normalized")],
)
async def test_decision_format_repair_lists_exact_target_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    marker: str,
) -> None:
    project = create_decision_project(tmp_path)
    monkeypatch.setattr("app.term_decision._pack_batches", single_term_batches)
    calls = 0
    repairs = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls, repairs
        calls += 1
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        if "format_correction" in payload:
            repairs += 1
            correction = str(payload["format_correction"])
            assert marker in correction
            assert json.dumps(
                [item["normalized"] for item in payload["terms"]],
                ensure_ascii=False,
            ) in correction
            assert "anchors" in correction
            if language == "en":
                assert "未知术语" not in correction
                assert "Every action requires a non-empty string reason" in correction
                assert "all nine keys are required" in correction
                assert "description may only copy" in correction
                assert "aliases may only use existing source/alias forms" in correction
            else:
                assert "每种 action 都必须有非空字符串 reason" in correction
                assert "九个键全部必填" in correction
                assert "description 只能逐字保持" in correction
                assert "aliases 只能使用" in correction
            content = llm_jsonl(decision_response(payload))
        else:
            content = llm_jsonl(
                [
                    *decision_response(payload),
                    {
                        "type": "decision",
                        "normalized": "not-an-anchor",
                        "action": "keep",
                        "reason": "越界",
                    },
                ]
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    os.environ["LLM_API_KEY"] = "test"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        summary = await run_terminology_decision(
            project, prompt_language=language, http_client=client
        )
    del os.environ["LLM_API_KEY"]

    assert summary["proposals"] == 1
    assert calls == 8
    assert repairs == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["zh-CN", "en"])
async def test_update_without_reason_is_repaired_with_complete_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    language: str,
) -> None:
    project = create_decision_project(tmp_path)
    monkeypatch.setattr("app.term_decision._pack_batches", single_term_batches)
    sent_invalid = False
    repairs = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent_invalid, repairs
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        if "format_correction" in payload:
            repairs += 1
            correction = str(payload["format_correction"])
            assert "reason" in correction
            assert "category" in correction
            assert "description" in correction
            assert "preferred_translation" in correction
            assert "aliases" in correction
            assert "group_primary" in correction
            content = llm_jsonl(decision_response(payload))
        elif (
            not sent_invalid
            and payload["phase"] == "adjudication"
            and payload["terms"][0]["normalized"] == "alice"
        ):
            sent_invalid = True
            invalid = decision_response(payload)[0]
            invalid.pop("reason")
            content = llm_jsonl([invalid])
        else:
            content = llm_jsonl(decision_response(payload))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )

    os.environ["LLM_API_KEY"] = "test"
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            summary = await run_terminology_decision(
                project, prompt_language=language, http_client=client
            )
    finally:
        del os.environ["LLM_API_KEY"]

    assert summary["proposals"] == 1
    assert repairs == 1


@pytest.mark.asyncio
async def test_decision_generates_persistent_two_pass_draft_and_applies(
    tmp_path: Path,
) -> None:
    project = create_decision_project(tmp_path)
    phases: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        payload = json.loads(body["messages"][1]["content"])
        phases.append(payload["phase"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": llm_jsonl(decision_response(payload))}}
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
        )

    os.environ["LLM_API_KEY"] = "test"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        summary = await run_terminology_decision(project, http_client=client)
    del os.environ["LLM_API_KEY"]

    assert phases == ["adjudication", "consistency"]
    assert summary["proposals"] == 1
    run_dir = project / "runs" / summary["run_id"]
    checkpoint = json.loads((run_dir / CHECKPOINT_FILE).read_text(encoding="utf-8"))
    assert (
        checkpoint["phases"]["adjudication"]["alice"]["prompt_fingerprint"]
        != checkpoint["phases"]["consistency"]["alice"]["prompt_fingerprint"]
    )
    prompt_snapshot = (run_dir / "prompt.txt").read_text(encoding="utf-8")
    assert "===== terminology_decision/adjudication =====" in prompt_snapshot
    assert "===== terminology_decision/consistency =====" in prompt_snapshot
    draft = current_decision_draft(project)
    assert draft is not None
    assert draft["source_terms_revision"] == 1
    assert draft["proposals"][0]["after"][0]["preferred_translation"] == "爱丽丝"

    applied = apply_decision_draft(project, confirm_all=True)
    assert applied["terms_revision"] == 2
    alice = next(
        item
        for item in read_json(project, project / "terminology" / "terms.json")[
            "terms"
        ]
        if item["normalized"] == "alice"
    )
    assert alice["preferred_translation"] == "爱丽丝"
    assert alice["description"] == ""
    assert current_decision_draft(project) is None

    rolled_back = rollback_decision(project, confirm=True)
    assert rolled_back["terms_revision"] == 3
    restored = read_json(project, project / "terminology" / "terms.json")
    alice = next(item for item in restored["terms"] if item["normalized"] == "alice")
    assert alice["preferred_translation"] is None
    assert alice["description"] == "unhelpful"


@pytest.mark.asyncio
async def test_decision_ignores_extra_read_only_anchor_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_decision_project(tmp_path)

    def scoped_batches(
        states: list[dict[str, object]], *, phase: str, **_: object
    ) -> tuple[list, int]:
        batches = []
        for index, state in enumerate(states):
            references = (
                [states[1 - index]] if phase == "consistency" else []
            )
            batches.append(([state], references))
        return batches, len(batches)

    monkeypatch.setattr("app.term_decision._pack_batches", scoped_batches)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        records = decision_response(payload)
        if payload["phase"] == "consistency":
            anchor = payload["anchors"][0]
            records.append(
                {
                    "type": "decision",
                    "normalized": anchor["normalized"],
                    "action": "update" if calls % 2 else "disable",
                    "reason": "越界参照，不应应用",
                    **(
                        {
                            "category": "错误类别",
                            "description": "不应写入",
                            "preferred_translation": "错误译名",
                            "aliases": ["不存在的 alias"],
                            "group_primary": None,
                        }
                        if calls % 2
                        else {}
                    ),
                }
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": llm_jsonl(records)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    os.environ["LLM_API_KEY"] = "test"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        summary = await run_terminology_decision(project, http_client=client)
    del os.environ["LLM_API_KEY"]

    assert calls == 4
    assert summary["proposals"] == 1
    draft = current_decision_draft(project)
    assert draft is not None
    assert draft["proposals"][0]["after"][0]["preferred_translation"] == "爱丽丝"


@pytest.mark.asyncio
async def test_decision_runs_batches_concurrently_with_phase_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_decision_project(tmp_path)
    preset_path = tmp_path / "app-root" / "llm_presets" / "default.json"
    preset = json.loads(preset_path.read_text(encoding="utf-8"))
    preset["max_parallel"] = 2
    preset_path.write_text(json.dumps(preset), encoding="utf-8")
    monkeypatch.setattr("app.term_decision._pack_batches", single_term_batches)
    phase_one_started = asyncio.Event()
    release_phase_one = asyncio.Event()
    active = 0
    maximum = 0
    phase_one_finished = 0

    async def request_batch(*_: object, **kwargs: object) -> dict[str, dict]:
        nonlocal active, maximum, phase_one_finished
        phase = str(kwargs["phase"])
        focus = kwargs["focus"]
        normalized = str(focus[0]["normalized"])
        if phase == "consistency":
            assert phase_one_finished == 2
        active += 1
        maximum = max(maximum, active)
        if phase == "adjudication":
            if active == 2:
                phase_one_started.set()
            await release_phase_one.wait()
        await asyncio.sleep(0)
        active -= 1
        if phase == "adjudication":
            phase_one_finished += 1
        return {normalized: {"action": "keep", "reason": "保持"}}

    monkeypatch.setattr("app.term_decision._request_batch", request_batch)
    async with httpx.AsyncClient() as client:
        task = asyncio.create_task(
            run_terminology_decision(project, http_client=client)
        )
        await asyncio.wait_for(phase_one_started.wait(), timeout=1)
        release_phase_one.set()
        await task

    assert maximum == 2
    assert current_decision_draft(project) is not None


@pytest.mark.asyncio
async def test_decision_cancel_checkpoints_completed_batches_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_decision_project(tmp_path)
    monkeypatch.setattr("app.term_decision._pack_batches", single_term_batches)
    alice_done = asyncio.Event()
    hold_bob = asyncio.Event()
    calls: list[tuple[str, str]] = []

    async def interrupted_batch(*_: object, **kwargs: object) -> dict[str, dict]:
        phase = str(kwargs["phase"])
        normalized = str(kwargs["focus"][0]["normalized"])
        calls.append((phase, normalized))
        if phase == "adjudication" and normalized == "bob":
            await hold_bob.wait()
        if phase == "adjudication" and normalized == "alice":
            alice_done.set()
        return {normalized: {"action": "keep", "reason": "保持"}}

    monkeypatch.setattr("app.term_decision._request_batch", interrupted_batch)
    async with httpx.AsyncClient() as client:
        task = asyncio.create_task(
            run_terminology_decision(project, http_client=client)
        )
        await asyncio.wait_for(alice_done.wait(), timeout=1)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        runs = [
            item
            for item in (project / "runs").iterdir()
            if (item / CHECKPOINT_FILE).is_file()
        ]
        assert len(runs) == 1
        run_id = runs[0].name
        manifest = read_json(project, runs[0] / "manifest.json")
        assert manifest["status"] == "running"
        checkpoint = json.loads(
            (runs[0] / CHECKPOINT_FILE).read_text(encoding="utf-8")
        )
        assert set(checkpoint["phases"]["adjudication"]) == {"alice"}

        library_path = project / "terminology" / "terms.json"
        library = read_json(project, library_path)
        library["terms_revision"] = 2
        write_json(project, library_path, library)
        with pytest.raises(UsageError, match="revision 已变化"):
            await run_terminology_decision(
                project, resume_run_id=run_id, http_client=client
            )
        library["terms_revision"] = 1
        write_json(project, library_path, library)

        prompt_path = project / "prompts" / "terminology_decision.zh-CN.middle.txt"
        prompt_path.write_text(
            prompt_path.read_text(encoding="utf-8") + "\n继续时使用当前 Prompt。\n",
            encoding="utf-8",
        )

        resumed_calls: list[tuple[str, str]] = []

        async def resumed_batch(*_: object, **kwargs: object) -> dict[str, dict]:
            phase = str(kwargs["phase"])
            normalized = str(kwargs["focus"][0]["normalized"])
            resumed_calls.append((phase, normalized))
            return {normalized: {"action": "keep", "reason": "保持"}}

        monkeypatch.setattr("app.term_decision._request_batch", resumed_batch)
        await run_terminology_decision(
            project, resume_run_id=run_id, http_client=client
        )

    assert ("adjudication", "alice") not in resumed_calls
    assert ("adjudication", "bob") in resumed_calls
    assert {value for value in resumed_calls if value[0] == "consistency"} == {
        ("consistency", "alice"),
        ("consistency", "bob"),
    }
    manifest = read_json(project, project / "runs" / run_id / "manifest.json")
    assert len(manifest["continuations"]) == 1
    assert manifest["usage_invocation_count"] == 2
    assert current_decision_draft(project)["prompt_fingerprint"].startswith(
        "sha256:"
    )


@pytest.mark.asyncio
async def test_decision_error_keeps_completed_batches_resumable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_decision_project(tmp_path)
    monkeypatch.setattr("app.term_decision._pack_batches", single_term_batches)
    alice_done = asyncio.Event()

    async def failing_batch(*_: object, **kwargs: object) -> dict[str, dict]:
        phase = str(kwargs["phase"])
        normalized = str(kwargs["focus"][0]["normalized"])
        if phase == "adjudication" and normalized == "alice":
            alice_done.set()
            return {normalized: {"action": "keep", "reason": "保持"}}
        if phase == "adjudication" and normalized == "bob":
            await alice_done.wait()
            raise UsageError("模型协议错误")
        return {normalized: {"action": "keep", "reason": "保持"}}

    monkeypatch.setattr("app.term_decision._request_batch", failing_batch)
    async with httpx.AsyncClient() as client:
        with pytest.raises(UsageError, match="模型协议错误"):
            await run_terminology_decision(project, http_client=client)

        run_dir = next(
            item
            for item in (project / "runs").iterdir()
            if (item / CHECKPOINT_FILE).is_file()
        )
        run_id = run_dir.name
        manifest = read_json(project, run_dir / "manifest.json")
        assert manifest["status"] == "running"
        assert manifest["decision_status"] == "generating"
        assert manifest["completed_segment_count"] == 1
        assert manifest["failed_segment_count"] == 0
        assert manifest["completed_at"] is None
        checkpoint = json.loads((run_dir / CHECKPOINT_FILE).read_text("utf-8"))
        assert set(checkpoint["phases"]["adjudication"]) == {"alice"}

        resumed_calls: list[tuple[str, str]] = []

        async def resumed_batch(*_: object, **kwargs: object) -> dict[str, dict]:
            phase = str(kwargs["phase"])
            normalized = str(kwargs["focus"][0]["normalized"])
            resumed_calls.append((phase, normalized))
            return {normalized: {"action": "keep", "reason": "保持"}}

        monkeypatch.setattr("app.term_decision._request_batch", resumed_batch)
        await run_terminology_decision(
            project, resume_run_id=run_id, http_client=client
        )

    assert ("adjudication", "alice") not in resumed_calls
    assert ("adjudication", "bob") in resumed_calls
    assert read_json(project, run_dir / "manifest.json")["usage_invocation_count"] == 2


@pytest.mark.asyncio
async def test_decision_second_phase_error_reuses_both_phase_checkpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_decision_project(tmp_path)
    monkeypatch.setattr("app.term_decision._pack_batches", single_term_batches)
    alice_reviewed = asyncio.Event()

    async def failing_review(*_: object, **kwargs: object) -> dict[str, dict]:
        phase = str(kwargs["phase"])
        normalized = str(kwargs["focus"][0]["normalized"])
        if phase == "consistency" and normalized == "alice":
            alice_reviewed.set()
            return {
                "alice": {
                    "action": "update",
                    "reason": "第二阶段补全译名",
                    "after": {
                        **kwargs["focus"][0],
                        "preferred_translation": "爱丽丝",
                    },
                }
            }
        if phase == "consistency" and normalized == "bob":
            await alice_reviewed.wait()
            raise UsageError("一致性协议错误")
        return {normalized: {"action": "keep", "reason": "保持"}}

    monkeypatch.setattr("app.term_decision._request_batch", failing_review)
    async with httpx.AsyncClient() as client:
        with pytest.raises(UsageError, match="一致性协议错误"):
            await run_terminology_decision(project, http_client=client)

        run_dir = next(
            item
            for item in (project / "runs").iterdir()
            if (item / CHECKPOINT_FILE).is_file()
        )
        checkpoint = json.loads((run_dir / CHECKPOINT_FILE).read_text("utf-8"))
        assert set(checkpoint["phases"]["adjudication"]) == {"alice", "bob"}
        assert set(checkpoint["phases"]["consistency"]) == {"alice"}

        resumed_calls: list[tuple[str, str]] = []

        async def resumed_review(*_: object, **kwargs: object) -> dict[str, dict]:
            phase = str(kwargs["phase"])
            normalized = str(kwargs["focus"][0]["normalized"])
            resumed_calls.append((phase, normalized))
            assert kwargs["known_states"]["alice"]["preferred_translation"] == "爱丽丝"
            return {normalized: {"action": "keep", "reason": "保持"}}

        monkeypatch.setattr("app.term_decision._request_batch", resumed_review)
        await run_terminology_decision(
            project, resume_run_id=run_dir.name, http_client=client
        )

    assert resumed_calls == [("consistency", "bob")]


@pytest.mark.asyncio
async def test_decision_draft_write_error_resumes_without_model_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_decision_project(tmp_path)

    async def keep_batch(*_: object, **kwargs: object) -> dict[str, dict]:
        return {
            str(item["normalized"]): {"action": "keep", "reason": "保持"}
            for item in kwargs["focus"]
        }

    monkeypatch.setattr("app.term_decision._request_batch", keep_batch)
    original_atomic_write = atomic_write_json

    def fail_draft_write(path: Path, value: object) -> None:
        if path.name == "terminology_decision_draft.json":
            raise StorageError("草案写入失败")
        original_atomic_write(path, value)

    monkeypatch.setattr("app.term_decision.atomic_write_json", fail_draft_write)
    async with httpx.AsyncClient() as client:
        with pytest.raises(StorageError, match="草案写入失败"):
            await run_terminology_decision(project, http_client=client)

        run_dir = next(
            item
            for item in (project / "runs").iterdir()
            if (item / CHECKPOINT_FILE).is_file()
        )
        run_id = run_dir.name
        manifest = read_json(project, run_dir / "manifest.json")
        assert manifest["status"] == "running"
        assert manifest["completed_segment_count"] == 4

        async def unexpected_request(*_: object, **__: object) -> dict[str, dict]:
            raise AssertionError("完整检查点续作不应再次请求模型")

        monkeypatch.setattr("app.term_decision.atomic_write_json", original_atomic_write)
        monkeypatch.setattr("app.term_decision._request_batch", unexpected_request)
        await run_terminology_decision(
            project, resume_run_id=run_id, http_client=client
        )

    assert current_decision_draft(project) is not None


@pytest.mark.asyncio
async def test_complete_legacy_checkpoint_recovers_invalid_group_without_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_decision_project(tmp_path)
    run_id, run_dir, checkpoint_before, observed_usage = (
        create_complete_legacy_group_run(project)
    )
    checkpoint_path = run_dir / CHECKPOINT_FILE

    async def unexpected_request(*_: object, **__: object) -> dict[str, dict]:
        raise AssertionError("完整检查点恢复不得重新请求模型")

    monkeypatch.setattr("app.term_decision._request_batch", unexpected_request)
    async with httpx.AsyncClient() as client:
        summary = await run_terminology_decision(
            project, resume_run_id=run_id, http_client=client
        )

    assert checkpoint_path.read_bytes() == checkpoint_before
    assert summary["proposals"] == 1
    assert summary["needs_review"] == 1
    draft = current_decision_draft(project)
    assert draft is not None
    assert draft["proposals"][0]["normalized"] == ["bob"]
    assert draft["proposals"][0]["after"][0]["preferred_translation"] == "罗伯特"
    assert draft["needs_review"][0]["normalized"] == "alice"
    assert "alice -> alice" in draft["needs_review"][0]["reason"]
    manifest = read_json(project, run_dir / "manifest.json")
    assert manifest["status"] == "completed"
    assert manifest["decision_status"] == "pending"
    assert manifest["completed_segment_count"] == 4
    assert manifest["usage"] == observed_usage
    assert manifest["usage_invocation_count"] == 1

    applied = apply_decision_draft(project, confirm_all=True)
    assert applied["applied"] == 1
    overrides = read_json(project, project / "terminology" / "overrides.json")
    assert [item["normalized"] for item in overrides["overrides"]] == ["bob"]


@pytest.mark.asyncio
async def test_legacy_group_recovery_draft_failure_preserves_complete_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_decision_project(tmp_path)
    run_id, run_dir, checkpoint_before, observed_usage = (
        create_complete_legacy_group_run(project)
    )
    original_atomic_write = atomic_write_json

    def fail_draft_write(path: Path, value: object) -> None:
        if path.name == "terminology_decision_draft.json":
            raise StorageError("恢复草案写入失败")
        original_atomic_write(path, value)

    async def unexpected_request(*_: object, **__: object) -> dict[str, dict]:
        raise AssertionError("完整检查点恢复不得重新请求模型")

    monkeypatch.setattr("app.term_decision.atomic_write_json", fail_draft_write)
    monkeypatch.setattr("app.term_decision._request_batch", unexpected_request)
    async with httpx.AsyncClient() as client:
        with pytest.raises(StorageError, match="恢复草案写入失败"):
            await run_terminology_decision(
                project, resume_run_id=run_id, http_client=client
            )

        assert not (run_dir / "terminology_decision_draft.json").exists()
        assert (run_dir / CHECKPOINT_FILE).read_bytes() == checkpoint_before
        manifest = read_json(project, run_dir / "manifest.json")
        assert manifest["status"] == "running"
        assert manifest["completed_segment_count"] == 4
        assert manifest["usage"] == observed_usage
        assert manifest["usage_invocation_count"] == 1

        monkeypatch.setattr("app.term_decision.atomic_write_json", original_atomic_write)
        await run_terminology_decision(
            project, resume_run_id=run_id, http_client=client
        )

    assert current_decision_draft(project) is not None


@pytest.mark.asyncio
async def test_web_decision_review_rejections_and_apply(tmp_path: Path) -> None:
    project = create_decision_project(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": llm_jsonl(decision_response(payload))}}
                ]
            },
        )

    os.environ["LLM_API_KEY"] = "test"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await run_terminology_decision(project, http_client=client)
    del os.environ["LLM_API_KEY"]

    client = TestClient(create_app(projects_root=project.parent))
    options = client.get(
        "/api/v1/projects/decision-demo/task-options/terminology_decision"
    )
    assert options.status_code == 200
    assert options.json()["selected"] == 2
    assert options.json()["has_pending_draft"] is True
    assert options.json()["overflow_policy"] == {
        "allow_soft_target_overflow": True,
        "anchor_overflow_mode": "error",
    }
    review = client.get(
        "/api/v1/projects/decision-demo/terms/decision"
    ).json()
    proposal_id = review["draft"]["proposals"][0]["proposal_id"]
    rejected = client.put(
        "/api/v1/projects/decision-demo/terms/decision/rejections",
        json={"rejected_proposal_ids": [proposal_id]},
    )
    assert rejected.status_code == 200
    assert rejected.json()["draft"]["rejected_proposal_ids"] == [proposal_id]

    applied = client.post(
        "/api/v1/projects/decision-demo/terms/decision/apply",
        json={"confirm": True},
    )
    assert applied.status_code == 200
    assert applied.json()["applied"] == 0
    assert applied.json()["terms"]["terms_revision"] == 1


def test_web_starts_terminology_decision_task_without_options_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_decision_project(tmp_path)

    async def fake_decision(_: Path, **kwargs: object) -> dict[str, object]:
        progress = kwargs["on_progress"]
        assert callable(progress)
        progress(4, 0, 4)
        return {"completed": 4, "failed": 0, "pending": 0}

    monkeypatch.setattr("app.web_tasks.run_terminology_decision", fake_decision)
    client = TestClient(create_app(projects_root=project.parent))
    started = client.post(
        "/api/v1/projects/decision-demo/tasks",
        json={"stage": "terminology_decision"},
    )
    assert started.status_code == 200
    assert started.json()["stage"] == "terminology_decision"
    assert started.json()["total_segments"] == 0

    task_id = started.json()["task_id"]
    state = client.get(f"/api/v1/tasks/{task_id}").json()
    assert state["status"] == "completed"
    assert state["completed_segments"] == 4
    assert state["total_segments"] == 4


def test_web_resumes_complete_legacy_group_checkpoint_into_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_decision_project(tmp_path)
    run_id, run_dir, checkpoint_before, observed_usage = (
        create_complete_legacy_group_run(project)
    )

    async def unexpected_request(*_: object, **__: object) -> dict[str, dict]:
        raise AssertionError("Web 恢复完整检查点不得重新请求模型")

    monkeypatch.setattr("app.term_decision._request_batch", unexpected_request)
    client = TestClient(create_app(projects_root=project.parent))
    options = client.get(
        "/api/v1/projects/decision-demo/task-options/terminology_decision"
    )
    assert options.status_code == 200
    assert options.json()["running_run"]["run_id"] == run_id
    assert options.json()["running_run"]["completed_steps"] == 4
    assert options.json()["running_run"]["total_steps"] == 4

    started = client.post(
        "/api/v1/projects/decision-demo/tasks",
        json={"stage": "terminology_decision", "run_action": "resume"},
    )
    assert started.status_code == 200
    task_id = started.json()["task_id"]
    for _ in range(20):
        state = client.get(f"/api/v1/tasks/{task_id}").json()
        if state["status"] in {"completed", "failed", "cancelled"}:
            break

    assert state["status"] == "completed"
    assert state["completed_segments"] == 4
    assert state["failed_segments"] == 0
    assert state["total_segments"] == 4
    assert state["usage"] == observed_usage
    assert state["summary"]["proposals"] == 1
    assert state["summary"]["needs_review"] == 1
    review = client.get(
        "/api/v1/projects/decision-demo/terms/decision"
    )
    assert review.status_code == 200
    assert review.json()["draft"]["run_id"] == run_id
    assert review.json()["draft"]["needs_review"][0]["normalized"] == "alice"
    assert (run_dir / CHECKPOINT_FILE).read_bytes() == checkpoint_before

    refreshed = client.get(
        "/api/v1/projects/decision-demo/task-options/terminology_decision"
    ).json()
    assert refreshed["running_run"] is None
    assert refreshed["has_pending_draft"] is True


def test_web_decision_exposes_checkpoint_and_supports_resume_or_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_decision_project(tmp_path)
    config = load_project_config(project, stage="terminology_decision")
    run_id, run_dir = create_run(
        project,
        config=config,
        stage="terminology_decision",
        fingerprint="sha256:test",
        prompt="test",
        selected_count=2,
        requested_count=2,
        reused_count=0,
        details={
            "source_terms_revision": 1,
            "decision_status": "generating",
            "rejected_proposal_ids": [],
            "prompt_language": "zh-CN",
        },
    )
    atomic_write_json(
        run_dir / CHECKPOINT_FILE,
        record_header(
            "terminology_decision_checkpoint",
            str(read_json(project, project / "project.json")["project_id"]),
            run_id=run_id,
            source_terms_revision=1,
            phases={
                "adjudication": {
                    "alice": {
                        "decision": {"action": "keep", "reason": "保持"},
                        "decision_fingerprint": "sha256:test",
                        "model_fingerprint": "sha256:model",
                        "prompt_fingerprint": "sha256:prompt",
                    }
                },
                "consistency": {},
            },
        ),
    )
    received: list[str | None] = []

    async def fake_decision(_: Path, **kwargs: object) -> dict[str, object]:
        received.append(kwargs.get("resume_run_id"))
        progress = kwargs["on_progress"]
        assert callable(progress)
        progress(4, 0, 4)
        return {"completed": 4, "failed": 0, "pending": 0}

    monkeypatch.setattr("app.web_tasks.run_terminology_decision", fake_decision)
    client = TestClient(create_app(projects_root=project.parent))
    options = client.get(
        "/api/v1/projects/decision-demo/task-options/terminology_decision"
    )
    assert options.status_code == 200
    assert options.json()["running_run"]["run_id"] == run_id
    assert options.json()["running_run"]["completed_steps"] == 1
    assert options.json()["running_run"]["total_steps"] == 4
    assert client.post(
        "/api/v1/projects/decision-demo/tasks",
        json={"stage": "terminology_decision"},
    ).status_code == 400
    assert client.post(
        "/api/v1/projects/decision-demo/tasks",
        json={
            "stage": "terminology_decision",
            "run_action": "resume",
            "force": True,
        },
    ).status_code == 400
    assert client.post(
        "/api/v1/projects/decision-demo/tasks",
        json={"stage": "terminology_decision", "run_action": "decline"},
    ).status_code == 400

    resumed = client.post(
        "/api/v1/projects/decision-demo/tasks",
        json={"stage": "terminology_decision", "run_action": "resume"},
    )
    assert resumed.status_code == 200
    client.get(f"/api/v1/tasks/{resumed.json()['task_id']}")
    assert received == [run_id]

    forced = client.post(
        "/api/v1/projects/decision-demo/tasks",
        json={
            "stage": "terminology_decision",
            "run_action": "decline",
            "force": True,
        },
    )
    assert forced.status_code == 200
    client.get(f"/api/v1/tasks/{forced.json()['task_id']}")
    assert received == [run_id, None]
    assert read_json(project, run_dir / "manifest.json")["status"] == "interrupted"


def test_web_decision_failure_exposes_saved_run_for_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_decision_project(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test")

    async def fail_batch(*_: object, **__: object) -> dict[str, dict]:
        raise UsageError("模型协议错误")

    monkeypatch.setattr("app.term_decision._request_batch", fail_batch)
    client = TestClient(create_app(projects_root=project.parent))
    started = client.post(
        "/api/v1/projects/decision-demo/tasks",
        json={"stage": "terminology_decision"},
    )
    assert started.status_code == 200
    task_id = started.json()["task_id"]
    state = client.get(f"/api/v1/tasks/{task_id}").json()
    assert state["status"] == "failed"
    assert state["error"] == "模型协议错误"

    options = client.get(
        "/api/v1/projects/decision-demo/task-options/terminology_decision"
    )
    assert options.status_code == 200
    assert options.json()["running_run"]["completed_steps"] == 0
    assert options.json()["running_run"]["total_steps"] == 4


def test_evidence_counts_source_alias_and_aozora_views(tmp_path: Path) -> None:
    project = create_decision_project(tmp_path, "｜Alice《アリス》 Ally\nBob")
    library = read_json(project, project / "terminology" / "terms.json")

    evidence = collect_term_evidence(project, library["terms"])

    assert evidence["alice"]["hit_count"] == 1
    assert evidence["alice"]["source_hit_count"] == 1
    assert evidence["alice"]["alias_hit_counts"] == {"Ally": 1}
    assert evidence["bob"]["hit_count"] == 1


def test_evidence_samples_center_the_match_and_label_forms(tmp_path: Path) -> None:
    project = create_decision_project(tmp_path, "x" * 700 + "Alice" + " tail")
    library = read_json(project, project / "terminology" / "terms.json")

    sample = collect_term_evidence(project, library["terms"])["alice"]["samples"][0]

    assert sample["match_view"] == "source"
    assert sample["matched_forms"] == [{"kind": "source", "value": "Alice"}]
    assert "Alice" in sample["source"]
    assert len(sample["source"]) < 600


def test_evidence_labels_aozora_base_and_reading_views(tmp_path: Path) -> None:
    project = create_decision_project(tmp_path, "｜漢《かん》｜字《じ》")
    terms = [
        {
            "source": "漢字",
            "normalized": "漢字",
            "category": "其他",
            "aliases": [],
        },
        {
            "source": "かんじ",
            "normalized": "かんじ",
            "category": "其他",
            "aliases": [],
        },
    ]

    evidence = collect_term_evidence(project, terms)

    assert evidence["漢字"]["samples"][0]["match_view"] == "aozora_base"
    assert evidence["漢字"]["samples"][0]["matched_forms"] == [
        {"kind": "source", "value": "漢字"}
    ]
    assert evidence["かんじ"]["samples"][0]["match_view"] == "aozora_reading"
    assert evidence["かんじ"]["samples"][0]["matched_forms"] == [
        {"kind": "source", "value": "かんじ"}
    ]


def test_alias_transfer_requires_a_complete_relationship() -> None:
    spec = term_normalization(
        {
            "terminology": {
                "unicode_normalization": "NFKC",
                "case_insensitive": False,
            }
        }
    )
    original = {
        "alice": {**_batch_state("alice", "Alice"), "aliases": ["Ally"]},
        "bob": _batch_state("bob", "Bob"),
    }
    final = {
        "alice": deepcopy(original["alice"]),
        "bob": {**original["bob"], "aliases": ["Ally"]},
    }
    decisions = {
        "bob": {"action": "update", "reason": "接收简称", "after": final["bob"]}
    }

    assert _alias_violations(original, final, spec) == [
        ("alias_transfer", ("bob", "alice", "Ally"))
    ]
    _recover_invalid_relationship_components(
        original=original,
        final=final,
        decisions=decisions,
        language="zh-CN",
        spec=spec,
    )
    assert final == original
    assert decisions["bob"]["action"] == "needs_review"


def test_alias_transfer_and_source_grouping_are_valid_combinations() -> None:
    spec = term_normalization(
        {
            "terminology": {
                "unicode_normalization": "NFKC",
                "case_insensitive": False,
            }
        }
    )
    original = {
        "alice": {**_batch_state("alice", "Alice"), "aliases": ["Ally"]},
        "bob": _batch_state("bob", "Bob"),
    }
    alias_transfer = {
        "alice": {**original["alice"], "aliases": []},
        "bob": {**original["bob"], "aliases": ["Ally"]},
    }
    assert _alias_violations(original, alias_transfer, spec) == []

    source_group = {
        "alice": {**original["alice"], "aliases": ["Ally", "Bob"]},
        "bob": {**original["bob"], "group_primary": "alice"},
    }
    assert _alias_violations(original, source_group, spec) == []


@pytest.mark.asyncio
async def test_decision_protects_override_and_replacement_failure_keeps_draft(
    tmp_path: Path,
) -> None:
    project = create_decision_project(tmp_path)
    WebStore(project).save_term(
        {
            "old_normalized": "bob",
            "source": "Bob",
            "category": "男性人名",
            "description": "人工确认",
            "preferred_translation": "鲍勃",
            "aliases": [],
        }
    )
    seen_terms: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        seen_terms.append([item["normalized"] for item in payload["terms"]])
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(decision_response(payload))}}]},
        )

    os.environ["LLM_API_KEY"] = "test"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await run_terminology_decision(project, http_client=client)
    assert all(values == ["alice"] for values in seen_terms)
    old = current_decision_draft(project)
    assert old is not None

    def invalid_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": llm_jsonl([])}}]}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(invalid_handler)
    ) as client:
        with pytest.raises(UsageError, match="重试耗尽"):
            await run_terminology_decision(
                project, replace_draft=True, http_client=client
            )
    del os.environ["LLM_API_KEY"]
    assert current_decision_draft(project)["run_id"] == old["run_id"]


@pytest.mark.asyncio
async def test_alias_transfer_and_disable_form_one_relationship_proposal(
    tmp_path: Path,
) -> None:
    project = create_decision_project(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        records = []
        for term in payload["terms"]:
            if term["normalized"] == "alice":
                records.append(
                    {
                        "type": "decision",
                        "normalized": "alice",
                        "action": "update",
                        "reason": "转移简称",
                        "category": "人物",
                        "description": None,
                        "preferred_translation": "爱丽丝",
                        "aliases": ["Ally", "Bob"],
                        "group_primary": None,
                    }
                )
            else:
                records.append(
                    {
                        "type": "decision",
                        "normalized": "bob",
                        "action": "disable",
                        "reason": "并入 Alice",
                    }
                )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": llm_jsonl(records)}}]}
        )

    os.environ["LLM_API_KEY"] = "test"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await run_terminology_decision(project, http_client=client)
    del os.environ["LLM_API_KEY"]

    draft = current_decision_draft(project)
    assert draft is not None
    assert draft["model_fingerprint"].startswith("sha256:")
    assert draft["prompt_fingerprint"].startswith("sha256:")
    assert len(draft["proposals"]) == 1
    assert draft["proposals"][0]["kind"] == "relationship"
    assert draft["proposals"][0]["normalized"] == ["alice", "bob"]


@pytest.mark.asyncio
async def test_alias_theft_is_reviewed_without_implicit_grouping(
    tmp_path: Path,
) -> None:
    project = create_decision_project(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        records = []
        for term in payload["terms"]:
            if term["normalized"] == "alice":
                records.append(
                    {
                        "type": "decision",
                        "normalized": "alice",
                        "action": "update",
                        "reason": "接收 Bob 作为 alias",
                        "category": "人物",
                        "description": None,
                        "preferred_translation": "爱丽丝",
                        "aliases": ["Ally", "Bob"],
                        "group_primary": None,
                    }
                )
            else:
                records.append(
                    {
                        "type": "decision",
                        "normalized": term["normalized"],
                        "action": "keep",
                        "reason": "保留原状态",
                    }
                )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": llm_jsonl(records)}}]}
        )

    os.environ["LLM_API_KEY"] = "test"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        summary = await run_terminology_decision(project, http_client=client)
    del os.environ["LLM_API_KEY"]

    draft = current_decision_draft(project)
    assert draft is not None
    assert summary["proposals"] == 0
    assert summary["needs_review"] == 2
    assert {item["normalized"] for item in draft["needs_review"]} == {"alice", "bob"}
    assert all("alias" in item["reason"] for item in draft["needs_review"])
    assert all(
        item["group_primary"] is None
        for item in draft["source_library"]["terms"]
        if item["normalized"] in {"alice", "bob"}
    )


def test_decision_rejections_and_stale_revision(tmp_path: Path) -> None:
    project = create_decision_project(tmp_path)
    metadata = read_json(project, project / "project.json")
    run_id = "RUN-DECISION"
    run_dir = project / "runs" / run_id
    run_dir.mkdir()
    before = read_json(project, project / "terminology" / "terms.json")
    overrides = read_json(project, project / "terminology" / "overrides.json")
    draft = record_header(
        "terminology_decision_draft",
        str(metadata["project_id"]),
        record_id="DRAFT-1",
        run_id=run_id,
        status="pending",
        source_terms_revision=1,
        decision_fingerprint="sha256:test",
        proposals=[
            {
                "proposal_id": "TDP-1",
                "kind": "term_update",
                "normalized": ["alice"],
                "before": [],
                "after": [
                    {
                        "normalized": "alice",
                        "source": "Alice",
                        "category": "人物",
                        "description": None,
                        "preferred_translation": "爱丽丝",
                        "aliases": ["Ally"],
                        "group_primary": None,
                        "disabled": False,
                    }
                ],
                "changes": ["preferred_translation"],
                "reason": "补全",
                "evidence": {},
            }
        ],
        needs_review=[],
        rejected_proposal_ids=[],
        source_library=before,
        source_overrides=overrides,
    )
    (run_dir / "terminology_decision_draft.json").write_text(
        json.dumps(draft), encoding="utf-8"
    )
    write_json(
        project,
        run_dir / "manifest.json",
        record_header(
            "run",
            str(metadata["project_id"]),
            record_id=run_id,
            run_id=run_id,
            stage="terminology_decision",
            status="completed",
            started_at="2026-01-01T00:00:00+00:00",
            decision_status="pending",
            rejected_proposal_ids=[],
        ),
    )

    save_decision_rejections(project, ["TDP-1"])
    result = apply_decision_draft(project, confirm_all=True)
    assert result["applied"] == 0
    assert result["terms_revision"] == 1

    # Re-open the draft and then change the library revision to prove stale checks.
    manifest = read_json(project, run_dir / "manifest.json")
    manifest["decision_status"] = "pending"
    write_json(project, run_dir / "manifest.json", manifest)
    library = read_json(project, project / "terminology" / "terms.json")
    library["terms_revision"] = 2
    write_json(project, project / "terminology" / "terms.json", library)
    with pytest.raises(UsageError, match="已过期"):
        save_decision_rejections(project, [])
    with pytest.raises(UsageError, match="已过期"):
        apply_decision_draft(project, confirm_all=True)


def test_atomic_apply_failure_preserves_terms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_decision_project(tmp_path)
    before = read_json(project, project / "terminology" / "terms.json")
    # Reuse the draft fixture builder from the rejection test by producing a tiny Run.
    metadata = read_json(project, project / "project.json")
    run_id = "RUN-ATOMIC"
    run_dir = project / "runs" / run_id
    run_dir.mkdir()
    draft = record_header(
        "terminology_decision_draft",
        str(metadata["project_id"]),
        run_id=run_id,
        status="pending",
        source_terms_revision=1,
        proposals=[
            {
                "proposal_id": "TDP-A",
                "after": [
                    {
                        "normalized": "alice",
                        "source": "Alice",
                        "category": "人物",
                        "description": None,
                        "preferred_translation": "爱丽丝",
                        "aliases": ["Ally"],
                        "group_primary": None,
                        "disabled": False,
                    }
                ],
            }
        ],
        rejected_proposal_ids=[],
        source_library=before,
        source_overrides=read_json(
            project, project / "terminology" / "overrides.json"
        ),
    )
    (run_dir / "terminology_decision_draft.json").write_text(
        json.dumps(draft), encoding="utf-8"
    )
    write_json(
        project,
        run_dir / "manifest.json",
        record_header(
            "run",
            str(metadata["project_id"]),
            record_id=run_id,
            run_id=run_id,
            stage="terminology_decision",
            status="completed",
            started_at="2026-01-01T00:00:00+00:00",
            decision_status="pending",
            rejected_proposal_ids=[],
        ),
    )

    def fail(*_: object, **__: object) -> None:
        raise StorageError("injected")

    monkeypatch.setattr("app.term_decision.write_terminology_decision_state", fail)
    with pytest.raises(StorageError, match="injected"):
        apply_decision_draft(project, confirm_all=True)
    assert read_json(project, project / "terminology" / "terms.json") == before
