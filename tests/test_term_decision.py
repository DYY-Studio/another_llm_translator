from __future__ import annotations

import asyncio
import json
import logging
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
from app.sqlite_storage import (
    atomic_write_json,
    list_runs,
    read_json,
    record_header,
    write_json,
)
from app.stages import build_term_library_rows, term_normalization
from app.term_decision import (
    CHECKPOINT_FILE,
    DRAFT_FILE,
    _alias_violations,
    _analyze_decisions,
    _apply_tentative,
    _automatic_phase_two_anchors,
    _consistency_states,
    _effective_conflicts,
    _group_violations,
    _hard_components,
    _make_payload,
    _merge_phase_decisions,
    _pack_batches,
    _recover_invalid_relationship_components,
    _related_anchors,
    apply_decision_draft,
    collect_term_evidence,
    current_decision_draft,
    decision_plan,
    discard_decision_draft,
    manual_review_state,
    rollback_decision,
    run_terminology_decision,
    save_decision_rejections,
)
from app.term_decision_protocol import DECISION_RULES_VERSION
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


def write_pending_decision_draft(
    project: Path,
    *,
    run_id: str,
    after: list[dict[str, object]],
    rules_version: int = DECISION_RULES_VERSION,
) -> Path:
    metadata = read_json(project, project / "project.json")
    library = read_json(project, project / "terminology" / "terms.json")
    overrides = read_json(project, project / "terminology" / "overrides.json")
    run_dir = project / "runs" / run_id
    run_dir.mkdir()
    draft = record_header(
        "terminology_decision_draft",
        str(metadata["project_id"]),
        record_id=f"DRAFT-{run_id}",
        run_id=run_id,
        status="pending",
        source_terms_revision=int(library["terms_revision"]),
        decision_rules_version=rules_version,
        decision_fingerprint="sha256:test",
        model_fingerprint="sha256:model",
        prompt_fingerprint="sha256:prompt",
        proposals=[
            {
                "proposal_id": "TDP-TEST",
                "kind": "term_update",
                "normalized": [str(item["normalized"]) for item in after],
                "before": [],
                "after": after,
                "changes": [],
                "reason": "测试草案",
                "evidence": {},
            }
        ],
        needs_review=[],
        rejected_proposal_ids=[],
        source_library=library,
        source_overrides=overrides,
    )
    atomic_write_json(run_dir / DRAFT_FILE, draft)
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
    return run_dir / DRAFT_FILE


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
                    "changes": {
                        "category": "女性人名",
                        "description": None,
                        "preferred_translation": "爱丽丝",
                    },
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


def single_term_batches_with_anchors(
    states: list[dict], *, anchors: list[dict], **_: object
) -> tuple[list, int]:
    return [
        (
            [state],
            [
                anchor
                for anchor in anchors
                if anchor["normalized"] != state["normalized"]
            ],
        )
        for state in states
    ], len(states)


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
            decision_rules_version=DECISION_RULES_VERSION,
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
    acknowledged = parser.parse_args(
        ["terms-decide", "demo", "--acknowledge-manual-review"]
    )
    assert acknowledged.acknowledge_manual_review is True
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


def test_automatic_phase_two_anchors_require_effective_resolution() -> None:
    keys = (
        "certain",
        "phase-one-review",
        "conflicted",
        "disabled",
        "resolved-on-resume",
        "reopened-on-resume",
        "cycle-a",
        "cycle-b",
    )
    eligible = [_batch_state(key, key.title()) for key in keys]
    states = {str(state["normalized"]): state for state in eligible}
    states["disabled"] = {**states["disabled"], "disabled": True}
    states["cycle-a"] = {**states["cycle-a"], "group_primary": "cycle-b"}
    states["cycle-b"] = {**states["cycle-b"], "group_primary": "cycle-a"}
    adjudication = {
        "certain": {"action": "update", "reason": "已决定"},
        "phase-one-review": {"action": "needs_review", "reason": "不确定"},
        "conflicted": {"action": "keep", "reason": "暂时保持"},
        "disabled": {"action": "disable", "reason": "禁用"},
        "resolved-on-resume": {"action": "needs_review", "reason": "第一阶段不确定"},
        "reopened-on-resume": {"action": "update", "reason": "第一阶段已决定"},
        "cycle-a": {"action": "update", "reason": "形成跨批次关系"},
        "cycle-b": {"action": "update", "reason": "形成跨批次关系"},
    }
    consistency = {
        "phase-one-review": {"action": "keep", "reason": "保留人工复核"},
        "resolved-on-resume": {"action": "update", "reason": "续作前已解决"},
        "reopened-on-resume": {"action": "needs_review", "reason": "续作前重开"},
    }
    conflicts = {
        key: {
            "categories": [],
            "preferred_translations": [],
            "alias_primaries": [],
            "group_claims": [],
        }
        for key in keys
    }
    conflicts["conflicted"]["group_claims"] = [
        {
            "entry": "conflicted",
            "claimed_by": "certain",
            "alias": "Conflict",
            "reason": "policy",
        }
    ]

    anchors = _automatic_phase_two_anchors(
        eligible,
        states,
        adjudication,
        consistency,
        conflicts,
        term_normalization(
            {
                "terminology": {
                    "unicode_normalization": "NFKC",
                    "case_insensitive": False,
                }
            }
        ),
    )

    assert [state["normalized"] for state in anchors] == [
        "certain",
        "resolved-on-resume",
    ]


def test_decision_payload_exposes_read_only_disabled_state() -> None:
    focus = {
        **_batch_state("focus", "Focus"),
        "disabled": True,
        "_prior_decision": {"action": "needs_review", "reason": "第一阶段"},
    }
    anchor = _batch_state("anchor", "Anchor")
    payload = _make_payload(
        phase="consistency",
        target_language="简体中文",
        focus=[focus],
        anchors=[anchor],
        evidence=_batch_evidence(focus, anchor),
    )

    assert payload["terms"][0]["disabled"] is True
    assert payload["terms"][0]["prior_decision"] == {
        "action": "needs_review",
        "reason": "第一阶段",
    }
    assert payload["anchors"][0]["disabled"] is False
    assert "prior_decision" not in payload["anchors"][0]
    phase_one = _make_payload(
        phase="adjudication",
        target_language="简体中文",
        focus=[focus],
        anchors=[anchor],
        evidence=_batch_evidence(focus, anchor),
    )
    assert "disabled" not in phase_one["terms"][0]
    assert "disabled" not in phase_one["anchors"][0]


