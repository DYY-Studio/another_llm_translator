import assert from "node:assert/strict";
import test from "node:test";

import {
  ApiError,
  apiErrorFromResponse,
  errorPayloadFrom,
  parseErrorPayload,
} from "../src/api.ts";
import {
  errorMessage,
  formatErrorPayload,
  translateError,
} from "../src/i18n.ts";

test("preserves structured API error fields and HTTP status", async () => {
  const error = await apiErrorFromResponse(new Response(JSON.stringify({
    code: "export_error",
    params: {
      reason: "missing_target_language_tag",
      setting: "project.target_language_tag",
    },
    error: "EPUB export fallback",
  }), { status: 400, headers: { "content-type": "application/json" } }));

  assert.ok(error instanceof ApiError);
  assert.equal(error.status, 400);
  assert.deepEqual(error.payload, {
    code: "export_error",
    params: {
      reason: "missing_target_language_tag",
      setting: "project.target_language_tag",
    },
    error: "EPUB export fallback",
  });
});

test("normalizes malformed error bodies without throwing", () => {
  assert.deepEqual(parseErrorPayload("not-json", 502), {
    code: "http_error",
    params: { status: 502 },
    error: "",
  });
});

test("recovers the backend envelope carried by a native rejection string", () => {
  const payload = errorPayloadFrom(JSON.parse(JSON.stringify({
    error: "EPUB export fallback",
    code: "export_error",
    params: { reason: "missing_target_language_tag" },
  })));
  assert.deepEqual(payload, {
    error: "EPUB export fallback",
    code: "export_error",
    params: { reason: "missing_target_language_tag" },
  });
});

test("localizes actionable export errors in both languages", () => {
  const params = { reason: "missing_target_language_tag" };
  assert.match(translateError("export_error", params, "zh-CN"), /BCP 47.*zh-Hans/);
  assert.match(translateError("export_error", params, "en"), /BCP 47.*zh-Hans/);
});

test("keeps backend detail for broad codes", () => {
  const payload = {
    code: "incomplete_error",
    params: {},
    error: "EPUB 状态引用了缺失资源：chapter.xhtml",
  };
  assert.equal(
    formatErrorPayload(payload, "zh-CN"),
    "结果不完整：EPUB 状态引用了缺失资源：chapter.xhtml",
  );
});

test("formats structured, ordinary, and unknown errors without an Error prefix", () => {
  const apiError = new ApiError(500, {
    code: "internal_error",
    params: {},
    error: "内部错误",
  });
  assert.equal(errorMessage(apiError, "zh-CN"), "服务发生内部错误，请查看服务日志");
  assert.equal(errorMessage(new Error("网络连接失败"), "zh-CN"), "网络连接失败");
  assert.equal(errorMessage(null, "en"), "Request failed");
});
