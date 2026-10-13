from __future__ import annotations

from typing import Any

DECISION_RULES_VERSION = 11
DECISION_ACTIONS = frozenset({"keep", "update", "disable", "needs_review"})
SIMPLE_ACTION_KEYS = frozenset({"type", "normalized", "action", "reason"})
PATCH_FIELDS = frozenset(
    {
        "category",
        "preferred_translation",
        "aliases",
        "group_primary",
    }
)
UPDATE_ACTION_KEYS = SIMPLE_ACTION_KEYS | {"changes"}

_COMMON_PROTOCOL = {
    "zh-CN": (
        "必须遵守以下输出协议。terms[] 是唯一决策目标；每项必须恰好输出一条 "
        "decision 并逐字照录 normalized。anchors[]、evidence、conflicts、source、disabled、第一阶段 "
        "action/reason 均为只读数据，不得输出 anchor 决策。每条记录必须有非空字符串 "
        "reason。keep、disable、needs_review 必须且只能含 type、normalized、action、reason。"
        "update 必须且只能含 type、normalized、action、reason、changes；changes 是 Patch，"
        "只能包含 category、preferred_translation、aliases、group_primary 中实际"
        "需要修改的键。category、preferred_translation、group_primary 为字符串或 "
        "JSON null；aliases 为字符串数组。description 是只读说明。aliases 只能使用本次 terms[]/anchors[] 中可见的 source/alias 原文，不得虚构"
        "或重复。group_primary "
        "只能为 null 或本次可见、启用且自身 group_primary=null 的根术语 normalized。"
        "禁止自指、指向 disabled 术语、成员指向成员以及任何链或循环。"
        "主术语 aliases 不得与启用组成员的 source 或 aliases 规范化后相同；可保留成员并移除主术语重复 alias，"
        "或禁用成员并保留主术语 alias。无法完整解决时使用 needs_review。update 会重新启用术语；"
        "changes 中填写的字段必须相对于本次 terms 中的当前状态产生实际变化。"
        "无法完整表达同组 alias 或组关系变更时使用 needs_review。示例："
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
        "action/reason are read-only; never output an anchor decision. "
        "Every record requires a non-empty string "
        "reason. keep, disable, and needs_review contain exactly type, normalized, action, reason. "
        "update contains exactly type, normalized, action, reason, changes. changes is a Patch and "
        "may contain only fields actually changed from category, preferred_translation, "
        "aliases, and group_primary. Nullable scalar fields use strings or JSON null; aliases is a "
        "string array. description is read-only context. aliases may use "
        "only source/alias spellings visible in this request. group_primary is null or the normalized "
        "of a visible enabled root whose group_primary is null. It must not self-reference, target a "
        "disabled term or another member, or form a chain or cycle. Root aliases must not equal the normalized source "
        "or aliases of active group members. Keep the member and remove the overlapping root alias, or disable "
        "the member and keep the root alias; use needs_review when the complete change cannot be expressed. "
        "update enables the term. Fields in changes must make an actual change relative to the "
        "current state in this request's terms. Use needs_review when a complete related change "
        "cannot be expressed. Example: "
        '{"type":"decision","normalized":"alice","action":"update","reason":"add translation",'
        '"changes":{"preferred_translation":"Alice"}}\n'
        '{"type":"decision","normalized":"academy","action":"keep","reason":"keep current decision"}\n'
        '{"type":"decision","normalized":"incidental","action":"disable","reason":"ordinary word"}\n'
        '{"type":"decision","normalized":"uncertain","action":"needs_review","reason":"insufficient evidence"}\n'
        '{"type":"end"}'
    ),
}


_PHASE_PROTOCOL = {
    "adjudication": {
        "zh-CN": (
            "第一阶段存在 category 或 preferred_translation 冲突时不得 keep；"
            "update 必须为每个冲突字段提供非空决议，无法可靠决定时使用 needs_review。"
            "空 changes 仅用于重新启用当前 disabled 术语。"
        ),
        "en": (
            "In phase one, a term with category or preferred_translation conflicts must not use keep; "
            "update must provide a non-empty decision for every conflicted scalar field, or use "
            "needs_review when evidence is insufficient. Empty changes are allowed only to re-enable "
            "a currently disabled term."
        ),
    },
    "consistency": {
        "zh-CN": (
            "第二阶段 conflicts 中的类别和推荐译名候选是历史证据。"
            "确认第一阶段已裁决的当前状态时使用 keep，不要求重复写入冲突字段。"
            "keep 保留上一阶段有效裁决；第二阶段只有显式 update、disable、needs_review 才覆盖第一阶段。"
            "空 changes 仅用于明确解决第一阶段 needs_review，或重新启用当前 disabled 术语。"
        ),
        "en": (
            "In phase two, category and preferred_translation candidates in conflicts are historical evidence. "
            "Use keep to confirm the current state resolved in phase one; do not rewrite conflicted fields "
            "merely to confirm them. keep preserves the prior-phase disposition; only an explicit phase-two "
            "update, disable, or needs_review overrides it. Empty changes are allowed only to resolve a prior "
            "needs_review explicitly or to re-enable a currently disabled term."
        ),
    },
    "containment": {
        "zh-CN": (
            "keep 保留此前阶段的有效裁决。keep、update、disable 均确认本次复核关系的最终状态，"
            "合理语法差异或独立译名不要求连续子串包含。update 应确认修改后的全部可见关系；"
            "无法确认时使用 needs_review。空 changes 仅用于重新启用当前 disabled 术语。"
        ),
        "en": (
            "keep preserves the effective decision from preceding phases. keep, update and disable confirm "
            "the final state of reviewed relationships; grammatical differences and independent names "
            "need not retain literal substring containment. For update, confirm all visible relationships "
            "after the change; use needs_review when unable to confirm. Empty changes are allowed "
            "only to re-enable a currently disabled term."
        ),
    },
}


