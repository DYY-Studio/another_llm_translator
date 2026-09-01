import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const styles = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");

test("mobile overview file rows keep metadata beside the replacement action", () => {
  const mobileRule = styles.match(/@media \(max-width: 780px\), \(pointer: coarse\) \{([\s\S]*?)\n\}/)?.[1] ?? "";

  assert.match(mobileRule, /\.overview-file-list \.file-row \{[^}]*position:\s*relative;/);
  assert.match(mobileRule, /\.overview-file-list \.file-row-meta \{ position: absolute; /);
});
