from __future__ import annotations
from collections.abc import Callable
import tempfile
from pathlib import Path
from typing import Any
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import Response
from .errors import (
    UsageError,
)
from .locking import project_write_lock
from .term_exchange import export_terms, import_terms
from .term_library import publish_partial_terms
from .term_decision_drafts import (
    apply_decision_draft,
    decision_review_state,
    discard_decision_draft,
    rollback_decision,
    save_decision_rejections,
    set_manual_review_resolved,
)
from .web_store import WebStore




def register_term_routes(*, app: FastAPI, projects_root: Path, app_root: Path, project: Callable[[str], Path]) -> None:
    SESSION_COOKIE = "another_llm_session"
    _SESSION_TTL_SECONDS = 30 * 24 * 3600
    _WINDOWS_DRIVE_TYPES = {0: "unknown", 1: "unavailable", 2: "removable", 3: "fixed", 4: "network", 5: "cdrom", 6: "ramdisk"}


    @app.get("/api/v1/projects/{name}/terms")
    def terms(name: str) -> dict[str, Any]:
        return WebStore(project(name)).terms()

    @app.get("/api/v1/projects/{name}/terms/knowledge")
    def get_knowledge(name: str, normalized: str) -> dict[str, Any]:
        from .sqlite_storage import read_json, record_exists
        from .term_knowledge import entity_detail_fragment, entity_view
        root = project(name)
        path = root / "terminology" / "terms.json"
        if not record_exists(root, path):
            raise UsageError("没有已发布术语库")
        library = read_json(root, path)
        view = entity_view(root, library, normalized)
        return {"entity_key": view["entity_key"], "members": view["members"], "status": view["status"],
                "items": [{"text": item["text"], "fragment_ids": item["fragment_ids"]} for item in view["items"]],
                "fragments": [entity_detail_fragment(f) for f in view["fragments"]],
                "terms_revision": library["terms_revision"]}

    @app.put("/api/v1/projects/{name}/terms/knowledge")
    def put_knowledge(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        from .term_knowledge import update_entity
        root = project(name)
        if app.state.tasks.is_project_running(root):
            raise UsageError("项目存在运行中的任务，不能修改实体说明")
        update_entity(root, payload)
        return WebStore(root).terms()

    @app.post("/api/v1/projects/{name}/terms/hits")
    def term_hits(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        normalized = payload.get("normalized")
        if not normalized:
            raise UsageError("术语命中查询必须提供 normalized")
        if not isinstance(normalized, str):
            raise UsageError("normalized 必须是字符串")
        try:
            offset = int(payload.get("offset", 0))
            limit = int(payload.get("limit", 50))
        except (TypeError, ValueError) as exc:
            raise UsageError("术语命中窗口参数必须是整数") from exc
        return WebStore(project(name)).term_hits(
            normalized, offset=offset, limit=limit
        )

    @app.post("/api/v1/projects/{name}/terms/related")
    def related_terms(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        normalized = payload.get("normalized")
        if not normalized:
            raise UsageError("术语推荐查询必须提供 normalized")
        if not isinstance(normalized, str):
            raise UsageError("normalized 必须是字符串")
        try:
            limit = int(payload.get("limit", 20))
        except (TypeError, ValueError) as exc:
            raise UsageError("术语推荐数量参数必须是整数") from exc
        return WebStore(project(name)).related_terms(normalized, limit=limit)

    @app.post("/api/v1/projects/{name}/terms")
    def save_term(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return WebStore(project(name)).save_term(payload)

    @app.post("/api/v1/projects/{name}/terms/materialize")
    def materialize_term(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return WebStore(project(name)).materialize_term(payload)

    @app.post("/api/v1/projects/{name}/terms/set-primary")
    def set_term_primary(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return WebStore(project(name)).set_term_primary(payload)

    @app.post("/api/v1/projects/{name}/terms/leave-group")
    def leave_term_group(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return WebStore(project(name)).leave_term_group(payload)

    @app.post("/api/v1/projects/{name}/terms/group-related")
    def group_related_terms(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return WebStore(project(name)).group_related_terms(payload)

    @app.post("/api/v1/projects/{name}/terms/convert-to-alias")
    def convert_related_to_alias(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return WebStore(project(name)).convert_related_to_alias(payload)

    @app.post("/api/v1/projects/{name}/terms/remove")
    def remove_terms(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return WebStore(project(name)).remove_terms(payload)

    @app.post("/api/v1/projects/{name}/terms/clear")
    def clear_terms(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("confirm") is not True:
            raise UsageError("必须明确确认清空术语阶段")
        root = project(name)
        if app.state.tasks.is_project_running(root):
            raise UsageError("项目存在运行中的任务，结束或取消后才能清空术语阶段")
        return WebStore(root).clear_terms()

    @app.post("/api/v1/projects/{name}/terms/delete")
    def delete_terms(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return WebStore(project(name)).delete_terms(payload)

    @app.get("/api/v1/projects/{name}/terms/decision")
    def get_term_decision(name: str):
        from fastapi.responses import JSONResponse
        return JSONResponse(decision_review_state(project(name)))

    @app.get("/api/v1/projects/{name}/terms/decision/{run_id}/proposals/{proposal_id}")
    def get_decision_proposal(name: str, run_id: str, proposal_id: str) -> dict[str, Any]:
        from .term_decision_drafts import decision_proposal_detail
        return decision_proposal_detail(project(name), run_id, proposal_id)

    @app.get("/api/v1/projects/{name}/terms/decision/{run_id}/fragments/{fragment_id}")
    def get_decision_fragment(name: str, run_id: str, fragment_id: str) -> dict[str, Any]:
        from .term_decision_drafts import decision_fragment_detail
        return decision_fragment_detail(project(name), run_id, fragment_id)

    @app.put("/api/v1/projects/{name}/terms/decision/rejections")
    def put_term_decision_rejections(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        root = project(name)
        if app.state.tasks.is_project_running(root):
            raise UsageError("项目存在运行中的任务，不能修改术语决策草案")
        values = payload.get("rejected_proposal_ids")
        if not isinstance(values, list):
            raise UsageError("rejected_proposal_ids 必须是字符串数组")
        with project_write_lock(root):
            return {"draft": save_decision_rejections(root, values)}

    @app.put("/api/v1/projects/{name}/terms/decision/manual-review")
    def put_term_decision_manual_review(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        root = project(name)
        run_id = payload.get("run_id")
        normalized = payload.get("normalized")
        resolved = payload.get("resolved")
        if not isinstance(run_id, str) or not run_id:
            raise UsageError("run_id 必须是非空字符串")
        if not isinstance(normalized, str) or not normalized:
            raise UsageError("normalized 必须是非空字符串")
        if not isinstance(resolved, bool):
            raise UsageError("resolved 必须是布尔值")
        with project_write_lock(root):
            return {"manual_review": set_manual_review_resolved(
                root,
                run_id=run_id,
                normalized=normalized,
                resolved=resolved,
            )}

    @app.post("/api/v1/projects/{name}/terms/decision/apply")
    def apply_term_decision(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        root = project(name)
        if app.state.tasks.is_project_running(root):
            raise UsageError("项目存在运行中的任务，不能应用术语决策")
        with project_write_lock(root):
            result = apply_decision_draft(
                root,
                confirm_all=payload.get("confirm") is True,
                rejected_proposal_ids=[],
            )
            return {**result, "terms": WebStore(root).terms()}

    @app.post("/api/v1/projects/{name}/terms/decision/discard")
    def discard_term_decision(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        root = project(name)
        if app.state.tasks.is_project_running(root):
            raise UsageError("项目存在运行中的任务，不能丢弃术语决策草案")
        with project_write_lock(root):
            return discard_decision_draft(
                root, confirm=payload.get("confirm") is True
            )

    @app.post("/api/v1/projects/{name}/terms/decision/rollback")
    def rollback_term_decision(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        root = project(name)
        if app.state.tasks.is_project_running(root):
            raise UsageError("项目存在运行中的任务，不能撤销术语决策")
        with project_write_lock(root):
            result = rollback_decision(
                root, confirm=payload.get("confirm") is True
            )
            return {**result, "terms": WebStore(root).terms()}

    @app.post("/api/v1/projects/{name}/terms/publish-partial")
    def publish_partial_term_results(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if payload.get("confirm") is not True:
            raise UsageError("必须明确确认发布当前扫描候选")
        root = project(name)
        if app.state.tasks.is_running(root, "terminology"):
            raise UsageError("术语扫描仍在运行，结束 Run 后才能发布现有结果")
        with project_write_lock(root):
            return publish_partial_terms(root)

    @app.post("/api/v1/projects/{name}/terms/import")
    def import_term_file(
        name: str,
        file: UploadFile = File(...),
    ) -> dict[str, Any]:
        suffix = Path(file.filename or "").suffix.casefold()
        if suffix not in {".json", ".csv"}:
            raise UsageError("术语文件扩展名必须是 .json 或 .csv")
        root = project(name)
        with tempfile.NamedTemporaryFile(
            dir=root,
            prefix=".terms-import.",
            suffix=suffix,
            delete=False,
        ) as handle:
            handle.write(file.file.read())
            temporary = Path(handle.name)
        try:
            with project_write_lock(root):
                return import_terms(root, temporary, dry_run=False)
        finally:
            temporary.unlink(missing_ok=True)

    @app.get("/api/v1/projects/{name}/terms/export")
    def export_term_file(
        name: str,
        format: str = "json",
        include_disabled: bool = False,
        source: str = "published",
    ) -> Response:
        if format not in {"json", "csv"}:
            raise UsageError("术语导出格式必须是 json 或 csv")
        if source not in {"published", "scanned"}:
            raise UsageError("术语导出 source 必须是 published 或 scanned")
        root = project(name)
        with tempfile.TemporaryDirectory(
            dir=root, prefix=".terms-export."
        ) as raw:
            output = Path(raw) / f"{name}-terms.{format}"
            export_terms(
                root,
                output,
                include_disabled=include_disabled,
                source=source,
            )
            content = output.read_bytes()
        media_type = (
            "application/json"
            if format == "json"
            else "text/csv; charset=utf-8"
        )
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="terms.{format}"'
                )
            },
        )
