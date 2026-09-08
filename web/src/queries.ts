import { api } from "./api.ts";
import type {
  ProjectOverview,
  ProjectSummary,
  RelatedTermsResponse,
  SummariesResponse,
  TermHitsResponse,
  TermsResponse,
} from "./types";

export const queryKeys = {
  projects: () => ["projects"] as const,
  overview: (project: string) => ["overview", project] as const,
  terms: (project: string) => ["terms", project] as const,
  termHits: (project: string, normalized: string) => ["term-hits", project, normalized] as const,
  relatedTerms: (project: string, termsRevision: number | null, matchKey: string) => ["related-terms", project, termsRevision, matchKey] as const,
  summaries: (project: string) => ["summaries", project] as const,
};

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
