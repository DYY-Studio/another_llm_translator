from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import load_config, load_project_config
from .editor import EditorStore
from .errors import AppError, UsageError
from .execution import Scope
from .llm_adapter import adapter_path, load_json_adapter
from .locking import project_write_lock
from .plugins import document_adapter_summaries
from .project import (
    PROJECTS_ROOT,
    init_project,
    resolve_project,
    sync_global_templates,
)
from .stages import export_project, run_apply
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
        files: list[UploadFile] = File(...),
    ) -> dict[str, Any]:
        if not files:
            raise UsageError("至少上传一个输入文件")
        with tempfile.TemporaryDirectory(prefix="translator-upload-") as raw:
            upload_root = Path(raw)
            inputs = []
            for index, upload in enumerate(files, start=1):
                filename = Path(upload.filename or f"input-{index}.txt").name
                target = upload_root / f"{index:04d}" / filename
                target.parent.mkdir()
                target.write_bytes(await upload.read())
                inputs.append(str(target))
            path, summary = init_project(
                inputs,
                name=name,
                document_adapter_id=document_adapter,
                projects_root=projects_root,
            )
        summary["project_path"] = str(path)
        return summary

    @app.get("/api/v1/projects/{name}")
    async def overview(name: str) -> dict[str, Any]:
        return EditorStore(project(name)).overview()

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

    @app.get("/api/v1/projects/{name}/config")
    async def get_config(name: str) -> dict[str, str]:
        path = project(name) / "config.toml"
        return {"content": path.read_text(encoding="utf-8")}

    @app.put("/api/v1/projects/{name}/config")
    async def put_config(name: str, payload: dict[str, Any]) -> dict[str, bool]:
        content = payload.get("content")
        if not isinstance(content, str):
            raise UsageError("config content 必须是字符串")
        root = project(name)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=root,
            prefix=".config.",
            suffix=".toml",
            delete=False,
        ) as handle:
            handle.write(content)
            temporary = Path(handle.name)
        try:
            config = load_config(temporary)
            selected = adapter_path(root, str(config["llm"]["adapter"]))
            load_json_adapter(selected)
            with project_write_lock(root):
                temporary.replace(root / "config.toml")
        finally:
            temporary.unlink(missing_ok=True)
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
        scope = Scope(
            from_file=payload.get("from_file"),
            only_file=payload.get("only_file"),
            only_segment=payload.get("only_segment"),
        )
        with project_write_lock(root):
            return run_apply(
                root,
                stage,
                scope,
                allow_outdated_base=bool(
                    payload.get("allow_outdated_base", False)
                ),
                confirmed_all=bool(payload.get("all", False)),
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
