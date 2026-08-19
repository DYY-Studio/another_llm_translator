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
  assert.match(appShell, /task\.usage\.partial/);
  assert.match(appShell, /run\.tokensPartial/);
  assert.match(diagnostics, /usage_partial/);
  assert.match(diagnostics, /diagnostics\.atLeast/);
});
