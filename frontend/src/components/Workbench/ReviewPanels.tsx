import { useId, useState } from "react";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import {
  type IntegrationJson,
  type ReviewApi,
  ReviewApiError,
  type ReviewChange,
  type ReviewKind,
} from "../../clients/code/reviewApi";
import { rootRefusal } from "./Worktrees";

const IN_PROGRESS =
  "A merge, rebase, cherry-pick or revert is in progress here. Finish it in a terminal first.";
const DETACHED = "No branch is checked out here (detached HEAD). Commit in a terminal.";
const DRIVE_UNSUPPORTED =
  "This folder's drive doesn't support commit, integrate and push from VIKI.";
const NO_REMOTE = "This branch has no remote, and there is no origin.";
const REMOTE_UNSUPPORTED =
  "This remote pushes somewhere other than it fetches from, or to several places. Push in a terminal.";
const STALE_REVIEW = "This changed since you reviewed it. Refresh and review again.";
const CANNOT_USE_FILE = "VIKI can't use this file.";
// The codes the review, commit, integrate and push routes add to the roots routes' own;
// anything else is `rootRefusal`'s.
const REFUSALS: Record<string, string> = {
  stale_review: STALE_REVIEW,
  stale_base: "The folder's branch moved since you reviewed it. Refresh and review again.",
  unrelated_staged:
    "Git already has other files staged here. Commit or unstage them in a terminal first.",
  in_progress: IN_PROGRESS,
  detached_head: DETACHED,
  detached_target: "The folder has no branch checked out.",
  identity_missing:
    "Git doesn't know your name and email. Run git config --global user.name and git config --global user.email in a terminal, then try again.",
  commit_refused:
    "Git or a hook refused the commit, and the selected files stay staged. Run git commit in a terminal to see why.",
  unsupported_path: "A selected file can't be committed from VIKI. Refresh and review again.",
  too_many_paths: "Commit at most 200 files at a time.",
  invalid_message: "Write a commit message of up to 4,000 characters.",
  checkout_busy: "A session is working in this folder. Wait until its turn ends, then try again.",
  dirty_target:
    "The folder has uncommitted changes to tracked files. Commit or discard them first.",
  not_fast_forward:
    "The folder's branch has moved on, so this can't fast-forward. Merge or rebase in a terminal.",
  up_to_date: "Nothing to integrate.",
  unsupported_change: "These commits change files VIKI can't review. Integrate in a terminal.",
  merge_refused:
    "Git refused the fast-forward, so nothing changed. An untracked file may be in the way.",
  merge_failed: "The fast-forward stopped partway. Check the folder with git status in a terminal.",
  push_rejected:
    "The remote did not take the push. Its branch may have moved: fetch and integrate in a terminal.",
  push_failed: "VIKI could not reach the remote. Try again later.",
  no_remote: NO_REMOTE,
  remote_unsupported: REMOTE_UNSUPPORTED,
  unsupported_branch: "VIKI can't use this branch name. Use a terminal.",
  worktree_moved: "This worktree changed. Refresh and try again.",
  unsupported_filesystem: DRIVE_UNSUPPORTED,
  private_path: CANNOT_USE_FILE,
  invalid_path: CANNOT_USE_FILE,
};

/** The words for a refusal code from the review routes; the roots routes' codes are theirs. */
export function reviewRefusal(code: string): string {
  return (Object.hasOwn(REFUSALS, code) ? REFUSALS[code] : undefined) ?? rootRefusal(code);
}

const KINDS: Record<ReviewKind, string> = {
  text: "",
  binary: "Binary file: commit it in a terminal or VS Code.",
  symlink: "Link: commit it in a terminal.",
  submodule: "Submodule: commit it in a terminal.",
  nested_repository: "A separate repository inside this folder: VIKI does not commit it.",
  filtered: "Git LFS or another filter handles this file: commit it in a terminal.",
  private: "Private file: VIKI does not show or commit it.",
  too_large: "Too large to review in VIKI: commit it in a terminal or VS Code.",
  unmerged: "Unmerged: resolve it in a terminal.",
  unsupported_name: "VIKI can't handle this file name: commit it in a terminal.",
  other: "Git's index and this folder disagree about this file: fix it in a terminal.",
};
const CHANGES: Record<ReviewChange, string> = {
  added: "Added",
  modified: "Modified",
  deleted: "Deleted",
};

