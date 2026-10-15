"""Resolve explicit project LLM prompt inheritance without missing-file fallback."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import StorageError, UsageError
from .project import PROMPT_LANGUAGES, PROMPT_NAMES, PROMPT_RESOURCE_STAGES, prompt_file
from .sqlite_storage import atomic_write_text, read_project_meta_read_only, write_json
from .user_config import APP_ROOT, effective_path


def global_prompt_path(stage: str, language: str, *, builtin_root: Path = APP_ROOT) -> Path:
    if stage not in PROMPT_RESOURCE_STAGES or language not in PROMPT_LANGUAGES:
        raise UsageError("未知 Prompt 阶段或语言")
    return effective_path(f"prompts/{prompt_file(stage, language)}", builtin_root=builtin_root)


class ProjectPrompts:
    """Read inheritance metadata once for a preview, preflight, or assembly."""

    def __init__(self, project: Path, *, builtin_root: Path = APP_ROOT,
                 metadata: dict[str, Any] | None = None) -> None:
        self.project = project
        self.builtin_root = builtin_root
        self.metadata = metadata if metadata is not None else read_project_meta_read_only(project)
        names = self.metadata.get("inherited_llm_prompts", [])
        if (not isinstance(names, list) or any(not isinstance(name, str) or name not in PROMPT_NAMES for name in names)
                or len(set(names)) != len(names)):
            raise StorageError("项目 Prompt 继承声明无效")
        self.inherited = set(names)

    def path(self, stage: str, language: str) -> Path:
        if stage not in PROMPT_RESOURCE_STAGES or language not in PROMPT_LANGUAGES:
            raise UsageError("未知 Prompt 阶段或语言")
        name = prompt_file(stage, language)
        return (global_prompt_path(stage, language, builtin_root=self.builtin_root)
                if name in self.inherited else self.project / "prompts" / name)

    def languages(self, stage: str) -> list[str]:
        return [language for language in PROMPT_LANGUAGES if self.path(stage, language).is_file()]

    def read(self, stage: str, language: str) -> str:
        try:
            return self.path(stage, language).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise StorageError(f"无法读取 Prompt：{prompt_file(stage, language)}: {exc}") from exc

    def _save_inheritance(self) -> None:
        if self.inherited:
            self.metadata["inherited_llm_prompts"] = sorted(self.inherited)
        else:
            self.metadata.pop("inherited_llm_prompts", None)
        write_json(self.project, self.project / "project.json", self.metadata)

    def save(self, stage: str, language: str, content: str) -> None:
        # The caller holds the project write lock.
        name = prompt_file(stage, language)
        atomic_write_text(self.project / "prompts" / name, content)
        if name in self.inherited:
            self.inherited.remove(name)
            self._save_inheritance()

    def restore(self, stage: str, language: str) -> None:
        # Validate the selected global resource before removing the override.
        path = global_prompt_path(stage, language, builtin_root=self.builtin_root)
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise StorageError(f"无法读取全局 Prompt：{path.name}: {exc}") from exc
        if not content.strip():
            raise UsageError("全局 Prompt 不能为空")
        name = prompt_file(stage, language)
        self.inherited.add(name)
        self._save_inheritance()
        try:
            (self.project / "prompts" / name).unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError("已恢复全局继承，但无法删除旧项目 Prompt 副本") from exc
