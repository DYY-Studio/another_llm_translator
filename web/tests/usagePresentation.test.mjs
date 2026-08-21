import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("task and diagnostics surfaces distinguish partial token usage", async () => {
  const appShell = await readFile(
    new URL("../src/components/AppShell.tsx", import.meta.url),
    "utf8",
  );
  const diagnostics = await readFile(
    new URL("../src/components/DiagnosticsView.tsx", import.meta.url),
    "utf8",
  );
  const i18n = await readFile(
    new URL("../src/i18n.ts", import.meta.url),
    "utf8",
  );
  assert.match(appShell, /task\.usage\.partial/);
  assert.match(appShell, /run\.tokensPartial/);
  assert.match(appShell, /run\.tokensUnavailable/);
  assert.match(diagnostics, /usage_partial/);
  assert.match(diagnostics, /metrics\?\.usage_available \|\| metrics\?\.usage_partial \? number/);
  assert.match(diagnostics, /diagnostics\.runPartial/);
  assert.match(diagnostics, /diagnostics\.runTotal/);
  assert.match(diagnostics, /diagnostics\.unavailable/);
  assert.match(diagnostics, /diagnostics\.usageComplete/);
  assert.match(diagnostics, /diagnostics\.usagePartial/);
  assert.doesNotMatch(diagnostics, /diagnostics\.atLeast/);
  assert.match(i18n, /"run\.tokensPartial": "输入 \{input\} · 输出 \{output\} Tokens（不完整）"/);
  assert.match(i18n, /"run\.tokensPartial": "Input \{input\} · output \{output\} tokens \(incomplete\)"/);
  assert.match(i18n, /"diagnostics\.runPartial": "当前 Run 统计不完整"/);
  assert.match(i18n, /"diagnostics\.runPartial": "Current run: usage incomplete"/);
  assert.match(i18n, /"diagnostics\.usagePartial": "不完整"/);
  assert.match(i18n, /"diagnostics\.usagePartial": "Incomplete"/);
  assert.doesNotMatch(i18n, /"diagnostics\.atLeast"/);
  assert.doesNotMatch(i18n, /tokensPartial": "[^\n]*(至少|at least)/i);
});
