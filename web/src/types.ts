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
  | "terminology_decision"
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
  stage_errors?: Partial<Record<LLMStage, StageError>>;
  translation: ResultView | null;
  reviews: {
    proofreading: ReviewView;
    polishing: ReviewView;
  };
}

export interface SegmentDetail extends Segment {
  context: {
    before: Segment[];
    after: Segment[];
  };
}

export interface StageError {
  error_class: string;
  error_message: string;
  run_id?: string | null;
  request_id?: string | null;
  created_at?: string | null;
}

export interface ProjectOverview {
  name: string;
  path: string;
  nonempty_segment_count: number;
  completed_segments: number;
  total_segments: number;
  offset: number;
  limit: number;
  stage?: LLMStage;
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

export interface PromptLibraryEntry {
  id: string;
  digest: string;
}

export interface TranslationValidatorSummary {
  validator_id: string;
  version: string;
  label: string;
  plugin_id: string;
  plugin_version: string;
}

export interface TaskState {
  task_id: string;
  project: string;
  stage: string;
  status: string;
  error?: string | null;
  summary?: Record<string, unknown> | null;
  completed_segments: number;
  failed_segments: number;
  pending_segments: number;
  total_segments: number;
  failure_counts: Record<string, number>;
  usage: TaskUsage;
}

export interface TaskUsage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  available: boolean;
}

export type DiagnosticsRequestStatus =
  | "running"
  | "retrying"
  | "completed"
  | "failed"
  | "interrupted";

export interface DiagnosticsRequestSummary {
  timestamp: string;
  finished_at: string | null;
  project: string | null;
  stage: string | null;
  request_id: string;
  model: string;
  transport: "non_streaming" | "sse";
  status: DiagnosticsRequestStatus;
  attempt_count: number;
  last_http_status: number | null;
  latest_latency_ms: number | null;
  has_content: boolean;
  has_reasoning: boolean;
  error: string | null;
  detail_available: boolean;
  stream_event_count: number;
  stream_received_bytes: number;
  stream_first_event_latency_ms: number | null;
  provider_error_status: number | null;
}

export interface DiagnosticsResponse {
  metrics: {
    project: string | null;
    stage: string | null;
    active_requests: number;
    total_requests: number;
    http_errors: number;
    retry_count: number;
    rate_limit_waiting_requests: number;
    average_latency_ms: number | null;
    p95_latency_ms: number | null;
    input_tokens: number;
    output_tokens: number;
    usage_available: boolean;
    throughput_input_tokens_per_second: number | null;
    throughput_output_tokens_per_second: number | null;
    throughput_tokens_per_second: number | null;
  };
  logs: Array<{
    timestamp: string;
    level: string;
    project: string;
    stage: string;
    message: string;
  }>;
  requests: {
    session_id: string;
    cursor: number;
    reset: boolean;
    total: number;
    items: DiagnosticsRequestSummary[];
  };
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
  transport: "non_streaming" | "sse";
  stream_event_count: number;
  stream_received_bytes: number;
  stream_first_event_latency_ms: number | null;
  provider_error_status: number | null;
  status: DiagnosticsRequestStatus;
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
    transport: "non_streaming" | "sse";
    http_status: number | null;
    latency_ms: number;
    outcome: "succeeded" | "http_error" | "network_error" | "stream_error" | "response_parse_error" | "cancelled";
    provider_error_status: number | null;
    stream_event_count?: number;
    stream_received_bytes?: number;
    stream_first_event_latency_ms?: number | null;
  }>;
  error: string | null;
}

export interface ModelRow {
  id: string;
  display: string;
}

export interface TaskOptions {
  stage: LLMStage;
  preset: {
    id: string;
    model: string;
  };
  selected: number;
  completed: number;
  pending: number;
  failed: number;
  current_fingerprint_completed: number;
  mismatched_fingerprint_completed: number;
  protected?: number;
  has_pending_draft?: boolean;
  estimated_requests?: number;
  estimated_input_tokens?: number;
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
  group_primary: string | null;
  disabled: boolean;
  conflicts: {
    categories: string[];
    preferred_translations: string[];
    alias_primaries: Array<{
      alias: string;
      primary_source: string;
      reason: "policy" | "cycle" | "multiple_owners";
    }>;
    group_claims: Array<{
      entry: string;
      claimed_by: string;
      alias: string;
      reason: "policy" | "multiple_owners" | "cycle" | "group_collision";
    }>;
  };
  has_conflicts: boolean;
}

export interface TermDecisionState {
  normalized: string;
  source: string;
  category: string | null;
  description: string | null;
  preferred_translation: string | null;
  aliases: string[];
  group_primary: string | null;
  disabled: boolean;
}

