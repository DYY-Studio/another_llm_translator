import type { KnowledgeFragment, KnowledgeSourceRange } from "./types";

export function compactKnowledgeRanges(fragment: Pick<KnowledgeFragment, "source_range" | "_positions">) {
  const groups: Array<{ first: KnowledgeSourceRange; last: KnowledgeSourceRange }> = [];
  for (const range of fragment.source_range ?? []) {
    const previous = groups.at(-1);
    const position = fragment._positions[range.segment_id];
    const previousPosition = previous && fragment._positions[previous.last.segment_id];
    if (previous && range.file_id === previous.last.file_id && range.part_id === previous.last.part_id
      && position >= 0 && previousPosition != null && previousPosition >= 0
      && (position === previousPosition || position === previousPosition + 1)) {
      previous.last = range;
    } else {
      groups.push({ first: range, last: range });
    }
  }
  return groups.map(({ first, last }) => ({
    segmentId: first.segment_id,
    key: `${first.segment_id}:${first.start}:${last.segment_id}:${last.end}`,
    label: first.segment_id === last.segment_id
      ? `${first.segment_id} [${first.start}–${last.end}]`
      : `${first.segment_id} — ${last.segment_id}`,
  }));
}