def test_decision_payload_exposes_only_nonempty_read_only_conflicts() -> None:
    focus = _batch_state("focus", "Focus")
    anchor = _batch_state("anchor", "Anchor")
    conflicts = {
        "focus": {
            "categories": ["人物", "地点"],
            "preferred_translations": ["福克斯", "弗克斯"],
            "alias_primaries": [],
            "group_claims": [],
        },
        "anchor": {
            "categories": [],
            "preferred_translations": [],
            "alias_primaries": [],
            "group_claims": [],
        },
    }

    payload = _make_payload(
        phase="adjudication",
        target_language="简体中文",
        focus=[focus],
        anchors=[anchor],
        evidence=_batch_evidence(focus, anchor),
        conflicts=conflicts,
    )

    assert payload["terms"][0]["conflicts"] == conflicts["focus"]
    assert "conflicts" not in payload["anchors"][0]


def test_phase_two_keep_preserves_every_phase_one_disposition() -> None:
    original = {
        key: _batch_state(key, key.title())
        for key in ("keep", "update", "disable", "review")
    }
    adjudication = {
        "keep": {"action": "keep", "reason": "一保留"},
        "update": {
            "action": "update",
            "reason": "一更新",
            "after": {**original["update"], "preferred_translation": "更新"},
        },
        "disable": {"action": "disable", "reason": "一禁用"},
        "review": {"action": "needs_review", "reason": "一人工"},
    }
    tentative = deepcopy(original)
    _apply_tentative(tentative, adjudication)
    consistency = {key: {"action": "keep", "reason": "二保持"} for key in original}
    merged = _merge_phase_decisions(
        original=original,
        tentative=tentative,
        final=deepcopy(tentative),
        adjudication=adjudication,
        consistency=consistency,
        language="zh-CN",
    )

    assert {key: value["action"] for key, value in merged.items()} == {
        "keep": "keep",
        "update": "update",
        "disable": "disable",
        "review": "needs_review",
    }
    assert merged["review"]["reason"] == "一人工"


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


def test_hard_components_join_groups_and_normalized_form_owners(tmp_path: Path) -> None:
    project = create_decision_project(tmp_path)
    spec = term_normalization(load_project_config(project))
    root = {**_batch_state("root", "Root"), "aliases": ["Shared"]}
    member = {**_batch_state("member", "Member"), "group_primary": "root"}
    owner = _batch_state("owner", "Ｓｈａｒｅｄ")
    separate = _batch_state("separate", "Elsewhere")

    components = _hard_components([root, member, owner, separate], spec)

    assert [[item["normalized"] for item in component] for component in components] == [
        ["member", "owner", "root"],
        ["separate"],
    ]


def test_hard_component_is_never_split_and_reports_all_members_when_oversized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_decision_project(tmp_path)
    config = load_project_config(project, stage="terminology_decision")
    config["chunking"]["target_chunk_input_tokens"] = 50
    config["llm"]["context_window_tokens"] = 100
    config["llm"]["context_safety_margin_tokens"] = 0
    config["execution"]["input_tokens_per_minute"] = 0
    root = _batch_state("root", "Root")
    member = {**_batch_state("member", "Member"), "group_primary": "root"}
    evidence = _batch_evidence(root, member)

    def estimate(messages: list[dict[str, str]], _: float) -> int:
        payload = json.loads(messages[1]["content"])
        return 30 * len(payload["terms"])

    monkeypatch.setattr("app.term_decision.estimate_messages", estimate)
    spec = term_normalization(config)
    batches, _ = _pack_batches(
        [root, member],
        phase="adjudication",
        target_language="简体中文",
        anchors=[],
        evidence=evidence,
        prompt="prompt",
        config=config,
        spec=spec,
    )
    assert {item["normalized"] for item in batches[0][0]} == {"root", "member"}

    config["llm"]["context_window_tokens"] = 50
    with pytest.raises(RequestSizeError, match=r"Root.*Member|Member.*Root"):
        _pack_batches(
            [root, member],
            phase="adjudication",
            target_language="简体中文",
            anchors=[],
            evidence=evidence,
            prompt="prompt",
            config=config,
            spec=spec,
        )


def test_dry_run_plans_second_phase_instead_of_doubling_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_decision_project(tmp_path)
    observed: list[tuple[str, int]] = []

    def planned(states: list[dict], *, phase: str, anchors: list[dict], **_: object):
        observed.append((phase, len(anchors)))
        return ([(states, anchors)], 10 if phase == "adjudication" else 25)

    monkeypatch.setattr("app.term_decision._pack_batches", planned)
    plan = decision_plan(project)

    assert observed == [("adjudication", 0), ("consistency", 2)]
    assert plan["estimated_requests"] == 2
    assert plan["estimated_input_tokens"] == 35


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
    decisions, ignored_read_only, _, errors, _, batch_error = _analyze_decisions(
        content,
        focus,
        visible_states=focus,
        known_states={"target": focus[0]},
        read_only_terms={"known-anchor"},
        prompt_language="zh-CN",
        review_states=None,
        spec=None,
        phase="adjudication",
    )
    assert decisions == {"target": {"action": "keep", "reason": "保持"}}
    assert ignored_read_only == []
    assert len(errors) == 1
    assert errors[0]["code"] == "unknown_record"
    assert "未知术语决策 normalized：not-an-anchor" in errors[0]["message"]
    assert batch_error is True


def test_scalar_conflicts_require_explicit_resolution_and_allow_new_values() -> None:
    focus = [
        {
            **_batch_state("target", "Target"),
            "category": None,
            "preferred_translation": None,
        }
    ]
    conflicts = {
        "target": {
            "categories": ["人物", "地点"],
            "preferred_translations": ["塔吉特", "塔盖特"],
            "alias_primaries": [],
            "group_claims": [],
        }
    }

    _, _, _, keep_errors, _, _ = _analyze_decisions(
        llm_jsonl(
            [
                {
                    "type": "decision",
                    "normalized": "target",
                    "action": "keep",
                    "reason": "保持",
                }
            ]
        ),
        focus,
        visible_states=focus,
        known_states={"target": focus[0]},
        read_only_terms=set(),
        prompt_language="zh-CN",
        review_states=None,
        spec=None,
        phase="adjudication",
        conflicts=conflicts,
    )
    assert [error["code"] for error in keep_errors] == ["unresolved_conflict"]

    _, _, _, partial_errors, _, _ = _analyze_decisions(
        llm_jsonl(
            [
                {
                    "type": "decision",
                    "normalized": "target",
                    "action": "update",
                    "reason": "只处理类别",
                    "changes": {"category": "角色"},
                }
            ]
        ),
        focus,
        visible_states=focus,
        known_states={"target": focus[0]},
        read_only_terms=set(),
        prompt_language="zh-CN",
        review_states=None,
        spec=None,
        phase="adjudication",
        conflicts=conflicts,
    )
    assert [error["code"] for error in partial_errors] == ["unresolved_conflict"]
    assert "preferred_translation" in partial_errors[0]["message"]

    decisions, _, _, errors, _, _ = _analyze_decisions(
        llm_jsonl(
            [
                {
                    "type": "decision",
                    "normalized": "target",
                    "action": "update",
                    "reason": "依据全文采用更一致的新值",
                    "changes": {
                        "category": "核心角色",
                        "preferred_translation": "目标者",
                    },
                }
            ]
        ),
        focus,
        visible_states=focus,
        known_states={"target": focus[0]},
        read_only_terms=set(),
        prompt_language="zh-CN",
        review_states=None,
        spec=None,
        phase="adjudication",
        conflicts=conflicts,
    )
    assert errors == []
    assert decisions["target"]["after"]["category"] == "核心角色"
    assert decisions["target"]["after"]["preferred_translation"] == "目标者"