/** Why a path can't be committed from VIKI; empty for a `text` path. */
export const kindWords = (kind: ReviewKind): string => KINDS[kind];
export const changeWords = (change: ReviewChange): string => CHANGES[change];

const copy = {
  integrate: "Integrate",
  integrateInto: (target: string) => `Integrate into ${target}`,
  integrating: "Integrating…",
  folderAt: (target: string, oid: string) => `Folder branch ${target} at ${oid}`,
  counts: (ahead: number, behind: number) => `${ahead} commits to integrate; ${behind} behind`,
  uncommitted: "Uncommitted changes in the worktree are not integrated.",
  firstCommits: "Only the first 50 commits are listed.",
  integrated: (target: string, oid: string) => `Integrated: ${target} is now at ${oid}.`,
  push: "Push",
  pushBranch: (branch: string) => `Push ${branch}`,
  pushing: "Pushing…",
  seen: (remote: string, oid: string) => `Remote ${remote}; last fetched at ${oid}`,
  unseen: (remote: string) => `Remote ${remote}; not on the remote when last fetched`,
  pushed: (branch: string, remote: string) => `Pushed ${branch} to ${remote}.`,
  unknown: "VIKI couldn't tell whether the push finished.",
  check: "Check again",
  notPushed: "The remote branch is not at this commit: the push did not land.",
  page: "Open pull request page",
  noPage: "VIKI doesn't know this host's pull request page. Open it on your host.",
  noRoot: "This folder can't be changed now.",
};

const codeOf = (error: unknown) => (error instanceof ReviewApiError ? error.code : "unavailable");
const oid12 = (oid: string) => oid.slice(0, 12);

/** The outcome in a polite live region, a refusal as an alert. */
function Feedback({ message, refusal }: { message: string; refusal: string | null }) {
  return (
    <>
      <p role="status" aria-live="polite">
        {message}
      </p>
      {refusal !== null && (
        <p role="alert" className="review-refusal">
          {reviewRefusal(refusal)}
        </p>
      )}
    </>
  );
}

export function IntegrationPanel({
  api,
  folder,
  worktreeId,
  headOid,
  integration,
  onDone,
  onOpenRange,
}: {
  api: ReviewApi;
  folder: WorkspaceJson;
  worktreeId: string;
  headOid: string;
  integration: IntegrationJson;
  onDone(): void;
  onOpenRange(path: string): void;
}) {
  const reasonId = useId();
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [refusal, setRefusal] = useState<string | null>(null);
  const rootId = folder.root_physical_id;
  const target = integration.target_branch;
  const reason = !rootId
    ? copy.noRoot
    : target === null
      ? reviewRefusal("detached_target")
      : integration.ahead === 0
        ? reviewRefusal("up_to_date")
        : !integration.fast_forward
          ? reviewRefusal("not_fast_forward")
          : integration.paths_truncated || integration.paths.some((p) => p.kind !== "text")
            ? reviewRefusal("unsupported_change")
            : null;

  async function integrate() {
    if (busy || reason !== null || !rootId || target === null) return;
    setBusy(true);
    setMessage("");
    setRefusal(null);
    try {
      const done = await api.integrate(worktreeId, {
        policy_revision: folder.policy_revision,
        root_physical_id: rootId,
        target_branch: target,
        target_oid: integration.target_oid,
        head_oid: headOid,
      });
      setMessage(copy.integrated(target, oid12(done.head_oid)));
      onDone();
    } catch (error) {
      setRefusal(codeOf(error));
    }
    setBusy(false);
  }

  return (
    <section className="review-panel" aria-label={copy.integrate}>
      {target !== null && (
        <p className="review-reason">{copy.folderAt(target, oid12(integration.target_oid))}</p>
      )}
      <p className="review-reason">{copy.counts(integration.ahead, integration.behind)}</p>
      <ul className="review-panel-list">
        {integration.commits.map((commit) => (
          <li key={commit.oid}>
            <code>{oid12(commit.oid)}</code> <span>{commit.subject}</span>
          </li>
        ))}
      </ul>
      {integration.commits_truncated && <p className="review-reason">{copy.firstCommits}</p>}
      <ul className="review-panel-list">
        {integration.paths.map((path) => (
          <li key={path.path}>
            {path.kind === "text" ? (
              <button
                type="button"
                className="review-path-button"
                data-path={path.path}
                data-range=""
                onClick={() => onOpenRange(path.path)}
              >
                {path.path}
              </button>
            ) : (
              <span>{path.path}</span>
            )}{" "}
            <span className="chip">{changeWords(path.change)}</span>
            {path.kind !== "text" && <span className="review-reason"> {kindWords(path.kind)}</span>}
          </li>
        ))}
      </ul>
      <p className="review-reason">{copy.uncommitted}</p>
      <button
        type="button"
        className="btn go sm"
        aria-describedby={reason !== null ? reasonId : undefined}
        disabled={busy || reason !== null}
        onClick={() => void integrate()}
      >
        {busy ? copy.integrating : target === null ? copy.integrate : copy.integrateInto(target)}
      </button>
      {reason !== null && (
        <p id={reasonId} className="review-reason">
          {reason}
        </p>
      )}
      <Feedback message={message} refusal={refusal} />
    </section>
  );
}

