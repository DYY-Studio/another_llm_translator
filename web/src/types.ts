export type Stage =
  | "overview"
  | "diagnostics"
  | "terminology"
  | "translation"
  | "proofreading"
  | "polishing"
  | "export"
  | "settings";

export interface SummaryBoundary {
  file_id: string;
  part_id: string;
  segment_count: number;
}

export type SummaryExpiryReason =
  | "stale_status"
  | "source_changed"
  | "dependency_changed"
  | "provenance_unavailable"
  | string;

export interface SummaryArtifact {
  record_type: "content_summary";
  record_id: string;
  kind: "fragment" | "reduction" | "full";
  file_id: string;
  part_id: string;
  status: "completed" | "failed" | "stale" | string;
  text: string | null;
  error_class?: string | null;
  error_message?: string | null;
  error?: string | null;
  refs?: string[];
  source_range: Record<string, unknown>;
  source_changed: boolean;
  expired: boolean;
  expiry_reason: SummaryExpiryReason | null;
  provenance?: {
    origin?: string;
    artifact_ids?: string[];
    source_ranges?: Array<Record<string, unknown>>;
    dependencies?: Array<{
      record_id: string;
      kind: "fragment" | "reduction";
      text_digest: string;
      source_digest: string;
    }>;
    [key: string]: unknown;
  };
  source_digest: string;
  input_digest: string;
  prompt_digest: string;
  model: string;
  run_id?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface SummariesResponse {
  participation: Array<{
    file_id: string;
    part_id: string;
    selected: boolean;
  }>;
  boundaries: SummaryBoundary[];
  artifacts: SummaryArtifact[];
}

export type SettingsField =
  | "target_language"
  | "target_language_tag"
  | "output_encoding";

export type LLMStage =
  | "terminology"
  | "terminology_decision"
  | "translation"
  | "proofreading"
  | "polishing";

export type RunStage = LLMStage | "content_summary";

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
  storage: {
    total_bytes: number;
    sqlite_bytes: number;
  };
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
    has_run_options: boolean;
    part_ids: string[];
    size_bytes: number;
  }>;
  segments: Segment[];
}

export interface SegmentQueryResponse {
  completed_segments: number;
  total_segments: number;
  offset: number;
  limit: number;
  stage: LLMStage;
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
  repair_needed: boolean;
}

export type StorageCategoryId =
  | "settings"
  | "logs"
  | "sqlite"
  | "input"
  | "project_config"
  | "run_snapshots"
  | "debug_attachments"
  | "output"
  | "snapshots"
  | "other";

export interface StorageCategory {
  id: StorageCategoryId;
  bytes: number;
  file_count: number;
  reclaimable_bytes: number;
  can_clear: boolean;
  blocked_reason: string | null;
}

export interface StorageProjectSummary {
  selector: string;
  name: string;
  project_id: string | null;
  path: string;
  external: boolean;
  categories: StorageCategory[];
  total_bytes: number;
  reclaimable_bytes: number;
  complete: boolean;
  errors: string[];
}

export interface StorageSummary {
  scanned_at: string;
  complete: boolean;
  errors: string[];
  total_bytes: number;
  reclaimable_bytes: number;
  global: StorageCategory[];
  projects: StorageProjectSummary[];
}

export interface StorageDebugRun {
  run_id: string;
  stage: string | null;
  status: string | null;
  bytes: number;
  file_count: number;
  reclaimable_bytes: number;
  can_clear: boolean;
  blocked_reason: string | null;
}

export interface StorageOutputFile {
  path: string;
  bytes: number;
  can_clear: boolean;
  blocked_reason: string | null;
}

export interface StorageLogGroup {
  id: string;
  bytes: number;
  file_count: number;
  reclaimable_bytes: number;
  can_clear: boolean;
  blocked_reason: string | null;
}

export interface StorageProjectDetail {
  complete: boolean;
  project: StorageProjectSummary;
  debug_runs: StorageDebugRun[];
  output_files: StorageOutputFile[];
  logs: StorageLogGroup[];
  errors: string[];
}

export interface StorageCleanupResult {
  affected_files: number;
  reclaimed_bytes: number;
}

export interface ErrorPayload {
  code: string;
  params: Record<string, unknown>;
  error: string;
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
  project_id: string;
  stage: string;
  status: string;
  include_summaries?: boolean;
  summary_selection?: Array<{ file_id: string; part_id: string }>;
  error?: ErrorPayload | null;
  summary?: Record<string, unknown> | null;
  completed_segments: number;
  failed_segments: number;
  pending_segments: number;
  total_segments: number;
  summary_selection_progress?: { completed: number; failed: number; total: number } | null;
  failure_counts: Record<string, number>;
  usage: TaskUsage;
}