def test_phase_two_keep_preserves_prior_review_with_scalar_conflicts() -> None:
    focus = {
        **_batch_state("target", "Target"),
        "category": None,
        "_prior_decision": {"action": "needs_review", "reason": "类别冲突"},
    }
    conflicts = {
        "target": {
            "categories": ["人物", "地点"],
            "preferred_translations": [],
            "alias_primaries": [],
            "group_claims": [],
        }
    }
    decisions, _, _, errors, _, _ = _analyze_decisions(
        llm_jsonl(
            [
                {
                    "type": "decision",
                    "normalized": "target",
                    "action": "keep",
                    "reason": "仍需人工确认",
                }
            ]
        ),
        [focus],
        visible_states=[focus],
        known_states={"target": focus},
        read_only_terms=set(),
        prompt_language="zh-CN",
        review_states=None,
        spec=None,
        phase="consistency",
        conflicts=conflicts,
    )
    assert errors == []
    assert decisions["target"]["action"] == "keep"


def test_description_patch_allows_rewrite_and_clear_but_rejects_type_and_noop() -> None:
    rewrite = {**_batch_state("rewrite", "Rewrite"), "description": "旧说明；重复说明"}
    clear = {**_batch_state("clear", "Clear"), "description": "无助于区分的说明"}
    states = {"rewrite": rewrite, "clear": clear}
    decisions, _, _, errors, _, _ = _analyze_decisions(
        llm_jsonl(
            [
                {
                    "type": "decision",
                    "normalized": "rewrite",
                    "action": "update",
                    "reason": "依据当前说明与源文样本整理",
                    "changes": {"description": "作品中的核心角色"},
                },
                {
                    "type": "decision",
                    "normalized": "clear",
                    "action": "update",
                    "reason": "现有说明没有区分作用",
                    "changes": {"description": None},
                },
            ]
        ),
        [rewrite, clear],
        visible_states=[rewrite, clear],
        known_states=states,
        read_only_terms=set(),
        prompt_language="zh-CN",
        review_states=None,
        spec=None,
        phase="adjudication",
    )
    assert errors == []
    assert decisions["rewrite"]["after"]["description"] == "作品中的核心角色"
    assert decisions["clear"]["after"]["description"] is None

    for value, code in (
        (" 旧说明；重复说明 ", "no_op_patch"),
        (1, "invalid_patch_value"),
    ):
        _, _, _, errors, _, _ = _analyze_decisions(
            llm_jsonl(
                [
                    {
                        "type": "decision",
                        "normalized": "rewrite",
                        "action": "update",
                        "reason": "测试宿主校验",
                        "changes": {"description": value},
                    }
                ]
            ),
            [rewrite],
            visible_states=[rewrite],
            known_states={"rewrite": rewrite},
            read_only_terms=set(),
            prompt_language="zh-CN",
            review_states=None,
            spec=None,
            phase="adjudication",
        )
        assert [error["code"] for error in errors] == [code]


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (
            {
                "type": "decision",
                "normalized": "target",
                "action": "update",
                "reason": "补全译名",
                "changes": {"category": "人物"},
            },
            "术语决策 changes 未修改状态：target",
        ),
        (
            {
                "type": "decision",
                "normalized": "target",
                "action": "keep",
                "reason": "保持",
                "source": "Target",
            },
            "keep 决策字段无效：target（禁止字段 source）",
        ),
    ],
)
def test_decision_parser_reports_exact_field_mismatch(
    record: dict[str, object], message: str
) -> None:
    focus = [_batch_state("target", "Target")]
    _, _, _, errors, _, _ = _analyze_decisions(
        llm_jsonl([record]),
        focus,
        visible_states=focus,
        known_states={"target": focus[0]},
        read_only_terms=set(),
        prompt_language="zh-CN",
        review_states=None,
        spec=None,
        phase="adjudication",
    )
    assert len(errors) == 1
    assert message in errors[0]["message"]


def test_simple_actions_accept_only_matching_redundant_state_fields() -> None:
    focus = [
        _batch_state("keep-term", "Keep"),
        _batch_state("disable-term", "Disable"),
        _batch_state("review-term", "Review"),
    ]
    records = []
    for state, action in zip(focus, ("keep", "disable", "needs_review"), strict=True):
        records.append(
            {
                "type": "decision",
                "normalized": state["normalized"],
                "action": action,
                "reason": f"{action} reason",
                "category": state["category"],
                "description": state["description"],
                "preferred_translation": state["preferred_translation"],
                "aliases": state["aliases"],
                "group_primary": state["group_primary"],
            }
        )

    decisions, ignored_read_only, normalized_redundant, errors, _, _ = (
        _analyze_decisions(
            llm_jsonl(records),
            focus,
            visible_states=focus,
            known_states={str(item["normalized"]): item for item in focus},
            read_only_terms=set(),
            prompt_language="zh-CN",
            review_states=None,
            spec=None,
            phase="adjudication",
        )
    )

    assert errors == []
    assert ignored_read_only == []
    assert normalized_redundant == [
        ("keep-term", "keep"),
        ("disable-term", "disable"),
        ("review-term", "needs_review"),
    ]
    assert decisions == {
        str(item["normalized"]): {
            "action": action,
            "reason": f"{action} reason",
        }
        for item, action in zip(focus, ("keep", "disable", "needs_review"), strict=True)
    }


def test_simple_action_field_error_does_not_report_target_as_missing() -> None:
    focus = [_batch_state("target", "Target")]
    *_, errors, _, _ = _analyze_decisions(
        llm_jsonl(
            [
                {
                    "type": "decision",
                    "normalized": "target",
                    "action": "keep",
                    "reason": "保持",
                    "disabled": False,
                }
            ]
        ),
        focus,
        visible_states=focus,
        known_states={"target": focus[0]},
        read_only_terms=set(),
        prompt_language="zh-CN",
        review_states=None,
        spec=None,
        phase="adjudication",
    )
    message = "；".join(error["message"] for error in errors)
    assert "禁止字段 disabled" in message
    assert "缺少记录" not in message


