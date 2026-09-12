import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  fetchOverview,
  fetchHistoricalRequest,
  fetchHistoricalRun,
  fetchHistoricalRuns,
  fetchProjects,
  fetchRelatedTerms,
  fetchSummaries,
  fetchTermHits,
  fetchTerms,
  queryKeys,
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

test("read query keys isolate projects and related revisions", () => {
  assert.deepEqual(queryKeys.projects(), ["projects"]);
  const project = { projectId: "project-id-a", selector: "same-name" };
  const otherProject = { projectId: "project-id-b", selector: "same-name" };
  assert.deepEqual(queryKeys.overview(project), ["overview", "project-id-a"]);
  assert.deepEqual(queryKeys.terms(project), ["terms", "project-id-a"]);
  assert.deepEqual(queryKeys.termHits(project, "term"), ["term-hits", "project-id-a", "term"]);
  assert.deepEqual(queryKeys.relatedTerms(project, 7, "match"), ["related-terms", "project-id-a", 7, "match"]);
  assert.deepEqual(queryKeys.summaries(project), ["summaries", "project-id-a"]);
  assert.notDeepEqual(queryKeys.overview(project), queryKeys.overview(otherProject));
});

test("fetchProjects uses a cancellable GET request", async () => {
  const signal = new AbortController().signal;
  const calls = stubFetch({ projects: [] });

  await fetchProjects(signal);

  assertGet(calls[0], "/api/v1/projects", signal);
});

test("fetchOverview uses the project-specific overview URL", async () => {
  const signal = new AbortController().signal;
  const calls = stubFetch({ name: "A" });

  await fetchOverview("project-a", signal);

  assertGet(calls[0], "/api/v1/projects/project-a?offset=0&limit=1", signal);
});

test("fetchTerms uses the project-specific terminology URL", async () => {
  const signal = new AbortController().signal;
  const calls = stubFetch({ terms: [] });

  await fetchTerms("project-a", signal);

  assertGet(calls[0], "/api/v1/projects/project-a/terms", signal);
});

test("fetchTermHits sends the normalized term and offset", async () => {
  const signal = new AbortController().signal;
  const calls = stubFetch({ hits: [] });

  await fetchTermHits("project-a", "hello", 50, signal);

  assert.equal(calls[0].input, "/api/v1/projects/project-a/terms/hits");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.body, JSON.stringify({ normalized: "hello", offset: 50, limit: 50 }));
  assert.equal(calls[0].init.signal, signal);
});

test("fetchRelatedTerms sends the normalized term and fixed limit", async () => {
  const signal = new AbortController().signal;
  const calls = stubFetch({ related: [] });

  await fetchRelatedTerms("project-a", "hello", signal);

  assert.equal(calls[0].input, "/api/v1/projects/project-a/terms/related");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.body, JSON.stringify({ normalized: "hello", limit: 20 }));
  assert.equal(calls[0].init.signal, signal);
});

test("fetchSummaries uses the project-specific summaries URL", async () => {
  const signal = new AbortController().signal;
  const calls = stubFetch({ boundaries: [], artifacts: [], participation: [] });

  await fetchSummaries("project-a", signal);

  assertGet(calls[0], "/api/v1/projects/project-a/summaries", signal);
});

test("historical run query keys include scope and pagination", () => {
  assert.deepEqual(
    queryKeys.historicalRuns({
      project: "sample",
      stage: "translation",
      status: "failed",
      offset: 20,
      limit: 20,
    }),
    ["historical-runs", "sample", "translation", "failed", 20, 20],
  );
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
