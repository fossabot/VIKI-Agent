import { type FocusEvent, type FormEvent, useEffect, useId, useRef, useState } from "react";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import {
  type PreviewJson,
  type PreviewsApi,
  PreviewsApiError,
} from "../../clients/code/previewsApi";
import { previewRefusal, previewStateWords } from "../../clients/code/previewWords";
import { useRoots } from "../../state/sessionRoots";
import { append, EMPTY, POLL_MS } from "./CheckOutput";
import { rootRefusal } from "./Worktrees";
import "./Previews.css";

/** How often the list is read again while a preview of it is live. */
const LIST_POLL_MS = 1000;
const PORT_MIN = 1024;
const PORT_MAX = 65535;

type View = { previewId: string; port: number };

const copy = {
  note: "A preview runs a command you type, through cmd.exe, in this folder or one of its worktrees, and opens the port you name in a separate browser window that has an empty profile, is not signed in to VIKI and sends its web requests only to that port. The command is not a sandbox: like a check, it can change files and use the network.",
  runIn: "Run in",
  thisFolder: "This folder",
  worktree: (branch: string) => `${branch} (worktree)`,
  unknownWorktree: "Unknown worktree",
  start: "Start a preview",
  command: "Command",
  port: "Port",
  submit: "Start",
  submitting: "Starting…",
  none: "No previews.",
  open: "Open preview",
  opening: "Opening…",
  opened: "Opened a preview window.",
  stop: "Stop",
  stopping: "Stopping…",
  output: "Output",
  remove: "Remove",
  back: "Back to the list",
  heading: (port: number) => `Output on port ${port}`,
  waiting: "Waiting for output…",
  noOutput: "No output.",
  dropped: "Earlier output was dropped.",
  failed: "The output could not be loaded.",
};

const codeOf = (error: unknown) => (error instanceof PreviewsApiError ? error.code : "unavailable");
const isLive = (preview: PreviewJson) => preview.ended_at === null;
/** Newest first; the stamps are fixed-length ISO times, so text order is time order. */
const newestFirst = (a: PreviewJson, b: PreviewJson) => b.started_at.localeCompare(a.started_at);

const FIRST_ENABLED = "button[data-preview]:not(:disabled)";

/**
 * Focuses the row button a view was opened from, or the row's first enabled button when that one
 * is missing or disabled. Says whether it focused anything: the row may be gone.
 */
function focusAction(root: HTMLElement | null, target: { id: string; action: string } | null) {
  if (target === null || root === null) return false;
  const row = [...root.querySelectorAll<HTMLButtonElement>("button[data-preview]")].filter(
    (button) => button.dataset.preview === target.id,
  );
  const wanted =
    row.find((button) => button.dataset.action === target.action && !button.disabled) ??
    row.find((button) => !button.disabled);
  wanted?.focus();
  return wanted !== undefined;
}

/** Off Windows the tab shows words and asks nothing; an untrusted folder lists live previews only. */
export function Previews({
  api,
  folder,
  canRun,
}: {
  api: PreviewsApi;
  folder: WorkspaceJson;
  canRun: boolean;
}) {
  if (!canRun) {
    return <p className="previews-reason">{previewRefusal("unsupported_platform")}</p>;
  }
  return <PreviewsPanel api={api} folder={folder} trusted={folder.policy === "ordinary"} />;
}

