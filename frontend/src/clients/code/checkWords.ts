import { rootRefusal } from "../../components/Workbench/Worktrees";
import type { CheckRunJson } from "./checksApi";

const UNREADABLE = "This check can't be read on this computer. Delete it and add it again.";
const UNSUPPORTED_PLATFORM = "Checks need Windows, macOS or Linux.";
const NO_CHECK = "This check no longer exists. Refresh the list.";
const NOT_READY = "This worktree is not ready.";

const REFUSALS: Record<string, string> = {
  invalid_name: "Use 1 to 100 characters on one line.",
  invalid_command: "Write the command on one line, up to 8,000 characters.",
  invalid_timeout: "Set a time limit of 1 to 60 minutes.",
  check_limit: "A folder keeps at most 32 checks. Delete one first.",
  check_running: "This check is running. Cancel it or wait until it ends.",
  run_limit: "Four checks are running. Wait until one ends.",
  check_unreadable: UNREADABLE,
  unknown_check: NO_CHECK,
  unknown_run: NO_CHECK,
  start_failed: "The command could not start. Check that the folder still exists.",
  unsupported_platform: UNSUPPORTED_PLATFORM,
  worktree_not_ready: NOT_READY,
  unknown_worktree: NOT_READY,
};

export function checksRefusal(code: string): string {
  const words = Object.hasOwn(REFUSALS, code) ? REFUSALS[code] : undefined;
  return words ?? rootRefusal(code);
}

const OUTCOMES: Record<Exclude<CheckRunJson["outcome"], "failed">, string> = {
  running: "Running",
  passed: "Passed",
  cancelled: "Cancelled",
  timed_out: "Stopped at the time limit",
  interrupted: "Interrupted: VIKI closed while it ran",
};

export function outcomeWords(run: CheckRunJson): string {
  if (run.outcome !== "failed") return OUTCOMES[run.outcome];
  // A failed run carries its exit code; the bare word covers an answer without one.
  if (run.exit_code === null) return "Failed";
  return `Failed (exit code ${run.exit_code})`;
}

export function durationWords(ms: number): string {
  const total = Math.round(ms / 1000);
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  if (minutes === 0) return `${seconds} s`;
  return `${minutes} min ${seconds} s`;
}

/** "<outcome> · <oid12> · <state> · <duration> · <time>"; a running run has no duration. */
export function runLine(run: CheckRunJson): string {
  const git =
    run.head_oid === null
      ? ["no Git state"]
      : [run.head_oid.slice(0, 12), run.dirty ? "uncommitted changes" : "no uncommitted changes"];
  const stamp = new Date(run.ended_at ?? run.started_at).toLocaleString();
  const duration = run.duration_ms === null ? [] : [durationWords(run.duration_ms)];
  return [outcomeWords(run), ...git, ...duration, stamp].join(" · ");
}
