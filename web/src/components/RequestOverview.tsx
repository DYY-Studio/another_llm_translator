import { translate, type Language } from "../i18n";
import { object, text, records, parseRequestOverview, type RequestOverviewSnapshot, type OverviewRecord } from "../requestOverview";
import type { TermDecisionState } from "../types";
import { compactSegmentReferences } from "../segmentReferences";
import { TermDecisionChanges } from "./TermDecisionChanges";

function SourceBlock({ values, title }: {values: string[]; title?: string}) {
  return <section className="overview-cell">{title && <h4>{title}</h4>}{values.map((value, index) => <p key={index}>{value}</p>)}</section>;
}
function termState(item: OverviewRecord): TermDecisionState {
  return {normalized: text(item.normalized), source: text(item.source), category: text(item.category) || null,
    description: text(item.description) || null, preferred_translation: text(item.preferred_translation) || null,
    aliases: Array.isArray(item.aliases) ? item.aliases.filter((value): value is string => typeof value === "string") : [],
    group_primary: text(item.group_primary) || null, disabled: item.disabled === true};
}

function DecisionOverview({ snapshot, language }: {snapshot: RequestOverviewSnapshot; language: Language}) {
  const input = object(snapshot.input);
  const terms = object(input.terms);
  const segments = records(input.segments);
  return <div className="decision-request-overview">
    {segments.length === 0 && <div className="overview-pair">
      <SourceBlock values={[text(input.source)]} />
      <SourceBlock values={[text(input.translation)]} />
    </div>}
    <div className="overview-decision-questions">{snapshot.questions?.map((question, index) => {
      const term = object(terms[question.name]);
      const segment = segments[index];
      const answer = snapshot.answers?.[question.name];
      const choiceKey = `overview.choice.${answer?.choice}`;
      const translatedChoice = answer?.choice ? translate(choiceKey, language) : translate("overview.noResult", language);
      const choiceLabel = translatedChoice === choiceKey ? answer?.choice : translatedChoice;
      return <article key={question.name}>
        {segment && <div className="overview-pair"><SourceBlock values={[text(segment.source)]} /><SourceBlock values={[text(segment.translation)]} /></div>}
        <div className="overview-question-heading"><strong>{text(term.source) || text(segment?.id) || question.name}</strong>
          {text(term.matched_text) && <span>{translate("overview.matched", language)}: {text(term.matched_text)}</span>}
          {text(term.preferred_translation) && <span>→ {text(term.preferred_translation)}</span>}
        </div>
        <div className="overview-answer"><strong>{answer?.refused ? translate("overview.refused", language) : choiceLabel}</strong>
          {answer && !answer.refused && <span>Confidence: {answer.confidence.toLocaleString(language, {maximumFractionDigits: 4})}</span>}
        </div>
        {answer && !answer.refused && <div className="overview-scores"><span>Scores</span>{Object.entries(answer.probabilities).map(([choice, score]) => <span key={choice} title={question.choices[choice]} className={choice === answer.choice ? "selected" : ""}>{choice}: {score.toLocaleString(language, {maximumFractionDigits: 4})}</span>)}</div>}
      </article>;
    })}</div>
  </div>;
}

