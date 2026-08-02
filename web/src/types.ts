export type Stage =
  | "overview"
  | "diagnostics"
  | "terminology"
  | "translation"
  | "proofreading"
  | "polishing"
  | "export"
  | "settings";

export type LLMStage =
  | "terminology"
  | "translation"
  | "proofreading"
  | "polishing";

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
  part_id: string;
  line_index: number;
  source: string;
  model_source?: string | null;
  format_count?: number;
  translation: ResultView | null;
  reviews: {
    proofreading: ReviewView;
    polishing: ReviewView;
  };
}

export interface ProjectOverview {
  name: string;
  path: string;
  nonempty_segment_count: number;
  files: Array<{
    file_id: string;
    file_order: number;
    name: string;
    document_adapter_id: string;
  }>;
  segments: Segment[];
}

export interface ProjectSummary {
  selector: string;
  name: string;
  project_id: string;
  path: string;
  external: boolean;
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
  processed_segments: number;
  total_segments: number;
  usage: TaskUsage;
}

export interface TaskUsage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  available: boolean;
}

export interface DiagnosticsResponse {
  metrics: {
    project: string | null;
    stage: string | null;
    active_requests: number;
    http_errors: number;
    retry_count: number;
    rate_limit_waiting_requests: number;
    latest_latency_ms: number | null;
    input_tokens: number;
    output_tokens: number;
    usage_available: boolean;
    throughput_tokens_per_second: number | null;
  };
  logs: Array<{
    timestamp: string;
    level: string;
    project: string;
    stage: string;
    message: string;
  }>;
  requests: Array<{
    timestamp: string;
    project: string | null;
    stage: string | null;
    request_id: string;
    model: string;
    status: "running" | "retrying" | "completed" | "failed" | "interrupted";
    attempt_count: number;
    last_http_status: number | null;
    latest_latency_ms: number | null;
    has_content: boolean;
    has_reasoning: boolean;
    error: string | null;
  }>;
  filters: {
    levels: string[];
    projects: string[];
    stages: string[];
  };
}

export interface DiagnosticsRequestDetail {
  timestamp: string;
  project: string | null;
  stage: string | null;
  request_id: string;
  model: string;
  status: "running" | "retrying" | "completed" | "failed" | "interrupted";
  max_attempts: number;
  segment_id_map: Record<string, string>;
  messages: Array<{
    role: string;
    content: string;
    truncated: boolean;
  }>;
  response_content: string | null;
  response_content_truncated: boolean;
  reasoning_content: string | null;
  reasoning_content_truncated: boolean;
  attempts: Array<{
    attempt: number;
    http_status: number | null;
    latency_ms: number;
    outcome: "succeeded" | "http_error" | "network_error";
  }>;
  error: string | null;
}

export interface ModelRow {
  id: string;
  display: string;
}

export interface TaskOptions {
  stage: LLMStage;
  selected: number;
  completed: number;
  pending: number;
  failed: number;
  fingerprint_count: number;
  current_fingerprint: string;
  current_fingerprint_completed: number;
  mismatched_fingerprint_completed: number;
  running_run: {
    run_id: string;
    started_at: string | null;
    scope: Record<string, unknown> | null;
    previous: { model: string; endpoint: string };
    current: { model: string; endpoint: string };
  } | null;
}

export interface RunDecision {
  force: boolean;
  reuse_mixed_fingerprints: boolean;
  run_action: "resume" | "decline" | null;
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
    alias_primaries: Array<{
      alias: string;
      primary_source: string;
      reason: "policy" | "cycle" | "multiple_owners";
    }>;
  };
  has_conflicts: boolean;
}

export interface TermsResponse {
  terms_revision: number | null;
  conflict_count: number;
  terms: Term[];
}

export type ThemeMode = "system" | "light" | "dark";

export interface ProjectConfig {
  project: {
    target_language: string;
    output_encoding: string;
  };
  input: {
    encoding_confidence_threshold: number;
    fallback_encoding: string;
  };
  llm: {
    preset: string;
    preset_terminology: string;
    preset_translation: string;
    preset_proofreading: string;
    preset_polishing: string;
    temperature_terminology: number;
    temperature_translation: number;
    temperature_proofreading: number;
    temperature_polishing: number;
  };
  execution: {
    scheduling_mode: "ordered_by_file" | "parallel";
  };
  chunking: {
    target_chunk_input_tokens: number;
    allow_split_oversized_segment: boolean;
  };
  context: Record<"terminology" | "translation" | "proofreading" | "polishing", {
    enabled: boolean;
    previous_segments: number;
  }>;
  terminology: {
    unicode_normalization: "NFKC";
    case_insensitive: true;
    max_terms_per_segment: number;
    alias_primary_collision: "conflict" | "merge";
  };
  validation: {
    translation: {
      japanese_kana: boolean;
      korean_hangul: boolean;
      max_retry_attempts: number;
      exhausted_mode: "fail" | "warning";
    };
  };
  retry: {
    http_max_attempts: number;
    format_max_attempts: number;
    base_delay_seconds: number;
    max_delay_seconds: number;
    jitter_seconds: number;
  };
  debug: {
    enabled: boolean;
    inject_429_every: number;
    inject_500_every: number;
    inject_timeout_every: number;
    inject_invalid_json_every: number;
    inject_missing_segment_every: number;
  };
}

export interface LLMPresetSummary {
  preset_id: string;
  adapter_id?: string;
  model?: string;
  selected: boolean;
  valid: boolean;
  digest?: string;
  error?: string;
}

export interface LLMPreset {
  schema_version: 1;
  preset_id: string;
  adapter_id: string;
  base_url: string;
  endpoint: string;
  model: string;
  api_key_env: string;
  proxy_url: string;
  context_window_tokens: number;
  max_output_tokens: number;
  context_safety_margin_tokens: number;
  token_safety_factor: number;
  requests_per_minute: number;
  input_tokens_per_minute: number;
  max_parallel: number;
  request_timeout_seconds: number;
  extra_body: Record<string, unknown>;
}
