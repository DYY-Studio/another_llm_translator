import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const styles = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");

test("mobile form controls use a Safari-safe focus font size", () => {
  const mobileRule = styles.match(/@media \(max-width: 780px\) \{([\s\S]*?)\n\}/)?.[1] ?? "";

  assert.match(mobileRule, /\.app select, \.app input, \.app textarea \{ font-size: 16px; \}/);
});
