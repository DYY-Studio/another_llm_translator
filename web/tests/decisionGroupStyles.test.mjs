import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const styles = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");

test("decision group legends keep their labels on one line in WebKit", () => {
  const declaration = styles.match(/\.decision-group legend \{([^}]*)\}/)?.[1] ?? "";

  assert.match(declaration, /min-width:\s*max-content/);
  assert.match(declaration, /white-space:\s*nowrap/);
});