def _update_decision(normalized: str, primary: str | None) -> dict[str, object]:
    return {
        "type": "decision",
        "normalized": normalized,
        "action": "update",
        "reason": "调整组关系",
        "changes": {"group_primary": primary},
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

    decisions, _, _, errors, _, _ = _analyze_decisions(
        llm_jsonl([_update_decision("thunder", "monarch")]),
        [tentative["thunder"]],
        visible_states=list(effective.values()),
        known_states=effective,
        read_only_terms={"princess", "monarch"},
        prompt_language="zh-CN",
        review_states=original,
        spec=None,
        phase="adjudication",
    )

    assert errors == []
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
    _, _, _, errors, _, _ = _analyze_decisions(
        llm_jsonl(records),
        focus,
        visible_states=list(known_states.values()),
        known_states=known_states,
        read_only_terms=set(),
        prompt_language="zh-CN",
        review_states=None,
        spec=None,
        phase="adjudication",
    )
    assert message in [error["message"] for error in errors]


def test_decision_parser_accepts_direct_member_to_enabled_root() -> None:
    focus = [_batch_state("alice", "Alice")]
    states = {
        "alice": focus[0],
        "bob": _batch_state("bob", "Bob"),
    }
    decisions, _, _, errors, _, _ = _analyze_decisions(
        llm_jsonl([_update_decision("alice", "bob")]),
        focus,
        visible_states=list(states.values()),
        known_states=states,
        read_only_terms=set(),
        prompt_language="zh-CN",
        review_states=None,
        spec=None,
        phase="adjudication",
    )
    assert errors == []
    assert decisions["alice"]["after"]["group_primary"] == "bob"


def test_decision_parser_allows_review_to_restore_tentative_group_state() -> None:
    original = _batch_state("alice", "Alice")
    tentative = {**original, "group_primary": "alice"}
    decisions, _, _, errors, _, _ = _analyze_decisions(
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
        visible_states=[tentative],
        known_states={"alice": tentative},
        read_only_terms=set(),
        prompt_language="zh-CN",
        review_states={"alice": original},
        spec=None,
        phase="adjudication",
    )
    assert errors == []
    assert decisions["alice"]["action"] == "needs_review"


def test_empty_patch_only_resolves_prior_review_or_reenables_disabled() -> None:
    reviewed = {
        **_batch_state("reviewed", "Reviewed"),
        "_prior_decision": {"action": "needs_review", "reason": "证据不足"},
    }
    disabled = {**_batch_state("disabled", "Disabled"), "disabled": True}
    ordinary = _batch_state("ordinary", "Ordinary")
    records = [
        {
            "type": "decision",
            "normalized": normalized,
            "action": "update",
            "reason": "明确恢复",
            "changes": {},
        }
        for normalized in ("reviewed", "disabled")
    ]
    decisions, _, _, errors, _, _ = _analyze_decisions(
        llm_jsonl(records),
        [reviewed, disabled],
        visible_states=[reviewed, disabled],
        known_states={"reviewed": reviewed, "disabled": disabled},
        read_only_terms=set(),
        prompt_language="zh-CN",
        review_states=None,
        spec=None,
        phase="consistency",
    )
    assert errors == []
    assert decisions["reviewed"]["after"]["disabled"] is False
    assert decisions["disabled"]["after"]["disabled"] is False

    _, _, _, errors, _, _ = _analyze_decisions(
        llm_jsonl(
            [
                {
                    "type": "decision",
                    "normalized": "ordinary",
                    "action": "update",
                    "reason": "无变化",
                    "changes": {},
                }
            ]
        ),
        [ordinary],
        visible_states=[ordinary],
        known_states={"ordinary": ordinary},
        read_only_terms=set(),
        prompt_language="zh-CN",
        review_states=None,
        spec=None,
        phase="consistency",
    )
    assert len(errors) == 1
    assert errors[0]["code"] == "empty_patch"
    assert "changes 不得为空" in errors[0]["message"]


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

    def cross_referenced_batches(states: list[dict], **_: object) -> tuple[list, int]:
        return [([state], [other]) for state, other in zip(states, reversed(states))], 2

    monkeypatch.setattr("app.term_decision._pack_batches", cross_referenced_batches)

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
    assert all(
        "alice -> bob -> alice" in item["reason"] for item in draft["needs_review"]
    )


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
            correction = payload["format_correction"]
            assert marker in correction["errors"][0]["message"]
            assert correction["errors"][0]["code"] == "invalid_relationship"
            assert correction["target_normalized"] == ["alice"]
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
@pytest.mark.parametrize("language", ["zh-CN", "en"])
async def test_decision_format_repair_lists_exact_target_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    language: str,
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
            correction = payload["format_correction"]
            assert correction["target_normalized"] == [
                item["normalized"] for item in payload["terms"]
            ]
            assert correction["accepted_normalized"] == []
            assert correction["errors"][0]["code"] == "unknown_record"
            assert correction["previous_invalid_records"][0]["normalized"] == (
                "not-an-anchor"
            )
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
async def test_format_repair_preserves_valid_records_and_retries_only_unresolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_decision_project(tmp_path)

    def one_batch(states: list[dict], **_: object) -> tuple[list, int]:
        return [(states, [])], 1

    monkeypatch.setattr("app.term_decision._pack_batches", one_batch)
    adjudication_scopes: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        scope = [item["normalized"] for item in payload["terms"]]
        if payload["phase"] == "adjudication":
            adjudication_scopes.append(scope)
            if len(adjudication_scopes) == 1:
                content = llm_jsonl(
                    [
                        {
                            "type": "decision",
                            "normalized": "alice",
                            "action": "keep",
                            "reason": "已验证",
                        },
                        {
                            "type": "decision",
                            "normalized": "bob",
                            "action": "keep",
                        },
                    ]
                )
            else:
                correction = payload["format_correction"]
                assert correction["accepted_normalized"] == ["alice"]
                assert correction["target_normalized"] == ["bob"]
                assert correction["previous_invalid_records"][0]["normalized"] == "bob"
                content = llm_jsonl(decision_response(payload))
        else:
            content = llm_jsonl(decision_response(payload))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )

    os.environ["LLM_API_KEY"] = "test"
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await run_terminology_decision(project, http_client=client)
    finally:
        del os.environ["LLM_API_KEY"]

    assert adjudication_scopes == [["alice", "bob"], ["bob"]]


