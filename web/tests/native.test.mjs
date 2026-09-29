import assert from "node:assert/strict";
import test from "node:test";

import { openExternalUrl } from "../src/native.ts";

const guideUrl = "https://example.com/guide";

function installWindow(value) {
  globalThis.window = value;
}

function removeWindow() {
  delete globalThis.window;
}

test("openExternalUrl resolves when a web link opens with noopener", async () => {
  const calls = [];
  installWindow({
    open: (...args) => {
      calls.push(args);
      return null;
    },
  });

  try {
    await openExternalUrl(guideUrl);
    assert.deepEqual(calls, [[guideUrl, "_blank", "noopener,noreferrer"]]);
  } finally {
    removeWindow();
  }
});

test("openExternalUrl uses the native opener without a web fallback", async () => {
  const calls = [];
  installWindow({
    __TAURI__: {
      core: { invoke: async () => undefined },
      opener: {
        openUrl: async (url) => {
          calls.push(url);
        },
      },
    },
    open: () => {
      throw new Error("web fallback must not run");
    },
  });

  try {
    await openExternalUrl(guideUrl);
    assert.deepEqual(calls, [guideUrl]);
  } finally {
    removeWindow();
  }
});

test("openExternalUrl fails when the native opener is unavailable", async () => {
  installWindow({
    __TAURI__: { core: { invoke: async () => undefined } },
    open: () => {
      throw new Error("web fallback must not run");
    },
  });

  try {
    await assert.rejects(openExternalUrl(guideUrl), /Tauri opener is unavailable/);
  } finally {
    removeWindow();
  }
});