type PushPhase = "idle" | "pushing" | "unknown" | "checking";

export function PushPanel({
  api,
  folder,
  branch,
  headOid,
  remote,
  remoteState,
  remoteOid,
  pullRequestUrl,
  isWorktree,
  onOpenUrl,
  onDone,
}: {
  api: ReviewApi;
  folder: WorkspaceJson;
  branch: string;
  headOid: string;
  remote: string | null;
  remoteState: "ready" | "none" | "unsupported";
  remoteOid: string | null;
  pullRequestUrl: string | null;
  isWorktree: boolean;
  onOpenUrl(url: string): void;
  onDone(): void;
}) {
  const reasonId = useId();
  const [phase, setPhase] = useState<PushPhase>("idle");
  const [message, setMessage] = useState("");
  const [refusal, setRefusal] = useState<string | null>(null);
  const rootId = folder.root_physical_id;
  const reason = !rootId ? copy.noRoot : null;
  const remoteLine =
    remoteState === "none"
      ? NO_REMOTE
      : remoteState === "unsupported"
        ? REMOTE_UNSUPPORTED
        : remote === null
          ? null
          : remoteOid !== null
            ? copy.seen(remote, oid12(remoteOid))
            : copy.unseen(remote);

  function landed(remoteName: string) {
    setPhase("idle");
    setMessage(copy.pushed(branch, remoteName));
    onDone();
  }

  async function push() {
    if (phase !== "idle" || reason !== null || remoteState !== "ready" || !rootId || !remote)
      return;
    setPhase("pushing");
    setMessage("");
    setRefusal(null);
    try {
      const answer = await api.push(folder.workspace_id, {
        policy_revision: folder.policy_revision,
        root_physical_id: rootId,
        remote,
        branch,
        head_oid: headOid,
      });
      if (answer.outcome === "pushed") {
        landed(remote);
      } else {
        setPhase("unknown");
        setMessage(copy.unknown);
      }
    } catch (error) {
      setPhase("idle");
      setRefusal(codeOf(error));
    }
  }

  async function check() {
    if (phase !== "unknown" || !remote) return;
    setPhase("checking");
    setRefusal(null);
    try {
      const answer = await api.check(folder.workspace_id, {
        remote,
        branch,
        head_oid: headOid,
      });
      if (answer.outcome === "pushed") {
        landed(remote);
      } else {
        setPhase("idle");
        setMessage(copy.notPushed);
      }
    } catch (error) {
      setPhase("unknown");
      setRefusal(codeOf(error));
    }
  }

  return (
    <section className="review-panel" aria-label={copy.push}>
      {remoteLine !== null && <p className="review-reason">{remoteLine}</p>}
      <button
        type="button"
        className="btn go sm"
        aria-describedby={reason !== null ? reasonId : undefined}
        disabled={phase !== "idle" || reason !== null || remoteState !== "ready" || !remote}
        onClick={() => void push()}
      >
        {phase === "pushing" ? copy.pushing : copy.pushBranch(branch)}
      </button>
      {phase === "unknown" || phase === "checking" ? (
        <button
          type="button"
          className="btn txt sm"
          disabled={phase === "checking"}
          onClick={() => void check()}
        >
          {copy.check}
        </button>
      ) : null}
      {reason !== null && (
        <p id={reasonId} className="review-reason">
          {reason}
        </p>
      )}
      {pullRequestUrl !== null ? (
        <button type="button" className="btn txt sm" onClick={() => onOpenUrl(pullRequestUrl)}>
          {copy.page}
        </button>
      ) : isWorktree ? (
        <p className="review-reason">{copy.noPage}</p>
      ) : null}
      <Feedback message={message} refusal={refusal} />
    </section>
  );
}
