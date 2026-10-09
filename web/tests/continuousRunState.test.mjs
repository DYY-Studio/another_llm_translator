import test from "node:test";
import assert from "node:assert/strict";
import {
  CONTINUOUS_ORDER,
  chainFor,
  continuousPayload,
  isContinuousBlockingResolved,
  reconcileRunActions,
  resultPolicyAfterRunAction,
  toggleEndIndex,
} from "../src/continuousRunState.ts";

test("continuous stages remain a canonical interval and clearing truncates later stages", () => {
  assert.deepEqual(chainFor("terminology", 2), [
    "terminology",
    "terminology_decision",
    "translation",
  ]);
  assert.equal(toggleEndIndex(3, 2, false), 1);
  assert.equal(toggleEndIndex(1, 2, true), 2);
  assert.deepEqual(CONTINUOUS_ORDER.slice(1, 4), [
    "terminology_decision",
    "translation",
    "proofreading",
  ]);
});

test("continuous payload forces middle decision review and explicit apply", () => {
  assert.deepEqual(
    continuousPayload(
      ["terminology", "terminology_decision", "translation"],
      {
        finalReview: false,
        applyTerminologyDecision: true,
        force: false,
        reuseMixedFingerprints: false,
        runActions: { translation: "resume" },
        includeDraftTranslation: false,
        includeSummaries: false,
        aggregateFullSummaries: false,
      },
    ),
    {
      stage: "continuous",
      stages: ["terminology", "terminology_decision", "translation"],
      final_review: true,
      apply_terminology_decision: true,
      force: false,
      reuse_mixed_fingerprints: false,
      run_actions: { translation: "resume" },
      include_draft_translation: false,
      include_summaries: false,
      aggregate_full_summaries: false,
    },
  );
});

test("terminal decision keeps optional final review and does not authorize apply", () => {
  assert.deepEqual(
    continuousPayload(["terminology", "terminology_decision"], {
      finalReview: true,
      applyTerminologyDecision: true,
      force: true,
      reuseMixedFingerprints: false,
      runActions: {},
      includeDraftTranslation: false,
      includeSummaries: false,
      aggregateFullSummaries: false,
    }),
    {
      stage: "continuous",
      stages: ["terminology", "terminology_decision"],
      final_review: true,
      apply_terminology_decision: false,
      force: true,
      reuse_mixed_fingerprints: false,
      run_actions: {},
      include_draft_translation: false,
      include_summaries: false,
      aggregate_full_summaries: false,
    },
  );
});

test("run actions follow the current running Run and remove incompatible resume", () => {
  const selections = {
    translation: { action: "resume", runId: "old-translation" },
    proofreading: { action: "decline", runId: "proofreading-1" },
    polishing: { action: "resume", runId: "polishing-1" },
  };
  assert.deepEqual(
    reconcileRunActions(selections, [
      { stage: "translation", running_run: { run_id: "new-translation", resume_compatible: true } },
      { stage: "proofreading", running_run: { run_id: "proofreading-1", resume_compatible: false } },
      { stage: "polishing", running_run: { run_id: "polishing-1", resume_compatible: true } },
    ]),
    {
      proofreading: { action: "decline", runId: "proofreading-1" },
      polishing: { action: "resume", runId: "polishing-1" },
    },
  );
  assert.equal(
    isContinuousBlockingResolved("run_action_required", "polishing", {
      polishing: { action: "resume", runId: "polishing-1" },
    }, "pending"),
    true,
  );
});

test("only decision decline selects force; resume clears reuse or force", () => {
  assert.equal(resultPolicyAfterRunAction("pending", "translation", "decline"), "pending");
  assert.equal(resultPolicyAfterRunAction("reuse", "translation", "decline"), "reuse");
  assert.equal(resultPolicyAfterRunAction("pending", "terminology_decision", "decline"), "force");
  assert.equal(resultPolicyAfterRunAction("force", "translation", "resume"), "pending");
  assert.equal(resultPolicyAfterRunAction("reuse", "translation", "resume"), "pending");
  assert.equal(
    isContinuousBlockingResolved("resume_policy_conflict", undefined, {
      translation: { action: "resume", runId: "translation-1" },
    }, "pending"),
    true,
  );
});

test("continuous joint options require terminology and aggregation requires summaries", () => {
  const options = {
    finalReview: false,
    applyTerminologyDecision: false,
    force: false,
    reuseMixedFingerprints: false,
    runActions: {},
    includeDraftTranslation: true,
    includeSummaries: true,
    aggregateFullSummaries: true,
  };
  const joint = continuousPayload(["terminology"], options);
  assert.equal(joint.include_draft_translation, true);
  assert.equal(joint.include_summaries, true);
  assert.equal(joint.aggregate_full_summaries, true);
  assert.equal(continuousPayload(["terminology"], { ...options, includeSummaries: false }).aggregate_full_summaries, false);
  const standard = continuousPayload(["translation"], options);
  assert.equal(standard.include_draft_translation, false);
  assert.equal(standard.include_summaries, false);
  assert.equal(standard.aggregate_full_summaries, false);
});