@pytest.mark.asyncio
async def test_redundant_simple_fields_are_logged_without_format_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_decision_project(tmp_path)
    monkeypatch.setattr("app.term_decision._pack_batches", single_term_batches)
    calls = 0
    corrections = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls, corrections
        calls += 1
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        if "format_correction" in payload:
            corrections += 1
        records = decision_response(payload)
        terms = {item["normalized"]: item for item in payload["terms"]}
        for record in records:
            if record["action"] != "update":
                term = terms[record["normalized"]]
                record.update(
                    {
                        "category": term["category"],
                        "description": term["description"],
                        "preferred_translation": term["preferred_translation"],
                        "aliases": term["aliases"],
                        "group_primary": term["group_primary"],
                    }
                )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": llm_jsonl(records)}}]},
        )

    messages: list[str] = []

    class CaptureWarning(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    capture_handler = CaptureWarning(level=logging.WARNING)
    decision_logger = logging.getLogger("another_llm_translator")
    decision_logger.addHandler(capture_handler)
    os.environ["LLM_API_KEY"] = "test"
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            summary = await run_terminology_decision(project, http_client=client)
    finally:
        del os.environ["LLM_API_KEY"]
        decision_logger.removeHandler(capture_handler)
        capture_handler.close()

    assert summary["proposals"] == 1
    assert calls == 4
    assert corrections == 0
    assert "normalized redundant terminology fields" in "\n".join(messages)
    assert any(
        "request=REQ-" in message
        and "count=1" in message
        and "normalized=bob" in message
        for message in messages
    )


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
            correction = payload["format_correction"]
            assert correction["errors"][0]["code"] == "invalid_reason"
            assert correction["previous_invalid_records"][0]["changes"] == {
                "category": "女性人名",
                "description": None,
                "preferred_translation": "爱丽丝",
            }
            assert correction["target_normalized"] == ["alice"]
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
        for item in read_json(project, project / "terminology" / "terms.json")["terms"]
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
async def test_grounded_description_rewrite_has_full_draft_diff_apply_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_decision_project(tmp_path)
    rewritten = "女性主角，源文中亦称 Ally"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        records = []
        for term in payload["terms"]:
            if payload["phase"] == "adjudication" and term["normalized"] == "alice":
                records.append(
                    {
                        "type": "decision",
                        "normalized": "alice",
                        "action": "update",
                        "reason": "当前说明与 Alice Ally 样本支持精简改写",
                        "changes": {"description": rewritten},
                    }
                )
            else:
                records.append(
                    {
                        "type": "decision",
                        "normalized": term["normalized"],
                        "action": "keep",
                        "reason": "保持当前有效裁决",
                    }
                )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": llm_jsonl(records)}}]}
        )

    monkeypatch.setenv("LLM_API_KEY", "test")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await run_terminology_decision(project, http_client=client)

    draft = current_decision_draft(project)
    assert draft is not None
    proposal = draft["proposals"][0]
    assert proposal["changes"] == ["description"]
    assert proposal["before"][0]["description"] == "unhelpful"
    assert proposal["after"][0]["description"] == rewritten

    apply_decision_draft(project, confirm_all=True)
    applied = next(
        term
        for term in read_json(project, project / "terminology" / "terms.json")["terms"]
        if term["normalized"] == "alice"
    )
    assert applied["description"] == rewritten

    rollback_decision(project, confirm=True)
    restored = next(
        term
        for term in read_json(project, project / "terminology" / "terms.json")["terms"]
        if term["normalized"] == "alice"
    )
    assert restored["description"] == "unhelpful"


@pytest.mark.asyncio
async def test_conflict_candidates_reach_both_phases_and_new_resolution_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_decision_project(tmp_path)
    library = read_json(project, project / "terminology" / "terms.json")
    alice = next(term for term in library["terms"] if term["normalized"] == "alice")
    alice["category"] = None
    alice["preferred_translation"] = None
    alice["conflicts"]["categories"] = ["人物", "主角"]
    alice["conflicts"]["preferred_translations"] = ["爱丽丝", "艾丽丝"]
    write_json(project, project / "terminology" / "terms.json", library)
    seen: dict[str, dict[str, list[object]]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        records = []
        for term in payload["terms"]:
            if term["normalized"] == "alice":
                seen[payload["phase"]] = term["conflicts"]
                if payload["phase"] == "adjudication":
                    records.append(
                        {
                            "type": "decision",
                            "normalized": "alice",
                            "action": "update",
                            "reason": "源文与全书上下文支持新的统一写法",
                            "changes": {
                                "category": "核心角色",
                                "preferred_translation": "艾莉丝",
                            },
                        }
                    )
                    continue
            records.append(
                {
                    "type": "decision",
                    "normalized": term["normalized"],
                    "action": "keep",
                    "reason": "保持当前有效裁决",
                }
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": llm_jsonl(records)}}]}
        )

    monkeypatch.setenv("LLM_API_KEY", "test")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        summary = await run_terminology_decision(project, http_client=client)

    expected = {
        "categories": ["人物", "主角"],
        "preferred_translations": ["爱丽丝", "艾丽丝"],
        "alias_primaries": [],
        "group_claims": [],
    }
    assert seen == {"adjudication": expected, "consistency": expected}
    draft = current_decision_draft(project)
    assert draft is not None
    assert draft["decision_rules_version"] == DECISION_RULES_VERSION
    assert draft["proposals"][0]["conflicts"]["alice"] == expected
    assert draft["proposals"][0]["after"][0]["category"] == "核心角色"
    assert draft["proposals"][0]["after"][0]["preferred_translation"] == ("艾莉丝")

    applied = apply_decision_draft(project, confirm_all=True)
    assert applied["run_id"] == summary["run_id"]
    updated = next(
        term
        for term in read_json(project, project / "terminology" / "terms.json")["terms"]
        if term["normalized"] == "alice"
    )
    assert updated["category"] == "核心角色"
    assert updated["preferred_translation"] == "艾莉丝"
    assert updated["conflicts"] == {
        "categories": [],
        "preferred_translations": [],
        "alias_primaries": [],
        "group_claims": [],
    }


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
            references = [states[1 - index]] if phase == "consistency" else []
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
async def test_phase_one_needs_review_is_not_a_phase_two_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_decision_project(tmp_path)
    library = read_json(project, project / "terminology" / "terms.json")
    alice = next(term for term in library["terms"] if term["normalized"] == "alice")
    alice["category"] = None
    alice["conflicts"]["categories"] = ["人物", "核心角色"]
    write_json(project, project / "terminology" / "terms.json", library)
    observed: dict[str, list[str]] = {}

    async def request_batch(*_: object, **kwargs: object) -> dict[str, dict]:
        phase = str(kwargs["phase"])
        normalized = str(kwargs["focus"][0]["normalized"])
        if phase == "adjudication":
            action = "needs_review" if normalized == "alice" else "keep"
            return {normalized: {"action": action, "reason": "第一阶段判断"}}
        observed[normalized] = [
            str(anchor["normalized"]) for anchor in kwargs["anchors"]
        ]
        return {normalized: {"action": "keep", "reason": "保持有效裁决"}}

    monkeypatch.setenv("LLM_API_KEY", "test")
    monkeypatch.setattr(
        "app.term_decision._pack_batches", single_term_batches_with_anchors
    )
    monkeypatch.setattr("app.term_decision._request_batch", request_batch)
    async with httpx.AsyncClient() as client:
        await run_terminology_decision(project, http_client=client)

    assert observed["alice"] == ["bob"]
    assert "alice" not in observed["bob"]
    draft = current_decision_draft(project)
    assert draft["needs_review"][0]["normalized"] == "alice"
    assert draft["needs_review"][0]["conflicts"]["categories"] == [
        "人物",
        "核心角色",
    ]
    client = TestClient(create_app(projects_root=project.parent))
    review = client.get("/api/v1/projects/decision-demo/terms/decision")
    assert review.status_code == 200
    assert review.json()["draft"]["needs_review"][0]["conflicts"]["categories"] == [
        "人物",
        "核心角色",
    ]

    apply_decision_draft(project, confirm_all=True)
    queue = client.get("/api/v1/projects/decision-demo/terms/decision").json()[
        "manual_review"
    ]
    assert queue["items"][0]["conflicts"]["categories"] == [
        "人物",
        "核心角色",
    ]


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
        checkpoint = json.loads((runs[0] / CHECKPOINT_FILE).read_text(encoding="utf-8"))
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
    assert current_decision_draft(project)["prompt_fingerprint"].startswith("sha256:")


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
@pytest.mark.parametrize(
    ("completed_action", "expected_anchor"),
    [("update", True), ("needs_review", False)],
)
async def test_decision_second_phase_error_reuses_both_phase_checkpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    completed_action: str,
    expected_anchor: bool,
) -> None:
    project = create_decision_project(tmp_path)

    monkeypatch.setattr(
        "app.term_decision._pack_batches", single_term_batches_with_anchors
    )
    alice_reviewed = asyncio.Event()

    async def failing_review(*_: object, **kwargs: object) -> dict[str, dict]:
        phase = str(kwargs["phase"])
        normalized = str(kwargs["focus"][0]["normalized"])
        if phase == "consistency" and normalized == "alice":
            alice_reviewed.set()
            if completed_action == "needs_review":
                return {
                    "alice": {
                        "action": "needs_review",
                        "reason": "第二阶段仍不确定",
                    }
                }
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
            expected_translation = "爱丽丝" if completed_action == "update" else None
            assert kwargs["known_states"]["alice"]["preferred_translation"] == (
                expected_translation
            )
            anchor_ids = {str(anchor["normalized"]) for anchor in kwargs["anchors"]}
            assert ("alice" in anchor_ids) is expected_anchor
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

        monkeypatch.setattr(
            "app.term_decision.atomic_write_json", original_atomic_write
        )
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

        monkeypatch.setattr(
            "app.term_decision.atomic_write_json", original_atomic_write
        )
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
    review = client.get("/api/v1/projects/decision-demo/terms/decision").json()
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


