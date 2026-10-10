/** Compact consecutive Segment IDs (or request-local numeric references). */
export function compactSegmentReferences(ids: string[]): string[] {
  const groups: string[] = [];
  let first = "", last = "";
  const ordinal = (id: string) => id.match(/^(.*-S)(\d+)$/) ?? id.match(/^()(\d+)$/);
  const flush = () => { if (first) groups.push(first === last ? first : `${first}—${last}`); };
  for (const id of ids) {
    const previous = ordinal(last), current = ordinal(id);
    if (first && previous && current && previous[1] === current[1] && Number(current[2]) === Number(previous[2]) + 1) last = id;
    else { flush(); first = last = id; }
  }
  flush();
  return groups;
}
