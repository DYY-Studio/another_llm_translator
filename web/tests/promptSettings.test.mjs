import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const settingsSource = readFileSync(new URL("../src/components/SettingsView.tsx", import.meta.url), "utf8");

test("prompt settings exposes both summary prompt resources", () => {
  assert.match(settingsSource, /value="content_summary"/);
  assert.match(settingsSource, /value="fragment_summary"/);
});

test("prompt settings renders generated terminology modes and mode errors", () => {
  assert.match(settingsSource, /assembled_modes/);
  assert.match(settingsSource, /assembled_mode_languages/);
  assert.match(settingsSource, /assembled_mode_errors/);
  assert.match(settingsSource, /terms\+fragment-summary/);
  assert.match(settingsSource, /summary-only/);
});
