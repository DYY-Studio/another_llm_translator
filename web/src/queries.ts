import { api } from "./api.ts";
import type {
  ProjectOverview,
  ProjectSummary,
  HistoricalRunDetail,
  HistoricalRunRequestDetail,
  HistoricalRunSummary,
  RelatedTermsResponse,
  SummariesResponse,
  TermHitsResponse,
  TermsResponse,
} from "./types";

type ProjectQueryIdentity = Readonly<{ projectId: string }>;

export const queryKeys = {
  projects: () => ["projects"] as const,
  overview: ({ projectId }: ProjectQueryIdentity) => ["overview", projectId] as const,
  terms: ({ projectId }: ProjectQueryIdentity) => ["terms", projectId] as const,
  termHits: ({ projectId }: ProjectQueryIdentity, normalized: string) => ["term-hits", projectId, normalized] as const,
  relatedTerms: ({ projectId }: ProjectQueryIdentity, termsRevision: number | null, matchKey: string) => ["related-terms", projectId, termsRevision, matchKey] as const,
  summaries: ({ projectId }: ProjectQueryIdentity) => ["summaries", projectId] as const,
  historicalRuns: ({ project, stage, status, offset, limit }: HistoricalRunQuery) => (
    ["historical-runs", project, stage, status, offset, limit] as const
  ),
  historicalRun: ({ project, runId }: HistoricalRunIdentity) => (
    ["historical-run", project, runId] as const
  ),
  historicalRequest: ({ project, runId, requestId, full }: HistoricalRequestIdentity) => (
    ["historical-request", project, runId, requestId, full] as const
  ),
};

export interface HistoricalRunQuery {
  project: string;
  stage: string;
  status: string;
  offset: number;
  limit: number;
}

interface HistoricalRunIdentity {
  project: string;
  runId: string;
}

interface HistoricalRequestIdentity extends HistoricalRunIdentity {
  requestId: string;
  full: boolean;
}

export async function fetchProjects(signal?: AbortSignal): Promise<ProjectSummary[]> {
  const value = await api<{ projects: ProjectSummary[] }>("/api/v1/projects", { signal });
  return value.projects;
}

export function fetchOverview(project: string, signal?: AbortSignal): Promise<ProjectOverview> {
  return api<ProjectOverview>(`/api/v1/projects/${project}?offset=0&limit=1`, { signal });
}

export function fetchTerms(project: string, signal?: AbortSignal): Promise<TermsResponse> {
  return api<TermsResponse>(`/api/v1/projects/${project}/terms`, { signal });
}

export function fetchTermHits(
  project: string,
  normalized: string,
  offset: number,
  signal?: AbortSignal,
  limit = 50,
): Promise<TermHitsResponse> {
  return api<TermHitsResponse>(
    `/api/v1/projects/${project}/terms/hits`,
    {
      method: "POST",
      body: JSON.stringify({ normalized, offset, limit }),
      signal,
    },
  );
}

export function fetchRelatedTerms(
  project: string,
  normalized: string,
  signal?: AbortSignal,
): Promise<RelatedTermsResponse> {
  return api<RelatedTermsResponse>(
    `/api/v1/projects/${project}/terms/related`,
    {
      method: "POST",
      body: JSON.stringify({ normalized, limit: 20 }),
      signal,
    },
  );
}

export function fetchSummaries(project: string, signal?: AbortSignal): Promise<SummariesResponse> {
  return api<SummariesResponse>(`/api/v1/projects/${project}/summaries`, { signal });
}

export function fetchHistoricalRuns(
  query: HistoricalRunQuery,
  signal?: AbortSignal,
): Promise<{ items: HistoricalRunSummary[]; total: number; offset: number; limit: number }> {
  const params = new URLSearchParams();
  if (query.project) params.set("project", query.project);
  if (query.stage) params.set("stage", query.stage);
  if (query.status) params.set("status", query.status);
  params.set("offset", String(query.offset));
  params.set("limit", String(query.limit));
  return api(`/api/v1/runs?${params.toString()}`, { signal });
}

export function fetchHistoricalRun(
  project: string,
  runId: string,
  signal?: AbortSignal,
): Promise<HistoricalRunDetail> {
  return api<HistoricalRunDetail>(
    `/api/v1/projects/${encodeURIComponent(project)}/runs/${encodeURIComponent(runId)}`,
    { signal },
  );
}

export function fetchHistoricalRequest(
  project: string,
  runId: string,
  requestId: string,
  full: boolean,
  signal?: AbortSignal,
): Promise<HistoricalRunRequestDetail> {
  const suffix = full ? "?full=true" : "";
  return api<HistoricalRunRequestDetail>(
    `/api/v1/projects/${encodeURIComponent(project)}/runs/${encodeURIComponent(runId)}/requests/${encodeURIComponent(requestId)}${suffix}`,
    { signal },
  );
}
