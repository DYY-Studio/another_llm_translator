import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { errorMessage, translate, type Language } from "../i18n";
import { compactScanFailures } from "../termScanFailures";
import type { ProjectOverview, TerminologyFailures } from "../types";
import { Modal } from "./Modal";

export function TermScanFailuresDialog({ project, projectId, files, language, onClose, onLocate }: {
  project: string;
  projectId: string;
  files: ProjectOverview["files"];
  language: Language;
  onClose: () => void;
  onLocate: (segmentId: string) => void;
}) {
  const query = useQuery({
    queryKey: ["term-scan-failures", projectId],
    queryFn: async ({ signal }) => {
      const [failures, index] = await Promise.all([
        api<TerminologyFailures>(`/api/v1/projects/${project}/terms/failures`, { signal }),
        api<{ segment_ids: string[] }>(`/api/v1/projects/${project}/segments/ids`, {
          method: "POST", body: JSON.stringify({ stage: "translation" }), signal,
        }),
      ]);
      return { ...failures, ranges: compactScanFailures(failures.segments, index.segment_ids) };
    },
  });
  const data = query.data;
  const title = translate("terms.scanFailuresTitle", language);
  return <Modal ariaLabel={title} onClose={onClose} className="scan-failures-modal">
    <div className="directory-picker-heading">
      <h2>{title}</h2>
      <button className="quiet-button" onClick={onClose} aria-label={translate("textTranslation.close", language)}>×</button>
    </div>
    {query.isPending && <p>{translate("common.loading", language)}</p>}
    {query.isError && <div role="alert" className="error-text">
      <p>{errorMessage(query.error, language)}</p>
      <button onClick={() => { void query.refetch(); }}>{translate("common.retry", language)}</button>
    </div>}
    {!query.isError && data && <>
      <p className="muted">{translate("terms.scanFailureSummary", language, { count: data.failed, ranges: data.ranges.length })}</p>
      {data.truncated && <p className="warning-banner">{translate("terms.first200Failures", language)}</p>}
      <div className="scan-failure-ranges">
        {data.ranges.map((range) => {
          const file = files.find((item) => range.first.startsWith(`${item.file_id}-S`));
          return <article className="scan-failure-range" key={range.first}>
            <div className="scan-failure-range-heading">
              <strong>{file?.name}</strong>
              <span>{translate("terms.scanFailureCount", language, { count: range.count })}</span>
            </div>
            <button className="scan-failure-range-link" title={translate("terms.locateFailureRange", language)} onClick={() => onLocate(range.first)}>
              <code>{range.first === range.last ? range.first : `${range.first} — ${range.last}`}</code>
            </button>
            {range.errors.map((error) => <p key={`${error.stage}:${error.error_class}:${error.error_message}`}>
              <strong>{translate(error.stage === "content_summary" ? "stage.contentSummary" : error.stage === "translation" ? "stage.draftTranslation" : "stage.terminology", language)}</strong> <code>{error.error_class}</code> <span>{error.error_message}</span> <small>×{error.count}</small>
            </p>)}
          </article>;
        })}
        {!data.ranges.length && <p>{translate("terms.noScanFailures", language)}</p>}
      </div>
    </>}
    <div className="modal-actions">
      <button disabled={query.isFetching} onClick={() => { void query.refetch(); }}>{translate("terms.refreshScanFailures", language)}</button>
      <button onClick={onClose}>{translate("textTranslation.close", language)}</button>
    </div>
  </Modal>;
}
