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

export function parseRepetitionOverview(snapshot: RequestOverviewSnapshot) {
  const input = object(snapshot.input);
  let output: OverviewRecord = {};
  let invalid = false;
  if (snapshot.response_content) {
    try { output = object(JSON.parse(snapshot.response_content)); }
    catch { invalid = true; }
    if (!Array.isArray(output.groups) || !Array.isArray(output.keep) || !Array.isArray(output.uncertain)) invalid = true;
  }
  return repetitionOverview(input, output, invalid);
}

export function parseRepetitionOverviews(snapshot: RequestOverviewSnapshot) {
  const input = object(snapshot.input);
  let output: OverviewRecord = {};
  let invalid = false;
  if (snapshot.response_content) {
    try { output = object(JSON.parse(snapshot.response_content)); }
    catch { invalid = true; }
    if (!Array.isArray(output.repetitions)) invalid = true;
  }
  const results = new Map<string, OverviewRecord[]>();
  for (const item of records(output.repetitions)) {
    const id = text(item.id);
    results.set(id, [...(results.get(id) ?? []), item]);
  }
  return records(input.repetitions).map(group => {
    const matches = results.get(text(group.id)) ?? [];
    const result = matches.length === 1 ? matches[0] : {};
    const incomplete = !!snapshot.response_content && (matches.length !== 1 || !Array.isArray(result.groups)
      || !Array.isArray(result.keep) || !Array.isArray(result.uncertain));
    return {id: text(group.id), ...repetitionOverview({...input, ...group}, result, invalid || incomplete)};
  });
}

function repetitionOverview(input: OverviewRecord, output: OverviewRecord, invalid: boolean) {
  const translations = Array.isArray(input.translations) ? input.translations.map(text) : [];
  const contexts = object(input.context);
  const files = object(input.files);
  const parts = object(input.parts);
  const occurrences = records(input.occurrences);
  const members = new Map(occurrences.map(item => [item.id, item]));
  const groups = records(output.groups);
  const proposals = new Map(groups.flatMap(group => Array.isArray(group.members) ? group.members.map(id => [id, group] as const) : []));
  const appliedMembers = new Set(groups.flatMap(group => Array.isArray(group.apply) ? group.apply : Array.isArray(group.members) ? group.members : []));
  const uncertain = new Set(Array.isArray(output.uncertain) ? output.uncertain : []);
  const kept = new Set(Array.isArray(output.keep) ? output.keep : []);
  return {source: text(input.source), invalid,
    groups: groups.map(group => ({members: Array.isArray(group.members) ? group.members.map(text) : [], reason: text(group.reason)})),
    occurrences: occurrences.map(item => {
    const proposal = proposals.get(item.id);
    const applied = proposal && appliedMembers.has(item.id);
    const target = applied ? members.get(proposal.target) : undefined;
    const context = (refs: unknown) => Array.isArray(refs) ? refs.map(id => text(contexts[String(id)])) : [];
    return {id: text(item.id), file: String(item.file), filename: text(files[String(item.file)]),
      part: typeof item.part === "number" ? text(parts[String(item.part)]) : text(item.part), position: String(item.position), current: translations[Number(item.variant)] ?? "",
      result: target ? translations[Number(target.variant)] : null,
      uncertain: uncertain.has(item.id), kept: kept.has(item.id) || !!proposal && !applied,
      previous: context(item.previous), next: context(item.next)};
  })};
}

export function parseRequestOverview(snapshot: RequestOverviewSnapshot) {
  const input = object(snapshot.input);
  const output: OverviewRecord[] = [];
  const issues: string[] = [];
  const content = snapshot.response_content?.trim().replace(/^```(?:jsonl|json)?\s*\n?/i, "").replace(/\n?```$/, "") ?? "";
  for (const line of content.split("\n").filter((line) => line.trim())) {
    try {
      const item = object(JSON.parse(line));
      const valid = ["segment", "term", "no_terms", "summary", "decision", "entity_knowledge", "end"].includes(text(item.type))
        && (item.type !== "segment" || (typeof item.id === "string" && (typeof item.translation === "string" || item.status === "accepted" || (item.status === "suggested" && typeof item.suggested_text === "string"))))
        && (item.type !== "entity_knowledge" || (typeof item.entity === "string" && Array.isArray(item.items)
          && item.items.every((raw) => { const entry = object(raw); return typeof entry.text === "string"
            && Array.isArray(entry.refs) && entry.refs.every((ref) => typeof ref === "string"); })))
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
  const entityResults = new Map<string, OverviewRecord[]>();
  const entityIds = new Set(records(input.entities).map((item) => text(item.entity)));
  for (const item of output.filter((item) => item.type === "entity_knowledge")) {
    const key = text(item.entity);
    if (!entityIds.has(key)) issues.push("unknown");
    const matches = entityResults.get(key) ?? [];
    matches.push(item);
    entityResults.set(key, matches);
  }
  const entities = records(input.entities).map((item) => {
    const entity = text(item.entity);
    const fragments = records(item.fragments);
    const refs = new Set(fragments.map((fragment) => text(fragment.id)));
    const matches = entityResults.get(entity) ?? [];
    let result = matches.length === 1 ? matches[0] : null;
    if (matches.length > 1) issues.push("duplicate");
    if (content && !matches.length) issues.push("missing");
    if (result && records(result.items).some((entry) => !Array.isArray(entry.refs) || !entry.refs.length
      || entry.refs.some((ref) => !refs.has(ref)))) {
      issues.push("invalid");
      result = null;
    }
    return {entity, members: Array.isArray(item.members) ? item.members.map(text) : [],
      fragments, currentItems: records(item.current_items), result};
  });
  const complete = output.at(-1)?.type === "end";
  if (content && !complete) issues.push("incomplete");
  return {input, pairs, sources, entities, issues, complete,
    terms: output.filter((item) => item.type === "term"), noTerms: output.some((item) => item.type === "no_terms"),
    summaries: output.filter((item) => item.type === "summary"), decisions: output.filter((item) => item.type === "decision")};
}