export interface TaskUsage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  available: boolean;
  partial: boolean;
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
  task_id?: string | null;
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
    usage_partial: boolean;
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
    retry_round?: number | null;
    key_index?: number | null;
    transport: "non_streaming" | "sse";
    http_status: number | null;
    latency_ms: number;
    outcome: "succeeded" | "http_error" | "network_error" | "stream_error" | "response_parse_error" | "authentication_error" | "rate_limit_error" | "cancelled";
    provider_error_status: number | null;
    stream_event_count?: number;
    stream_received_bytes?: number;
    stream_first_event_latency_ms?: number | null;
  }>;
  error: string | null;
}

export type HistoricalSnapshotStatus =
  | "available"
  | "missing"
  | "invalid"
  | "unavailable";

export interface HistoricalSnapshot {
  status: HistoricalSnapshotStatus;
  format?: "text" | "json" | "toml";
  content?: string;
}

export interface HistoricalPromptVariant {
  name: string;
  requirements: string[];
  primary_mode: string | null;
  snapshot: HistoricalSnapshot;
}

export interface HistoricalExecutionSnapshots {
  config: HistoricalSnapshot;
  prompt: HistoricalSnapshot;
  adapter: HistoricalSnapshot;
  preset: HistoricalSnapshot;
  requirements: HistoricalSnapshot;
  prompt_variants:
    | { status: "available"; items: HistoricalPromptVariant[] }
    | HistoricalSnapshot;
}

export interface HistoricalRunExecution {
  id: string;
  kind: "root" | "continuation";
  started_at?: string;
  completed_at?: string;
  fingerprint?: string;
  prompt_language?: string;
  primary_mode?: string;
  scope: Record<string, unknown> | null;
  selected_segment_count?: number;
  requested_segment_count?: number;
  reused_segment_count?: number;
  document_adapters: Record<string, { adapter_id: string; version: string }>;
  document_adapter_options: Record<string, Record<string, unknown>>;
  document_adapter_prompt_requirements: Record<string, Record<string, string>>;
  prompt_languages: Record<string, string>;
  snapshots: HistoricalExecutionSnapshots;
}

export type HistoricalPayloadStatus =
  | "available"
  | "missing"
  | "invalid"
  | "unavailable";

export interface HistoricalDebugPayload {
  status: HistoricalPayloadStatus;
  value?: unknown;
}

export interface HistoricalDebugAttempt {
  attempt: number;
  retry_round: number | null;
  key_index: number | null;
  http_status: number | null;
  provider_error_status: number | null;
  outcome: string | null;
  status: string;
  error: string | null;
  request_payload: HistoricalPayloadStatus;
  response_payload: HistoricalPayloadStatus;
  error_payload: HistoricalPayloadStatus | HistoricalDebugPayload;
  request?: HistoricalDebugPayload;
  response?: HistoricalDebugPayload;
}

export interface HistoricalRunRequest {
  request_id: string;
  parent_request_id: string | null;
  stage: string | null;
  attempt_count: number;
  attempts: HistoricalDebugAttempt[];
}

export interface HistoricalRunRequestDetail {
  status: "available" | "partial";
  request_id: string;
  parent_request_id: string | null;
  stage: string | null;
  attempts: HistoricalDebugAttempt[];
  errors?: HistoricalRequestError[];
}

export interface HistoricalRequestError {
  line: number;
  reason: string;
}

export interface HistoricalRequestIndex {
  status: "available" | "partial" | "unavailable";
  reason?: string;
  errors?: HistoricalRequestError[];
  items: HistoricalRunRequest[];
}

export interface HistoricalRunSummary {
  run_id: string;
  project_id: string;
  project_name: string;
  stage: string;
  status: string;
  started_at: string | null;
  created_at: string | null;
  completed_at: string | null;
  selected_segment_count: number | null;
  requested_segment_count: number | null;
  reused_segment_count: number | null;
  completed_segment_count: number | null;
  failed_segment_count: number | null;
  failure_counts: Record<string, number>;
  warnings: string[];
  usage: TaskUsage | null;
  scope: Record<string, unknown> | null;
  debug_available: boolean;
  stage_fingerprint?: string;
  prompt_language?: string;
  review_stage?: string;
  primary_mode?: string;
  terms_revision?: number;
  document_adapters: Record<string, { adapter_id: string; version: string }>;
  document_adapter_options: Record<string, Record<string, unknown>>;
  document_adapter_prompt_requirements: Record<string, Record<string, string>>;
  translation_validators: Array<Record<string, string>>;
}

export interface HistoricalRunDetail extends HistoricalRunSummary {
  executions: HistoricalRunExecution[];
  requests: HistoricalRequestIndex;
}

