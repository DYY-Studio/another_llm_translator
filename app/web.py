from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import dump_config, load_config, load_project_config, validate_config
from .editor import EditorStore
from .errors import AppError, UsageError
from .execution import Scope
from .llm_adapter import adapter_path, load_json_adapter
from .locking import project_write_lock
from .plugins import document_adapter_summaries
from .project import (
    PROJECTS_ROOT,
    add_project_files,
    init_project,
    remove_project_files,
    resolve_project,
    sync_global_templates,
)
from .stages import export_project, export_terms, import_terms, run_apply
from .storage import atomic_write_json, atomic_write_text, read_json
from .web_tasks import WebTaskManager, task_options


WEB_DIST = Path(__file__).with_name("web_dist")
PROMPT_FILES = {
    "terminology": "terminology.middle.txt",
    "translation": "translation.middle.txt",
    "proofreading": "proofreading.middle.txt",
    "polishing": "polishing.middle.txt",
}


def create_app(
    *,
    projects_root: Path = PROJECTS_ROOT,
    web_dist: Path = WEB_DIST,
) -> FastAPI:
    app = FastAPI(title="Minimal LLM Translator", version="1")
    app.state.projects_root = projects_root
    app.state.tasks = WebTaskManager()

    @app.middleware("http")
    async def local_only(request: Request, call_next: Any) -> Any:
        host = request.headers.get("host", "").split(":", 1)[0]
        if host not in {"127.0.0.1", "localhost", "testserver"}:
            return JSONResponse({"error": "只允许本机访问"}, status_code=403)
        origin = request.headers.get("origin")
        if origin:
            origin_host = origin.split("://", 1)[-1].split("/", 1)[0]
            origin_host = origin_host.split(":", 1)[0]
            if origin_host not in {"127.0.0.1", "localhost", "testserver"}:
                return JSONResponse(
                    {"error": "不允许的请求来源"}, status_code=403
                )
        return await call_next(request)

    @app.exception_handler(AppError)
    async def app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=400)

    def project(name: str) -> Path:
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            raise UsageError("项目名无效")
        return resolve_project(name, projects_root)

    @app.get("/api/v1/projects")
    async def list_projects() -> dict[str, Any]:
        values = []
        if projects_root.exists():
            for item in sorted(projects_root.iterdir(), key=lambda path: path.name):
                if not (item / "project.json").is_file():
                    continue
                metadata = read_json(item / "project.json")
                values.append(
                    {
                        "name": metadata["name"],
                        "project_id": metadata["project_id"],
                        "file_count": metadata["file_count"],
                        "segment_count": metadata["segment_count"],
                    }
                )
        return {"projects": values}

    @app.get("/api/v1/document-adapters")
    async def document_adapters() -> dict[str, Any]:
        return {"adapters": document_adapter_summaries()}

    @app.post("/api/v1/projects")
    async def create_project(
        name: str = Form(...),
        document_adapter: str = Form("txt"),
        empty: bool = Form(False),
        files: list[UploadFile] | None = File(None),
    ) -> dict[str, Any]:
        uploads = files or []
        if empty == bool(uploads):
            raise UsageError("必须上传输入文件，或显式选择创建空项目")
        with tempfile.TemporaryDirectory(prefix="translator-upload-") as raw:
            upload_root = Path(raw)
            inputs = []
            for index, upload in enumerate(uploads, start=1):
                filename = Path(upload.filename or f"input-{index}.txt").name
                target = upload_root / f"{index:04d}" / filename
                target.parent.mkdir()
                target.write_bytes(await upload.read())
                inputs.append(str(target))
            path, summary = init_project(
                inputs,
                name=name,
                document_adapter_id=document_adapter,
                empty=empty,
                projects_root=projects_root,
            )
        summary["project_path"] = str(path)
        return summary

    @app.get("/api/v1/projects/{name}")
    async def overview(name: str) -> dict[str, Any]:
        return EditorStore(project(name)).overview()

    @app.post("/api/v1/projects/{name}/files")
    async def add_files(
        name: str,
        files: list[UploadFile] = File(...),
    ) -> dict[str, Any]:
        if not files:
            raise UsageError("至少上传一个输入文件")
        root = project(name)
        with tempfile.TemporaryDirectory(prefix="translator-upload-") as raw:
            upload_root = Path(raw)
            inputs = []
            for index, upload in enumerate(files, start=1):
                filename = Path(upload.filename or f"input-{index}").name
                target = upload_root / f"{index:04d}" / filename
                target.parent.mkdir()
                target.write_bytes(await upload.read())
                inputs.append(str(target))
            with project_write_lock(root):
                return add_project_files(root, inputs)

    @app.post("/api/v1/projects/{name}/files/remove")
    async def remove_files(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        file_ids = payload.get("file_ids")
        if (
            not isinstance(file_ids, list)
            or not file_ids
            or not all(isinstance(value, str) and value for value in file_ids)
        ):
            raise UsageError("file_ids 必须是非空字符串数组")
        root = project(name)
        with project_write_lock(root):
            return remove_project_files(root, file_ids)

    @app.get("/api/v1/projects/{name}/segments/{segment_id}")
    async def segment(name: str, segment_id: str) -> dict[str, Any]:
        return EditorStore(project(name)).segment_detail(segment_id)

    @app.post("/api/v1/projects/{name}/translations")
    async def save_translation(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return EditorStore(project(name)).save_translation(payload)

    @app.post("/api/v1/projects/{name}/reviews")
    async def save_review(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return EditorStore(project(name)).save_review(payload)

    @app.get("/api/v1/projects/{name}/terms")
    async def terms(name: str) -> dict[str, Any]:
        return EditorStore(project(name)).terms()

    @app.post("/api/v1/projects/{name}/terms")
    async def save_term(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return EditorStore(project(name)).save_term(payload)

    @app.post("/api/v1/projects/{name}/terms/remove")
    async def remove_terms(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return EditorStore(project(name)).remove_terms(payload)

    @app.post("/api/v1/projects/{name}/terms/import")
    async def import_term_file(
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
            handle.write(await file.read())
            temporary = Path(handle.name)
        try:
            with project_write_lock(root):
                return import_terms(root, temporary, dry_run=False)
        finally:
            temporary.unlink(missing_ok=True)

    @app.get("/api/v1/projects/{name}/terms/export")
    async def export_term_file(
        name: str,
        format: str = "json",
        include_disabled: bool = False,
    ) -> Response:
        if format not in {"json", "csv"}:
            raise UsageError("术语导出格式必须是 json 或 csv")
        root = project(name)
        with tempfile.TemporaryDirectory(
            dir=root, prefix=".terms-export."
        ) as raw:
            output = Path(raw) / f"{name}-terms.{format}"
            export_terms(
                root,
                output,
                include_disabled=include_disabled,
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

    @app.post("/api/v1/projects/{name}/results/reset")
    async def reset_results(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return EditorStore(project(name)).reset_results(payload)

    @app.get("/api/v1/projects/{name}/config")
    async def get_config(name: str) -> dict[str, Any]:
        return {"config": load_config(project(name) / "config.toml")}

    @app.put("/api/v1/projects/{name}/config")
    async def put_config(name: str, payload: dict[str, Any]) -> dict[str, bool]:
        config = payload.get("config")
        if not isinstance(config, dict):
            raise UsageError("config 必须是对象")
        validate_config(config)
        root = project(name)
        selected_id = str(config["llm"]["adapter"])
        adapter = load_json_adapter(adapter_path(root, selected_id))
        if adapter.adapter_id != selected_id:
            raise UsageError("LLM Adapter 文件中的 adapter_id 与项目配置不一致")
        content = dump_config(config)
        with project_write_lock(root):
            atomic_write_text(root / "config.toml", content)
        return {"saved": True}

    @app.get("/api/v1/projects/{name}/prompts/{stage}")
    async def get_prompt(name: str, stage: str) -> dict[str, str]:
        try:
            filename = PROMPT_FILES[stage]
        except KeyError as exc:
            raise UsageError(f"未知 Prompt 阶段：{stage}") from exc
        return {
            "content": (
                project(name) / "prompts" / filename
            ).read_text(encoding="utf-8")
        }

    @app.put("/api/v1/projects/{name}/prompts/{stage}")
    async def put_prompt(
        name: str, stage: str, payload: dict[str, Any]
    ) -> dict[str, bool]:
        try:
            filename = PROMPT_FILES[stage]
        except KeyError as exc:
            raise UsageError(f"未知 Prompt 阶段：{stage}") from exc
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            raise UsageError("Prompt 不能为空")
        root = project(name)
        with project_write_lock(root):
            atomic_write_text(root / "prompts" / filename, content)
        return {"saved": True}

    @app.get("/api/v1/projects/{name}/adapters")
    async def list_adapters(name: str) -> dict[str, Any]:
        root = project(name)
        selected = str(load_config(root / "config.toml")["llm"]["adapter"])
        adapters = []
        for path in sorted((root / "llm_adapters").glob("*.json")):
            try:
                adapter = load_json_adapter(path)
                adapters.append(
                    {
                        "adapter_id": adapter.adapter_id,
                        "digest": adapter.digest,
                        "selected": adapter.adapter_id == selected,
                        "valid": True,
                    }
                )
            except AppError as exc:
                adapters.append(
                    {
                        "adapter_id": path.stem,
                        "selected": path.stem == selected,
                        "valid": False,
                        "error": str(exc),
                    }
                )
        return {"adapters": adapters}

    @app.get("/api/v1/projects/{name}/adapters/{adapter_id}")
    async def get_adapter(name: str, adapter_id: str) -> dict[str, Any]:
        adapter = load_json_adapter(adapter_path(project(name), adapter_id))
        return adapter.definition

    @app.put("/api/v1/projects/{name}/adapters/{adapter_id}")
    async def put_adapter(
        name: str, adapter_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        root = project(name)
        if payload.get("adapter_id") != adapter_id:
            raise UsageError("URL 与 Adapter ID 不一致")
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=root,
            prefix=".adapter.",
            suffix=".json",
            delete=False,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            temporary = Path(handle.name)
        try:
            adapter = load_json_adapter(temporary)
            with project_write_lock(root):
                atomic_write_json(adapter_path(root, adapter_id), payload)
        finally:
            temporary.unlink(missing_ok=True)
        return {"saved": True, "digest": adapter.digest}

    @app.post("/api/v1/projects/{name}/tasks")
    async def start_task(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        stage = str(payload.get("stage", ""))

        def boolean_option(key: str) -> bool:
            value = payload.get(key, False)
            if not isinstance(value, bool):
                raise UsageError(f"{key} 必须是布尔值")
            return value

        force = boolean_option("force")
        reuse_mixed_fingerprints = boolean_option(
            "reuse_mixed_fingerprints"
        )
        run_action = payload.get("run_action")
        if run_action is not None and not isinstance(run_action, str):
            raise UsageError("run_action 必须是字符串或 null")
        scope = Scope(
            from_file=payload.get("from_file"),
            only_file=payload.get("only_file"),
            only_segment=payload.get("only_segment"),
            force=force,
            dry_run=False,
        )
        scope.validate()
        return await app.state.tasks.start(
            project(name),
            stage,
            scope=scope,
            reuse_mixed_fingerprints=reuse_mixed_fingerprints,
            run_action=run_action,
        )

    @app.get("/api/v1/projects/{name}/task-options/{stage}")
    async def get_task_options(name: str, stage: str) -> dict[str, Any]:
        return task_options(project(name), stage)

    @app.get("/api/v1/tasks/{task_id}")
    async def task(task_id: str) -> dict[str, Any]:
        return app.state.tasks.get(task_id)

    @app.post("/api/v1/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str) -> dict[str, Any]:
        return await app.state.tasks.cancel(task_id)

    @app.post("/api/v1/projects/{name}/apply")
    async def apply_results(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        root = project(name)
        stage = str(payload.get("stage", ""))
        segment_ids = payload.get("segment_ids")
        if segment_ids is not None and (
            not isinstance(segment_ids, list)
            or not segment_ids
            or not all(isinstance(value, str) for value in segment_ids)
        ):
            raise UsageError("segment_ids 必须是非空字符串数组")
        allow_outdated = payload.get("allow_outdated_base", False)
        confirmed_all = payload.get("all", False)
        if not isinstance(allow_outdated, bool):
            raise UsageError("allow_outdated_base 必须是布尔值")
        if not isinstance(confirmed_all, bool):
            raise UsageError("all 必须是布尔值")
        scope = Scope(
            from_file=payload.get("from_file"),
            only_file=payload.get("only_file"),
            only_segment=payload.get("only_segment"),
            segment_ids=(
                tuple(dict.fromkeys(segment_ids))
                if isinstance(segment_ids, list)
                else None
            ),
        )
        with project_write_lock(root):
            return run_apply(
                root,
                stage,
                scope,
                allow_outdated_base=allow_outdated,
                confirmed_all=confirmed_all,
            )

    @app.post("/api/v1/projects/{name}/export")
    async def export(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        root = project(name)
        with project_write_lock(root):
            return export_project(
                root,
                str(payload.get("stage", "")),
                bilingual=bool(payload.get("bilingual", False)),
                allow_missing=bool(payload.get("allow_missing", False)),
            )

    @app.post("/api/v1/projects/{name}/sync-templates")
    async def sync_templates(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        root = project(name)
        choice = payload.get("choice")
        if choice not in {"update", "keep"}:
            raise UsageError("模板选择必须是 update 或 keep")
        with project_write_lock(root):
            warnings = sync_global_templates(
                root,
                interactive=True,
                choice=choice,
            )
        return {"warnings": warnings}

    @app.get("/api/v1/projects/{name}/adapter-preview")
    async def adapter_preview(name: str) -> dict[str, Any]:
        config = load_project_config(project(name))
        adapter = config["_llm_adapter"]
        headers, body = adapter.build_request(
            api_key="***",
            model=str(config["llm"]["model"]),
            messages=[{"role": "user", "content": "…"}],
            temperature=float(config["llm"]["temperature_translation"]),
            max_output_tokens=int(config["llm"]["max_output_tokens"]),
            stream=False,
        )
        return {
            "url": (
                str(config["llm"]["base_url"]).rstrip("/")
                + "/"
                + str(config["llm"]["endpoint"]).lstrip("/")
            ),
            "headers": headers,
            "body": body,
        }

    if web_dist.is_dir():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.web",
        description="启动本地 Web 翻译工作台",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("error: Web Alpha 只允许绑定本机回环地址")
    uvicorn.run(
        "app.web:create_app",
        factory=True,
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
