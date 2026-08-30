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
