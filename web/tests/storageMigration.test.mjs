import assert from "node:assert/strict";
import test from "node:test";

import { migrateLegacyLocalStorage, STORAGE_KEYS } from "../src/storageMigration.ts";

class MemoryStorage {
  values = new Map();

  getItem(key) {
    return this.values.has(key) ? this.values.get(key) : null;
  }

  setItem(key, value) {
    this.values.set(key, String(value));
  }

  removeItem(key) {
    this.values.delete(key);
  }
}

test("migrates legacy values into empty release keys", () => {
  const storage = new MemoryStorage();
  storage.setItem("minimal-llm-translator.theme.v1", "dark");
  storage.setItem("minimal-llm-translator.language.v1", "en");

  migrateLegacyLocalStorage(storage);

  assert.equal(storage.getItem(STORAGE_KEYS.theme), "dark");
  assert.equal(storage.getItem(STORAGE_KEYS.language), "en");
  assert.equal(storage.getItem("minimal-llm-translator.theme.v1"), null);
  assert.equal(storage.getItem("minimal-llm-translator.language.v1"), null);
});

test("keeps release values and conflicting legacy values untouched", () => {
  const storage = new MemoryStorage();
  storage.setItem(STORAGE_KEYS.theme, "light");
  storage.setItem("minimal-llm-translator.theme.v1", "dark");

  migrateLegacyLocalStorage(storage);

  assert.equal(storage.getItem(STORAGE_KEYS.theme), "light");
  assert.equal(storage.getItem("minimal-llm-translator.theme.v1"), "dark");
});
