"""Plugin-owned Decision prompt declarations and strict content validation."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError


@dataclass(frozen=True)
class DecisionPromptDeclaration:
    validator_id: str
    choice_ids: tuple[str, ...]
    defaults: dict[str, dict]


def validate_content(content: object, choice_ids: tuple[str, ...]) -> dict:
    if not isinstance(content, dict) or set(content) != {"instructions", "criteria"}:
        raise ConfigError("Decision 提示词必须包含 instructions 和 criteria")
    instructions = content["instructions"]
    criteria = content["criteria"]
    if not isinstance(instructions, str) or not instructions.strip():
        raise ConfigError("Decision 判断说明不能为空")
    if not isinstance(criteria, dict) or set(criteria) != set(choice_ids):
        raise ConfigError("Decision 选项必须与插件声明一致")
    if any(not isinstance(value, str) or not value.strip() for value in criteria.values()):
        raise ConfigError("Decision 选项描述不能为空")
    return {"instructions": instructions, "criteria": {key: criteria[key] for key in choice_ids}}


def validate_declaration(prompt: object) -> None:
    if (not isinstance(prompt, DecisionPromptDeclaration)
        or not isinstance(prompt.validator_id, str)
        or not re.fullmatch(r"[a-z][a-z0-9_-]*", prompt.validator_id)
        or not isinstance(prompt.choice_ids, tuple)
        or not 2 <= len(prompt.choice_ids) <= 255
        or any(not isinstance(choice, str) or not choice.strip() for choice in prompt.choice_ids)
        or len(set(prompt.choice_ids)) != len(prompt.choice_ids)
        or not isinstance(prompt.defaults, dict)
        or "en" not in prompt.defaults
        or not set(prompt.defaults) <= {"en", "zh-CN"}):
        raise ConfigError("Decision 提示词声明无效")
    for content in prompt.defaults.values():
        validate_content(content, prompt.choice_ids)


def declaration_from_files(validator_id: str, choices: tuple[str, ...], directory: Path) -> DecisionPromptDeclaration:
    defaults = {}
    for path in sorted(directory.glob("*.json")):
        try:
            defaults[path.stem] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"无法读取 Decision 默认提示词：{path}") from exc
    return DecisionPromptDeclaration(validator_id, choices, defaults)


def prompt_declarations() -> dict[str, DecisionPromptDeclaration]:
    from .plugins import load_plugins
    return {prompt.validator_id: prompt for plugin in load_plugins() for prompt in plugin.decision_prompts}


def get_declaration(validator_id: str) -> DecisionPromptDeclaration:
    try:
        return prompt_declarations()[validator_id]
    except KeyError as exc:
        raise ConfigError(f"未安装可编辑 Decision 提示词：{validator_id}") from exc


def prompt_path(root: Path, validator_id: str, language: str) -> Path:
    return root / "decision_prompts" / validator_id / f"{language}.json"


def read_content(path: Path, choice_ids: tuple[str, ...]) -> dict:
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"无法读取 Decision 提示词：{path}") from exc
    return validate_content(content, choice_ids)


def resolve_prompt(declaration: DecisionPromptDeclaration, language: str, project: Path | None = None) -> tuple[dict, str]:
    from .user_config import user_root
    if language not in declaration.defaults:
        raise ConfigError(f"Decision 提示词不支持语言：{language}")
    paths = [(prompt_path(project, declaration.validator_id, language), "project")] if project is not None else []
    paths.append((prompt_path(user_root(), declaration.validator_id, language), "global"))
    for path, source in paths:
        if path.exists() or path.is_symlink():
            return read_content(path, declaration.choice_ids), source
    return validate_content(declaration.defaults[language], declaration.choice_ids), "default"


def resolve_run_prompts(config: dict, *, project: Path | None = None, global_languages: dict | None = None, snapshot: Path | None = None) -> None:
    definitions = {}
    declarations = prompt_declarations()
    options = config["validation"]["translation"]
    active = set(options["validators"])
    if not options["decision_enabled"]:
        active.discard("preferred_term_usage")
    for validator_id in sorted(active & declarations.keys()):
        declaration = declarations[validator_id]
        if snapshot is not None:
            path = snapshot / "decision_prompts" / f"{validator_id}.json"
            try:
                definition = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ConfigError(f"无法读取 Decision 提示词快照：{validator_id}") from exc
            if not isinstance(definition, dict) or set(definition) != {"language", "choice_ids", "content"}:
                raise ConfigError("Decision 提示词快照无效")
            if definition["language"] not in declaration.defaults or definition["choice_ids"] != list(declaration.choice_ids):
                raise ConfigError("Decision 提示词快照与插件声明不一致")
            content = validate_content(definition["content"], declaration.choice_ids)
            language = definition["language"]
        else:
            language = config["decision"]["prompt_languages"].get(validator_id, (global_languages or {}).get(validator_id, "en"))
            content, _ = resolve_prompt(declaration, language, project)
        definitions[validator_id] = {"language": language, "choice_ids": list(declaration.choice_ids), "content": content}
    config["_decision_prompt_definitions"] = definitions
