from __future__ import annotations

import argparse
import json
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import load_project_config
from .documents import normalize_document_output
from .errors import AppError, UsageError
from .execution import (
    latest_completed_by_segment,
    load_stage_history,
    stage_fingerprint,
    stage_result_path,
)
from .locking import project_write_lock
from .project import load_segments, load_source_files, resolve_project
from .stages import (
    build_term_library_rows,
    load_terms,
    normalize_term,
    validate_translation_text,
)
from .storage import (
    append_jsonl,
    atomic_write_json,
    new_record_id,
    read_json,
    record_header,
)

EDITOR_HTML = Path(__file__).with_name("editor.html")
REVIEW_STAGES = {"proofreading", "polishing"}


class EditorStore:
    """Small storage adapter for the development-only project editor."""

    def __init__(self, project: Path):
        self.project = project
        self.config = load_project_config(project)
        self.metadata = read_json(project / "project.json")
        self.files = load_source_files(project)
        self.segments = load_segments(project)
        self.segments_by_id = {
            str(item["segment_id"]): item
            for item in self.segments
            if not item["is_empty"]
        }

    @property
    def project_id(self) -> str:
        return str(self.metadata["project_id"])

    def _history(self, stage: str) -> dict[str, dict[str, Any]]:
        return latest_completed_by_segment(load_stage_history(self.project, stage))

    def _terms_revision(self) -> int | None:
        library = load_terms(self.project)
        return int(library["terms_revision"]) if library else None

    def _prompt(self, stage: str) -> str:
        from .execution import full_prompt

        name = {
            "translation": "translation.middle.txt",
            "proofreading": "proofreading.middle.txt",
            "polishing": "polishing.middle.txt",
        }[stage]
        middle = (self.project / "prompts" / name).read_text(encoding="utf-8")
        return full_prompt(stage, middle)

    def _fingerprint(self, stage: str) -> str:
        if stage.endswith("_applied"):
            return stage_fingerprint(
                self.config,
                stage,
                None,
                apply_semantics={
                    "review_stage": stage.removesuffix("_applied"),
                    "allow_outdated_base": False,
                },
            )
        return stage_fingerprint(
            self.config,
            stage,
            self._prompt(stage),
            terms_revision=self._terms_revision(),
        )

    def _require_segment(self, segment_id: object) -> dict[str, Any]:
        if not isinstance(segment_id, str) or segment_id not in self.segments_by_id:
            raise UsageError(f"未知或空 Segment：{segment_id}")
        return self.segments_by_id[segment_id]

    def _normalize_text(
        self, segment: dict[str, Any], text: str, stage: str
    ) -> str:
        file_record = next(
            item
            for item in self.files
            if str(item["file_id"]) == str(segment["file_id"])
        )
        from .plugins import get_document_adapter

        adapter = get_document_adapter(str(file_record["document_adapter_id"]))
        return normalize_document_output(
            adapter, segment=segment, text=text, stage=stage
        )

    def _base_results(self, stage: str) -> dict[str, dict[str, Any]]:
        translations = self._history("translation")
        if stage == "proofreading":
            return translations
        applied = self._history("proofreading_applied")
        return {**translations, **applied}

    def _review_view(
        self,
        stage: str,
        segment_id: str,
        histories: dict[str, dict[str, dict[str, Any]]],
    ) -> dict[str, Any]:
        base = histories["translation"].get(segment_id)
        if stage == "polishing":
            base = histories["proofreading_applied"].get(segment_id) or base
        suggestion = histories[stage].get(segment_id)
        applied = histories[f"{stage}_applied"].get(segment_id)
        return {
            "base": self._result_view(base),
            "suggestion": self._result_view(suggestion),
            "applied": self._result_view(applied),
            "outdated": bool(
                base
                and suggestion
                and suggestion.get("base_result_id") != base.get("record_id")
            ),
            "applied_current": bool(
                suggestion
                and applied
                and applied.get("suggestion_result_id")
                == suggestion.get("record_id")
            ),
        }

    def overview(self) -> dict[str, Any]:
        histories = {
            stage: self._history(stage)
            for stage in (
                "translation",
                "proofreading",
                "proofreading_applied",
                "polishing",
                "polishing_applied",
            )
        }
        files = [
            {
                "file_id": item["file_id"],
                "file_order": item["file_order"],
                "name": item["original_name"],
                "document_adapter_id": item["document_adapter_id"],
            }
            for item in sorted(self.files, key=lambda value: int(value["file_order"]))
        ]
        segments = []
        for item in sorted(
            self.segments_by_id.values(),
            key=lambda value: (
                str(value["file_id"]),
                int(value["line_index"]),
            ),
        ):
            segment_id = str(item["segment_id"])
            segments.append(
                {
                    "segment_id": segment_id,
                    "file_id": item["file_id"],
                    "part_id": item["part_id"],
                    "line_index": item["line_index"],
                    "source": item["source"],
                    "model_source": item.get("model_source"),
                    "format_count": len(item.get("_format_markers", [])),
                    "completed": {
                        stage: segment_id in history
                        for stage, history in histories.items()
                    },
                    "translation": self._result_view(
                        histories["translation"].get(segment_id)
                    ),
                    "reviews": {
                        stage: self._review_view(stage, segment_id, histories)
                        for stage in sorted(REVIEW_STAGES)
                    },
                }
            )
        return {
            "name": self.metadata["name"],
            "path": str(self.project),
            "nonempty_segment_count": sum(
                not bool(item["is_empty"])
                for item in self.segments_by_id.values()
            ),
            "files": files,
            "segments": segments,
        }

    def segment_detail(self, segment_id: str) -> dict[str, Any]:
        segment = self._require_segment(segment_id)
        histories = {
            stage: self._history(stage)
            for stage in (
                "translation",
                "proofreading",
                "proofreading_applied",
                "polishing",
                "polishing_applied",
            )
        }
        return {
            "segment_id": segment_id,
            "file_id": segment["file_id"],
            "part_id": segment["part_id"],
            "line_index": segment["line_index"],
            "source": segment["source"],
            "model_source": segment.get("model_source"),
            "format_count": len(segment.get("_format_markers", [])),
            "translation": self._result_view(
                histories["translation"].get(segment_id)
            ),
            "reviews": {
                stage: self._review_view(stage, segment_id, histories)
                for stage in sorted(REVIEW_STAGES)
            },
        }

    @staticmethod
    def _result_view(record: dict[str, Any] | None) -> dict[str, Any] | None:
        if record is None:
            return None
        return {
            key: record.get(key)
            for key in (
                "record_id",
                "text",
                "review_status",
                "suggested_text",
                "reason",
                "base_result_id",
                "validation_status",
                "created_at",
                "origin",
            )
        }

    def save_translation(self, payload: dict[str, Any]) -> dict[str, Any]:
        with project_write_lock(self.project):
            return self._save_translation(payload)

    def _save_translation(self, payload: dict[str, Any]) -> dict[str, Any]:
        segment_id = payload.get("segment_id")
        self._require_segment(segment_id)
        text = payload.get("text")
        if not isinstance(text, str):
            raise UsageError("译文必须是字符串")
        segment = self._require_segment(segment_id)
        text = self._normalize_text(segment, text, "translation")
        findings = validate_translation_text(
            text, self.config["validation"]["translation"]
        )
        record = record_header(
            "stage_result",
            self.project_id,
            stage="translation",
            segment_id=segment_id,
            status="completed",
            text=text,
            validation_status="warning" if findings else "passed",
            validation_findings=findings,
            stage_fingerprint=self._fingerprint("translation"),
            terms_revision=self._terms_revision(),
            run_id=None,
            request_id=None,
            origin="project_editor",
        )
        append_jsonl(stage_result_path(self.project, "translation"), record)
        return self._result_view(record) or {}

    def save_review(self, payload: dict[str, Any]) -> dict[str, Any]:
        with project_write_lock(self.project):
            return self._save_review(payload)

    def _save_review(self, payload: dict[str, Any]) -> dict[str, Any]:
        stage = payload.get("stage")
        if stage not in REVIEW_STAGES:
            raise UsageError(f"不支持的建议阶段：{stage}")
        segment_id = payload.get("segment_id")
        self._require_segment(segment_id)
        base = self._base_results(stage).get(str(segment_id))
        if base is None:
            raise UsageError("当前 Segment 缺少可用基准结果")
        review_status = payload.get("review_status")
        if review_status not in {"accepted", "suggested"}:
            raise UsageError("建议状态必须是 accepted 或 suggested")
        suggested_text = payload.get("suggested_text")
        if review_status == "suggested":
            if not isinstance(suggested_text, str) or not suggested_text:
                raise UsageError("suggested 状态需要非空建议文本")
            suggested_text = self._normalize_text(
                self._require_segment(segment_id), suggested_text, str(stage)
            )
        else:
            suggested_text = None
        reason = payload.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise UsageError("原因必须是字符串或 null")
        suggestion = record_header(
            "stage_result",
            self.project_id,
            stage=stage,
            segment_id=segment_id,
            status="completed",
            review_status=review_status,
            suggested_text=suggested_text,
            reason=reason or None,
            base_result_id=base["record_id"],
            stage_fingerprint=self._fingerprint(stage),
            terms_revision=self._terms_revision(),
            run_id=None,
            request_id=None,
            origin="project_editor",
        )
        append_jsonl(stage_result_path(self.project, stage), suggestion)
        applied = None
        if payload.get("apply"):
            text = (
                str(suggested_text)
                if review_status == "suggested"
                else str(base["text"])
            )
            applied_stage = f"{stage}_applied"
            applied = record_header(
                "stage_result",
                self.project_id,
                stage=applied_stage,
                segment_id=segment_id,
                status="completed",
                text=text,
                suggestion_result_id=suggestion["record_id"],
                base_result_id=base["record_id"],
                allowed_outdated_base=False,
                stage_fingerprint=self._fingerprint(applied_stage),
                run_id=None,
                request_id=None,
                origin="project_editor",
            )
            append_jsonl(stage_result_path(self.project, applied_stage), applied)
        return {
            "suggestion": self._result_view(suggestion),
            "applied": self._result_view(applied),
        }

    def reset_results(self, payload: dict[str, Any]) -> dict[str, Any]:
        with project_write_lock(self.project):
            stage = payload.get("stage")
            if stage not in {"translation", *REVIEW_STAGES}:
                raise UsageError(f"不支持的重置阶段：{stage}")
            raw_ids = payload.get("segment_ids")
            if (
                not isinstance(raw_ids, list)
                or not raw_ids
                or not all(isinstance(value, str) for value in raw_ids)
            ):
                raise UsageError("segment_ids 必须是非空字符串数组")
            segment_ids = tuple(dict.fromkeys(raw_ids))
            for segment_id in segment_ids:
                self._require_segment(segment_id)
            stages = [str(stage)]
            if stage in REVIEW_STAGES:
                stages.append(f"{stage}_applied")
            batch_id = new_record_id("RESET")
            cleared_ids: set[str] = set()
            reset_records = 0
            for target_stage in stages:
                current = self._history(target_stage)
                for segment_id in segment_ids:
                    if segment_id not in current:
                        continue
                    append_jsonl(
                        stage_result_path(self.project, target_stage),
                        record_header(
                            "stage_reset",
                            self.project_id,
                            stage=target_stage,
                            segment_id=segment_id,
                            status="reset",
                            reset_batch_id=batch_id,
                            origin="web_editor",
                        ),
                    )
                    cleared_ids.add(segment_id)
                    reset_records += 1
            return {
                "stage": stage,
                "selected": len(segment_ids),
                "cleared": len(cleared_ids),
                "unchanged": len(segment_ids) - len(cleared_ids),
                "reset_records": reset_records,
                "reset_batch_id": batch_id if cleared_ids else None,
            }

    def terms(self) -> dict[str, Any]:
        library = load_terms(self.project)
        current = {
            str(item["normalized"]): dict(item)
            for item in (library or {}).get("terms", [])
        }
        overrides_document = read_json(self.project / "terminology" / "overrides.json")
        overrides = {
            str(item["normalized"]): dict(item)
            for item in overrides_document.get("overrides", [])
        }
        rows: list[dict[str, Any]] = []
        for normalized in sorted(set(current) | set(overrides)):
            term = current.get(normalized, {})
            override = overrides.get(normalized, {})
            raw_conflicts = term.get("conflicts", {})
            conflicts = {
                "categories": list(raw_conflicts.get("categories", [])),
                "preferred_translations": list(
                    raw_conflicts.get("preferred_translations", [])
                ),
                "alias_primaries": list(
                    raw_conflicts.get("alias_primaries", [])
                ),
            }
            disabled = bool(override.get("disabled", False))
            rows.append(
                {
                    "normalized": normalized,
                    "source": override.get("source", term.get("source", normalized)),
                    "category": override.get("category", term.get("category")),
                    "description": override.get(
                        "description", term.get("description")
                    ),
                    "preferred_translation": override.get(
                        "preferred_translation", term.get("preferred_translation")
                    ),
                    "aliases": override.get("aliases", term.get("aliases", [])),
                    "disabled": disabled,
                    "conflicts": conflicts,
                    "has_conflicts": not disabled
                    and bool(
                        conflicts["categories"]
                        or conflicts["preferred_translations"]
                        or conflicts["alias_primaries"]
                    ),
                }
            )
        rows.sort(
            key=lambda item: (
                not item["has_conflicts"],
                item["disabled"],
                item["normalized"],
            )
        )
        return {
            "terms_revision": (
                int(library["terms_revision"]) if library is not None else None
            ),
            "conflict_count": sum(bool(item["has_conflicts"]) for item in rows),
            "terms": rows,
        }

    @staticmethod
    def _term_value(payload: dict[str, Any], key: str) -> str | None:
        value = payload.get(key)
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise UsageError(f"{key} 必须是字符串")
        return value

    def save_term(self, payload: dict[str, Any]) -> dict[str, Any]:
        with project_write_lock(self.project):
            return self._save_term(payload)

    def _publish_terms(
        self,
        library: dict[str, Any] | None,
        current: dict[str, dict[str, Any]],
        overrides: dict[str, dict[str, Any]],
        *,
        origin: str,
    ) -> dict[str, Any]:
        override_record = record_header(
            "terminology_overrides",
            self.project_id,
            record_id="TERMINOLOGY-OVERRIDES",
            overrides=[overrides[key] for key in sorted(overrides)],
            origin=origin,
        )
        atomic_write_json(
            self.project / "terminology" / "overrides.json",
            override_record,
        )
        revision = int(library["terms_revision"]) + 1 if library else 1
        terms = build_term_library_rows(
            self.project,
            [current[key] for key in sorted(current)],
            overrides,
        )
        term_record = record_header(
            "terminology_library",
            self.project_id,
            record_id=f"TERMS-{revision}",
            terms_revision=revision,
            published_run_id=(
                library.get("published_run_id") if library is not None else None
            ),
            active_task_id=(
                library.get("active_task_id") if library is not None else None
            ),
            terms=terms,
            origin=origin,
        )
        atomic_write_json(
            self.project / "terminology" / "terms.json",
            term_record,
        )
        return self.terms()

    def remove_terms(self, payload: dict[str, Any]) -> dict[str, Any]:
        with project_write_lock(self.project):
            raw_values = payload.get("normalized")
            if (
                not isinstance(raw_values, list)
                or not raw_values
                or not all(isinstance(value, str) and value for value in raw_values)
            ):
                raise UsageError("normalized 必须是非空字符串数组")
            values = tuple(dict.fromkeys(raw_values))
            library = load_terms(self.project)
            current = {
                str(item["normalized"]): dict(item)
                for item in (library or {}).get("terms", [])
            }
            overrides_path = self.project / "terminology" / "overrides.json"
            overrides_document = read_json(overrides_path)
            overrides = {
                str(item["normalized"]): dict(item)
                for item in overrides_document.get("overrides", [])
            }
            unknown = [
                value
                for value in values
                if value not in current and value not in overrides
            ]
            if unknown:
                raise UsageError(f"未知术语：{', '.join(unknown[:10])}")
            changed = 0
            for normalized in values:
                override = overrides.get(
                    normalized,
                    {
                        "normalized": normalized,
                        "source": current.get(normalized, {}).get(
                            "source", normalized
                        ),
                    },
                )
                if override.get("disabled"):
                    continue
                overrides[normalized] = {**override, "disabled": True}
                current.pop(normalized, None)
                changed += 1
            if not changed:
                result = self.terms()
            else:
                result = self._publish_terms(
                    library,
                    current,
                    overrides,
                    origin="web_editor",
                )
            result["removed"] = changed
            result["unchanged"] = len(values) - changed
            return result

    def _save_term(self, payload: dict[str, Any]) -> dict[str, Any]:
        source = payload.get("source")
        if not isinstance(source, str) or not source.strip():
            raise UsageError("术语 source 不能为空")
        normalized = normalize_term(source)
        old_normalized = payload.get("old_normalized")
        if old_normalized is not None and not isinstance(old_normalized, str):
            raise UsageError("old_normalized 类型错误")
        aliases = payload.get("aliases", [])
        if not isinstance(aliases, list) or not all(
            isinstance(alias, str) for alias in aliases
        ):
            raise UsageError("aliases 必须是字符串数组")
        aliases = [
            alias.strip()
            for alias in aliases
            if alias.strip() and normalize_term(alias) != normalized
        ]
        category = self._term_value(payload, "category")
        description = self._term_value(payload, "description")
        preferred = self._term_value(payload, "preferred_translation")
        disabled = bool(payload.get("disabled", False))

        library = load_terms(self.project)
        current = {
            str(item["normalized"]): dict(item)
            for item in (library or {}).get("terms", [])
        }
        overrides_path = self.project / "terminology" / "overrides.json"
        overrides_document = read_json(overrides_path)
        overrides = {
            str(item["normalized"]): dict(item)
            for item in overrides_document.get("overrides", [])
        }
        conflict_source = current.get(str(old_normalized or normalized), {}).get(
            "conflicts", {}
        )
        if (
            not disabled
            and conflict_source.get("categories")
            and not category
        ):
            raise UsageError("类别冲突尚未裁决")
        if (
            not disabled
            and conflict_source.get("preferred_translations")
            and not preferred
        ):
            raise UsageError("推荐译名冲突尚未裁决")
        if (
            normalized != old_normalized
            and normalized in set(current) | set(overrides)
            and not overrides.get(normalized, {}).get("disabled")
        ):
            raise UsageError(f"normalized source 已存在：{normalized}")
        if old_normalized and old_normalized != normalized:
            current.pop(old_normalized, None)
            old = overrides.get(old_normalized, {"normalized": old_normalized})
            overrides[old_normalized] = {**old, "disabled": True}

        override = {
            "normalized": normalized,
            "source": source,
            "category": category,
            "description": description,
            "preferred_translation": preferred,
            "aliases": aliases,
            "disabled": disabled,
        }
        overrides[normalized] = override
        if disabled:
            current.pop(normalized, None)
        else:
            current[normalized] = {
                "source": source,
                "normalized": normalized,
                "category": category,
                "description": description or "",
                "preferred_translation": preferred,
                "aliases": sorted(set(aliases)),
                "conflicts": {
                    "categories": [],
                    "preferred_translations": [],
                },
            }

        return self._publish_terms(
            library,
            current,
            overrides,
            origin="project_editor",
        )