_DESCRIPTION_CONTEXT = {
    "zh-CN": "description 是有效说明片段的只读拼接文本，方括号标明原术语归属；applied_description 是已应用实体说明，仅供对照。片段可能重复、存在分歧或描述不同位置的情况，须结合原文判断，不得把排列顺序当作事实发生时间。两者均不得修改，说明只能在最后的 knowledge 阶段更新。",
    "en": "description joins valid explanation fragments as read-only text; brackets identify the original term. applied_description is the applied entity explanation for comparison. Fragments may repeat, disagree or describe different source positions; assess them against source evidence and do not treat their order as event chronology. Neither field may be changed; explanations may only be updated in the final knowledge phase.",
}


def terminology_decision_protocol(language: str, *, phase: str) -> str:
    if phase == "knowledge":
        return {
            "zh-CN": '为 entities 中每个实体恰好输出一条 {"type":"entity_knowledge","entity":"对应 entity","items":[{"text":"目标语说明","refs":["K1"]}],"reason":"更新理由"} 和末行 {"type":"end"}。items 可以为空。允许实体结果乱序。每条说明必须引用对应实体 fragments 中至少一个 id，禁止跨实体引用，不得引用 current_items 或虚构引用。不修改名称、译名或分组。',
            "en": 'Output exactly one record for each entity in entities: {"type":"entity_knowledge","entity":"input entity","items":[{"text":"target-language explanation","refs":["K1"]}],"reason":"reason"}, followed by {"type":"end"}. items may be empty. Entity records may be unordered. Each item must cite at least one id from its own fragments; cross-entity refs are forbidden; never cite current_items or invent refs. Do not change names, translations or groups.',
        }[language]
    return f"{_PHASE_PROTOCOL[phase][language]}\n{_COMMON_PROTOCOL[language]}\n{_DESCRIPTION_CONTEXT[language]}"


_FINAL_REVIEW_PROTOCOL = {
    "zh-CN": (
        "必须遵守以下终审输出协议。terms[] 是唯一决策目标；每项必须恰好输出一条 decision，"
        "并逐字照录 normalized。anchors[]、evidence、windows、conflicts、source、disabled 和 prior action/reason "
        "均为只读数据，不得输出 anchor 决策。每条记录必须有非空字符串 reason。终审 action 只能是 keep、update 或 disable；"
        "keep、disable 必须且只能含 type、normalized、action、reason，update 必须且只能含 type、normalized、action、reason、changes。"
        "changes 是 Patch，只能包含 category、preferred_translation、aliases、group_primary 中实际需要修改的键；"
        "category、preferred_translation、group_primary 为字符串或 JSON null，aliases 为字符串数组。"
        "description 是只读说明。"
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
        "from category, preferred_translation, aliases, and group_primary. category, preferred_translation, and "
        "group_primary use strings or JSON null; aliases is a string array. description is read-only context. aliases may use only source/alias "
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
    return f"{_FINAL_REVIEW_PROTOCOL[language]}\n{_DESCRIPTION_CONTEXT[language]}"


_CONTENT_RETRY_GUIDANCE = {
    "zh-CN": {
        "unresolved_conflict": "对本阶段尚未裁决的类别或译名冲突给出非空决定",
        "invisible_alias": "aliases 仅使用本次 terms/anchors 中可见的 source/alias 原文",
        "invisible_group_primary": "group_primary 仅使用本次可见、启用的根术语 normalized",
        "self_alias": "aliases 排除术语自身的 source",
        "no_op_patch": "确认本次 terms 的当前状态时使用 keep；update 仅填写实际改变的字段",
        "invalid_relationship": "依据本次 terms/anchors 联合检查组关系和 alias 归属",
    },
    "en": {
        "unresolved_conflict": "Resolve this phase's pending category or translation conflicts with non-empty values.",
        "invisible_alias": "Use only source/alias spellings visible in this request's terms/anchors for aliases.",
        "invisible_group_primary": "Use only a visible, enabled root term's normalized for group_primary.",
        "self_alias": "Exclude the term's own source from aliases.",
        "no_op_patch": "Use keep to confirm the current state in terms; update only fields that actually change.",
        "invalid_relationship": "Check group relationships and alias ownership against this request's terms/anchors.",
    },
}


def validation_feedback(
    errors: list[dict[str, Any]], target_normalized: set[str], *, language: str
) -> str:
    if any(error.get("_document_error_code") for error in errors):
        return ""
    guidance = _CONTENT_RETRY_GUIDANCE[language]
    codes = sorted({
        error["code"] for error in errors
        if error.get("code") in guidance
        and error.get("normalized") in target_normalized
    })
    return "\n".join(guidance[code] for code in codes)
