export type Stage =
  | "overview"
  | "terminology"
  | "translation"
  | "proofreading"
  | "polishing"
  | "export"
  | "settings";

export interface ResultView {
  record_id: string;
  text?: string;
  review_status?: "accepted" | "suggested";
  suggested_text?: string | null;
  reason?: string | null;
  validation_status?: "passed" | "warning";
}

export interface ReviewView {
  base: ResultView | null;
  suggestion: ResultView | null;
  applied: ResultView | null;
  outdated: boolean;
  applied_current: boolean;
}

export interface Segment {
  segment_id: string;
  file_id: string;
  line_index: number;
  source: string;
  translation: ResultView | null;
  reviews: {
    proofreading: ReviewView;
    polishing: ReviewView;
  };
}

export interface ProjectOverview {
  name: string;
  path: string;
  files: Array<{ file_id: string; file_order: number; name: string }>;
  segments: Segment[];
}

export interface ProjectSummary {
  name: string;
  project_id: string;
  file_count: number;
  segment_count: number;
}

export interface TaskState {
  task_id: string;
  project: string;
  stage: string;
  status: string;
  error?: string | null;
  summary?: Record<string, unknown> | null;
}

export interface Term {
  normalized: string;
  source: string;
  category: string | null;
  description: string | null;
  preferred_translation: string | null;
  aliases: string[];
  disabled: boolean;
  conflicts: {
    categories: string[];
    preferred_translations: string[];
  };
  has_conflicts: boolean;
}

export interface TermsResponse {
  terms_revision: number | null;
  conflict_count: number;
  terms: Term[];
}

export type ThemeMode = "system" | "light" | "dark";