export interface ModelRow {
  id: string;
  display: string;
}

export interface TaskOptions {
  stage: RunStage;
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
  summary_selected_boundaries?: number;
  summary_only_work?: boolean;
  summary_prompt_preflight?: {
    ok: boolean;
    language: string;
    required_stages: string[];
    missing: string[];
  };
  overflow_policy?: {
    allow_soft_target_overflow: boolean;
    anchor_overflow_mode: "error" | "trim" | "compact";
  };
  document_adapter_run_options?: Array<{
    adapter_id: string;
    file_count: number;
    options: Array<{ option_id: string; label: string; value: string | null }>;
  }>;
  running_run: {
    run_id: string;
    started_at: string | null;
    scope: Record<string, unknown> | null;
    previous: { model: string; endpoint: string };
    current: { model: string; endpoint: string };
    completed_steps?: number;
    total_steps?: number;
    resume_compatible?: boolean;
    resume_incompatibility_reason?: string | null;
    last_interruption?: {
      at: string;
      error_code: string;
      reason: string;
      request_id?: string;
      completed_steps: number;
      total_steps: number;
    };
  } | null;
}

export interface RunDecision {
  force: boolean;
  reuse_mixed_fingerprints: boolean;
  run_action: "resume" | "decline" | null;
}

export interface TermDecisionConflicts {
  categories: string[];
  preferred_translations: string[];
  alias_primaries: Array<{
    alias: string;
    primary_source: string;
    reason: "policy" | "cycle" | "multiple_owners" | "group_collision";
  }>;
  group_claims: Array<{
    entry: string;
    claimed_by: string;
    alias: string;
    reason: "policy" | "multiple_owners" | "cycle" | "group_collision";
  }>;
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
  conflicts: TermDecisionConflicts;
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
  samples: Array<{
    file_id: string;
    part_id?: string;
    segment_id: string;
    source: string;
    match_view?: string;
    matched_forms?: Array<{ kind: "source" | "alias"; value: string }>;
  }>;
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
  conflicts?: Record<string, TermDecisionConflicts>;
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
    conflicts?: TermDecisionConflicts;
  }>;
  rejected_proposal_ids: string[];
}

export interface TermDecisionManualReviewItem {
  run_id: string;
  normalized: string;
  source: string;
  reason: string;
  evidence: TermDecisionEvidence;
  conflicts?: TermDecisionConflicts;
  resolved: boolean;
}

export interface TermDecisionReviewState {
  draft: TermDecisionDraft | null;
  rollback: { run_id: string; applied_terms_revision: number } | null;
  manual_review: {
    items: TermDecisionManualReviewItem[];
    total: number;
    resolved: number;
    remaining: number;
  };
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
    preset_content_summary: string;
    preset_translation: string;
    preset_proofreading: string;
    preset_polishing: string;
    temperature_terminology: number;
    temperature_terminology_decision: number;
    temperature_content_summary: number;
    temperature_translation: number;
    temperature_proofreading: number;
    temperature_polishing: number;
  };
  execution: {
    scheduling_mode: "ordered_by_file" | "parallel";
  };
  chunking: {
    allow_split_oversized_segment: boolean;
    cross_boundary_batching: Array<"terminology" | "translation" | "proofreading" | "polishing">;
  };
  context: Record<"terminology" | "translation" | "proofreading" | "polishing", {
    enabled: boolean;
    previous_segments: number;
  }> & {
    translation: {
      enabled: boolean;
      previous_segments: number;
      previous_summaries: boolean;
    };
  };
  terminology: {
    unicode_normalization: "" | "NFC" | "NFD" | "NFKC" | "NFKD";
    case_insensitive: boolean;
    max_terms_per_segment: number;
    alias_primary_collision: "conflict" | "merge";
  };
  terminology_decision: {
    allow_soft_target_overflow: boolean;
    anchor_overflow_mode: "error" | "trim" | "compact";
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
  schema_version: 7;
  preset_id: string;
  adapter_id: string;
  base_url: string;
  model: string;
  credential: LLMCredential;
  proxy_url: string;
  context_window_tokens: number;
  target_chunk_input_tokens: number;
  max_output_tokens: number;
  context_safety_margin_tokens: number;
  token_safety_factor: number;
  requests_per_minute: number;
  input_tokens_per_minute: number;
  max_parallel: number;
  max_parallel_per_key: number;
  request_timeout_seconds: number;
  stream: boolean;
  stream_read_timeout_enabled: boolean;
  extra_body: Record<string, unknown>;
  extra_headers: Record<string, string>;
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
  tasks: { max_active_projects: number };
}

export interface InterfaceEntry {
  name: string;
  address: string;
}
