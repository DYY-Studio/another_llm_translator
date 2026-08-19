import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { filterDecisionProposals, summarizeDecisionProposals } from "../src/termDecision.ts";

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

test("automatic decision dialog separates closing from task cancellation", async () => {
  const source = await readFile(
    new URL("../src/components/TermDecisionDialog.tsx", import.meta.url),
    "utf8",
  );
  assert.match(source, /translate\("common\.close", language\)/);
  assert.match(source, /translate\("terms\.decisionCloseHint", language\)/);
  assert.match(source, /run_action: options\?\.running_run \? \(force \? "decline" : "resume"\) : null/);
});
