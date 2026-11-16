import { type KeyboardEvent, memo, useEffect, useId, useMemo, useState } from "react";
import type { CodeFile } from "../../clients/code/codeApi";
import { languageKind } from "./codeLanguage";
import type { CodeToken } from "./codemirror";
import "./CodeViews.css";
import "./codemirror.css";

interface FileViewProps {
  file: CodeFile;
  canQuote: boolean;
  onQuote: (ref: string, start: number, end: number) => void;
  onOpen: (line: number | null) => void;
}

interface Selection {
  path: string;
  anchor: number;
  focus: number;
  start: number;
  end: number;
}

interface SourceLine {
  text: string;
  from: number;
}

interface LineChunk {
  from: number;
  text: string;
  className?: string;
}

const NO_TOKENS: readonly CodeToken[] = [];

const CodeLine = memo(function CodeLine(props: {
  id: string;
  n: number;
  text: string;
  chunks: readonly LineChunk[] | null;
  chosen: boolean;
}) {
  return (
    <div
      id={props.id}
      role="option"
      aria-selected={props.chosen}
      tabIndex={-1}
      data-line={props.n}
      className={props.chosen ? "code-line code-line-selected" : "code-line"}
    >
      <span className="code-gutter">{props.n}</span>
      <span className="code-text">
        {props.chunks === null
          ? props.text
          : props.chunks.map((chunk) =>
              chunk.className ? (
                <span key={chunk.from} className={chunk.className}>
                  {chunk.text}
                </span>
              ) : (
                chunk.text
              ),
            )}
      </span>
    </div>
  );
});

const copy = {
  newline: { lf: "LF", crlf: "CRLF", cr: "CR", mixed: "Mixed", none: "None" },
  size: (n: number) => `${n} bytes`,
  quote: "Quote in chat",
  open: "Open in VS Code",
  notQuotable: "Only files in the active code folder can be quoted",
  noSelection: "Select the lines to quote first",
  reasons: {
    binary: "This file is binary, so it is not shown.",
    unsupported_encoding: "This file's encoding is not supported, so it is not shown.",
    too_large: "This file is over 1 MiB, so it is not shown.",
  },
};

function splitLines(text: string): SourceLine[] {
  const lines = text.split(/\r\n|\r|\n/);
  if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
  let from = 0;
  return lines.map((line) => {
    const result = { text: line, from };
    const end = from + line.length;
    from = end + (text[end] === "\r" && text[end + 1] === "\n" ? 2 : 1);
    return result;
  });
}

/** Token offsets name the original UTF-16 source, including its line separators. */
function lineChunks(lines: readonly SourceLine[], tokens: readonly CodeToken[]) {
  let index = 0;
  return lines.map((line): readonly LineChunk[] | null => {
    const end = line.from + line.text.length;
    const chunks: LineChunk[] = [];
    let cursor = line.from;
    let token = tokens[index];
    while (token && token.to <= line.from) token = tokens[++index];
    while (token && token.from < end) {
      const from = Math.max(line.from, token.from);
      const to = Math.min(end, token.to);
      if (from > cursor) {
        chunks.push({ from: cursor, text: line.text.slice(cursor - line.from, from - line.from) });
      }
      chunks.push({
        from,
        text: line.text.slice(from - line.from, to - line.from),
        className: token.className,
      });
      cursor = to;
      if (token.to > end) break;
      token = tokens[++index];
    }
    if (chunks.length === 0) return null;
    if (cursor < end) chunks.push({ from: cursor, text: line.text.slice(cursor - line.from) });
    return chunks;
  });
}