@pytest.mark.asyncio
async def test_manual_review_queue_survives_apply_and_revision_change(
    tmp_path: Path,
) -> None:
    project = create_decision_project(tmp_path)
    run_id, run_dir, _, _ = create_complete_legacy_group_run(project)
    async with httpx.AsyncClient() as http_client:
        await run_terminology_decision(
            project, resume_run_id=run_id, http_client=http_client
        )
    client = TestClient(create_app(projects_root=project.parent))

    # A legacy manifest has no manual-review field; it must still be treated
    # as unresolved when the completed checkpoint is applied.
    resumed = read_json(project, run_dir / "manifest.json")
    resumed.pop("manual_review_resolved_normalized", None)
    write_json(project, run_dir / "manifest.json", resumed)
    applied = client.post(
        "/api/v1/projects/decision-demo/terms/decision/apply",
        json={"confirm": True},
    )
    assert applied.status_code == 200
    queue = client.get("/api/v1/projects/decision-demo/terms/decision").json()[
        "manual_review"
    ]
    assert queue["remaining"] == 1
    item = queue["items"][0]
    assert item["run_id"] == run_id
    assert item["normalized"] == "alice"

    # Manual editing changes the terminology revision, but the queue remains
    # attached to the completed decision Run until explicitly handled.
    terms = read_json(project, project / "terminology" / "terms.json")
    terms["terms_revision"] = 99
    write_json(project, project / "terminology" / "terms.json", terms)
    resolved = client.put(
        "/api/v1/projects/decision-demo/terms/decision/manual-review",
        json={"run_id": run_id, "normalized": "alice", "resolved": True},
    )
    assert resolved.status_code == 200
    assert resolved.json()["manual_review"]["remaining"] == 0

    reopened = client.put(
        "/api/v1/projects/decision-demo/terms/decision/manual-review",
        json={"run_id": run_id, "normalized": "alice", "resolved": False},
    )
    assert reopened.status_code == 200
    assert reopened.json()["manual_review"]["remaining"] == 1

    blocked = client.post(
        "/api/v1/projects/decision-demo/tasks",
        json={"stage": "terminology_decision"},
    )
    assert blocked.status_code == 400
    assert "未处理人工待办" in blocked.json()["error"]