export interface TermDecisionEvidence {
  hit_count: number;
  source_hit_count: number;
  alias_hit_counts: Record<string, number>;
  samples: Array<{ file_id: string; segment_id: string; source: string }>;
}

export interface TermDecisionProposal {
  proposal_id: string;
  kind: "term_update" | "relationship";
  normalized: string[];
  before: TermDecisionState[];
  after: TermDecisionState[];
  changes: string[];
  reason: string;
  evidence: Record<string, TermDecisionEvidence>;
}

export interface TermDecisionDraft {
  run_id: string;
  source_terms_revision: number;
  proposals: TermDecisionProposal[];
  needs_review: Array<{
    normalized: string;
    source: string;
    reason: string;
    evidence: TermDecisionEvidence;
  }>;
  rejected_proposal_ids: string[];
}

export interface TermDecisionReviewState {
  draft: TermDecisionDraft | null;
  rollback: { run_id: string; applied_terms_revision: number } | null;
}

export interface TermsResponse {
  terms_revision: number | null;
  conflict_count: number;
  terms: Term[];
  scan: TerminologyScan;
}

export interface TermHit {
  segment_id: string;
  file_id: string;
  line_index: number;
  source: string;
}

export interface TermHitsResponse {
  normalized: string;
  source: string;
  total: number;
  offset: number;
  limit: number;
  hits: TermHit[];
}

export interface RelatedTerm {
  normalized: string;
  source: string;
  preferred_translation: string | null;
  group_primary: string | null;
  group_root_normalized: string;
  group_root_source: string;
  group_size: number;
  disabled: boolean;
  has_conflicts: boolean;
  relation: "contains_selected" | "contained_by_selected";
  selected_match: string;
  selected_match_type: "source" | "alias";
  related_match: string;
  related_match_type: "source" | "alias";
  can_group: boolean;
  can_convert_alias: boolean;
  can_remove: boolean;
  blocked_reason: "group_claim" | "cross_group" | null;
}

export interface RelatedTermsResponse {
  normalized: string;
  related: RelatedTerm[];
}

export interface TerminologyScan {
  active_task_id: string | null;
  status: "none" | "active" | "completed" | "partial_published" | string;
  completed: number;
  failed: number;
  pending: number;
  candidate_count: number;
  candidate_records: number;
  failure_counts: Record<string, number>;
  failed_segments: Array<{
    segment_id: string;
    error_class: string;
    error_message: string;
    run_id?: string | null;
    request_id?: string | null;
  }>;
  failed_segments_truncated: boolean;
}

export type ThemeMode = "system" | "light" | "dark";

export interface ProjectConfig {
  project: {
    target_language: string;
    target_language_tag: string;
    output_encoding: string;
  };
  input: {
    encoding_confidence_threshold: number;
    fallback_encoding: string;
  };
  llm: {
    preset: string;
    preset_terminology: string;
    preset_terminology_decision: string;
    preset_translation: string;
    preset_proofreading: string;
    preset_polishing: string;
    temperature_terminology: number;
    temperature_terminology_decision: number;
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
    cross_boundary_batching: LLMStage[];
  };
  context: Record<"terminology" | "translation" | "proofreading" | "polishing", {
    enabled: boolean;
    previous_segments: number;
  }>;
  terminology: {
    unicode_normalization: "" | "NFC" | "NFD" | "NFKC" | "NFKD";
    case_insensitive: boolean;
    max_terms_per_segment: number;
    alias_primary_collision: "conflict" | "merge";
  };
  validation: {
    translation: {
      validators: string[];
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
  stream?: boolean;
  selected: boolean;
  valid: boolean;
  digest?: string;
  error?: string;
}

export interface LLMPreset {
  schema_version: 3;
  preset_id: string;
  adapter_id: string;
  base_url: string;
  endpoint: string;
  model: string;
  credential: LLMCredential;
  proxy_url: string;
  context_window_tokens: number;
  max_output_tokens: number;
  context_safety_margin_tokens: number;
  token_safety_factor: number;
  requests_per_minute: number;
  input_tokens_per_minute: number;
  max_parallel: number;
  request_timeout_seconds: number;
  stream: boolean;
  stream_endpoint: string;
  extra_body: Record<string, unknown>;
}

export interface LLMCredential {
  kind: "environment" | "keychain";
  name: string;
}

export interface CredentialSummary {
  id: string;
  updated_at: number;
}

export interface ServerStatus {
  lan: { enabled: boolean; bind_address: string };
  auth: { required: boolean; username: string };
  authed: boolean;
  loopback: boolean;
}

export interface InterfaceEntry {
  name: string;
  address: string;
}