function PreviewsPanel({
  api,
  folder,
  trusted,
}: {
  api: PreviewsApi;
  folder: WorkspaceJson;
  trusted: boolean;
}) {
  const ids = useId();
  const ready = useRoots().worktrees.filter(
    (row) => row.workspace_id === folder.workspace_id && row.state === "ready",
  );
  const [chosen, setChosen] = useState<string | null>(null);
  // A worktree that is gone or not ready falls back to the folder itself.
  const worktreeId = ready.find((row) => row.worktree_id === chosen)?.worktree_id ?? null;
  const [previews, setPreviews] = useState<PreviewJson[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  /** Counts the reads that settled, so a failed read arms the next one too. */
  const [settledReads, setSettledReads] = useState(0);
  const [view, setView] = useState<View | null>(null);
  const [opening, setOpening] = useState<ReadonlySet<string>>(new Set());
  const [stopping, setStopping] = useState<ReadonlySet<string>>(new Set());
  const [removing, setRemoving] = useState<ReadonlySet<string>>(new Set());
  const [refusal, setRefusal] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [command, setCommand] = useState("");
  const [port, setPort] = useState("");
  const [starting, setStarting] = useState(false);
  const listCall = useRef<AbortController | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const backRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const startRef = useRef<HTMLHeadingElement>(null);
  /** The row button that last had focus in the list, and where its row stood. */
  const lastFocus = useRef<{ button: HTMLButtonElement; id: string; index: number } | null>(null);
  /** The row button to focus once the output view closes. */
  const returnTo = useRef<{ id: string; action: string } | null>(null);

  function load() {
    listCall.current?.abort();
    const controller = new AbortController();
    listCall.current = controller;
    api.list(folder.workspace_id, controller.signal).then(
      (next) => {
        if (controller.signal.aborted) return;
        setPreviews(next);
        setLoadError(null);
        setSettledReads((n) => n + 1);
      },
      (error) => {
        if (controller.signal.aborted) return;
        setLoadError(codeOf(error));
        setSettledReads((n) => n + 1);
      },
    );
  }

  // biome-ignore lint/correctness/useExhaustiveDependencies: one read per folder.
  useEffect(() => {
    setPreviews(null);
    setLoadError(null);
    load();
    return () => listCall.current?.abort();
  }, [api, folder.workspace_id]);

  // A live preview changes without an action of ours, so the list is read again until none is.
  const live = previews?.filter(isLive).length ?? 0;
  // biome-ignore lint/correctness/useExhaustiveDependencies: `load` is the current render's.
  useEffect(() => {
    if (view !== null || live === 0) return;
    const timer = setTimeout(load, LIST_POLL_MS);
    return () => clearTimeout(timer);
  }, [settledReads, view, live]);

  const viewOpen = view !== null;
  useEffect(() => {
    if (viewOpen) {
      backRef.current?.focus();
    } else {
      focusAction(rootRef.current, returnTo.current);
      returnTo.current = null;
    }
  }, [viewOpen]);

  function noteFocus(event: FocusEvent<HTMLUListElement>) {
    const button = event.target;
    if (!(button instanceof HTMLButtonElement)) return;
    const id = button.dataset.preview;
    const item = button.closest("li");
    if (id === undefined || item === null) return;
    lastFocus.current = { button, id, index: [...event.currentTarget.children].indexOf(item) };
  }

  // A re-read can remove or disable the button that had focus (a Stop that goes when its preview
  // ends, a removed row), which drops focus to the page. A focus the user moved is left alone.
  // biome-ignore lint/correctness/useExhaustiveDependencies: it runs once per list change.
  useEffect(() => {
    const last = lastFocus.current;
    const active = document.activeElement;
    if (last === null) return;
    if (active !== null && active !== document.body && active !== last.button) return;
    if (last.button.isConnected && !last.button.disabled) return;
    const rows = [...(listRef.current?.children ?? [])];
    const row =
      rows.find(
        (item) => item.querySelector<HTMLElement>("[data-preview]")?.dataset.preview === last.id,
      ) ?? rows[Math.min(last.index, rows.length - 1)];
    (row?.querySelector<HTMLElement>(FIRST_ENABLED) ?? startRef.current)?.focus();
  }, [previews]);

  function back() {
    setView(null);
    load();
  }

  function showOutput(preview: PreviewJson) {
    returnTo.current = { id: preview.preview_id, action: "output" };
    setRefusal(null);
    setView({ previewId: preview.preview_id, port: preview.port });
  }

  /** Runs one action on a row: the row's busy flag is up while it is asked, then the list is read. */
  async function runAction(
    busy: (update: (now: ReadonlySet<string>) => ReadonlySet<string>) => void,
    preview: PreviewJson,
    call: () => Promise<unknown>,
    done = "",
  ) {
    busy((now) => new Set(now).add(preview.preview_id));
    setRefusal(null);
    setMessage("");
    try {
      await call();
      setMessage(done);
    } catch (error) {
      setRefusal(codeOf(error));
    }
    busy((now) => {
      const next = new Set(now);
      next.delete(preview.preview_id);
      return next;
    });
    load();
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (starting) return;
    setStarting(true);
    setRefusal(null);
    setMessage("");
    try {
      await api.start(folder.workspace_id, {
        worktree_id: worktreeId,
        command,
        port: Number(port),
      });
      setCommand("");
    } catch (error) {
      setRefusal(codeOf(error));
    }
    setStarting(false);
    load();
  }

  /** The checkout a preview was started in, by its worktree id. */
  const checkoutOf = (id: string | null) => {
    if (id === null) return copy.thisFolder;
    const row = ready.find((worktree) => worktree.worktree_id === id);
    return row ? copy.worktree(row.branch) : copy.unknownWorktree;
  };

  const portNumber = Number(port);
  const portValid =
    port.trim() !== "" &&
    Number.isInteger(portNumber) &&
    portNumber >= PORT_MIN &&
    portNumber <= PORT_MAX;
  const startable = command.trim() !== "" && portValid;
  const shown = (trusted ? previews : previews?.filter(isLive))?.toSorted(newestFirst) ?? null;

  return (
    <div ref={rootRef} className="previews">
      {trusted ? (
        <p className="previews-reason">{copy.note}</p>
      ) : (
        <p className="previews-reason">{rootRefusal("trust_required")}</p>
      )}
      {view === null ? (
        <>
          {trusted && (
            <div className="previews-bar">
              <label>
                {copy.runIn}
                <select
                  value={worktreeId ?? ""}
                  onChange={(event) => setChosen(event.target.value || null)}
                >
                  <option value="">{copy.thisFolder}</option>
                  {ready.map((row) => (
                    <option key={row.worktree_id} value={row.worktree_id}>
                      {copy.worktree(row.branch)}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          )}
          {loadError !== null && (
            <p role="alert" className="previews-refusal">
              {previewRefusal(loadError)}
            </p>
          )}
          {shown !== null &&
            (shown.length === 0 ? (
              trusted && <p className="previews-reason">{copy.none}</p>
            ) : (
              <ul ref={listRef} className="previews-list" onFocus={noteFocus}>
                {shown.map((preview) => {
                  const label = `${ids}-${preview.preview_id}`;
                  const id = preview.preview_id;
                  const running = isLive(preview);
                  return (
                    <li key={id} className="preview">
                      <code id={label} className="preview-command">
                        {preview.command}
                      </code>
                      <span className="previews-reason">{checkoutOf(preview.worktree_id)}</span>
                      <span>{previewStateWords(preview)}</span>
                      <div className="preview-actions">
                        {trusted && preview.state === "listening" && (
                          <button
                            type="button"
                            className="btn alt sm"
                            aria-describedby={label}
                            data-preview={id}
                            data-action="open"
                            disabled={opening.has(id)}
                            onClick={() =>
                              void runAction(setOpening, preview, () => api.open(id), copy.opened)
                            }
                          >
                            {opening.has(id) ? copy.opening : copy.open}
                          </button>
                        )}
                        {running && (
                          <button
                            type="button"
                            className="btn txt sm"
                            aria-describedby={label}
                            data-preview={id}
                            data-action="stop"
                            disabled={stopping.has(id)}
                            onClick={() => void runAction(setStopping, preview, () => api.stop(id))}
                          >
                            {stopping.has(id) ? copy.stopping : copy.stop}
                          </button>
                        )}
                        <button
                          type="button"
                          className="btn txt sm"
                          aria-describedby={label}
                          data-preview={id}
                          data-action="output"
                          onClick={() => showOutput(preview)}
                        >
                          {copy.output}
                        </button>
                        {trusted && !running && (
                          <button
                            type="button"
                            className="btn txt sm"
                            aria-describedby={label}
                            data-preview={id}
                            data-action="remove"
                            disabled={removing.has(id)}
                            onClick={() =>
                              void runAction(setRemoving, preview, () => api.remove(id))
                            }
                          >
                            {copy.remove}
                          </button>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ul>
            ))}
          {trusted && (
            <form
              className="previews-form"
              aria-labelledby={`${ids}-start`}
              onSubmit={(event) => void submit(event)}
            >
              <h4 ref={startRef} id={`${ids}-start`} tabIndex={-1}>
                {copy.start}
              </h4>
              <label>
                {copy.command}
                <input
                  type="text"
                  value={command}
                  spellCheck={false}
                  onChange={(event) => setCommand(event.target.value)}
                />
              </label>
              <label>
                {copy.port}
                <input
                  type="number"
                  min={PORT_MIN}
                  max={PORT_MAX}
                  value={port}
                  onChange={(event) => setPort(event.target.value)}
                />
              </label>
              <button type="submit" className="btn go sm" disabled={starting || !startable}>
                {starting ? copy.submitting : copy.submit}
              </button>
            </form>
          )}
        </>
      ) : (
        <section className="previews-view">
          <button ref={backRef} type="button" className="btn txt sm" onClick={back}>
            {copy.back}
          </button>
          <PreviewOutput api={api} previewId={view.previewId} port={view.port} />
        </section>
      )}
      <p role="status" aria-live="polite">
        {message}
      </p>
      {refusal !== null && (
        <p role="alert" className="previews-refusal">
          {previewRefusal(refusal)}
        </p>
      )}
    </div>
  );
}

/** A preview's output, polled until its end and cleaned as a check's output is. */
function PreviewOutput({
  api,
  previewId,
  port,
}: {
  api: PreviewsApi;
  previewId: string;
  port: number;
}) {
  const [shown, setShown] = useState({ text: "", dropped: false });
  const [ended, setEnded] = useState<PreviewJson | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let next = 0;
    let pending = EMPTY;
    let dropped = false;
    setShown({ text: "", dropped: false });
    setEnded(null);
    setFailed(false);

    function poll() {
      api.output(previewId, next, controller.signal).then(
        (answer) => {
          if (controller.signal.aborted) return;
          next = answer.next;
          pending = append(pending, answer.text, answer.done);
          dropped ||= answer.dropped || pending.trimmed;
          setShown({ text: pending.lines + pending.line, dropped });
          if (answer.done) setEnded(answer.preview);
          else timer = setTimeout(poll, POLL_MS);
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
  }, [api, previewId]);

  const status = failed
    ? copy.failed
    : ended
      ? previewStateWords(ended)
      : shown.text === ""
        ? copy.waiting
        : "";
  return (
    <div>
      <h4>{copy.heading(port)}</h4>
      {shown.dropped && <p>{copy.dropped}</p>}
      {shown.text !== "" && <pre className="preview-output">{shown.text}</pre>}
      {ended && shown.text === "" && <p>{copy.noOutput}</p>}
      <p role="status" aria-live="polite">
        {status}
      </p>
    </div>
  );
}
