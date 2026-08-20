import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { decisionAliasChanges, decisionProposalChanges, filterDecisionProposals, filterManualReviewItems, manualReviewProgress, summarizeDecisionProposals } from "../src/termDecision.ts";

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

test("filters and summarizes the persistent manual queue", () => {
  const items = [
    { run_id: "run", normalized: "alice", source: "Alice", reason: "group", evidence: { hit_count: 1 }, resolved: false },
    { run_id: "run", normalized: "bob", source: "Bob", reason: "checked", evidence: { hit_count: 0 }, resolved: true },
  ];
  assert.deepEqual(filterManualReviewItems(items, "alice", "open").map((item) => item.normalized), ["alice"]);
  assert.deepEqual(filterManualReviewItems(items, "", "resolved").map((item) => item.normalized), ["bob"]);
  assert.deepEqual(manualReviewProgress(items), { total: 2, resolved: 1, remaining: 1 });
});

test("automatic decision dialog separates closing from task cancellation", async () => {
  const source = await readFile(
    new URL("../src/components/TermDecisionWorkspace.tsx", import.meta.url),
    "utf8",
  );
  assert.match(source, /translate\("common\.close", language\)/);
  assert.match(source, /translate\("terms\.decisionCloseHint", language\)/);
  assert.match(source, /\["completed", "cancelled", "failed"\]\.includes\(task\.status\)/);
  assert.match(source, /run_action: options\?\.running_run \? \(force \? "decline" : "resume"\) : null/);
  assert.match(source, /terms\.decisionOverflowPolicy/);
  assert.match(source, /term-decision-workspace/);
  assert.match(source, /term-decision-bottom-actions/);
  assert.match(source, /terms\.decisionManualTab/);
  assert.match(source, /manual-review-actions/);
  assert.match(source, /terms\/decision\/manual-review/);
  const termsSource = await readFile(
    new URL("../src/components/TermsView.tsx", import.meta.url),
    "utf8",
  );
  assert.match(termsSource, /manual-review-editor-bar/);
  assert.match(termsSource, /decisionManualTermMissing/);
  assert.match(termsSource, /openDecision\("manual"\)/);
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
