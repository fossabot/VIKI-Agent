import { type FormEvent, useEffect, useId, useRef, useState } from "react";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import type { ChecksApi } from "../../clients/code/checksApi";
import {
  type ReviewApi,
  ReviewApiError,
  type ReviewEntryJson,
  type ReviewJson,
  type ReviewViewJson,
} from "../../clients/code/reviewApi";
import { useRoots } from "../../state/sessionRoots";
import { Loading } from "../Loading/Loading";
import { CheckEvidence } from "./CheckEvidence";
import { DiffView } from "./DiffView";
import { changeWords, IntegrationPanel, kindWords, PushPanel, reviewRefusal } from "./ReviewPanels";
import { rootRefusal } from "./Worktrees";
import "./Review.css";

type ViewState = { path: string; integration: boolean } & (
  | { status: "loading" }
  | { status: "failed" }
  | { status: "ready"; data: ReviewViewJson }
);

const copy = {
  checkout: "Checkout",
  thisFolder: "This folder",
  worktree: (branch: string) => `${branch} (worktree)`,
  refresh: "Refresh",
  branchAt: (branch: string, oid: string) => `Branch ${branch} at ${oid}`,
  noChanges: "No changes to review.",
  truncated: "More than 500 changes: only the first 500 are listed. Commit the rest in a terminal.",
  noRoot: "This folder can't be changed now.",
  include: (path: string) => `Include ${path}`,
  selectAll: "Select all",
  clear: "Clear",
  selected: (n: number, m: number) => `${n} of ${m} files selected`,
  loading: "Loading…",
  loadFailed: "The change could not be loaded.",
  newFile: "New file",
  downloadPatch: "Download patch",
  downloadPatchOf: (path: string) => `Download patch for ${path}`,
  back: "Back to the list",
  reasons: {
    binary: "Binary file: no text to show.",
    too_large: "Over 1 MiB: not shown.",
    no_diff: "No difference.",
    unsupported_encoding: "This file's encoding can't be shown.",
    no_commits: rootRefusal("no_commits"),
  },
  message: "Commit message",
  commit: "Commit selected files",
  committing: "Committing…",
  needFile: "Select at least one file.",
  needMessage: "Write a commit message.",
  committed: (oid: string) => `Committed ${oid}.`,
  hookChanged: (n: number) =>
    `A hook changed ${n} of the files while committing. Review the commit before you integrate or push.`,
  unexpected: (n: number) =>
    `The commit also holds ${n} files you did not select. Check it in a terminal.`,
};

const codeOf = (error: unknown) => (error instanceof ReviewApiError ? error.code : "unavailable");
const oid12 = (oid: string) => oid.slice(0, 12);
const isText = (entry: ReviewEntryJson): entry is ReviewEntryJson & { digest: string } =>
  entry.kind === "text" && entry.digest !== undefined;

