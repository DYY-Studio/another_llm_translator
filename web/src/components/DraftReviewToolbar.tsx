import type { ReactNode } from "react";
import { translate, type Language } from "../i18n";

export function ReviewPagination({ page, pageCount, language, onPage }: {
  page: number; pageCount: number; language: Language; onPage: (page: number) => void;
}) {
  if (pageCount <= 1) return null;
  return <nav className="draft-review-pagination" aria-label={translate("review.pagination", language)}>
    <button type="button" className="quiet-button" aria-label={translate("review.previousPage", language)} disabled={page === 0} onClick={() => onPage(page - 1)}>‹</button>
    <span>{page + 1} / {pageCount}</span>
    <button type="button" className="quiet-button" aria-label={translate("review.nextPage", language)} disabled={page + 1 >= pageCount} onClick={() => onPage(page + 1)}>›</button>
  </nav>;
}

export function DraftReviewToolbar({ search, onSearch, placeholder, count, page, pageSize, onPage, language, children }: {
  search: string; onSearch: (value: string) => void; placeholder: string;
  count: number; page: number; pageSize: number; onPage: (page: number) => void;
  language: Language; children: ReactNode;
}) {
  return <div className="draft-review-toolbar term-decision-sticky">
    <div className="draft-review-filters">
      <input type="search" aria-label={placeholder} placeholder={placeholder} value={search} onChange={event => onSearch(event.target.value)} />
      {children}
    </div>
    <div className="draft-review-results">
      <span aria-live="polite">{translate(count ? "review.resultRange" : "review.noResults", language, {
        start: page * pageSize + 1, end: Math.min((page + 1) * pageSize, count), count,
      })}</span>
      <ReviewPagination page={page} pageCount={Math.ceil(count / pageSize)} language={language} onPage={onPage} />
    </div>
  </div>;
}
