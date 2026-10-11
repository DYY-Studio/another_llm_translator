from __future__ import annotations

from typing import Any

DECISION_RULES_VERSION = 9
DECISION_ACTIONS = frozenset({"keep", "update", "disable", "needs_review"})
SIMPLE_ACTION_KEYS = frozenset({"type", "normalized", "action", "reason"})
PATCH_FIELDS = frozenset(
    {
        "category",
        "description",
        "preferred_translation",
        "aliases",
        "group_primary",
    }
)
UPDATE_ACTION_KEYS = SIMPLE_ACTION_KEYS | {"changes"}

_PROTOCOL = {
    "zh-CN": (
        "必须遵守以下输出协议。terms[] 是唯一决策目标；每项必须恰好输出一条 "
        "decision 并逐字照录 normalized。anchors[]、evidence、conflicts、source、disabled、第一阶段 "
        "action/reason 均为只读数据，不得输出 anchor 决策。第一阶段存在 category "
        "或 preferred_translation 冲突时不得 keep；update 必须为每个冲突字段提供非空决议，无法"
        "可靠决定时使用 needs_review。每条记录必须有非空字符串 "
        "reason。keep、disable、needs_review 必须且只能含 type、normalized、action、reason。"
        "update 必须且只能含 type、normalized、action、reason、changes；changes 是 Patch，"
        "只能包含 category、description、preferred_translation、aliases、group_primary 中实际"
        "需要修改的键。category、description、preferred_translation、group_primary 为字符串或 "
        "JSON null；aliases 为字符串数组。description 可保持、清为 null，或改写为简洁的目标语"
        "说明；非空新说明必须由当前说明、evidence 中的源文样本或可见 anchors 支持，不得增加无"
        "证据事实。aliases 只能使用本次 terms[]/anchors[] 中可见的 source/alias 原文，不得虚构"
        "或重复。group_primary "
        "只能为 null 或本次可见、启用且自身 group_primary=null 的根术语 normalized。"
        "禁止自指、指向 disabled 术语、成员指向成员以及任何链或循环。"
        "主术语 aliases 不得与启用组成员的 source 或 aliases 规范化后相同；可保留成员并移除主术语重复 alias，"
        "或禁用成员并保留主术语 alias。无法完整解决时使用 needs_review。update 会重新启用术语；"
        "空 changes 只用于第二阶段明确解决第一阶段 needs_review，或重新启用"
        "当前 disabled 术语。其他情形至少修改一个字段。keep 保留上一阶段有效裁决；第二阶段"
        "只有显式 update、disable、needs_review 才覆盖第一阶段。无法完整表达同组 alias 或"
        "组关系变更时使用 needs_review。示例："
        '{"type":"decision","normalized":"alice","action":"update","reason":"补全译名",'
        '"changes":{"preferred_translation":"爱丽丝"}}\n'
        '{"type":"decision","normalized":"academy","action":"keep","reason":"保持当前决定"}\n'
        '{"type":"decision","normalized":"incidental","action":"disable","reason":"普通词"}\n'
        '{"type":"decision","normalized":"uncertain","action":"needs_review","reason":"证据不足"}\n'
        '{"type":"end"}'
    ),
    "en": (
        "Follow this output contract. "
        "terms[] are the only decision targets; output exactly one decision per item and copy "
        "normalized verbatim. anchors[], evidence, conflicts, source, disabled, and prior-phase "
        "action/reason are read-only; never output an anchor decision. In phase one, a term with "
        "category or preferred_translation conflicts must not use keep; update must provide a non-empty "
        "decision for every conflicted scalar field, or use needs_review when evidence is insufficient. "
        "Every record requires a non-empty string "
        "reason. keep, disable, and needs_review contain exactly type, normalized, action, reason. "
        "update contains exactly type, normalized, action, reason, changes. changes is a Patch and "
        "may contain only fields actually changed from category, description, preferred_translation, "
        "aliases, and group_primary. Nullable scalar fields use strings or JSON null; aliases is a "
        "string array. description may be retained, cleared to null, or rewritten as a concise target-language "
        "explanation. A non-empty rewrite must be supported by the current description, source samples in "
        "evidence, or visible anchors and must not add unsupported facts. aliases may use "
        "only source/alias spellings visible in this request. group_primary is null or the normalized "
        "of a visible enabled root whose group_primary is null. It must not self-reference, target a "
        "disabled term or another member, or form a chain or cycle. Root aliases must not equal the normalized source "
        "or aliases of active group members. Keep the member and remove the overlapping root alias, or disable "
        "the member and keep the root alias; use needs_review when the complete change cannot be expressed. "
        "update enables the term. Empty changes "
        "is allowed only in phase two to resolve a prior needs_review explicitly, or to re-enable a "
        "currently disabled term. keep preserves the prior-phase disposition; only an explicit phase-two "
        "update, disable, or needs_review overrides it. Use needs_review when a complete related change "
        "cannot be expressed. Example: "
        '{"type":"decision","normalized":"alice","action":"update","reason":"add translation",'
        '"changes":{"preferred_translation":"Alice"}}\n'
        '{"type":"decision","normalized":"academy","action":"keep","reason":"keep current decision"}\n'
        '{"type":"decision","normalized":"incidental","action":"disable","reason":"ordinary word"}\n'
        '{"type":"decision","normalized":"uncertain","action":"needs_review","reason":"insufficient evidence"}\n'
        '{"type":"end"}'
    ),
}


