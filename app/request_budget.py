"""Project request budgets shared by stage planners and final request checks."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import ConfigError

BUDGET_STAGES = (
    "terminology", "terminology_decision", "terminology_knowledge", "content_summary",
    "translation", "repetition_consistency", "proofreading", "polishing",
)
BUDGET_FIELDS = ("target_content_tokens", "max_input_tokens", "context_reserve_tokens")


@dataclass(frozen=True)
class RequestBudget:
    target_content_tokens: int
    max_input_tokens: int
    context_reserve_tokens: int


def resolve_request_budget(config: dict[str, Any], stage: str) -> RequestBudget:
    if stage not in BUDGET_STAGES:
        raise ConfigError(f"未知预算阶段：{stage}")
    chunking = config["chunking"]
    override = chunking["stage_overrides"].get(stage, {})
    return RequestBudget(**{key: override.get(key, chunking[key]) for key in BUDGET_FIELDS})


def request_input_limit(config: dict[str, Any], budget: RequestBudget) -> int:
    available = config["llm"]["context_window_tokens"] - budget.context_reserve_tokens
    if available <= 0:
        raise ConfigError("请求上下文预留必须小于模型上下文窗口")
    limits = [budget.max_input_tokens, available]
    itpm = config["execution"]["input_tokens_per_minute"]
    if itpm:
        limits.append(itpm)
    return min(limits)