export function Review({
  api,
  folder,
  initialWorktree,
  onOpenUrl,
  checks,
  shown = true,
}: {
  api: ReviewApi;
  checks?: ChecksApi;
  /** False while the tab is hidden: the checks evidence waits to read until it shows. */
  shown?: boolean;
  folder: WorkspaceJson;
  initialWorktree: string | null;
  onOpenUrl(url: string): void;
}) {
  const ids = useId();
  const headId = `${ids}-head`;
  const progressId = `${ids}-progress`;
  const reasonId = `${ids}-reason`;
  const trusted = folder.policy === "ordinary";
  const rootId = folder.root_physical_id;
  const ready = useRoots().worktrees.filter(
    (row) => row.workspace_id === folder.workspace_id && row.state === "ready",
  );
  const [chosen, setChosen] = useState(initialWorktree);
  // A worktree that is gone or not ready falls back to the folder itself.
  const worktreeId = ready.find((row) => row.worktree_id === chosen)?.worktree_id ?? null;
  const [review, setReview] = useState<ReviewJson | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  /** Counts answered reviews: the checks evidence reads again with each one. */
  const [answers, setAnswers] = useState(0);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [view, setView] = useState<ViewState | null>(null);
  const [message, setMessage] = useState("");
  const [committing, setCommitting] = useState(false);
  const [outcome, setOutcome] = useState<string[]>([]);
  const [refusal, setRefusal] = useState<string | null>(null);
  const reviewTarget = useRef<{
    api: ReviewApi;
    workspaceId: string;
    worktreeId: string | null;
  } | null>(null);
  const reviewCall = useRef<AbortController | null>(null);
  const viewCall = useRef<AbortController | null>(null);
  /** Counts checkout changes, so a commit answered after one is not shown against the new checkout. */
  const checkoutGeneration = useRef(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const backRef = useRef<HTMLButtonElement>(null);
  /** The path button to focus once Back returns to the list: an entry's or an integration path's. */
  const returnTo = useRef<{ path: string; integration: boolean } | null>(null);

  function load() {
    // A completed mutation refreshes the checkout now on screen, if this view still lives.
    const target = reviewTarget.current;
    if (target === null) return;
    reviewCall.current?.abort();
    const controller = new AbortController();
    reviewCall.current = controller;
    target.api.review(target.workspaceId, target.worktreeId, controller.signal).then(
      (next) => {
        if (controller.signal.aborted) return;
        setReview(next);
        setLoadError(null);
        setAnswers((n) => n + 1);
        // A fresh review is reviewed again: nothing stays ticked from the old one.
        setSelected(new Set());
      },
      (error) => {
        if (!controller.signal.aborted) setLoadError(codeOf(error));
      },
    );
  }

  // biome-ignore lint/correctness/useExhaustiveDependencies: one read per checkout.
  useEffect(() => {
    checkoutGeneration.current += 1;
    reviewTarget.current = trusted ? { api, workspaceId: folder.workspace_id, worktreeId } : null;
    viewCall.current?.abort();
    setReview(null);
    setLoadError(null);
    setSelected(new Set());
    setView(null);
    setCommitting(false);
    setOutcome([]);
    setRefusal(null);
    load();
    return () => {
      reviewTarget.current = null;
      reviewCall.current?.abort();
      viewCall.current?.abort();
    };
  }, [api, folder.workspace_id, worktreeId, trusted]);

  const viewPath = view?.path ?? null;
  useEffect(() => {
    if (viewPath !== null) {
      backRef.current?.focus();
    } else if (returnTo.current !== null) {
      const { path, integration } = returnTo.current;
      returnTo.current = null;
      for (const button of rootRef.current?.querySelectorAll<HTMLButtonElement>(
        "button[data-path]",
      ) ?? []) {
        if (button.dataset.path === path && (button.dataset.range !== undefined) === integration) {
          button.focus();
        }
      }
    }
  }, [viewPath]);

  /** An entry's change, or with `integration` the integration range's diff of a path. */
  function open(path: string, integration: boolean) {
    if (!review) return;
    viewCall.current?.abort();
    const controller = new AbortController();
    viewCall.current = controller;
    setView({ path, integration, status: "loading" });
    api
      .view(
        folder.workspace_id,
        path,
        { worktreeId: review.worktree_id, integration },
        controller.signal,
      )
      .then(
        (data) => {
          if (!controller.signal.aborted) {
            setView({ path, integration, status: "ready", data });
          }
        },
        () => {
          if (!controller.signal.aborted) setView({ path, integration, status: "failed" });
        },
      );
  }

  function close() {
    viewCall.current?.abort();
    returnTo.current = view ? { path: view.path, integration: view.integration } : null;
    setView(null);
  }

  function toggle(path: string, on: boolean) {
    const next = new Set(selected);
    if (on) next.add(path);
    else next.delete(path);
    setSelected(next);
  }

  function refresh() {
    setOutcome([]);
    setRefusal(null);
    load();
  }

  const entries = review?.entries ?? [];
  const texts = entries.filter(isText);
  const chosenTexts = texts.filter((entry) => selected.has(entry.path));
  const blocked: { words?: string; describedBy: string } | null = !review
    ? { describedBy: reasonId }
    : !rootId
      ? { words: copy.noRoot, describedBy: reasonId }
      : review.branch === null
        ? { describedBy: headId }
        : review.in_progress
          ? { describedBy: progressId }
          : chosenTexts.length === 0
            ? { words: copy.needFile, describedBy: reasonId }
            : message.trim() === ""
              ? { words: copy.needMessage, describedBy: reasonId }
              : null;

  async function commit(event: FormEvent) {
    event.preventDefault();
    if (committing || blocked !== null || !review || !rootId || review.branch === null) return;
    const generation = checkoutGeneration.current;
    setCommitting(true);
    setOutcome([]);
    setRefusal(null);
    let words: string[] = [];
    let refused: string | null = null;
    try {
      const done = await api.commit(folder.workspace_id, {
        policy_revision: folder.policy_revision,
        root_physical_id: rootId,
        worktree_id: review.worktree_id,
        branch: review.branch,
        head_oid: review.head_oid,
        message,
        paths: chosenTexts.map((entry) => ({ path: entry.path, digest: entry.digest })),
      });
      words = [copy.committed(oid12(done.head_oid))];
      if (done.hook_changed.length > 0) words.push(copy.hookChanged(done.hook_changed.length));
      if (done.unexpected.length > 0) words.push(copy.unexpected(done.unexpected.length));
    } catch (error) {
      refused = codeOf(error);
    }
    if (generation !== checkoutGeneration.current) return;
    if (refused === null) setMessage("");
    setOutcome(words);
    setRefusal(refused);
    setCommitting(false);
    load();
  }

  if (!trusted) return <p className="review-reason">{rootRefusal("trust_required")}</p>;

  return (
    <div ref={rootRef} className="review">
      <div className="review-bar">
        <label>
          {copy.checkout}
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
        <button type="button" className="btn txt sm" onClick={refresh}>
          {copy.refresh}
        </button>
      </div>
      {loadError !== null && (
        <p role="alert" className="review-refusal">
          {reviewRefusal(loadError)}
        </p>
      )}
      {review && (
        <>
          <p id={headId} className="review-reason">
            {review.branch === null
              ? reviewRefusal("detached_head")
              : copy.branchAt(review.branch, oid12(review.head_oid))}
          </p>
          {review.in_progress && (
            <p id={progressId} className="review-reason">
              {reviewRefusal("in_progress")}
            </p>
          )}
          {view ? (
            <section className="review-view">
              <button ref={backRef} type="button" className="btn txt sm" onClick={close}>
                {copy.back}
              </button>
              <strong className="review-path">{view.path}</strong>
              {view.status === "ready" && view.data.diff !== null && (
                <DownloadPatch path={view.path} diff={view.data.diff} />
              )}
              {view.status === "loading" && (
                <Loading className="review-reason" announce={false}>
                  {copy.loading}
                </Loading>
              )}
              {view.status === "failed" && (
                <p role="alert" className="review-refusal">
                  {copy.loadFailed}
                </p>
              )}
              {view.status === "ready" && <ViewBody data={view.data} />}
            </section>
          ) : (
            <>
              {review.truncated && <p className="review-reason">{copy.truncated}</p>}
              {entries.length === 0 ? (
                <p className="review-reason">{copy.noChanges}</p>
              ) : (
                <>
                  <div className="review-tools">
                    <button
                      type="button"
                      className="btn txt sm"
                      disabled={chosenTexts.length === texts.length}
                      onClick={() => setSelected(new Set(texts.map((entry) => entry.path)))}
                    >
                      {copy.selectAll}
                    </button>
                    <button
                      type="button"
                      className="btn txt sm"
                      disabled={chosenTexts.length === 0}
                      onClick={() => setSelected(new Set())}
                    >
                      {copy.clear}
                    </button>
                    <span className="review-reason">
                      {copy.selected(chosenTexts.length, texts.length)}
                    </span>
                  </div>
                  <ul className="review-entries">
                    {entries.map((entry) => (
                      <li key={entry.path} className="review-entry">
                        {entry.kind === "text" ? (
                          <>
                            <input
                              type="checkbox"
                              aria-label={copy.include(entry.path)}
                              checked={selected.has(entry.path)}
                              onChange={(event) => toggle(entry.path, event.target.checked)}
                            />
                            <button
                              type="button"
                              className="review-path-button"
                              data-path={entry.path}
                              onClick={() => open(entry.path, false)}
                            >
                              {entry.path}
                            </button>
                          </>
                        ) : (
                          <span className="review-path">{entry.path}</span>
                        )}
                        <span className="chip">{changeWords(entry.change)}</span>
                        {entry.kind !== "text" && (
                          <span className="review-reason">{kindWords(entry.kind)}</span>
                        )}
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </>
          )}
          {entries.length > 0 && (
            <form className="review-form" onSubmit={(event) => void commit(event)}>
              <label>
                {copy.message}
                <textarea
                  value={message}
                  maxLength={4000}
                  spellCheck={false}
                  onChange={(event) => setMessage(event.target.value)}
                />
              </label>
              <button
                type="submit"
                className="btn go sm"
                aria-describedby={blocked?.describedBy}
                disabled={committing || blocked !== null}
              >
                {committing ? copy.committing : copy.commit}
              </button>
              {blocked?.words && (
                <p id={reasonId} className="review-reason">
                  {blocked.words}
                </p>
              )}
            </form>
          )}
          <p role="status" aria-live="polite">
            {outcome.map((line, i) => (
              <span key={line}>
                {i > 0 && " "}
                {line}
              </span>
            ))}
          </p>
          {refusal !== null && (
            <p role="alert" className="review-refusal">
              {reviewRefusal(refusal)}
            </p>
          )}
          {checks && (
            <CheckEvidence
              api={checks}
              workspaceId={folder.workspace_id}
              worktreeId={review.worktree_id}
              headOid={review.head_oid}
              shown={shown}
              reloadKey={answers}
            />
          )}
          {review.worktree_id !== null && review.integration !== null && (
            <IntegrationPanel
              api={api}
              folder={folder}
              worktreeId={review.worktree_id}
              headOid={review.head_oid}
              integration={review.integration}
              onDone={load}
              onOpenRange={(path) => open(path, true)}
            />
          )}
          {review.branch !== null && (
            <PushPanel
              api={api}
              folder={folder}
              branch={review.branch}
              headOid={review.head_oid}
              remote={review.remote}
              remoteState={review.remote_state}
              remoteOid={review.remote_oid}
              pullRequestUrl={review.pull_request_url}
              isWorktree={review.worktree_id !== null}
              onOpenUrl={onOpenUrl}
              onDone={load}
            />
          )}
        </>
      )}
    </div>
  );
}

/** Saves the diff as given, through an object URL: no request, and the frozen CSP allows it. */
function DownloadPatch({ path, diff }: { path: string; diff: string }) {
  function save() {
    const url = URL.createObjectURL(new Blob([diff], { type: "text/x-diff" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `${path.slice(path.lastIndexOf("/") + 1)}.patch`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return (
    <button
      type="button"
      className="btn txt sm"
      aria-label={copy.downloadPatchOf(path)}
      onClick={save}
    >
      {copy.downloadPatch}
    </button>
  );
}

/** A diff, a new file's content or the reason there is neither. Server text stays text. */
function ViewBody({ data }: { data: ReviewViewJson }) {
  if (data.diff !== null)
    return <DiffView diff={{ path: data.path, text: data.diff, reason: null }} />;
  if (data.content !== null) {
    return (
      <>
        <h4 className="review-view-title">{copy.newFile}</h4>
        <pre className="review-content">{data.content}</pre>
      </>
    );
  }
  return data.reason !== null ? <p className="review-reason">{copy.reasons[data.reason]}</p> : null;
}
