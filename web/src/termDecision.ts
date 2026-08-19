import type { TermDecisionProposal } from "./types";

export function filterDecisionProposals(
  proposals: TermDecisionProposal[],
  query: string,
  kind: string,
) {
  const needle = query.trim().toLocaleLowerCase();
  return proposals.filter((proposal) => {
    if (kind && proposal.kind !== kind) return false;
    if (!needle) return true;
    return [
      proposal.reason,
      ...proposal.before.flatMap((term) => [term.source, term.preferred_translation ?? ""]),
      ...proposal.after.flatMap((term) => [term.source, term.preferred_translation ?? ""]),
    ].join("\n").toLocaleLowerCase().includes(needle);
  });
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
