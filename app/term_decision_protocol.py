from __future__ import annotations

from typing import Any

DECISION_RULES_VERSION = 7
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

_JSONL_RETRY_CATEGORY = {
    "invalid_json": "record_json",
    "non_object": "record_object",
    "after_end": "end_record",
    "invalid_end": "end_record",
    "unknown_type": "record_type",
    "missing_end": "end_record",
}

_JSONL_RETRY_GUIDANCE = {
    "zh-CN": {
        "record_json": (
            "每个非空物理行都必须是合法 JSON 对象，且一个对象不能跨行。"
        ),
        "record_object": (
            "每个非空物理行都必须是 JSON 对象，不要输出数组、字符串或其他 JSON 值。"
        ),
        "end_record": (
            '最终记录必须且只能是精确的 {"type":"end"}，end 之后不得再有任何记录。'
        ),
        "record_type": "只输出协议允许的 decision 记录和最终 end 记录。",
    },
    "en": {
        "record_json": (
            "Each non-empty physical line must be one valid JSON object, and an "
            "object must not span lines."
        ),
        "record_object": (
            "Each non-empty physical line must be a JSON object; do not output an "
            "array, string, or other JSON value."
        ),
        "end_record": (
            'The final record must be exactly {"type":"end"}, and no record may '
            "follow it."
        ),
        "record_type": (
            "Only output protocol-allowed decision records and the final end record."
        ),
    },
}

_SEMANTIC_RETRY_GUIDANCE = {
    "zh-CN": {
        "invalid_document": "请修正决策 JSONL 记录并严格遵守固定字段协议。",
        "unknown_record": "normalized 必须来自 target_normalized；不要输出目标范围外的术语。",
        "duplicate_record": "每个 normalized 只能输出一条 decision 记录。",
        "missing_record": "必须为 target_normalized 中的每个术语输出一条 decision 记录。",
        "invalid_action": "action 只能是 keep、update、disable 或 needs_review。",
        "invalid_reason": "reason 必须是非空字符串。",
        "invalid_fields": "严格遵守当前 action 允许的字段集合，不要缺少或添加字段。",
        "unresolved_conflict": "必须明确解决冲突字段；证据不足时使用 needs_review。",
        "invalid_patch": "changes 必须是只包含允许字段的 Patch 对象。",
        "empty_patch": "除协议允许的例外外，update 的 changes 不能为空。",
        "invalid_patch_value": "Patch 字段必须使用协议规定的字符串、null 或字符串数组值。",
        "invalid_aliases": "aliases 必须是可见、非空且不重复的源文或 alias。",
        "invisible_alias": "aliases 只能使用请求中可见的 source 或 alias 原文。",
        "invisible_group_primary": "group_primary 只能指向请求中可见且有效的根术语，或使用 null。",
        "self_alias": "aliases 不得包含当前术语自身的 source 或 normalized。",
        "no_op_patch": "changes 必须实际修改术语状态。",
        "invalid_relationship": "请修正 alias 与 group_primary 关系，避免自指、成员指向、禁用目标、未知目标或循环。",
    },
    "en": {
        "invalid_document": "Correct the decision JSONL records and follow the fixed field contract.",
        "unknown_record": "normalized must be one of the terms in target_normalized; do not output an out-of-scope term.",
        "duplicate_record": "Output exactly one decision record for each normalized value.",
        "missing_record": "Output one decision record for every term in target_normalized.",
        "invalid_action": "action must be keep, update, disable, or needs_review.",
        "invalid_reason": "reason must be a non-empty string.",
        "invalid_fields": "Follow the field set allowed for this action exactly; do not omit or add fields.",
        "unresolved_conflict": "Resolve every conflicted field explicitly; use needs_review when evidence is insufficient.",
        "invalid_patch": "changes must be a Patch object containing only allowed fields.",
        "empty_patch": "Except for the protocol's allowed cases, update changes must not be empty.",
        "invalid_patch_value": "Patch fields must use the protocol's specified string, null, or string-array values.",
        "invalid_aliases": "aliases must be visible, non-empty, and non-duplicated source or alias spellings.",
        "invisible_alias": "aliases may use only source or alias spellings visible in this request.",
        "invisible_group_primary": "group_primary may name only a visible valid root term, or be null.",
        "self_alias": "aliases must not contain the current term's source or normalized value.",
        "no_op_patch": "changes must make an actual change to the term state.",
        "invalid_relationship": "Fix alias and group_primary relationships; avoid self-reference, member targets, disabled or unknown targets, and cycles.",
    },
}


_PROTOCOL = {
    "zh-CN": (
        "以下固定输出协议优先于可编辑中段。terms[] 是唯一决策目标；每项必须恰好输出一条 "
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
        "禁止自指、指向 disabled 术语、成员指向成员以及任何链或循环。update 会重新启用术语；"
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
        "The following fixed output contract takes precedence over the editable middle. "
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
        "disabled term or another member, or form a chain or cycle. update enables the term. Empty changes "
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


def _retry_errors(errors: list[dict[str, Any]], language: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen_document_categories: set[str] = set()
    guidance = _SEMANTIC_RETRY_GUIDANCE[language]
    for error in errors:
        document_code = error.get("_document_error_code")
        if isinstance(document_code, str):
            category = _JSONL_RETRY_CATEGORY[document_code]
            if category in seen_document_categories:
                continue
            seen_document_categories.add(category)
            result.append({"message": _JSONL_RETRY_GUIDANCE[language][category]})
            continue
        retry_error = {
            key: value
            for key, value in error.items()
            if not key.startswith("_") and key != "message"
        }
        code = retry_error.get("code")
        retry_error["message"] = guidance.get(
            code if isinstance(code, str) else "invalid_document",
            guidance["invalid_document"],
        )
        result.append(retry_error)
    return result


def format_correction(
    *,
    language: str,
    errors: list[dict[str, Any]],
    previous_invalid_records: list[dict[str, Any]],
    accepted_normalized: list[str],
    target_normalized: list[str],
) -> dict[str, Any]:
    instruction = (
        "只修正 target_normalized 中的未决术语；不要再次输出 accepted_normalized。严格遵守固定 Patch 协议并以精确 end 结束。"
        if language == "zh-CN"
        else "Correct only unresolved terms in target_normalized; do not output accepted_normalized again. Follow the fixed Patch contract and finish with the exact end record."
    )
    return {
        "instruction": instruction,
        "errors": _retry_errors(errors, language),
        "previous_invalid_records": previous_invalid_records,
        "accepted_normalized": accepted_normalized,
        "target_normalized": target_normalized,
    }
