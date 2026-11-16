import { useEffect, useRef, useState } from "react";
import type { CheckRunJson, ChecksApi } from "../../clients/code/checksApi";
import { runLine } from "../../clients/code/checkWords";

export const POLL_MS = 300;
const MAX_CHARS = 2_097_152;
/** The longest unfinished escape held back for the next chunk; a longer one is cleaned as it is. */
const HOLD_MAX = 4096;

/* biome-ignore-start lint/suspicious/noControlCharactersInRegex: escapes and controls are the point */
const CSI = /\x1b\[[0-?]*[ -/]*[@-~]/g;
const OSC = /\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g;
const ESCAPE = /\x1b[ -/]*[0-~]/g;
const CONTROLS = /[\x00-\x08\x0b\x0c\x0e-\x1f]/g;
const ESCAPE_TAIL = new RegExp(
  `\\x1b(?:\\[[0-?]*[ -/]*|\\][^\\x07\\x1b]{0,${HOLD_MAX}}\\x1b?|[ -/]*)$`,
);
/* biome-ignore-end lint/suspicious/noControlCharactersInRegex: escapes and controls are the point */

/** The text without escape sequences and controls; carriage returns and line feeds stay. */
function stripEscapes(text: string): string {
  return text.replace(CSI, "").replace(OSC, "").replace(ESCAPE, "").replace(CONTROLS, "");
}

/** Plain text of terminal output: no escape sequences or controls, one line break per line. */
export function cleanOutput(text: string): string {
  return stripEscapes(text)
    .replace(/\r\n/g, "\n")
    .split("\n")
    .map((line) => line.split("\r").filter(Boolean).pop() ?? "")
    .join("\n");
}

/** The cleaned text so far: whole lines, the line in progress, and raw text waiting for its end. */
export interface Pending {
  lines: string;
  line: string;
  held: string;
  trimmed: boolean;
}

export function append(pending: Pending, chunk: string, last: boolean): Pending {
  let raw = pending.held + chunk;
  let held = "";
  if (!last) {
    const tail = ESCAPE_TAIL.exec(raw.slice(-HOLD_MAX - 8))?.[0] ?? "";
    raw = stripEscapes(raw.slice(0, raw.length - tail.length));
    // A CR that only an escape or control hides from the end is held as well, so the next text
    // replaces the line in progress, as it does when the bytes arrive in one answer.
    const cr = raw.endsWith("\r") ? "\r" : "";
    raw = raw.slice(0, raw.length - cr.length);
    held = cr + tail;
  }
  const cleaned = cleanOutput(pending.line + raw);
  const cut = cleaned.lastIndexOf("\n") + 1;
  let lines = pending.lines + cleaned.slice(0, cut);
  let line = cleaned.slice(cut);
  let trimmed = pending.trimmed;
  if (lines.length + line.length > MAX_CHARS) {
    trimmed = true;
    const at = lines.indexOf("\n", lines.length + line.length - MAX_CHARS - 1);
    lines = at === -1 ? "" : lines.slice(at + 1);
    line = line.slice(-MAX_CHARS);
  }
  return { lines, line, held, trimmed };
}

export const EMPTY: Pending = { lines: "", line: "", held: "", trimmed: false };

const copy = {
  heading: (name: string) => `Output of ${name}`,
  waiting: "Waiting for output…",
  none: "No output.",
  dropped: "Earlier output was dropped.",
  failed: "The output could not be loaded.",
};

export function CheckOutput({
  api,
  runId,
  name,
  onSettled,
}: {
  api: ChecksApi;
  runId: string;
  name: string;
  onSettled: (run: CheckRunJson) => void;
}) {
  const [shown, setShown] = useState({ text: "", dropped: false });
  const [run, setRun] = useState<CheckRunJson | null>(null);
  const [failed, setFailed] = useState(false);
  const settled = useRef(onSettled);
  useEffect(() => {
    settled.current = onSettled;
  });

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let next = 0;
    let pending = EMPTY;
    let dropped = false;
    setShown({ text: "", dropped: false });
    setRun(null);
    setFailed(false);

    function poll() {
      api.output(runId, next, controller.signal).then(
        (answer) => {
          if (controller.signal.aborted) return;
          next = answer.next;
          pending = append(pending, answer.text, answer.done);
          dropped ||= answer.dropped || pending.trimmed;
          setShown({ text: pending.lines + pending.line, dropped });
          if (answer.done) {
            setRun(answer.run);
            settled.current(answer.run);
          } else {
            timer = setTimeout(poll, POLL_MS);
          }
        },
        () => {
          if (!controller.signal.aborted) setFailed(true);
        },
      );
    }
    poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [api, runId]);

  const status = failed ? copy.failed : run ? runLine(run) : shown.text === "" ? copy.waiting : "";
  return (
    <div>
      <h4>{copy.heading(name)}</h4>
      {shown.dropped && <p>{copy.dropped}</p>}
      {shown.text !== "" && <pre className="check-output">{shown.text}</pre>}
      {run && shown.text === "" && <p>{copy.none}</p>}
      <p role="status" aria-live="polite">
        {status}
      </p>
    </div>
  );
}