def _handler(store: EditorStore, html: bytes) -> type[BaseHTTPRequestHandler]:
    class EditorHandler(BaseHTTPRequestHandler):
        server_version = "MinimalProjectEditor/1"

        def log_message(self, format: str, *args: object) -> None:
            return

        def _json(self, status: int, value: object) -> None:
            payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _body(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise UsageError("请求 JSON 无效") from exc
            if not isinstance(value, dict):
                raise UsageError("请求 JSON 必须是对象")
            return value

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/":
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(html)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(html)
                elif parsed.path == "/api/project":
                    self._json(HTTPStatus.OK, store.overview())
                elif parsed.path == "/api/terms":
                    self._json(HTTPStatus.OK, store.terms())
                elif parsed.path == "/api/segment":
                    segment_id = parse_qs(parsed.query).get("id", [""])[0]
                    self._json(HTTPStatus.OK, store.segment_detail(segment_id))
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            except (AppError, OSError, KeyError, TypeError, ValueError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            try:
                body = self._body()
                if parsed.path == "/api/translation/save":
                    result = store.save_translation(body)
                elif parsed.path == "/api/review/save":
                    result = store.save_review(body)
                elif parsed.path == "/api/terms/save":
                    result = store.save_term(body)
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                self._json(HTTPStatus.OK, result)
            except (AppError, OSError, KeyError, TypeError, ValueError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    return EditorHandler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.editor",
        description="启动开发期项目结果编辑器",
    )
    parser.add_argument("project", help="项目名称或项目目录")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        project = resolve_project(args.project)
        store = EditorStore(project)
        html = EDITOR_HTML.read_bytes()
    except (AppError, OSError) as exc:
        raise SystemExit(f"error: {exc}") from exc
    server = HTTPServer(("127.0.0.1", 0), _handler(store, html))
    host, port = server.server_address
    url = f"http://{host}:{port}/"
    print(f"项目编辑器：{url}")
    print("请勿同时运行会写入同一项目的 CLI 命令；按 Ctrl-C 退出。")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n编辑器已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
