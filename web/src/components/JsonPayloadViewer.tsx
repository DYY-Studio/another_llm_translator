import { useState } from "react";
import { translate, type Language } from "../i18n";

type JsonViewMode = "readable" | "json";

type JsonPayloadViewerProps = {
  value: unknown;
  language: Language;
};

function isContainer(value: unknown): value is Record<string, unknown> | unknown[] {
  return value !== null && typeof value === "object";
}

function entriesFor(value: Record<string, unknown> | unknown[]): Array<[string, unknown]> {
  return Array.isArray(value)
    ? value.map((item, index) => [`[${index}]`, item])
    : Object.entries(value);
}

function JsonPrimitive({ value }: { value: unknown }) {
  if (typeof value === "string") {
    return <span className="history-json-value history-json-string">{value}</span>;
  }
  if (value === null) {
    return <span className="history-json-value history-json-null">null</span>;
  }
  if (typeof value === "undefined") {
    return <span className="history-json-value history-json-null">undefined</span>;
  }
  return <span className="history-json-value">{String(value)}</span>;
}

function JsonNode({
  label,
  value,
  depth,
}: {
  label?: string;
  value: unknown;
  depth: number;
}) {
  const [open, setOpen] = useState(depth === 0);
  if (!isContainer(value)) {
    return (
      <div className="history-json-field">
        {label && <span className="history-json-key">{label}</span>}
        <JsonPrimitive value={value} />
      </div>
    );
  }

  const entries = entriesFor(value);
  const marker = Array.isArray(value) ? "[]" : "{}";
  return (
    <details
      className="history-json-node"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <span className="history-json-key">{label ?? marker}</span>
        {label && <code className="history-json-marker">{marker}</code>}
      </summary>
      {open && (
        <div className="history-json-children">
          {entries.map(([entryLabel, entryValue]) => (
            <JsonNode key={entryLabel} label={entryLabel} value={entryValue} depth={depth + 1} />
          ))}
        </div>
      )}
    </details>
  );
}

function rawJson(value: unknown): string {
  const formatted = JSON.stringify(value, null, 2);
  return formatted === undefined ? String(value) : formatted;
}

export function JsonPayloadViewer({ value, language }: JsonPayloadViewerProps) {
  const [mode, setMode] = useState<JsonViewMode>("readable");
  return (
    <div className="history-json-viewer">
      <nav className="history-json-mode" role="tablist" aria-label={translate("diagnostics.history.jsonMode", language)}>
        <button
          type="button"
          role="tab"
          aria-selected={mode === "readable"}
          className={mode === "readable" ? "active" : ""}
          onClick={() => setMode("readable")}
        >
          {translate("diagnostics.history.jsonReadable", language)}
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={mode === "json"}
          className={mode === "json" ? "active" : ""}
          onClick={() => setMode("json")}
        >
          {translate("diagnostics.history.jsonRaw", language)}
        </button>
      </nav>
      {mode === "readable" ? (
        <div className="history-json-tree">
          <JsonNode value={value} depth={0} />
        </div>
      ) : (
        <pre className="history-code">{rawJson(value)}</pre>
      )}
    </div>
  );
}
