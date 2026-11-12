import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  fetchHistoricalRequest,
  fetchHistoricalRun,
  fetchHistoricalRuns,
  clearGlobalLogs,
  clearDebugAttachments,
  clearRunCheckpoints,
  clearOutputFile,
  clearProjectLogs,
} from "../src/queries.ts";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

function stubFetch(value) {
  const calls = [];
  globalThis.fetch = async (input, init) => {
    calls.push({ input, init });
    return new Response(JSON.stringify(value), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  return calls;
}

function assertGet(call, path, signal) {
  assert.equal(call.input, path);
  assert.equal(call.init.method ?? "GET", "GET");
  assert.equal(call.init.signal, signal);
}

test("storage cleanup requests always send explicit confirmation", async () => {
  const signal = new AbortController().signal;
  const calls = stubFetch({ affected_files: 1, reclaimed_bytes: 2 });

  await clearGlobalLogs(signal);
  await clearDebugAttachments("project/a", "RUN/1", signal);
  await clearOutputFile("project/a", "output/result.txt", signal);
  await clearProjectLogs("project/a", signal);
  await clearRunCheckpoints("project/a", "RUN/1");
  await clearRunCheckpoints("project/a");

  assert.equal(calls[0].input, "/api/v1/storage/logs/clear");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.body, JSON.stringify({ confirm: true }));
  assert.equal(calls[0].init.signal, signal);
  assert.equal(calls[1].input, "/api/v1/projects/project%2Fa/storage/runs/RUN%2F1/debug/clear");
  assert.equal(calls[1].init.body, JSON.stringify({ confirm: true }));
  assert.equal(calls[2].input, "/api/v1/projects/project%2Fa/storage/outputs/clear");
  assert.equal(calls[2].init.body, JSON.stringify({ path: "output/result.txt", confirm: true }));
  assert.equal(calls[3].input, "/api/v1/projects/project%2Fa/storage/logs/clear");
  assert.equal(calls[3].init.body, JSON.stringify({ confirm: true }));
  assert.equal(calls[4].input, "/api/v1/projects/project%2Fa/storage/runs/RUN%2F1/checkpoints/clear");
  assert.equal(calls[4].init.body, JSON.stringify({ confirm: true }));
  assert.equal(calls[5].input, "/api/v1/projects/project%2Fa/storage/checkpoints/clear");
  assert.equal(calls[5].init.body, JSON.stringify({ confirm: true }));
});

test("fetchHistoricalRuns sends filters and pagination as a GET", async () => {
  const signal = new AbortController().signal;
  const calls = stubFetch({ items: [], total: 0, offset: 20, limit: 20 });

  await fetchHistoricalRuns({
    project: "sample",
    stage: "translation",
    status: "failed",
    offset: 20,
    limit: 20,
  }, signal);

  assertGet(
    calls[0],
    "/api/v1/runs?project=sample&stage=translation&status=failed&offset=20&limit=20",
    signal,
  );
});

test("historical run detail queries address a run and optionally expand a request", async () => {
  const signal = new AbortController().signal;
  const calls = stubFetch({ run_id: "RUN-1" });

  await fetchHistoricalRun("sample", "RUN-1", signal);
  await fetchHistoricalRequest("sample", "RUN-1", "REQ-1", true, signal);

  assertGet(calls[0], "/api/v1/projects/sample/runs/RUN-1", signal);
  assertGet(
    calls[1],
    "/api/v1/projects/sample/runs/RUN-1/requests/REQ-1?full=true",
    signal,
  );
});
