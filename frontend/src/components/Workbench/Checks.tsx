import { type FocusEvent, type FormEvent, useEffect, useId, useRef, useState } from "react";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import {
  type CheckRunJson,
  type ChecksApi,
  ChecksApiError,
  type CodeCheckJson,
} from "../../clients/code/checksApi";
import { checksRefusal, outcomeWords, runLine } from "../../clients/code/checkWords";
import { useRoots } from "../../state/sessionRoots";
import { CheckOutput } from "./CheckOutput";
import { rootRefusal } from "./Worktrees";
import "./Checks.css";

/** How often the list is read again while a run of it is live. */
const LIST_POLL_MS = 1000;
const LIVE_LIMIT = 4;

type View =
  | { kind: "output"; runId: string; name: string }
  | { kind: "history"; name: string; runs: CheckRunJson[] | null; failed: boolean };

const copy = {
  note: "A check runs a command you write in this folder or one of its worktrees, through cmd.exe on Windows or /bin/sh on macOS and Linux. It is not a sandbox: like a terminal, it can change files and use the network.",
  runIn: "Run in",
  thisFolder: "This folder",
  worktree: (branch: string) => `${branch} (worktree)`,
  none: "No checks yet.",
  add: "Add a check",
  name: "Name",
  command: "Command",
  minutes: "Time limit (minutes)",
  submit: "Add",
  submitting: "Adding…",
  limit: (n: number) => `Time limit: ${n} min`,
  run: "Run",
  starting: "Starting…",
  cancel: "Cancel",
  output: "Output",
  earlier: "Earlier runs",
  remove: "Delete",
  confirm: (name: string) => `Delete the check ${name} and its run records?`,
  keep: "Keep",
  notRun: "Not run here yet.",
  earlierOf: (name: string) => `Earlier runs of ${name}`,
  showOutput: "Show output",
  unknownWorktree: "Unknown worktree",
  back: "Back to the list",
};

const codeOf = (error: unknown) => (error instanceof ChecksApiError ? error.code : "unavailable");
const isLive = (check: CodeCheckJson) => check.last_run?.outcome === "running";

const FIRST_ENABLED = "button[data-check]:not(:disabled)";

/**
 * Focuses the row button a view or a confirm was opened from, or the row's first enabled button
 * when that one is missing or disabled. Says whether it focused anything: the row may be gone.
 */
function focusAction(root: HTMLElement | null, target: { id: string; action: string } | null) {
  if (target === null || root === null) return false;
  const row = [...root.querySelectorAll<HTMLButtonElement>("button[data-check]")].filter(
    (button) => button.dataset.check === target.id,
  );
  const wanted =
    row.find((button) => button.dataset.action === target.action && !button.disabled) ??
    row.find((button) => !button.disabled);
  wanted?.focus();
  return wanted !== undefined;
}

/** An unsupported platform or an untrusted folder shows its refusal without running checks. */
export function Checks({
  api,
  folder,
  canRun,
}: {
  api: ChecksApi;
  folder: WorkspaceJson;
  canRun: boolean;
}) {
  if (!canRun) return <p className="checks-reason">{checksRefusal("unsupported_platform")}</p>;
  if (folder.policy !== "ordinary")
    return <p className="checks-reason">{rootRefusal("trust_required")}</p>;
  return <ChecksPanel api={api} folder={folder} />;
}

