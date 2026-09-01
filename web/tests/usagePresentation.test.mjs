import assert from "node:assert/strict";
import test from "node:test";
import { translate } from "../src/i18n.ts";

test("localizes partial token usage separately from complete and unavailable states", () => {
  for (const language of ["zh-CN", "en"]) {
    const partial = translate("run.tokensPartial", language, { input: 12, output: 3 });
    assert.match(partial, /12/);
    assert.match(partial, /3/);
    assert.notEqual(partial, translate("run.tokensUnavailable", language));
    assert.notEqual(
      translate("diagnostics.usagePartial", language),
      translate("diagnostics.usageComplete", language),
    );
  }
});

test("labels SQLite storage as the main database file", () => {
  assert.equal(translate("overview.storageSqlite", "zh-CN"), "SQLite 主文件");
  assert.equal(translate("overview.storageSqlite", "en"), "SQLite main file");
  assert.match(
    translate("overview.compactConfirm", "zh-CN"),
    /SQLite 主文件/,
  );
  assert.match(
    translate("overview.compactConfirm", "en"),
    /SQLite main file/,
  );
});
