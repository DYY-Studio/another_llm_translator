import { api } from "./api.ts";
import type {
  ProjectOverview,
  ProjectSummary,
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
