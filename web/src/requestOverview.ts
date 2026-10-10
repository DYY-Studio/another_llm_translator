export type OverviewRecord = Record<string, unknown>;
export interface RequestOverviewSnapshot {
  request_kind: "llm" | "decision";
  input: unknown;
  response_content?: string | null;
  segment_id_map?: Record<string, string>;
  segment_id?: string | null;
  questions?: Array<{ name: string; instructions: string; choices: Record<string, string> }>;
  answers?: Record<string, { choice: string | null; confidence: number; probabilities: Record<string, number>; refused: boolean }>;
  error?: string | null;
}
export function object(value: unknown): OverviewRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as OverviewRecord : {};
}
export function text(value: unknown): string { return typeof value === "string" ? value : ""; }
export function records(value: unknown): OverviewRecord[] { return Array.isArray(value) ? value.map(object) : []; }

export function parseRequestOverview(snapshot: RequestOverviewSnapshot) {
  const input = object(snapshot.input);
  const output: OverviewRecord[] = [];
  const issues: string[] = [];
  const content = snapshot.response_content?.trim().replace(/^```(?:jsonl|json)?\s*\n?/i, "").replace(/\n?```$/, "") ?? "";
  for (const line of content.split("\n").filter((line) => line.trim())) {
    try {
      const item = object(JSON.parse(line));
      const valid = ["segment", "term", "no_terms", "summary", "decision", "end"].includes(text(item.type))
        && (item.type !== "segment" || (typeof item.id === "string" && (typeof item.translation === "string" || item.status === "accepted" || (item.status === "suggested" && typeof item.suggested_text === "string"))))
        && (item.type !== "summary" || typeof item.text === "string")
        && (item.type !== "decision" || (typeof item.normalized === "string" && ["keep", "update", "disable", "needs_review"].includes(text(item.action))));
      if (!valid) issues.push("invalid");
      else output.push(item);
    } catch { issues.push("invalid"); }
  }
  const segmentResults = output.filter((item) => item.type === "segment");
  const segments = records(input.segments);
  const ids = new Set(segments.map((item) => text(item.id)));
  for (const item of segmentResults) if (!ids.has(text(item.id))) issues.push("unknown");
  const pairs = segments.map((item) => {
    const id = text(item.id);
    const matches = segmentResults.filter((result) => result.id === id);
    if (matches.length > 1) issues.push("duplicate");
    return {id, segmentId: snapshot.segment_id_map?.[id] ?? id, source: text(item.source), current: text(item.current_text),
      result: matches.length === 1 ? matches[0] : null};
  });
  const sourceItems = Array.isArray(input.source_segments) ? input.source_segments : [];
  const sources = pairs.length ? [] : sourceItems.map((item) => typeof item === "string" ? item : text(object(item).text));
  const decisionResults = output.filter((item) => item.type === "decision");
  const targetIds = new Set(records(input.terms).map((item) => text(item.normalized)));
  for (const item of decisionResults) {
    if (!targetIds.has(text(item.normalized))) issues.push("unknown");
    if (decisionResults.filter((other) => other.normalized === item.normalized).length > 1) issues.push("duplicate");
  }
  const complete = output.at(-1)?.type === "end";
  if (content && !complete) issues.push("incomplete");
  return {input, pairs, sources, issues, complete,
    terms: output.filter((item) => item.type === "term"), noTerms: output.some((item) => item.type === "no_terms"),
    summaries: output.filter((item) => item.type === "summary"), decisions: output.filter((item) => item.type === "decision")};
}