def test_manual_review_queue_replaced_only_after_new_decision_is_applied(
    tmp_path: Path,
) -> None:
    project = create_decision_project(tmp_path)
    run_id, run_dir, _, _ = create_complete_legacy_group_run(project)
    draft = {
        "run_id": run_id,
        "source_terms_revision": 1,
        "proposals": [],
        "needs_review": [
            {
                "normalized": "alice",
                "source": "Alice",
                "reason": "旧队列",
                "evidence": {"hit_count": 1},
            }
        ],
    }
    atomic_write_json(run_dir / DRAFT_FILE, draft)
    manifest = read_json(project, run_dir / "manifest.json")
    manifest.update(
        decision_status="applied",
        manual_review_resolved_normalized=[],
        manual_review_replaces_previous=True,
    )
    write_json(project, run_dir / "manifest.json", manifest)
    assert manual_review_state(project)["items"][0]["normalized"] == "alice"

    config = load_project_config(project, stage="terminology_decision")
    next_run_id, next_run_dir = create_run(
        project,
        config=config,
        stage="terminology_decision",
        fingerprint="sha256:new",
        prompt="new prompt",
        selected_count=0,
        requested_count=0,
        reused_count=0,
        details={
            "source_terms_revision": 1,
            "decision_status": "generating",
            "prompt_language": "zh-CN",
        },
    )
    next_draft = {
        "run_id": next_run_id,
        "source_terms_revision": 1,
        "proposals": [],
        "needs_review": [
            {
                "normalized": "bob",
                "source": "Bob",
                "reason": "新队列",
                "evidence": {"hit_count": 2},
            }
        ],
    }
    atomic_write_json(next_run_dir / DRAFT_FILE, next_draft)
    next_manifest = read_json(project, next_run_dir / "manifest.json")
    next_manifest.update(
        decision_status="pending",
        manual_review_resolved_normalized=[],
        started_at="2099-01-01T00:00:00Z",
    )
    write_json(project, next_run_dir / "manifest.json", next_manifest)
    assert manual_review_state(project)["items"][0]["normalized"] == "alice"

    next_manifest.update(
        decision_status="applied",
        manual_review_replaces_previous=True,
    )
    write_json(project, next_run_dir / "manifest.json", next_manifest)
    queue = manual_review_state(project)
    assert [item["normalized"] for item in queue["items"]] == ["bob"]


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
    review = client.get("/api/v1/projects/decision-demo/terms/decision")
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
            decision_rules_version=DECISION_RULES_VERSION,
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
    assert options.json()["running_run"]["resume_compatible"] is True
    assert options.json()["running_run"]["resume_incompatibility_reason"] is None
    assert (
        client.post(
            "/api/v1/projects/decision-demo/tasks",
            json={"stage": "terminology_decision"},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/projects/decision-demo/tasks",
            json={
                "stage": "terminology_decision",
                "run_action": "resume",
                "force": True,
            },
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/projects/decision-demo/tasks",
            json={"stage": "terminology_decision", "run_action": "decline"},
        ).status_code
        == 400
    )

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
    assert state["error"] == {
        "error": "模型协议错误",
        "code": "usage_error",
        "params": {},
    }

    options = client.get(
        "/api/v1/projects/decision-demo/task-options/terminology_decision"
    )
    assert options.status_code == 200
    assert options.json()["running_run"]["completed_steps"] == 0
    assert options.json()["running_run"]["total_steps"] == 4
    interruption = options.json()["running_run"]["last_interruption"]
    assert interruption["error_code"] == "usage_error"
    assert interruption["reason"] == "unexpected_error"
    assert interruption["completed_steps"] == 0
    assert interruption["total_steps"] == 4


@pytest.mark.asyncio
async def test_format_exhaustion_records_only_safe_request_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_decision_project(tmp_path)

    def one_batch(states: list[dict], **_: object) -> tuple[list, int]:
        return [(states, [])], 1

    monkeypatch.setattr("app.term_decision._pack_batches", one_batch)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        records = decision_response(payload)
        for record in records:
            record.pop("reason")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": llm_jsonl(records)}}]}
        )

    monkeypatch.setenv("LLM_API_KEY", "test")
    with pytest.raises(UsageError, match="格式修正重试耗尽") as caught:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await run_terminology_decision(project, http_client=client)

    request_id = caught.value.params["request_id"]
    assert isinstance(request_id, str) and request_id.startswith("REQ-")
    manifest = list_runs(project, stage="terminology_decision")[0]
    assert manifest["last_interruption"] == {
        "at": manifest["last_interruption"]["at"],
        "error_code": "usage_error",
        "reason": "format_retries_exhausted",
        "request_id": request_id,
        "completed_steps": 0,
        "total_steps": 4,
    }
    serialized = json.dumps(manifest["last_interruption"], ensure_ascii=False)
    assert "Alice" not in serialized
    assert "模型" not in serialized


def test_rule_five_decision_checkpoint_is_visible_but_cannot_resume(
    tmp_path: Path,
) -> None:
    project = create_decision_project(tmp_path)
    run_id, run_dir, _, _ = create_complete_legacy_group_run(project)
    checkpoint = json.loads((run_dir / CHECKPOINT_FILE).read_text(encoding="utf-8"))
    checkpoint["decision_rules_version"] = 5
    atomic_write_json(run_dir / CHECKPOINT_FILE, checkpoint)

    client = TestClient(create_app(projects_root=project.parent))
    options = client.get(
        "/api/v1/projects/decision-demo/task-options/terminology_decision"
    )
    assert options.status_code == 200
    running = options.json()["running_run"]
    assert running["run_id"] == run_id
    assert running["resume_compatible"] is False
    assert "不兼容" in running["resume_incompatibility_reason"]

    resumed = client.post(
        "/api/v1/projects/decision-demo/tasks",
        json={"stage": "terminology_decision", "run_action": "resume"},
    )
    assert resumed.status_code == 400
    assert "强制新建" in resumed.json()["error"]


def test_rule_five_draft_can_be_reviewed_rejected_and_discarded_but_not_applied(
    tmp_path: Path,
) -> None:
    project = create_decision_project(tmp_path)
    bob = next(
        term
        for term in read_json(project, project / "terminology" / "terms.json")["terms"]
        if term["normalized"] == "bob"
    )
    path = write_pending_decision_draft(
        project,
        run_id="RUN-RULE-5",
        rules_version=5,
        after=[
            {
                "normalized": "bob",
                "source": bob["source"],
                "category": bob["category"],
                "description": bob["description"] or None,
                "preferred_translation": "罗伯特",
                "aliases": bob["aliases"],
                "group_primary": bob["group_primary"],
                "disabled": False,
            }
        ],
    )

    assert current_decision_draft(project)["decision_rules_version"] == 5
    assert save_decision_rejections(project, [])["rejected_proposal_ids"] == []
    with pytest.raises(UsageError, match="规则版本不兼容"):
        apply_decision_draft(project, confirm_all=True)
    assert path.is_file()
    assert discard_decision_draft(project, confirm=True) == {
        "discarded": True,
        "run_id": "RUN-RULE-5",
    }
    assert current_decision_draft(project) is None


def test_evidence_counts_source_alias_and_aozora_views(tmp_path: Path) -> None:
    project = create_decision_project(tmp_path, "｜Alice《アリス》 Ally\nBob")
    library = read_json(project, project / "terminology" / "terms.json")

    evidence = collect_term_evidence(project, library["terms"])

    assert evidence["alice"]["hit_count"] == 1
    assert evidence["alice"]["source_hit_count"] == 1
    assert evidence["alice"]["alias_hit_counts"] == {"Ally": 1}
    assert evidence["bob"]["hit_count"] == 1


def test_single_file_evidence_fills_five_distinct_segments_in_source_order(
    tmp_path: Path,
) -> None:
    lines = ["Alice Alice first", *[f"Alice sample {index}" for index in range(2, 7)]]
    project = create_decision_project(tmp_path, "\n".join(lines))
    library = read_json(project, project / "terminology" / "terms.json")

    evidence = collect_term_evidence(project, library["terms"])["alice"]

    assert evidence["hit_count"] == 6
    assert evidence["source_hit_count"] == 6
    assert [sample["segment_id"] for sample in evidence["samples"]] == [
        f"F0001-S{index:06d}" for index in range(1, 6)
    ]
    assert len({sample["segment_id"] for sample in evidence["samples"]}) == 5


