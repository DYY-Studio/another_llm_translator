"""Decision prompt editing and named templates for installed validators."""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
from pathlib import Path
from typing import Callable

from fastapi import FastAPI

from .decision_prompt import get_declaration, prompt_declarations, prompt_path, read_content, resolve_prompt, validate_content
from .errors import ConfigError, UsageError
from .locking import project_write_lock
from .plugins import resolve_translation_validators
from .prompt_library import validate_prompt_library_id
from .sqlite_storage import atomic_write_json
from .user_config import user_root


def register_decision_prompt_routes(app: FastAPI, project: Callable[[str], Path]) -> None:
    def view(validator_id: str, root: Path | None) -> dict:
        declaration = get_declaration(validator_id)
        content, source = resolve_prompt(declaration, root)
        result = {"content": content, "choice_ids": list(declaration.choice_ids), "inherited": root is not None and source != "project"}
        if root is not None:
            try:
                global_content, _ = resolve_prompt(declaration)
                result["global_sync"] = {"available": True, "same": content == global_content}
            except ConfigError as exc:
                result["global_sync"] = {"available": False, "same": False, "error": str(exc)}
        return result

    def payload_content(validator_id: str, payload: dict) -> dict:
        if set(payload) != {"content"}:
            raise UsageError("Decision 提示词请求只允许 content")
        return validate_content(payload["content"], get_declaration(validator_id).choice_ids)

    def save(validator_id: str, payload: dict, root: Path | None) -> dict:
        content = payload_content(validator_id, payload)
        with project_write_lock(root) if root is not None else nullcontext():
            atomic_write_json(prompt_path(root or user_root(), validator_id), content)
        return {"saved": True}

    @app.get("/api/v1/decision-prompts")
    async def list_prompts() -> dict:
        summaries = {value["validator_id"]: value for _, value in resolve_translation_validators()}
        return {"prompts": [{"validator_id": key, "label": summaries[key]["label"], "choice_ids": list(value.choice_ids)} for key, value in prompt_declarations().items()]}

    @app.get("/api/v1/decision-prompts/{validator_id}/default")
    async def get_default(validator_id: str) -> dict:
        declaration = get_declaration(validator_id)
        return {"content": validate_content(declaration.default, declaration.choice_ids), "choice_ids": list(declaration.choice_ids)}

    @app.get("/api/v1/global/decision-prompts/{validator_id}")
    async def get_global(validator_id: str) -> dict:
        return view(validator_id, None)

    @app.put("/api/v1/global/decision-prompts/{validator_id}")
    async def save_global(validator_id: str, payload: dict) -> dict:
        return save(validator_id, payload, None)

    @app.get("/api/v1/projects/{name}/decision-prompts/{validator_id}")
    async def get_project(name: str, validator_id: str) -> dict:
        return view(validator_id, project(name))

    @app.put("/api/v1/projects/{name}/decision-prompts/{validator_id}")
    async def save_project(name: str, validator_id: str, payload: dict) -> dict:
        return save(validator_id, payload, project(name))

    @app.delete("/api/v1/projects/{name}/decision-prompts/{validator_id}")
    async def restore_project(name: str, validator_id: str) -> dict:
        get_declaration(validator_id)
        root = project(name)
        with project_write_lock(root):
            prompt_path(root, validator_id).unlink(missing_ok=True)
        return {"saved": True}

    def library_path(validator_id: str, prompt_id: str | None = None) -> Path:
        get_declaration(validator_id)
        path = user_root() / "prompt_library" / "decision" / validator_id
        if prompt_id is not None:
            path /= validate_prompt_library_id(prompt_id) + ".json"
        for parent in [path, *path.parents]:
            if parent.is_symlink():
                raise UsageError("Prompt 仓库路径不能是符号链接")
            if parent == user_root():
                break
        return path

    @app.get("/api/v1/decision-prompt-library/{validator_id}")
    async def list_library(validator_id: str) -> dict:
        directory = library_path(validator_id)
        entries = [{"id": path.stem, "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}
                   for path in sorted(directory.glob("*.json")) if path.is_file() and not path.is_symlink()]
        return {"entries": entries}

    @app.get("/api/v1/decision-prompt-library/{validator_id}/{prompt_id}")
    async def get_library(validator_id: str, prompt_id: str) -> dict:
        return {"id": prompt_id, "content": read_content(library_path(validator_id, prompt_id), get_declaration(validator_id).choice_ids)}

    @app.put("/api/v1/decision-prompt-library/{validator_id}/{prompt_id}")
    async def save_library(validator_id: str, prompt_id: str, payload: dict) -> dict:
        content = payload_content(validator_id, payload)
        atomic_write_json(library_path(validator_id, prompt_id), content)
        return {"saved": True}

    @app.delete("/api/v1/decision-prompt-library/{validator_id}/{prompt_id}")
    async def delete_library(validator_id: str, prompt_id: str) -> dict:
        library_path(validator_id, prompt_id).unlink(missing_ok=True)
        return {"deleted": True}
