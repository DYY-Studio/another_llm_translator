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


_PROTOCOL = {
    "zh-CN": (
        "以下固定输出协议优先于可编辑中段。terms[] 是唯一决策目标；每项必须恰好输出一条 "
        "decision 并逐字照录 normalized。anchors[]、evidence、conflicts、source、disabled、第一阶段 "
        "action/reason 均为只读数据，不得输出 anchor 决策。conflicts 是去重后的历史候选和关系"
        "争用证据，不是投票结果或可选值白名单；可以依据全文证据提出新值。第一阶段存在 category "
        "或 preferred_translation 冲突时不得 keep；update 必须为每个冲突字段提供非空决议，无法"
        "可靠决定时使用 needs_review。evidence.hit_count 是命中 Segment 数，不是字符出现次数；"
        "samples 最多五条，先覆盖不同 (file_id, part_id) 内容边界，再按源文顺序补充不同 Segment。"
        "boundary_ref 是只读的请求内内容边界引用；相同编号表示样本来自同一内容边界，不是全局 ID、"
        "顺序或权重。每条记录必须有非空字符串 "
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
        "action/reason are read-only; never output an anchor decision. conflicts contains deduplicated "
        "historical candidates and relationship disputes, not vote totals or an allowed-value whitelist; "
        "you may propose a new value when the work-wide evidence supports it. In phase one, a term with "
        "category or preferred_translation conflicts must not use keep; update must provide a non-empty "
        "decision for every conflicted scalar field, or use needs_review when evidence is insufficient. "
        "evidence.hit_count is the number of matching Segments, not substring occurrences. evidence.samples contains "
        "at most five distinct Segments, prioritizing first hits from different (file_id, part_id) content "
        "boundaries before source-order fill. boundary_ref is a read-only, request-local content-boundary "
        "reference: equal values mean the samples share a boundary, not a global ID, ordering, or weight. "
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
        "errors": errors,
        "previous_invalid_records": previous_invalid_records,
        "accepted_normalized": accepted_normalized,
        "target_normalized": target_normalized,
    }
