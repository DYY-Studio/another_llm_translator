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