export function FileView({ file, canQuote, onQuote, onOpen }: FileViewProps) {
  const uid = useId();
  const [picked, setPicked] = useState<Selection | null>(null);
  const [highlighted, setHighlighted] = useState<{
    path: string;
    text: string;
    tokens: readonly CodeToken[];
  } | null>(null);
  // A selection made in another file is stale: ignore it rather than clear it in an effect.
  const selection = picked !== null && picked.path === file.path ? picked : null;

  function pick(line: number, extend: boolean) {
    const anchor = extend && selection !== null ? selection.anchor : line;
    setPicked({
      path: file.path,
      anchor,
      focus: line,
      start: Math.min(anchor, line),
      end: Math.max(anchor, line),
    });
  }

  function onKey(e: KeyboardEvent<HTMLDivElement>, count: number) {
    const from = selection?.focus ?? 0;
    const target =
      e.key === "ArrowDown"
        ? Math.min(count, from + 1)
        : e.key === "ArrowUp"
          ? Math.max(1, from - 1)
          : e.key === "Home"
            ? 1
            : e.key === "End"
              ? count
              : null;
    if (target === null) return;
    e.preventDefault();
    pick(target, e.shiftKey);
    document.getElementById(`${uid}-${target}`)?.scrollIntoView?.({ block: "nearest" });
  }

  const quoteHint = !canQuote ? copy.notQuotable : selection === null ? copy.noSelection : null;
  const fileText = file.text;
  const filePath = file.path;
  const lines = useMemo(() => (fileText === null ? null : splitLines(fileText)), [fileText]);
  const tokens =
    highlighted?.path === filePath && highlighted.text === fileText
      ? highlighted.tokens
      : NO_TOKENS;
  const chunks = useMemo(
    () => (lines === null ? null : lineChunks(lines, tokens)),
    [lines, tokens],
  );

  useEffect(() => {
    if (fileText === null || languageKind(filePath) === null) return;
    const controller = new AbortController();
    void import("./codemirror")
      .then(({ codeTokens }) => codeTokens(filePath, fileText, controller.signal))
      .then((result) => {
        if (!controller.signal.aborted) {
          setHighlighted({ path: filePath, text: fileText, tokens: result });
        }
      })
      .catch(() => {
        // Optional coloring cannot replace the original text or surface parser errors.
      });
    return () => controller.abort();
  }, [filePath, fileText]);

  return (
    <section className="code-file code-syntax">
      <header className="code-file-head">
        <span className="mono">{file.path}</span>
        {file.encoding !== null ? <span className="mono">{file.encoding}</span> : null}
        {file.newline !== null ? <span>{copy.newline[file.newline]}</span> : null}
        <span>{copy.size(file.size)}</span>
      </header>
      <div className="code-file-actions">
        <button
          type="button"
          aria-disabled={quoteHint !== null}
          aria-describedby={quoteHint !== null ? "code-quote-hint" : undefined}
          title={quoteHint ?? undefined}
          onClick={() => {
            if (quoteHint === null && selection !== null) {
              onQuote(file.path, selection.start, selection.end);
            }
          }}
        >
          {copy.quote}
        </button>
        {quoteHint !== null ? (
          <span id="code-quote-hint" className="sr-only">
            {quoteHint}
          </span>
        ) : null}
        <button type="button" onClick={() => onOpen(selection === null ? null : selection.start)}>
          {copy.open}
        </button>
      </div>
      {lines === null ? (
        file.reason !== null ? (
          <p className="code-reason">{copy.reasons[file.reason]}</p>
        ) : null
      ) : (
        // One tab stop: the container takes focus and names the current row; rows are not focusable.
        <div
          className="code-lines"
          role="listbox"
          aria-label={file.path}
          aria-multiselectable="true"
          tabIndex={0}
          aria-activedescendant={selection !== null ? `${uid}-${selection.focus}` : undefined}
          onClick={(e) => {
            const row = (e.target as Element).closest("[data-line]");
            e.currentTarget.focus(); // a clicked row is focusable (-1); keep focus on the one stop
            if (row !== null) pick(Number(row.getAttribute("data-line")), e.shiftKey);
          }}
          onKeyDown={(e) => onKey(e, lines.length)}
        >
          {lines.map((line, i) => {
            const n = i + 1;
            const chosen = selection !== null && n >= selection.start && n <= selection.end;
            return (
              <CodeLine
                key={n}
                id={`${uid}-${n}`}
                n={n}
                text={line.text}
                chunks={chunks?.[i] ?? null}
                chosen={chosen}
              />
            );
          })}
        </div>
      )}
    </section>
  );
}