export function RequestOverview({ snapshot, language, truncated = false }: {snapshot?: RequestOverviewSnapshot | null; language: Language; truncated?: boolean}) {
  if (!snapshot || snapshot.input == null) return <div className="diagnostics-empty">{translate(truncated ? "overview.truncated" : "overview.notRecorded", language)}</div>;
  const value = parseRequestOverview(snapshot);
  const reference = Array.isArray(value.input.reference_context) ? value.input.reference_context : [];
  const targets = records(value.input.terms);
  const before = targets.map(termState);
  const after = before.map((state) => {
    const matches = value.decisions.filter((item) => item.normalized === state.normalized);
    const result = matches.length === 1 ? matches[0] : null;
    return result?.action === "update" ? termState({...state, ...object(result.changes), disabled: false}) : {...state, disabled: result?.action === "disable" ? true : state.disabled};
  });
  const isTermDecision = targets.length > 0 && targets.some((item) => typeof item.normalized === "string") && !value.pairs.length && !value.sources.length;
  const summariesInput = records(value.input.summaries);
  const showTerms = value.terms.length > 0 || value.noTerms || (value.sources.length > 0 && !value.summaries.length);
  const summarySources = summariesInput.length ? summariesInput.map((item) => text(item.text)) : !showTerms ? value.sources : [];
  const anchors = records(value.input.anchors).map(termState);
  return <div className="request-overview">
    {truncated && <p className="warning-banner">{translate("overview.truncated", language)}</p>}
    {snapshot.error && <p className="warning-banner">{snapshot.error}</p>}
    <div className="overview-coverage">{translate("overview.coverage", language, {count: snapshot.request_kind === "decision" ? snapshot.questions?.length ?? 0 : isTermDecision ? targets.length : value.pairs.length || value.sources.length || summariesInput.length})}</div>
    {snapshot.request_kind === "decision" ? <DecisionOverview snapshot={snapshot} language={language} /> : <>
      {value.issues.length > 0 && <p className="warning-banner">{translate("overview.incomplete", language)}</p>}
      {value.pairs.map((pair, index) => <article className="overview-segment" key={`${pair.id}:${index}`}>
        <span className="overview-row-number" title={pair.segmentId} aria-label={pair.segmentId}>{index + 1}</span>
        <div className="overview-pair"><SourceBlock values={[pair.source]} /><section className="overview-cell">
          {pair.current && <p>{pair.current}</p>}
          {!pair.result ? <span className="muted">{translate("overview.noResult", language)}</span> : pair.result.status ? <>
            <strong className={`decision-status ${pair.result.status === "accepted" ? "accepted" : ""}`}>{translate(pair.result.status === "accepted" ? "overview.accepted" : "overview.suggested", language)}</strong>
            {text(pair.result.suggested_text) && <p>{text(pair.result.suggested_text)}</p>}{text(pair.result.reason) && <p className="decision-reason">{text(pair.result.reason)}</p>}
          </> : <p>{text(pair.result.translation)}</p>}
        </section></div>
      </article>)}
      {showTerms && <div className={value.sources.length ? "overview-pair" : "overview-results"}>
        {value.sources.length > 0 && <SourceBlock values={value.sources} />}
        <section className="overview-cell"><h4>{translate("overview.terms", language)}</h4>
          {value.terms.map((term, index) => <article className="overview-term" key={index}><strong>{text(term.source)}</strong><span>→ {text(term.preferred_translation) || "—"}</span>
            {text(term.category) && <small>{text(term.category)}</small>}{text(term.description) && <p>{text(term.description)}</p>}
            {Array.isArray(term.aliases) && term.aliases.length > 0 && <small>Alias: {term.aliases.join(" · ")}</small>}
          </article>)}
          {!value.terms.length && <span className="muted">{translate(value.noTerms ? "overview.noTerms" : "overview.noResult", language)}</span>}
        </section>
      </div>}
      {isTermDecision && <div className="term-decision-list">{before.map((state) => {
        const matches = value.decisions.filter((item) => item.normalized === state.normalized);
        const result = matches.length === 1 ? matches[0] : null;
        return <article className="term-decision-proposal" key={state.normalized}>
          <header className="term-decision-proposal-heading"><strong>{state.source}</strong><span>{result ? translate(`overview.action.${text(result.action)}`, language) : translate("overview.noResult", language)}</span></header>
          {result && <TermDecisionChanges beforeStates={[...before, ...anchors]} afterStates={[...after, ...anchors]} focusNormalized={state.normalized} language={language} />}
          {result && <p className="decision-reason">{text(result.reason)}</p>}
        </article>;
      })}</div>}
      {(summariesInput.length > 0 || value.summaries.length > 0) && <div className={summarySources.length ? "overview-pair" : "overview-results"}>
        {summarySources.length > 0 && <SourceBlock values={summarySources} title={summariesInput.length ? translate("overview.inputSummaries", language) : undefined} />}
        <section className="overview-cell"><h4>{translate("overview.summary", language)}</h4>{value.summaries.length ? value.summaries.map((item, index) => <article key={index}>
          {Array.isArray(item.refs) && <small>{compactSegmentReferences(item.refs.map((id) => snapshot.segment_id_map?.[String(id)] ?? String(id))).join(" · ")}</small>}<p>{text(item.text)}</p>
        </article>) : <span className="muted">{translate("overview.noResult", language)}</span>}</section>
      </div>}
    </>}
    {(reference.length > 0 || records(value.input.anchors).length > 0) && <details className="overview-reference"><summary>{translate("overview.references", language)}</summary>
      {reference.map((item, index) => <p key={index}>{typeof item === "string" ? item : text(object(item).source)}</p>)}
      {records(value.input.anchors).map((item, index) => <p key={`anchor:${index}`}>{text(item.source)} · {text(item.preferred_translation)}</p>)}
    </details>}
  </div>;
}
