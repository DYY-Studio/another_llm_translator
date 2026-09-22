import assert from "node:assert/strict";
import test from "node:test";

import { translate } from "../src/i18n.ts";

const hintKeys = [
  "settings.targetLanguageTagHint",
  "settings.outputEncodingHint",
  "settings.encodingThresholdHint",
  "settings.fallbackEncodingHint",
  "settings.presetEmptyHint",
  "settings.temperatureHint",
  "settings.summaryContextHint",
  "settings.schedulingModeHint",
  "settings.splitOversizedHint",
  "settings.crossBoundaryHint",
  "settings.unicodeHint",
  "settings.casefoldHint",
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
  "settings.debugInjectionHint",
  "settings.validatorUnavailable",
  "preset.adapterHint",
  "preset.baseUrlHint",
  "preset.credentialHint",
  "preset.proxyUrlHint",
  "preset.contextWindowHint",
  "preset.targetChunkInputTokensHint",
  "preset.maxOutputTokensHint",
  "preset.contextSafetyMarginHint",
  "preset.tokenSafetyFactorHint",
  "preset.keyIndexHint",
  "preset.rpmHint",
  "preset.itpmHint",
  "preset.maxConcurrencyHint",
  "preset.maxConcurrencyPerKeyHint",
  "preset.timeoutSecondsHint",
  "preset.streamingHint",
  "preset.streamReadTimeoutHint",
  "preset.extraBodyHint",
  "preset.extraHeadersHint",
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
