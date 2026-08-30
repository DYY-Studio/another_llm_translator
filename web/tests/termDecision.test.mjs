import assert from "node:assert/strict";
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
    conflicts: {
      Alice: {
        categories: ["人物", "女性主角候选"],
        preferred_translations: ["爱丽丝", "艾丽丝"],
        alias_primaries: [{ alias: "Ally", primary_source: "Alicia", reason: "policy" }],
        group_claims: [{ entry: "Alice", claimed_by: "Alicia", alias: "Ally", reason: "multiple_owners" }],
      },
    },
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
  assert.deepEqual(filterDecisionProposals(proposals, "女性主角候选", "").map((item) => item.proposal_id), ["one"]);
  assert.deepEqual(filterDecisionProposals(proposals, "Alicia", "").map((item) => item.proposal_id), ["one"]);
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
  const before = { ...state("Alice", null), description: "旧说明全文", aliases: ["Ally"] };
  const after = { ...before, description: "基于源文证据整理后的完整说明", preferred_translation: "爱丽丝", group_primary: "root", aliases: ["Ally", "A"] };
  assert.deepEqual(decisionProposalChanges(before, after), [
    { field: "preferred_translation", before: "", after: "爱丽丝" },
    { field: "description", before: "旧说明全文", after: "基于源文证据整理后的完整说明" },
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
    { run_id: "run", normalized: "alice", source: "Alice", reason: "group", evidence: { hit_count: 1 }, conflicts: { categories: ["人物", "核心角色候选"], preferred_translations: [], alias_primaries: [], group_claims: [] }, resolved: false },
    { run_id: "run", normalized: "bob", source: "Bob", reason: "checked", evidence: { hit_count: 0 }, resolved: true },
  ];
  assert.deepEqual(filterManualReviewItems(items, "alice", "open").map((item) => item.normalized), ["alice"]);
  assert.deepEqual(filterManualReviewItems(items, "核心角色候选", "open").map((item) => item.normalized), ["alice"]);
  assert.deepEqual(filterManualReviewItems(items, "", "resolved").map((item) => item.normalized), ["bob"]);
});
