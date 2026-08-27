import assert from "node:assert/strict";
import test from "node:test";

import { translate } from "../src/i18n.ts";

const hintKeys = [
  "settings.outputEncodingHint",
  "settings.encodingThresholdHint",
  "settings.fallbackEncodingHint",
  "settings.temperatureHint",
  "settings.schedulingModeHint",
  "settings.targetChunkTokensHint",
  "settings.splitOversizedHint",
  "settings.maxTermsPerSegmentHint",
  "settings.aliasCollisionHint",
  "settings.terminologyDecisionHint",
  "settings.allowSoftTargetOverflowHint",
  "settings.anchorOverflowModeHint",
  "settings.repairAttemptsHint",
  "settings.exhaustedModeHint",
  "settings.httpMaxAttemptsHint",
  "settings.formatRepairAttemptsHint",
  "settings.baseDelayHint",
  "settings.maxDelayHint",
  "settings.jitterHint",
  "settings.enableDebugHint",
  "settings.debugInjectionHint",
  "preset.adapterHint",
  "preset.endpointHint",
  "preset.credentialHint",
  "preset.proxyUrlHint",
  "preset.contextWindowHint",
  "preset.maxOutputTokensHint",
  "preset.contextSafetyMarginHint",
  "preset.tokenSafetyFactorHint",
  "preset.timeoutSecondsHint",
];

test("configuration help text is localized in both supported languages", () => {
  for (const key of hintKeys) {
    const chinese = translate(key, "zh-CN");
    const english = translate(key, "en");
    assert.notEqual(chinese, key, `${key} is missing Chinese text`);
    assert.notEqual(english, key, `${key} is missing English text`);
    assert.notEqual(english, chinese, `${key} falls back to the other language`);
  }
});
