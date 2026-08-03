from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import (
    LLM_STAGES,
    dump_config,
    load_config,
    resolve_global_config,
    resolve_project_config,
)
from .diagnostics import Diagnostics
from .web_store import WebStore
from .errors import AppError, ExternalError, ProjectError, UsageError
from .execution import Scope
from .llm_adapter import load_json_adapter
from .llm_preset import LLMPreset, load_llm_preset, preset_path
from .locking import project_write_lock
from .plugins import (
    document_adapter_summaries,
    get_document_adapter_for_extension,
)
from .project import (
    APP_ROOT as DEFAULT_APP_ROOT,
    PROJECTS_ROOT,
    add_project_files,
    delete_project,
    init_project,
    remove_project_files,
    resolve_project,
    resolve_project_parent,
    sync_global_templates,
)
from .sqlite_storage import database_path
from .stages import (
    export_project,
    export_terms,
    import_terms,
    publish_partial_terms,
    run_apply,
)
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
    app_root: Path = DEFAULT_APP_ROOT,
) -> FastAPI:
    app = FastAPI(title="Minimal LLM Translator", version="1")
    app.state.projects_root = projects_root
    app.state.app_root = app_root
    app.state.diagnostics = Diagnostics(app_root / "logs" / "app.log")
    app.state.tasks = WebTaskManager(app.state.diagnostics)
    app.state.external_projects = set()

    async def stage_uploads(
        upload_root: Path,
        uploads: list[UploadFile],
        relative_paths: list[str] | None,
        input_kinds: list[str] | None,
    ) -> tuple[list[str], list[str], list[str]]:
        paths = (
            relative_paths
            if relative_paths is not None
            else [Path(upload.filename or "input").name for upload in uploads]
        )
        kinds = input_kinds if input_kinds is not None else ["file"] * len(uploads)
        if len(paths) != len(uploads) or len(kinds) != len(uploads):
            raise UsageError("上传文件、相对路径与来源类型数量不一致")
        normalized: list[str] = []
        seen: set[str] = set()
        for value in paths:
            path = PurePosixPath(value)
            if (
                not value
                or "\\" in value
                or path.is_absolute()
                or path.name in {"", ".", ".."}
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                raise UsageError(f"上传相对路径无效：{value}")
            name = path.as_posix()
            key = name.casefold()
            if key in seen:
                raise UsageError(f"上传文件存在重复相对路径：{name}")
            seen.add(key)
            normalized.append(name)
        if any(kind not in {"file", "folder"} for kind in kinds):
            raise UsageError("输入来源类型必须是 file 或 folder")

        inputs: list[str] = []
        original_names: list[str] = []
        ignored: list[str] = []
        for upload, name, kind in zip(
            uploads, normalized, kinds, strict=True
        ):
            try:
                get_document_adapter_for_extension(PurePosixPath(name).suffix)
            except UsageError:
                if kind == "folder":
                    ignored.append(name)
                    continue
                raise UsageError(f"不支持的输入文件：{name}") from None
            target = upload_root.joinpath(*PurePosixPath(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(await upload.read())
            inputs.append(str(target))
            original_names.append(name)
        warnings = []
        if ignored:
            examples = "、".join(ignored[:5])
            suffix = "…" if len(ignored) > 5 else ""
            warnings.append(
                f"已忽略 {len(ignored)} 个不支持的文件：{examples}{suffix}"
            )
        return inputs, original_names, warnings

    def parse_adapter_options(raw: str) -> dict[str, dict[str, str]]:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise UsageError("Document Adapter 导入选项不是合法 JSON") from exc
        if not isinstance(value, dict):
            raise UsageError("Document Adapter 导入选项必须是对象")
        for adapter_id, options in value.items():
            if (
                not isinstance(adapter_id, str)
                or not isinstance(options, dict)
                or any(
                    not isinstance(option_id, str)
                    or not isinstance(option_value, str)
                    for option_id, option_value in options.items()
                )
            ):
                raise UsageError("Document Adapter 导入选项格式无效")
        return value

    @app.middleware("http")
    async def local_only(request: Request, call_next: Any) -> Any:
        host = request.headers.get("host", "").split(":", 1)[0]
        if host not in {"127.0.0.1", "localhost", "testserver"}:
            return JSONResponse(
                {"error": "只允许本机访问", "code": "local_only"}, status_code=403
            )
        origin = request.headers.get("origin")
        if origin:
            origin_host = origin.split("://", 1)[-1].split("/", 1)[0]
            origin_host = origin_host.split(":", 1)[0]
            if origin_host not in {"127.0.0.1", "localhost", "testserver"}:
                return JSONResponse(
                    {"error": "不允许的请求来源", "code": "invalid_origin"}, status_code=403
                )
        return await call_next(request)

    @app.exception_handler(AppError)
    async def app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            {
                "error": str(exc),
                "code": exc.code,
                "params": exc.params,
            },
            status_code=400,
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        fields: set[str] = set()
        for error in exc.errors():
            location = error.get("loc", ())
            field = ".".join(
                str(part)
                for part in location
                if part not in {"body", "query", "path", "header", "cookie"}
            )
            if field:
                fields.add(field)
        return JSONResponse(
            {
                "error": "请求参数无效",
                "code": "request_validation_error",
                "params": {"fields": sorted(fields)},
            },
            status_code=400,
        )

    def remember_project(path: Path) -> None:
        normalized = path.resolve()
        if normalized.parent == projects_root.resolve():
            return
        app.state.external_projects.add(normalized)

    def project_paths() -> list[Path]:
        paths: list[Path] = []
        if projects_root.exists():
            paths.extend(
                item
                for item in sorted(
                    projects_root.iterdir(), key=lambda value: value.name
                )
                if database_path(item).is_file()
            )
        paths.extend(
            path
            for path in sorted(app.state.external_projects)
            if database_path(path).is_file()
        )
        unique: dict[Path, Path] = {}
        for path in paths:
            unique[path.resolve()] = path.resolve()
        return list(unique.values())

    def project_selector(path: Path, metadata: dict[str, Any]) -> str:
        return (
            path.name
            if path.parent == projects_root.resolve()
            else str(metadata["project_id"])
        )

    def project(name: str) -> Path:
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            raise UsageError("项目名无效")
        try:
            return resolve_project(name, projects_root)
        except ProjectError:
            matches = [
                path
                for path in project_paths()
                if str(read_json(path / "project.json")["project_id"]) == name
            ]
            if len(matches) != 1:
                raise ProjectError(f"项目不存在或标识冲突：{name}")
            return matches[0]

    @app.get("/api/v1/projects")
    async def list_projects() -> dict[str, Any]:
        values = []
        selectors: set[str] = set()
        for item in project_paths():
            metadata = read_json(item / "project.json")
            selector = project_selector(item, metadata)
            if selector in selectors:
                raise UsageError(f"项目标识冲突：{selector}")
            selectors.add(selector)
            values.append(
                {
                    "selector": selector,
                    "name": metadata["name"],
                    "project_id": metadata["project_id"],
                    "path": str(item),
                    "external": item.parent != projects_root.resolve(),
                    "file_count": metadata["file_count"],
                    "segment_count": metadata["segment_count"],
                }
            )
        return {
            "projects": values,
            "default_projects_path": str(projects_root.resolve()),
        }

    @app.get("/api/v1/document-adapters")
    async def document_adapters() -> dict[str, Any]:
        return {"adapters": document_adapter_summaries()}

    def validate_preset_payload(
        preset_id: str, payload: dict[str, Any]
    ) -> LLMPreset:
        if payload.get("preset_id") != preset_id:
            raise UsageError("URL 中的 Preset ID 必须与 preset_id 一致")
        presets = app_root / "llm_presets"
        presets.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=presets,
            prefix=".preset.",
            suffix=".json",
            delete=False,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False)
            temporary = Path(handle.name)
        try:
            preset = load_llm_preset(temporary)
            adapter = load_json_adapter(
                app_root / "llm_adapters" / f"{preset.adapter_id}.json"
            )
            if adapter.adapter_id != preset.adapter_id:
                raise UsageError(
                    "全局 Adapter 文件中的 adapter_id 与 Preset 不一致"
                )
            adapter.build_request(
                api_key="***",
                model=str(preset.definition["model"]),
                messages=[],
                temperature=0,
                max_output_tokens=int(preset.definition["max_output_tokens"]),
                stream=False,
                extra_body=preset.definition["extra_body"],
            )
            return preset
        finally:
            temporary.unlink(missing_ok=True)

    @app.get("/api/v1/global/config")
    async def get_global_config() -> dict[str, Any]:
        return {"config": load_config(app_root / "config" / "config.toml")}

    @app.put("/api/v1/global/config")
    async def put_global_config(payload: dict[str, Any]) -> dict[str, bool]:
        config = payload.get("config")
        if not isinstance(config, dict):
            raise UsageError("config 必须是对象")
        content = dump_config(config)
        resolve_global_config(config, app_root)
        for stage in LLM_STAGES:
            resolve_global_config(config, app_root, stage=stage)
        atomic_write_text(app_root / "config" / "config.toml", content)
        return {"saved": True}

    @app.get("/api/v1/global/prompts/{stage}")
    async def get_global_prompt(stage: str) -> dict[str, str]:
        try:
            filename = PROMPT_FILES[stage]
        except KeyError as exc:
            raise UsageError(f"未知 Prompt 阶段：{stage}") from exc
        return {
            "content": (app_root / "prompts" / filename).read_text(
                encoding="utf-8"
            )
        }

    @app.put("/api/v1/global/prompts/{stage}")
    async def put_global_prompt(
        stage: str, payload: dict[str, Any]
    ) -> dict[str, bool]:
        try:
            filename = PROMPT_FILES[stage]
        except KeyError as exc:
            raise UsageError(f"未知 Prompt 阶段：{stage}") from exc
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            raise UsageError("Prompt 必须是非空字符串")
        atomic_write_text(app_root / "prompts" / filename, content)
        return {"saved": True}

    @app.get("/api/v1/global/presets")
    async def list_global_presets() -> dict[str, Any]:
        selected = str(
            load_config(app_root / "config" / "config.toml")["llm"].get(
                "preset", ""
            )
        )
        values = []
        for path in sorted((app_root / "llm_presets").glob("*.json")):
            try:
                preset = load_llm_preset(path)
                values.append(
                    {
                        "preset_id": preset.preset_id,
                        "adapter_id": preset.adapter_id,
                        "model": preset.definition["model"],
                        "selected": preset.preset_id == selected,
                        "valid": True,
                        "digest": preset.digest,
                    }
                )
            except AppError as exc:
                values.append(
                    {
                        "preset_id": path.stem,
                        "selected": path.stem == selected,
                        "valid": False,
                        "error": str(exc),
                        "error_code": exc.code,
                    }
                )
        return {"presets": values}

    @app.get("/api/v1/global/presets/{preset_id}")
    async def get_global_preset(preset_id: str) -> dict[str, Any]:
        return load_llm_preset(preset_path(app_root, preset_id)).definition

    @app.put("/api/v1/global/presets/{preset_id}")
    async def put_global_preset(
        preset_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        preset = validate_preset_payload(preset_id, payload)
        atomic_write_json(preset_path(app_root, preset_id), payload)
        return {"saved": True, "digest": preset.digest}

    @app.delete("/api/v1/global/presets/{preset_id}")
    async def delete_global_preset(preset_id: str) -> dict[str, bool]:
        config = load_config(app_root / "config" / "config.toml")
        if config["llm"].get("preset") == preset_id:
            raise UsageError("不能删除全局配置正在使用的 LLM Preset")
        for item in projects_root.iterdir() if projects_root.exists() else ():
            if not database_path(item).is_file():
                continue
            project_config = load_config(item / "config.toml")
            if project_config["llm"].get("preset") == preset_id:
                raise UsageError(f"不能删除项目 {item.name} 正在使用的 LLM Preset")
        path = preset_path(app_root, preset_id)
        if not path.is_file():
            raise UsageError(f"LLM Preset 不存在：{preset_id}")
        path.unlink()
        return {"deleted": True}

    @app.get("/api/v1/global/presets/{preset_id}/preview")
    async def preview_global_preset(preset_id: str) -> dict[str, Any]:
        preset = load_llm_preset(preset_path(app_root, preset_id))
        adapter = load_json_adapter(
            app_root / "llm_adapters" / f"{preset.adapter_id}.json"
        )
        headers, body = adapter.build_request(
            api_key="***",
            model=str(preset.definition["model"]),
            messages=[{"role": "user", "content": "…"}],
            temperature=0.2,
            max_output_tokens=int(preset.definition["max_output_tokens"]),
            stream=False,
            extra_body=preset.definition["extra_body"],
        )
        return {
            "url": (
                str(preset.definition["base_url"]).rstrip("/")
                + "/"
                + str(preset.definition["endpoint"])
                .replace("${model}", str(preset.definition["model"]))
                .lstrip("/")
            ),
            "headers": headers,
            "body": body,
        }

    @app.post("/api/v1/global/presets/{preset_id}/models")
    async def discover_preset_models(
        preset_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        preset = validate_preset_payload(preset_id, payload)
        adapter = load_json_adapter(
            app_root / "llm_adapters" / f"{preset.adapter_id}.json"
        )
        if adapter.models_spec is None:
            raise UsageError("该 Adapter 未声明模型发现规格")
        api_key = os.getenv(str(preset.definition["api_key_env"]))
        if not api_key:
            raise UsageError(
                f"缺少环境变量：{preset.definition['api_key_env']}"
            )
        endpoint, headers = adapter.build_models_request(api_key=api_key)
        url = (
            str(preset.definition["base_url"]).rstrip("/")
            + "/"
            + endpoint.lstrip("/")
        )
        timeout = float(preset.definition["request_timeout_seconds"])
        proxy = str(preset.definition["proxy_url"]) or None
        try:
            async with httpx.AsyncClient(timeout=timeout, proxy=proxy) as client:
                response = await client.get(url, headers=headers)
        except (httpx.HTTPError, OSError) as exc:
            raise UsageError(f"模型列表请求失败：{exc}") from exc
        if response.status_code >= 400:
            raise UsageError(f"模型列表请求失败：HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise UsageError("模型列表响应不是合法 JSON") from exc
        try:
            models = adapter.parse_models_response(data)
        except ExternalError as exc:
            raise UsageError(str(exc)) from exc
        return {"models": models, "count": len(models)}

    @app.post("/api/v1/projects")
    async def create_project(
        name: str = Form(...),
        empty: bool = Form(False),
        parent_dir: str = Form(""),
        files: list[UploadFile] | None = File(None),
        relative_paths: list[str] | None = Form(None),
        input_kinds: list[str] | None = Form(None),
        adapter_options: str = Form("{}"),
    ) -> dict[str, Any]:
        uploads = files or []
        with tempfile.TemporaryDirectory(prefix="translator-upload-") as raw:
            upload_root = Path(raw)
            inputs, original_names, upload_warnings = await stage_uploads(
                upload_root, uploads, relative_paths, input_kinds
            )
            if empty == bool(inputs):
                raise UsageError("必须上传输入文件，或显式选择创建空项目")
            selected_root = (
                resolve_project_parent(parent_dir, require_absolute=True)
                if parent_dir
                else projects_root
            )
            path, summary = init_project(
                inputs,
                name=name,
                document_adapter_id=None,
                original_names=original_names,
                adapter_options=parse_adapter_options(adapter_options),
                empty=empty,
                app_root=app_root,
                projects_root=selected_root,
            )
            summary["warnings"] = [
                *upload_warnings,
                *list(summary["warnings"]),
            ]
        assert path is not None
        remember_project(path)
        metadata = read_json(path / "project.json")
        summary["project_path"] = str(path)
        summary["project_selector"] = project_selector(path, metadata)
        summary["external"] = path.parent != projects_root.resolve()
        return summary

    @app.post("/api/v1/projects/open")
    async def open_project(payload: dict[str, Any]) -> dict[str, Any]:
        value = payload.get("path")
        if not isinstance(value, str) or not value.strip():
            raise UsageError("项目路径必须是非空字符串")
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            raise UsageError("项目路径必须是绝对路径")
        root = resolve_project(str(candidate))
        metadata = read_json(root / "project.json")
        remember_project(root)
        return {
            "selector": project_selector(root, metadata),
            "name": metadata["name"],
            "path": str(root),
            "external": root.parent != projects_root.resolve(),
        }

    @app.delete("/api/v1/projects/{name}")
    async def delete_project_route(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if payload.get("confirm") is not True:
            raise UsageError("必须明确确认删除整个项目")
        root = project(name)
        if app.state.tasks.is_project_running(root):
            raise UsageError("项目存在运行中的任务，结束或取消后才能删除")
        with project_write_lock(root):
            result = delete_project(
                root,
                protected_roots=(projects_root, app_root),
            )
        app.state.external_projects.discard(root.resolve())
        return result

    @app.get("/api/v1/projects/{name}")
    async def overview(name: str, request: Request) -> dict[str, Any]:
        params = request.query_params
        try:
            offset = int(params.get("offset", "0"))
            limit = int(params.get("limit", "100"))
        except ValueError as exc:
            raise UsageError("Segment 窗口参数必须是整数") from exc
        return WebStore(project(name)).overview(
            offset=offset,
            limit=limit,
            file_id=params.get("file_id") or None,
            status=params.get("status") or None,
            search=params.get("q") or None,
            stage=params.get("stage", "translation"),
        )

    @app.get("/api/v1/projects/{name}/segments/ids")
    async def segment_index(name: str, request: Request) -> dict[str, Any]:
        params = request.query_params
        return WebStore(project(name)).segment_index(
            file_id=params.get("file_id") or None,
            status=params.get("status") or None,
            search=params.get("q") or None,
            stage=params.get("stage", "translation"),
        )

    @app.post("/api/v1/projects/{name}/files")
    async def add_files(
        name: str,
        files: list[UploadFile] = File(...),
        relative_paths: list[str] | None = Form(None),
        input_kinds: list[str] | None = Form(None),
        adapter_options: str = Form("{}"),
    ) -> dict[str, Any]:
        if not files:
            raise UsageError("至少上传一个输入文件")
        root = project(name)
        with tempfile.TemporaryDirectory(prefix="translator-upload-") as raw:
            upload_root = Path(raw)
            inputs, original_names, upload_warnings = await stage_uploads(
                upload_root, files, relative_paths, input_kinds
            )
            if not inputs:
                raise UsageError("没有受支持的输入文件")
            with project_write_lock(root):
                summary = add_project_files(
                    root,
                    inputs,
                    original_names=original_names,
                    adapter_options=parse_adapter_options(adapter_options),
                )
            summary["warnings"] = [
                *upload_warnings,
                *list(summary["warnings"]),
            ]
            return summary

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
        return WebStore(project(name)).segment_detail(segment_id)

    @app.post("/api/v1/projects/{name}/translations")
    async def save_translation(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return WebStore(project(name)).save_translation(payload)

    @app.post("/api/v1/projects/{name}/reviews")
    async def save_review(
        name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return WebStore(project(name)).save_review(payload)

    @app.get("/api/v1/projects/{name}/terms")
    async def terms(name: str) -> dict[str, Any]:
        return WebStore(project(name)).terms()

    @app.post("/api/v1/projects/{name}/terms")
    async def save_term(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return WebStore(project(name)).save_term(payload)

    @app.post("/api/v1/projects/{name}/terms/remove")
    async def remove_terms(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return WebStore(project(name)).remove_terms(payload)

    @app.post("/api/v1/projects/{name}/terms/delete")
    async def delete_terms(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return WebStore(project(name)).delete_terms(payload)

    @app.post("/api/v1/projects/{name}/terms/publish-partial")
    async def publish_partial_term_results(
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

    @app.post("/api/v1/projects/{name}/results/reset")
    async def reset_results(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return WebStore(project(name)).reset_results(payload)

    @app.get("/api/v1/projects/{name}/config")
    async def get_config(name: str) -> dict[str, Any]:
        return {"config": load_config(project(name) / "config.toml")}

    @app.put("/api/v1/projects/{name}/config")
    async def put_config(name: str, payload: dict[str, Any]) -> dict[str, bool]:
        config = payload.get("config")
        if not isinstance(config, dict):
            raise UsageError("config 必须是对象")
        content = dump_config(config)
        root = project(name)
        resolve_project_config(config, presets_root=app_root)
        for stage in LLM_STAGES:
            resolve_project_config(
                config, stage=stage, presets_root=app_root
            )
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

    @app.get("/api/v1/global/adapters")
    async def list_global_adapters() -> dict[str, Any]:
        adapters = []
        for path in sorted((app_root / "llm_adapters").glob("*.json")):
            try:
                adapter = load_json_adapter(path)
                adapters.append(
                    {
                        "adapter_id": adapter.adapter_id,
                        "valid": True,
                        "digest": adapter.digest,
                    }
                )
            except AppError as exc:
                adapters.append(
                    {
                        "adapter_id": path.stem,
                        "valid": False,
                        "error": str(exc),
                        "error_code": exc.code,
                    }
                )
        return {"adapters": adapters}

    @app.get("/api/v1/global/adapters/{adapter_id}")
    async def get_global_adapter(adapter_id: str) -> dict[str, Any]:
        adapter = load_json_adapter(
            app_root / "llm_adapters" / f"{adapter_id}.json"
        )
        return adapter.definition

    @app.put("/api/v1/global/adapters/{adapter_id}")
    async def put_global_adapter(
        adapter_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if payload.get("adapter_id") != adapter_id:
            raise UsageError("URL 与 Adapter ID 不一致")
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=app_root / "llm_adapters",
            prefix=".adapter.",
            suffix=".json",
            delete=False,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            temporary = Path(handle.name)
        try:
            adapter = load_json_adapter(temporary)
            atomic_write_json(
                app_root / "llm_adapters" / f"{adapter_id}.json", payload
            )
        finally:
            temporary.unlink(missing_ok=True)
        return {"saved": True, "digest": adapter.digest}

    @app.get("/api/v1/global/adapters/{adapter_id}/preview")
    async def adapter_preview(adapter_id: str) -> dict[str, Any]:
        adapter = load_json_adapter(
            app_root / "llm_adapters" / f"{adapter_id}.json"
        )
        headers, body = adapter.build_request(
            api_key="***",
            model="model",
            messages=[{"role": "user", "content": "…"}],
            temperature=0.2,
            max_output_tokens=4096,
            stream=False,
        )
        return {"headers": headers, "body": body}

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

    @app.get("/api/v1/diagnostics")
    async def diagnostics(
        level: str | None = None,
        project: str | None = None,
        stage: str | None = None,
        q: str | None = None,
    ) -> dict[str, Any]:
        try:
            return app.state.diagnostics.snapshot(
                level=level or None,
                project=project or None,
                stage=stage or None,
                query=q or None,
            )
        except ValueError as exc:
            raise UsageError(str(exc)) from exc

    @app.get("/api/v1/diagnostics/requests/{request_id}")
    async def diagnostic_request(request_id: str) -> dict[str, Any]:
        try:
            return app.state.diagnostics.request_detail(request_id)
        except ValueError as exc:
            raise UsageError(str(exc)) from exc

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
        file_ids = payload.get("file_ids")
        if file_ids is not None and (
            not isinstance(file_ids, list)
            or not all(isinstance(value, str) and value for value in file_ids)
        ):
            raise UsageError("file_ids 必须是字符串数组或 null")
        with project_write_lock(root):
            return export_project(
                root,
                str(payload.get("stage", "")),
                bilingual=bool(payload.get("bilingual", False)),
                allow_missing=bool(payload.get("allow_missing", False)),
                output_format=str(payload.get("format", "original")),
                file_ids=file_ids,
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
                app_root=app_root,
                interactive=True,
                choice=choice,
            )
        return {"warnings": warnings}

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