def test_multi_file_evidence_prefers_first_hit_per_file_then_source_order(
    tmp_path: Path,
) -> None:
    app_root = make_app_root(tmp_path)
    inputs = []
    for index in range(1, 4):
        path = tmp_path / f"source-{index}.txt"
        path.write_text(
            f"Alice file {index} first\nAlice file {index} second",
            encoding="utf-8-sig",
        )
        inputs.append(str(path))
    project, _ = init_project(
        inputs,
        name="multi-file-evidence",
        app_root=app_root,
        projects_root=tmp_path / "projects",
    )
    assert project is not None

    evidence = collect_term_evidence(
        project,
        [{"source": "Alice", "normalized": "alice", "aliases": []}],
    )["alice"]

    assert evidence["hit_count"] == 6
    assert [
        (sample["file_id"], sample["segment_id"]) for sample in evidence["samples"]
    ] == [
        ("F0001", "F0001-S000001"),
        ("F0002", "F0002-S000001"),
        ("F0003", "F0003-S000001"),
        ("F0001", "F0001-S000002"),
        ("F0002", "F0002-S000002"),
    ]
    assert len(evidence["samples"]) == 5


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
            json={
                "choices": [
                    {"message": {"content": llm_jsonl(decision_response(payload))}}
                ]
            },
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


def test_unrelated_draft_update_cannot_clear_scalar_conflicts(tmp_path: Path) -> None:
    project = create_decision_project(tmp_path)
    library = read_json(project, project / "terminology" / "terms.json")
    alice = next(term for term in library["terms"] if term["normalized"] == "alice")
    alice["category"] = None
    alice["preferred_translation"] = None
    alice["conflicts"]["categories"] = ["人物", "角色"]
    alice["conflicts"]["preferred_translations"] = ["爱丽丝", "艾丽丝"]
    write_json(project, project / "terminology" / "terms.json", library)
    before_terms = deepcopy(library)
    before_overrides = read_json(project, project / "terminology" / "overrides.json")
    write_pending_decision_draft(
        project,
        run_id="RUN-SCALAR-GUARD",
        after=[
            {
                "normalized": "alice",
                "source": "Alice",
                "category": None,
                "description": None,
                "preferred_translation": None,
                "aliases": ["Ally"],
                "group_primary": None,
                "disabled": False,
            }
        ],
    )

    with pytest.raises(UsageError, match="未明确解决冲突字段"):
        apply_decision_draft(project, confirm_all=True)
    assert read_json(project, project / "terminology" / "terms.json") == before_terms
    assert (
        read_json(project, project / "terminology" / "overrides.json")
        == before_overrides
    )


def test_unresolved_relationship_component_is_restored_and_cannot_be_applied(
    tmp_path: Path,
) -> None:
    project = create_decision_project(tmp_path)
    library = read_json(project, project / "terminology" / "terms.json")
    library["terms"] = build_term_library_rows(
        project,
        [
            {
                "source": "Alice",
                "category": "人物",
                "description": "",
                "preferred_translation": None,
                "aliases": ["Bob"],
                "group_primary": None,
            },
            {
                "source": "Bob",
                "category": "人物",
                "description": "",
                "preferred_translation": "鲍勃",
                "aliases": [],
                "group_primary": None,
            },
            {
                "source": "Carol",
                "category": "人物",
                "description": "",
                "preferred_translation": None,
                "aliases": ["Bob"],
                "group_primary": None,
            },
        ],
        {},
    )
    write_json(project, project / "terminology" / "terms.json", library)
    assert all(
        term["conflicts"]["group_claims"]
        for term in library["terms"]
        if term["normalized"] in {"alice", "bob", "carol"}
    )

    original = {
        str(term["normalized"]): {
            "normalized": term["normalized"],
            "source": term["source"],
            "category": term["category"],
            "description": term["description"] or None,
            "preferred_translation": term["preferred_translation"],
            "aliases": term["aliases"],
            "group_primary": term["group_primary"],
            "disabled": False,
        }
        for term in library["terms"]
    }
    final = deepcopy(original)
    final["carol"]["preferred_translation"] = "卡萝尔"
    decisions = {
        normalized: {"action": "keep", "reason": "保持"} for normalized in original
    }
    decisions["carol"] = {
        "action": "update",
        "reason": "补全译名",
        "after": deepcopy(final["carol"]),
    }
    source_conflicts = {
        str(term["normalized"]): deepcopy(term["conflicts"])
        for term in library["terms"]
    }
    conflicts = _effective_conflicts(project, final, source_conflicts)

    _recover_invalid_relationship_components(
        original=original,
        final=final,
        decisions=decisions,
        language="zh-CN",
        spec=term_normalization(load_project_config(project)),
        conflicts=conflicts,
    )

    assert final == original
    assert all(decision["action"] == "needs_review" for decision in decisions.values())

    before_overrides = read_json(project, project / "terminology" / "overrides.json")
    write_pending_decision_draft(
        project,
        run_id="RUN-RELATION-GUARD",
        after=[{**original["alice"], "category": "关键人物"}],
    )
    with pytest.raises(UsageError, match="未解决 alias 或组争用"):
        apply_decision_draft(project, confirm_all=True)
    assert read_json(project, project / "terminology" / "terms.json") == library
    assert (
        read_json(project, project / "terminology" / "overrides.json")
        == before_overrides
    )


@pytest.mark.asyncio
async def test_alias_transfer_and_disable_form_one_relationship_proposal(
    tmp_path: Path,
) -> None:
    project = create_decision_project(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        records = []
        for term in payload["terms"]:
            if payload["phase"] == "adjudication" and term["normalized"] == "alice":
                records.append(
                    {
                        "type": "decision",
                        "normalized": "alice",
                        "action": "update",
                        "reason": "转移简称",
                        "changes": {
                            "description": None,
                            "preferred_translation": "爱丽丝",
                            "aliases": ["Ally", "Bob"],
                        },
                    }
                )
            elif payload["phase"] == "adjudication":
                records.append(
                    {
                        "type": "decision",
                        "normalized": "bob",
                        "action": "disable",
                        "reason": "并入 Alice",
                    }
                )
            else:
                records.append(
                    {
                        "type": "decision",
                        "normalized": term["normalized"],
                        "action": "keep",
                        "reason": "关系一致",
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
async def test_provable_alias_theft_exhausts_local_repair_without_implicit_grouping(
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
                        "changes": {
                            "description": None,
                            "preferred_translation": "爱丽丝",
                            "aliases": ["Ally", "Bob"],
                        },
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
    with pytest.raises(UsageError, match="格式修正重试耗尽"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await run_terminology_decision(project, http_client=client)
    del os.environ["LLM_API_KEY"]


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
        decision_rules_version=DECISION_RULES_VERSION,
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
        decision_rules_version=DECISION_RULES_VERSION,
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
        source_overrides=read_json(project, project / "terminology" / "overrides.json"),
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
