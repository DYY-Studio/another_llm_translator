import type { TerminologyFailures } from "./types";

type ScanFailure = TerminologyFailures["segments"][number];
type OutputError = ScanFailure["errors"][number];

export interface ScanFailureRange {
  first: string;
  last: string;
  count: number;
  errors: Array<Pick<OutputError, "stage" | "error_class" | "error_message"> & { count: number }>;
}

export function compactScanFailures(failures: ScanFailure[], orderedIds: string[]): ScanFailureRange[] {
  const positions = new Map(orderedIds.map((id, index) => [id, index]));
  for (const failure of failures) {
    if (!positions.has(failure.segment_id)) throw new Error(`Segment position unavailable: ${failure.segment_id}`);
  }
  const sorted = [...failures].sort((a, b) => positions.get(a.segment_id)! - positions.get(b.segment_id)!);
  const ranges: ScanFailureRange[] = [];
  for (const failure of sorted) {
    const id = failure.segment_id;
    const previous = ranges.at(-1);
    const fileId = id.slice(0, id.lastIndexOf("-S"));
    const adjacent = previous && previous.last.slice(0, previous.last.lastIndexOf("-S")) === fileId
      && positions.get(id)! === positions.get(previous.last)! + 1;
    const range = adjacent ? previous : { first: id, last: id, count: 0, errors: [] };
    if (!adjacent) ranges.push(range);
    range.last = id;
    range.count += 1;
    for (const failureError of failure.errors) {
      const error = range.errors.find((item) => item.stage === failureError.stage
        && item.error_class === failureError.error_class && item.error_message === failureError.error_message);
      if (error) error.count += 1;
      else range.errors.push({ stage: failureError.stage, error_class: failureError.error_class, error_message: failureError.error_message, count: 1 });
    }
  }
  return ranges;
}
