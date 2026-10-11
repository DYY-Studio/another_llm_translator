"""Decision prompt editing and named templates for installed validators."""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
from pathlib import Path
from typing import Callable

from fastapi import FastAPI

from .config import dump_config, load_config
from .decision_prompt import get_declaration, prompt_declarations, prompt_path, read_content, resolve_prompt, validate_content
from .errors import UsageError
from .locking import project_write_lock
from .plugins import resolve_translation_validators
from .prompt_library import validate_prompt_library_id
from .sqlite_storage import atomic_write_json, atomic_write_text
from .user_config import effective_path, user_root


def register_decision_prompt_routes(app: FastAPI, project: Callable[[str], Path], app_root: Path) -> None:
    def config_path(root: Path | None) -> Path:
        return root / "config.toml" if root is not None else effective_path("config/config.toml", builtin_root=app_root)

    def selection(validator_id: str, root: Path | None) -> str:
        languages = load_config(config_path(root))["decision"]["prompt_languages"]
        if validator_id in languages:
            return languages[validator_id]
        if root is not None:
            return load_config(config_path(None))["decision"]["prompt_languages"].get(validator_id, "en")
        return "en"

    def view(validator_id: str, language: str | None, root: Path | None) -> dict:
        declaration = get_declaration(validator_id)
        selected = selection(validator_id, root)
        language = language or selected
        content, source = resolve_prompt(declaration, language, root)
        return {"content": content, "language": language, "languages": list(declaration.defaults), "choice_ids": list(declaration.choice_ids),
                "source": source, "inherited": root is not None and source != "project"}

    def save(validator_id: str, payload: dict, root: Path | None) -> dict:
        declaration = get_declaration(validator_id)
        language = payload.get("language")
        if not isinstance(language, str) or language not in declaration.defaults:
            raise UsageError("Decision 提示词语言无效")
        content = validate_content(payload.get("content"), declaration.choice_ids)
        with project_write_lock(root) if root is not None else nullcontext():
            atomic_write_json(prompt_path(root or user_root(), validator_id, language), content)
        return {"saved": True}

    def select_language(validator_id: str, payload: dict, root: Path | None) -> dict:
        declaration = get_declaration(validator_id)
        language = payload.get("language")
        if not isinstance(language, str) or language not in declaration.defaults:
            raise UsageError("Decision 提示词语言无效")
        with project_write_lock(root) if root is not None else nullcontext():
            config = load_config(config_path(root))
            config["decision"]["prompt_languages"][validator_id] = language
            atomic_write_text(root / "config.toml" if root is not None else user_root() / "config/config.toml", dump_config(config))
        return {"saved": True}

    @app.get("/api/v1/decision-prompts")
    async def list_prompts() -> dict:
        summaries = {value["validator_id"]: value for _, value in resolve_translation_validators()}
        return {"prompts": [{"validator_id": key, "label": summaries[key]["label"], "languages": list(value.defaults),
                             "choice_ids": list(value.choice_ids)} for key, value in prompt_declarations().items()]}

    def language_view(validator_id: str, root: Path | None) -> dict:
        declaration = get_declaration(validator_id)
        return {"language": selection(validator_id, root), "languages": list(declaration.defaults)}

    @app.get("/api/v1/global/decision-prompts/{validator_id}/language")
    async def get_global_language(validator_id: str) -> dict:
        return language_view(validator_id, None)

    @app.get("/api/v1/projects/{name}/decision-prompts/{validator_id}/language")
    async def get_project_language(name: str, validator_id: str) -> dict:
        return language_view(validator_id, project(name))

    @app.get("/api/v1/decision-prompts/{validator_id}/default")
    async def get_default(validator_id: str, language: str) -> dict:
        declaration = get_declaration(validator_id)
        if not isinstance(language, str) or language not in declaration.defaults:
            raise UsageError("Decision 提示词语言无效")
        return {"content": validate_content(declaration.defaults[language], declaration.choice_ids), "choice_ids": list(declaration.choice_ids)}

    @app.get("/api/v1/global/decision-prompts/{validator_id}")
    async def get_global(validator_id: str, language: str | None = None) -> dict:
        return view(validator_id, language, None)

    @app.put("/api/v1/global/decision-prompts/{validator_id}")
    async def save_global(validator_id: str, payload: dict) -> dict:
        return save(validator_id, payload, None)

    @app.put("/api/v1/global/decision-prompts/{validator_id}/language")
    async def select_global(validator_id: str, payload: dict) -> dict:
        return select_language(validator_id, payload, None)

    @app.get("/api/v1/projects/{name}/decision-prompts/{validator_id}")
    async def get_project(name: str, validator_id: str, language: str | None = None) -> dict:
        return view(validator_id, language, project(name))

    @app.put("/api/v1/projects/{name}/decision-prompts/{validator_id}")
    async def save_project(name: str, validator_id: str, payload: dict) -> dict:
        return save(validator_id, payload, project(name))

    @app.put("/api/v1/projects/{name}/decision-prompts/{validator_id}/language")
    async def select_project(name: str, validator_id: str, payload: dict) -> dict:
        return select_language(validator_id, payload, project(name))

    @app.delete("/api/v1/projects/{name}/decision-prompts/{validator_id}")
    async def restore_project(name: str, validator_id: str, language: str) -> dict:
        declaration = get_declaration(validator_id)
        if not isinstance(language, str) or language not in declaration.defaults:
            raise UsageError("Decision 提示词语言无效")
        root = project(name)
        with project_write_lock(root):
            prompt_path(root, validator_id, language).unlink(missing_ok=True)
        return {"saved": True}

    def library_path(validator_id: str, language: str, prompt_id: str | None = None) -> Path:
        declaration = get_declaration(validator_id)
        if not isinstance(language, str) or language not in declaration.defaults:
            raise UsageError("Decision 提示词语言无效")
        path = user_root() / "prompt_library" / "decision" / validator_id / language
        if prompt_id is not None:
            path /= validate_prompt_library_id(prompt_id) + ".json"
        for parent in [path, *path.parents]:
            if parent.is_symlink():
                raise UsageError("Prompt 仓库路径不能是符号链接")
            if parent == user_root():
                break
        return path

    @app.get("/api/v1/decision-prompt-library/{validator_id}/{language}")
    async def list_library(validator_id: str, language: str) -> dict:
        directory = library_path(validator_id, language)
        entries = []
        for path in sorted(directory.glob("*.json")):
            if path.is_file() and not path.is_symlink():
                entries.append({"id": path.stem, "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()})
        return {"entries": entries}

    @app.get("/api/v1/decision-prompt-library/{validator_id}/{language}/{prompt_id}")
    async def get_library(validator_id: str, language: str, prompt_id: str) -> dict:
        content = read_content(library_path(validator_id, language, prompt_id), get_declaration(validator_id).choice_ids)
        return {"id": prompt_id, "content": content}

    @app.put("/api/v1/decision-prompt-library/{validator_id}/{language}/{prompt_id}")
    async def save_library(validator_id: str, language: str, prompt_id: str, payload: dict) -> dict:
        content = validate_content(payload.get("content"), get_declaration(validator_id).choice_ids)
        atomic_write_json(library_path(validator_id, language, prompt_id), content)
        return {"saved": True}

    @app.delete("/api/v1/decision-prompt-library/{validator_id}/{language}/{prompt_id}")
    async def delete_library(validator_id: str, language: str, prompt_id: str) -> dict:
        library_path(validator_id, language, prompt_id).unlink(missing_ok=True)
        return {"deleted": True}
