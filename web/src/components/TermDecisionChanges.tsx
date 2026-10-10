import { translate, type Language } from "../i18n";
import { decisionAliasChanges, decisionProposalChanges, decisionRelationshipRole } from "../termDecision";
import type { TermDecisionState } from "../types";

function valueOrDash(value: string) {
  return value || "—";
}

function fieldLabel(field: string, language: Language) {
  const keys: Record<string, string> = {
    preferred_translation: "terms.decisionFieldTranslation",
    category: "terms.decisionFieldCategory",
    description: "terms.decisionFieldDescription",
    group_primary: "terms.decisionFieldGroup",
    disabled: "terms.decisionFieldStatus",
  };
  return translate(keys[field] ?? field, language);
}

function groupValue(state: TermDecisionState, states: TermDecisionState[], language: Language) {
  if (!state.group_primary) return translate("terms.decisionStandalone", language);
  const primary = states.find((item) => item.normalized === state.group_primary);
  return translate("terms.decisionMemberOf", language, { source: primary?.source ?? state.group_primary });
}

function changeValue(field: string, raw: string, state: TermDecisionState, states: TermDecisionState[], language: Language) {
  if (field === "group_primary") return raw ? groupValue(state, states, language) : translate("terms.decisionStandalone", language);
  if (field === "disabled") return raw ? translate("terms.decisionDisabledState", language) : translate("terms.decisionEnabledState", language);
  return valueOrDash(raw);
}

function StateDetails({ title, state, states, language }: { title: string; state: TermDecisionState; states: TermDecisionState[]; language: Language }) {
  return <details className="decision-state-details">
    <summary>{title}</summary>
    <dl>
      <div><dt>{translate("terms.decisionFieldTranslation", language)}</dt><dd>{valueOrDash(state.preferred_translation ?? "")}</dd></div>
      <div><dt>{translate("terms.decisionFieldCategory", language)}</dt><dd>{valueOrDash(state.category ?? "")}</dd></div>
      <div><dt>{translate("terms.decisionFieldDescription", language)}</dt><dd>{valueOrDash(state.description ?? "")}</dd></div>
      <div><dt>{translate("terms.decisionFieldGroup", language)}</dt><dd>{groupValue(state, states, language)}</dd></div>
      <div><dt>{translate("terms.decisionFieldAliases", language)}</dt><dd>{state.aliases.length ? state.aliases.join(" · ") : "—"}</dd></div>
      <div><dt>{translate("terms.decisionFieldStatus", language)}</dt><dd>{state.disabled ? translate("terms.decisionDisabledState", language) : translate("terms.decisionEnabledState", language)}</dd></div>
    </dl>
  </details>;
}

export function TermDecisionChanges({ beforeStates, afterStates, kind = "term_update", language, focusNormalized }: {
  beforeStates: TermDecisionState[]; afterStates: TermDecisionState[]; kind?: "term_update" | "relationship"; language: Language; focusNormalized?: string;
}) {
  const changes = beforeStates.flatMap((before, index) => {
    if (focusNormalized && before.normalized !== focusNormalized) return [];
    const after = afterStates[index];
    if (!after) return [];
    return {
      before,
      after,
      fields: decisionProposalChanges(before, after),
      aliases: decisionAliasChanges(before, after),
      role: kind === "relationship" ? decisionRelationshipRole(after, afterStates) : null,
    };
  }).sort((left, right) => {
    if (kind !== "relationship") return 0;
    const roleOrder = { primary: 0, member: 1 } as const;
    return (roleOrder[left.role ?? "member"] ?? 2) - (roleOrder[right.role ?? "member"] ?? 2);
  });
  return (
    <div className="decision-change-list">{changes.map(({ before, after, fields, aliases, role }) => <div className="decision-term-change" key={before.normalized}>
      <div className="decision-term-change-heading"><div><strong>{before.source}</strong>{role && <span className={`decision-relation-role ${role}`}>{translate(role === "primary" ? "terms.groupPrimary" : "terms.groupMember", language)}</span>}</div><span>{after.disabled ? translate("terms.decisionDisabledState", language) : translate("terms.decisionEnabledState", language)}</span></div>
      {fields.map((change) => <div className="decision-field-change" key={change.field}>
        <span className="decision-field-label">{fieldLabel(change.field, language)}</span>
        <span className="decision-old-value">{changeValue(change.field, change.before, before, beforeStates, language)}</span>
        <span className="decision-arrow">→</span>
        <span className="decision-new-value">{changeValue(change.field, change.after, after, afterStates, language)}</span>
      </div>)}
      {(aliases.added.length > 0 || aliases.removed.length > 0) && <div className="decision-alias-change"><span className="decision-field-label">{translate("terms.decisionFieldAliases", language)}</span><div>{aliases.removed.map((alias) => <span className="alias-chip removed" key={`removed:${alias}`}>− {alias}</span>)}{aliases.added.map((alias) => <span className="alias-chip added" key={`added:${alias}`}>+ {alias}</span>)}</div></div>}
      {fields.length === 0 && aliases.added.length === 0 && aliases.removed.length === 0 && <span className="decision-unchanged">{translate("terms.decisionNoVisibleChanges", language)}</span>}
      <div className="decision-state-pair"><StateDetails title={translate("terms.decisionBefore", language)} state={before} states={beforeStates} language={language} /><StateDetails title={translate("terms.decisionAfter", language)} state={after} states={afterStates} language={language} /></div>
    </div>)}</div>
  );
}
