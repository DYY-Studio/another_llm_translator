import type {
  TermDecisionManualReviewItem,
  TermDecisionProposal,
  TermDecisionState,
} from "./types";

export type DecisionProposalStatus = "all" | "accepted" | "rejected";

export function decisionProposalChanges(
  before: TermDecisionState,
  after: TermDecisionState,
) {
  const changes: Array<{
    field: "preferred_translation" | "category" | "description" | "group_primary" | "disabled";
    before: string;
    after: string;
  }> = [];
  const values: Array<[
    typeof changes[number]["field"],
    string | null | boolean,
    string | null | boolean,
  ]> = [
    ["preferred_translation", before.preferred_translation, after.preferred_translation],
    ["category", before.category, after.category],
    ["description", before.description, after.description],
    ["group_primary", before.group_primary, after.group_primary],
    ["disabled", before.disabled, after.disabled],
  ];
  for (const [field, oldValue, newValue] of values) {
    if (oldValue === newValue) continue;
    changes.push({
      field,
      before: oldValue === null || oldValue === false ? "" : String(oldValue),
      after: newValue === null || newValue === false ? "" : String(newValue),
    });
  }
  return changes;
}

export function decisionAliasChanges(before: TermDecisionState, after: TermDecisionState) {
  const oldAliases = new Set(before.aliases);
  const newAliases = new Set(after.aliases);
  return {
    added: after.aliases.filter((value) => !oldAliases.has(value)),
    removed: before.aliases.filter((value) => !newAliases.has(value)),
  };
}

export function filterDecisionProposals(
  proposals: TermDecisionProposal[],
  query: string,
  kind: string,
  status: DecisionProposalStatus = "all",
  rejected: ReadonlySet<string> = new Set(),
) {
  const needle = query.trim().toLocaleLowerCase();
  return proposals.filter((proposal) => {
    if (kind && proposal.kind !== kind) return false;
    const isRejected = rejected.has(proposal.proposal_id);
    if (status === "rejected" && !isRejected) return false;
    if (status === "accepted" && isRejected) return false;
    if (!needle) return true;
    return [
      proposal.reason,
      ...proposal.before.flatMap((term) => [term.source, term.preferred_translation ?? ""]),
      ...proposal.after.flatMap((term) => [term.source, term.preferred_translation ?? ""]),
    ].join("\n").toLocaleLowerCase().includes(needle);
  });
}

export function filterManualReviewItems(
  items: TermDecisionManualReviewItem[],
  query: string,
  status: "all" | "open" | "resolved",
) {
  const needle = query.trim().toLocaleLowerCase();
  return items.filter((item) => {
    if (status === "open" && item.resolved) return false;
    if (status === "resolved" && !item.resolved) return false;
    if (!needle) return true;
    return [item.source, item.normalized, item.reason]
      .join("\n")
      .toLocaleLowerCase()
      .includes(needle);
  });
}

export function manualReviewProgress(items: TermDecisionManualReviewItem[]) {
  const resolved = items.filter((item) => item.resolved).length;
  return { total: items.length, resolved, remaining: items.length - resolved };
}

export function summarizeDecisionProposals(
  proposals: TermDecisionProposal[],
  rejected: ReadonlySet<string>,
) {
  const accepted = proposals.filter((proposal) => !rejected.has(proposal.proposal_id));
  return {
    accepted: accepted.length,
    rejected: proposals.length - accepted.length,
    disabled: accepted.reduce(
      (count, proposal) => count + proposal.after.filter((term) => term.disabled).length,
      0,
    ),
    translations: accepted.reduce(
      (count, proposal) => count + proposal.after.filter((term, index) => (
        term.preferred_translation !== proposal.before[index]?.preferred_translation
      )).length,
      0,
    ),
    structural: accepted.filter((proposal) => proposal.kind === "relationship").length,
  };
}