function ChecksPanel({ api, folder }: { api: ChecksApi; folder: WorkspaceJson }) {
  const ids = useId();
  const ready = useRoots().worktrees.filter(
    (row) => row.workspace_id === folder.workspace_id && row.state === "ready",
  );
  const [chosen, setChosen] = useState<string | null>(null);
  // A worktree that is gone or not ready falls back to the folder itself.
  const worktreeId = ready.find((row) => row.worktree_id === chosen)?.worktree_id ?? null;
  const [checks, setChecks] = useState<CodeCheckJson[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  /** Counts the reads that settled, so a failed read arms the next one too. */
  const [settledReads, setSettledReads] = useState(0);
  const [view, setView] = useState<View | null>(null);
  const [starting, setStarting] = useState<ReadonlySet<string>>(new Set());
  const [confirming, setConfirming] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [name, setName] = useState("");
  const [command, setCommand] = useState("");
  const [minutes, setMinutes] = useState("10");
  const [adding, setAdding] = useState(false);
  const listCall = useRef<AbortController | null>(null);
  const viewCall = useRef<AbortController | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const backRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const addRef = useRef<HTMLHeadingElement>(null);
  /** The row button that last had focus in the list, and where its row stood. */
  const lastFocus = useRef<{ button: HTMLButtonElement; id: string; index: number } | null>(null);
  /** The row button to focus once a view or a confirm closes. */
  const returnTo = useRef<{ id: string; action: string } | null>(null);

  function load() {
    listCall.current?.abort();
    const controller = new AbortController();
    listCall.current = controller;
    api.list(folder.workspace_id, worktreeId, controller.signal).then(
      (next) => {
        if (controller.signal.aborted) return;
        setChecks(next);
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

  // biome-ignore lint/correctness/useExhaustiveDependencies: one read per checkout.
  useEffect(() => {
    setChecks(null);
    setLoadError(null);
    load();
    return () => listCall.current?.abort();
  }, [api, folder.workspace_id, worktreeId]);

  // A live run changes without an action of ours, so the list is read again until it ends.
  const live = checks?.filter(isLive).length ?? 0;
  // biome-ignore lint/correctness/useExhaustiveDependencies: `load` is the current render's.
  useEffect(() => {
    if (view !== null || live === 0) return;
    const timer = setTimeout(load, LIST_POLL_MS);
    return () => clearTimeout(timer);
  }, [settledReads, view, live]);

  useEffect(() => {
    return () => viewCall.current?.abort();
  }, []);

  const viewOpen = view !== null;
  useEffect(() => {
    if (viewOpen) {
      backRef.current?.focus();
    } else {
      focusAction(rootRef.current, returnTo.current);
      returnTo.current = null;
    }
  }, [viewOpen]);

  useEffect(() => {
    if (confirming !== null) focusAction(rootRef.current, { id: confirming, action: "keep" });
    else {
      focusAction(rootRef.current, returnTo.current);
      returnTo.current = null;
    }
  }, [confirming]);

  function noteFocus(event: FocusEvent<HTMLUListElement>) {
    const button = event.target;
    if (!(button instanceof HTMLButtonElement)) return;
    const id = button.dataset.check;
    const item = button.closest("li");
    if (id === undefined || item === null) return;
    lastFocus.current = { button, id, index: [...event.currentTarget.children].indexOf(item) };
  }

  // A re-read can remove or disable the button that had focus (a deleted row, a Cancel that goes
  // when its run ends), which drops focus to the page. A focus the user moved is left alone.
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
        (item) => item.querySelector<HTMLElement>("[data-check]")?.dataset.check === last.id,
      ) ?? rows[Math.min(last.index, rows.length - 1)];
    (row?.querySelector<HTMLElement>(FIRST_ENABLED) ?? addRef.current)?.focus();
  }, [checks]);

  function back() {
    viewCall.current?.abort();
    setView(null);
    load();
  }

  function showOutput(check: CodeCheckJson, run: CheckRunJson, action: string) {
    returnTo.current = { id: check.check_id, action };
    setRefusal(null);
    setView({ kind: "output", runId: run.run_id, name: check.name ?? "" });
  }

  async function start(check: CodeCheckJson) {
    setStarting((now) => new Set(now).add(check.check_id));
    setRefusal(null);
    try {
      const run = await api.run(check.check_id, worktreeId);
      showOutput(check, run, "run");
    } catch (error) {
      setRefusal(codeOf(error));
      load();
    }
    setStarting((now) => {
      const next = new Set(now);
      next.delete(check.check_id);
      return next;
    });
  }

  async function cancel(run: CheckRunJson) {
    setRefusal(null);
    try {
      await api.cancel(run.run_id);
    } catch (error) {
      setRefusal(codeOf(error));
    }
    load();
  }

  function openHistory(check: CodeCheckJson) {
    viewCall.current?.abort();
    const controller = new AbortController();
    viewCall.current = controller;
    returnTo.current = { id: check.check_id, action: "earlier" };
    setRefusal(null);
    const label = check.name ?? "";
    setView({ kind: "history", name: label, runs: null, failed: false });
    api.runs(check.check_id, controller.signal).then(
      (runs) => {
        if (!controller.signal.aborted) {
          setView({ kind: "history", name: label, runs, failed: false });
        }
      },
      () => {
        if (!controller.signal.aborted) {
          setView({ kind: "history", name: label, runs: null, failed: true });
        }
      },
    );
  }

  async function remove(check: CodeCheckJson) {
    setConfirming(null);
    setRefusal(null);
    try {
      await api.remove(check.check_id);
    } catch (error) {
      setRefusal(codeOf(error));
    }
    load();
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (adding) return;
    setAdding(true);
    setRefusal(null);
    const limit = Number(minutes);
    try {
      await api.add(folder.workspace_id, {
        name,
        command,
        timeout_s: Number.isFinite(limit) ? limit * 60 : 0,
      });
      setName("");
      setCommand("");
      load();
    } catch (error) {
      setRefusal(codeOf(error));
    }
    setAdding(false);
  }

  /** The checkout a run was made in, by its worktree id. */
  const checkoutOf = (id: string | null) => {
    if (id === null) return copy.thisFolder;
    const row = ready.find((worktree) => worktree.worktree_id === id);
    return row ? copy.worktree(row.branch) : copy.unknownWorktree;
  };

  const blank = name.trim() === "" || command.trim() === "" || minutes.trim() === "";

  return (
    <div ref={rootRef} className="checks">
      <p className="checks-reason">{copy.note}</p>
      {view === null ? (
        <>
          <div className="checks-bar">
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
          {loadError !== null && (
            <p role="alert" className="checks-refusal">
              {checksRefusal(loadError)}
            </p>
          )}
          {checks !== null &&
            (checks.length === 0 ? (
              <p className="checks-reason">{copy.none}</p>
            ) : (
              <ul ref={listRef} className="checks-list" onFocus={noteFocus}>
                {checks.map((check) => {
                  const label = `${ids}-${check.check_id}`;
                  const running = isLive(check);
                  const run = check.last_run;
                  const asking = starting.has(check.check_id);
                  return (
                    <li key={check.check_id} className="check">
                      {check.name !== null && (
                        <strong id={label} className="check-name">
                          {check.name}
                        </strong>
                      )}
                      {check.command !== null && (
                        <code className="check-command">{check.command}</code>
                      )}
                      {check.readable ? (
                        <>
                          <span className="checks-reason">{copy.limit(check.timeout_s / 60)}</span>
                          <span className="checks-reason">{run ? runLine(run) : copy.notRun}</span>
                        </>
                      ) : (
                        <span id={label} className="checks-reason">
                          {checksRefusal("check_unreadable")}
                        </span>
                      )}
                      {confirming === check.check_id ? (
                        <fieldset aria-labelledby={`${label}-ask`} className="check-actions">
                          <span id={`${label}-ask`}>
                            {copy.confirm(check.name ?? check.check_id.slice(0, 8))}
                          </span>
                          <button
                            type="button"
                            className="btn go sm"
                            data-check={check.check_id}
                            data-action="confirm"
                            onClick={() => void remove(check)}
                          >
                            {copy.remove}
                          </button>
                          <button
                            type="button"
                            className="btn txt sm"
                            data-check={check.check_id}
                            data-action="keep"
                            onClick={() => setConfirming(null)}
                          >
                            {copy.keep}
                          </button>
                        </fieldset>
                      ) : (
                        <div className="check-actions">
                          {check.readable && (
                            <button
                              type="button"
                              className="btn alt sm"
                              aria-describedby={label}
                              data-check={check.check_id}
                              data-action="run"
                              disabled={asking || running || live >= LIVE_LIMIT}
                              onClick={() => void start(check)}
                            >
                              {asking ? copy.starting : copy.run}
                            </button>
                          )}
                          {running && run && (
                            <button
                              type="button"
                              className="btn txt sm"
                              aria-describedby={label}
                              data-check={check.check_id}
                              data-action="cancel"
                              onClick={() => void cancel(run)}
                            >
                              {copy.cancel}
                            </button>
                          )}
                          {check.readable && run && (
                            <button
                              type="button"
                              className="btn txt sm"
                              aria-describedby={label}
                              data-check={check.check_id}
                              data-action="output"
                              onClick={() => showOutput(check, run, "output")}
                            >
                              {copy.output}
                            </button>
                          )}
                          {check.readable && (
                            <button
                              type="button"
                              className="btn txt sm"
                              aria-describedby={label}
                              data-check={check.check_id}
                              data-action="earlier"
                              onClick={() => openHistory(check)}
                            >
                              {copy.earlier}
                            </button>
                          )}
                          <button
                            type="button"
                            className="btn txt sm"
                            aria-describedby={label}
                            data-check={check.check_id}
                            data-action="delete"
                            disabled={running}
                            onClick={() => {
                              returnTo.current = { id: check.check_id, action: "delete" };
                              setConfirming(check.check_id);
                            }}
                          >
                            {copy.remove}
                          </button>
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
            ))}
          <form
            className="checks-form"
            aria-labelledby={`${ids}-add`}
            onSubmit={(event) => void submit(event)}
          >
            <h4 ref={addRef} id={`${ids}-add`} tabIndex={-1}>
              {copy.add}
            </h4>
            <label>
              {copy.name}
              <input
                type="text"
                value={name}
                spellCheck={false}
                onChange={(event) => setName(event.target.value)}
              />
            </label>
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
              {copy.minutes}
              <input
                type="number"
                min={1}
                max={60}
                value={minutes}
                onChange={(event) => setMinutes(event.target.value)}
              />
            </label>
            <button type="submit" className="btn go sm" disabled={adding || blank}>
              {adding ? copy.submitting : copy.submit}
            </button>
          </form>
        </>
      ) : (
        <section className="checks-view">
          <button ref={backRef} type="button" className="btn txt sm" onClick={back}>
            {copy.back}
          </button>
          {view.kind === "output" ? (
            <CheckOutput
              api={api}
              runId={view.runId}
              name={view.name}
              onSettled={(run) => {
                setMessage(`${view.name} — ${outcomeWords(run)}`);
                load();
              }}
            />
          ) : (
            <div>
              <h4>{copy.earlierOf(view.name)}</h4>
              {view.failed && (
                <p role="alert" className="checks-refusal">
                  {checksRefusal("unavailable")}
                </p>
              )}
              <ul className="checks-list">
                {view.runs?.map((run) => (
                  <li key={run.run_id} className="check">
                    <span className="checks-reason">{checkoutOf(run.worktree_id)}</span>
                    <span>{runLine(run)}</span>
                    <div className="check-actions">
                      <button
                        type="button"
                        className="btn txt sm"
                        onClick={() =>
                          setView({ kind: "output", runId: run.run_id, name: view.name })
                        }
                      >
                        {copy.showOutput}
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </section>
      )}
      <p role="status" aria-live="polite">
        {message}
      </p>
      {refusal !== null && (
        <p role="alert" className="checks-refusal">
          {checksRefusal(refusal)}
        </p>
      )}
    </div>
  );
}