def terminology_decision_protocol(language: str) -> str:
    return _PROTOCOL[language]


_FINAL_REVIEW_PROTOCOL = {
    "zh-CN": (
        "必须遵守以下终审输出协议。terms[] 是唯一决策目标；每项必须恰好输出一条 decision，"
        "并逐字照录 normalized。anchors[]、evidence、windows、conflicts、source、disabled 和 prior action/reason "
        "均为只读数据，不得输出 anchor 决策。每条记录必须有非空字符串 reason。终审 action 只能是 keep、update 或 disable；"
        "keep、disable 必须且只能含 type、normalized、action、reason，update 必须且只能含 type、normalized、action、reason、changes。"
        "changes 是 Patch，只能包含 category、description、preferred_translation、aliases、group_primary 中实际需要修改的键；"
        "category、description、preferred_translation、group_primary 为字符串或 JSON null，aliases 为字符串数组。"
        "description 的非空改写必须由当前说明、evidence 中的源文样本或可见 anchors 支持，不得增加无证据事实。"
        "aliases 只能使用本次 terms[]/anchors[] 中可见的 source/alias 原文，不得虚构或重复。"
        "group_primary 只能为 null 或本次可见、启用且自身 group_primary=null 的根术语 normalized；禁止自指、指向 disabled 术语、"
        "成员指向成员以及任何链或循环。主术语 aliases 不得与启用组成员的 source 或 aliases 规范化后相同；"
        "可保留成员并移除主术语重复 alias，或禁用成员并保留主术语 alias。"
        "keep 表示确认当前术语状态并清除此前 needs_review，不修改术语状态。"
        "update 必须实际修改术语状态，并会重新启用术语；如果当前术语 disabled，空 changes 仅用于重新启用，其他 update 的 changes 不得为空且不得是 no-op。"
        "仍存在 category 或 preferred_translation 冲突时不得 keep；update 必须为每个冲突字段提供非空决议，disable 则通过禁用术语解决冲突。"
        "disable 表示禁用术语。证据不足时也必须在 keep、update、disable 中选择最可靠的最终决定，禁止输出 needs_review。"
        "完整 JSONL 示例："
        '{"type":"decision","normalized":"alice","action":"keep","reason":"确认当前状态"}\n'
        '{"type":"end"}'
    ),
    "en": (
        "Follow this final-review output contract. terms[] are the only decision targets; "
        "output exactly one decision per item and copy normalized verbatim. anchors[], evidence, windows, conflicts, source, disabled, "
        "and prior action/reason are read-only; never output an anchor decision. Every record requires a non-empty string reason. "
        "Final-review action may only be keep, update, or disable. keep and disable contain exactly type, normalized, action, reason; "
        "update contains exactly type, normalized, action, reason, changes. changes is a Patch and may contain only fields actually changed "
        "from category, description, preferred_translation, aliases, and group_primary. category, description, preferred_translation, and "
        "group_primary use strings or JSON null; aliases is a string array. A non-empty description rewrite must be supported by the current "
        "description, source samples in evidence, or visible anchors and must not add unsupported facts. aliases may use only source/alias "
        "spellings visible in this request. group_primary is null or the normalized of a visible enabled root whose group_primary is null; "
        "it must not self-reference, target a disabled term or another member, or form a chain or cycle. "
        "Root aliases must not equal the normalized source or aliases of active group members. Keep the member "
        "and remove the overlapping root alias, or disable the member and keep the root alias. keep confirms the current term state "
        "and clears the prior needs_review status without changing the term. update must make an actual state change and re-enables the term; "
        "if the current term is disabled, empty changes are allowed only to re-enable it. Other update changes must be non-empty and not a no-op. "
        "If category or preferred_translation conflicts remain, keep is invalid; update must provide a non-empty decision for every conflicted field, "
        "and disable resolves the conflict by disabling the term. disable disables the term. Even with limited evidence, choose the most reliable "
        "final decision among keep, update, and disable; never output needs_review. Complete JSONL example: "
        '{"type":"decision","normalized":"alice","action":"keep","reason":"confirm current state"}\n'
        '{"type":"end"}'
    ),
}


def terminology_final_review_protocol(language: str) -> str:
    return _FINAL_REVIEW_PROTOCOL[language]


_CONTENT_RETRY_ERRORS = frozenset({
    "unresolved_conflict", "invisible_alias", "invisible_group_primary",
    "self_alias", "no_op_patch", "invalid_relationship",
})


def validation_feedback(errors: list[dict[str, Any]], target_normalized: set[str]) -> str:
    if any(error.get("_document_error_code") for error in errors):
        return ""
    return ", ".join(sorted({
        error["code"] for error in errors
        if error.get("code") in _CONTENT_RETRY_ERRORS
        and error.get("normalized") in target_normalized
    }))
