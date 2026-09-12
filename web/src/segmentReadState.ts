export type SegmentReadKind = "index" | "page" | "detail";

export interface SegmentReadErrors {
  index: string;
  pages: Record<string, string>;
  detail: string;
}

export function emptySegmentReadErrors(): SegmentReadErrors {
  return { index: "", pages: {}, detail: "" };
}

export function setSegmentReadError(
  current: SegmentReadErrors,
  kind: SegmentReadKind,
  message: string,
  pageKey?: string,
): SegmentReadErrors {
  if (kind === "index") return { ...current, index: message };
  if (kind === "detail") return { ...current, detail: message };
  if (!pageKey) return current;
  return { ...current, pages: { ...current.pages, [pageKey]: message } };
}

export function clearSegmentReadError(
  current: SegmentReadErrors,
  kind: SegmentReadKind,
  pageKey?: string,
): SegmentReadErrors {
  if (kind === "index") return { ...current, index: "" };
  if (kind === "detail") return { ...current, detail: "" };
  if (!pageKey || !(pageKey in current.pages)) return current;
  const pages = { ...current.pages };
  delete pages[pageKey];
  return { ...current, pages };
}

export function firstSegmentReadError(errors: SegmentReadErrors): string {
  return errors.index || Object.values(errors.pages)[0] || errors.detail;
}
