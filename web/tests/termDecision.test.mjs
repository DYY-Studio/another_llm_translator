import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { decisionAliasChanges, decisionProposalChanges, decisionRelationshipRole, decisionRelationshipSummary, filterDecisionProposals, filterManualReviewItems, summarizeDecisionProposals } from "../src/termDecision.ts";

const state = (normalized, translation, disabled = false) => ({
  normalized,
  source: normalized,
  category: null,
  description: null,
  preferred_translation: translation,
  aliases: [],
  group_primary: null,
  disabled,
});

const proposals = [
  {
    proposal_id: "one",
    kind: "term_update",
    before: [state("Alice", null)],
    after: [state("Alice", "爱丽丝")],
    reason: "fill translation",
  },
  {
    proposal_id: "two",
    kind: "relationship",
    before: [state("Bob", "鲍勃")],
    after: [state("Bob", "鲍勃", true)],
    reason: "unused duplicate",
  },
];

test("filters terminology proposals by kind and text", () => {
  assert.deepEqual(filterDecisionProposals(proposals, "爱丽丝", "").map((item) => item.proposal_id), ["one"]);
  assert.deepEqual(filterDecisionProposals(proposals, "", "relationship").map((item) => item.proposal_id), ["two"]);
});

test("summarizes accepted whole proposals", () => {
  assert.deepEqual(summarizeDecisionProposals(proposals, new Set(["one"])), {
    accepted: 1,
    rejected: 1,
    disabled: 1,
    translations: 0,
    structural: 1,
  });
});

test("renders semantic field and alias changes from states", () => {
  const before = { ...state("Alice", null), aliases: ["Ally"] };
  const after = { ...before, preferred_translation: "爱丽丝", group_primary: "root", aliases: ["Ally", "A"] };
  assert.deepEqual(decisionProposalChanges(before, after), [
    { field: "preferred_translation", before: "", after: "爱丽丝" },
    { field: "group_primary", before: "", after: "root" },
  ]);
  assert.deepEqual(decisionAliasChanges(before, after), { added: ["A"], removed: [] });
});

test("summarizes relationship components with primary and member roles", () => {
  const primary = { ...state("Alice", "爱丽丝"), source: "Alice" };
  const member = { ...state("Aly", "爱丽丝"), source: "Aly", group_primary: "Alice" };
  const states = [member, primary];
  assert.deepEqual(decisionRelationshipSummary(states), [{ primary: "Alice", members: ["Aly"] }]);
  assert.equal(decisionRelationshipRole(primary, states), "primary");
  assert.equal(decisionRelationshipRole(member, states), "member");
  assert.equal(decisionRelationshipRole(state("Standalone", null), states), null);
});

test("filters and summarizes the persistent manual queue", () => {
  const items = [
    { run_id: "run", normalized: "alice", source: "Alice", reason: "group", evidence: { hit_count: 1 }, resolved: false },
    { run_id: "run", normalized: "bob", source: "Bob", reason: "checked", evidence: { hit_count: 0 }, resolved: true },
  ];
  assert.deepEqual(filterManualReviewItems(items, "alice", "open").map((item) => item.normalized), ["alice"]);
  assert.deepEqual(filterManualReviewItems(items, "", "resolved").map((item) => item.normalized), ["bob"]);
});

test("automatic decision workspace keeps navigation separate from task cancellation", async () => {
  const source = await readFile(
    new URL("../src/components/TermDecisionWorkspace.tsx", import.meta.url),
    "utf8",
  );
  assert.doesNotMatch(source, /translate\("common\.close", language\)/);
  assert.match(source, /translate\("terms\.decisionCloseHint", language\)/);
  assert.match(source, /\["completed", "cancelled", "failed"\]\.includes\(task\.status\)/);
  assert.match(source, /run_action: decision\.run_action/);
  assert.match(source, /<RunDialog/);
  assert.match(source, /decisionRelationshipSummary/);
  assert.match(source, /decisionRelationshipRole/);
  assert.match(source, /terms\.decisionRelationshipPrimary/);
  assert.match(source, /terms\.decisionRelationshipMembers/);
  assert.match(source, /term-decision-workspace/);
  assert.doesNotMatch(source, /term-decision-bottom-actions/);
  assert.doesNotMatch(source, /term-decision-options/);
  assert.doesNotMatch(source, /term-decision-resume/);
  assert.match(source, /settings-action-heading/);
  assert.match(source, /acknowledge_manual_review/);
  assert.match(source, /decisionManualReplaceConfirm/);
  assert.match(source, /terms\.decisionManualTab/);
  assert.match(source, /manual-review-actions/);
  assert.match(source, /terms\/decision\/manual-review/);
  const dialogSource = await readFile(
    new URL("../src/components/RunDialog.tsx", import.meta.url),
    "utf8",
  );
  assert.match(dialogSource, /runDialog\.decisionTitle/);
  assert.match(dialogSource, /decisionMode/);
  assert.match(dialogSource, /resultPolicy === "force"/);
  const termsSource = await readFile(
    new URL("../src/components/TermsView.tsx", import.meta.url),
    "utf8",
  );
  assert.match(termsSource, /manual-review-editor-bar/);
  assert.match(termsSource, /decisionManualTermMissing/);
  assert.match(termsSource, /openDecision\("manual"\)/);
  assert.match(termsSource, /manualReviewQueueProgress/);
  assert.match(termsSource, /decisionDraftPending/);
  assert.match(termsSource, /manualReview\.remaining > 0/);
  assert.doesNotMatch(termsSource, /manualReview\.total > 0/);
  assert.match(termsSource, /selectedActive\.length > 0/);
  assert.match(termsSource, /term-actions-menu/);
  assert.match(termsSource, /term-actions-popover/);
  assert.match(termsSource, /terms\.moreActions/);
  const messages = await readFile(
    new URL("../src/i18n.ts", import.meta.url),
    "utf8",
  );
  assert.match(messages, /"terms\.moreActions": "更多操作"/);
  assert.match(messages, /"terms\.moreActions": "More actions"/);
});

test("automatic decision resume copy identifies persisted progress", async () => {
  const source = await readFile(
    new URL("../src/i18n.ts", import.meta.url),
    "utf8",
  );
  assert.match(source, /已保存 \{completed\} \/ \{total\}/);
  assert.match(source, /Saved \{completed\} \/ \{total\}/);
});

test("prompt settings expose distinct terminology decision phase previews", async () => {
  const source = await readFile(
    new URL("../src/components/SettingsView.tsx", import.meta.url),
    "utf8",
  );
  assert.match(source, /assembled_phases\?: Record<string, string>/);
  assert.match(source, /settings\.promptPhaseAdjudication/);
  assert.match(source, /settings\.promptPhaseConsistency/);
  assert.match(source, /previewPhase/);
});
