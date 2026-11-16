import { expect, test } from "vitest";
import { rootRefusal } from "../../components/Workbench/Worktrees";
import type { CheckRunJson } from "./checksApi";
import { checksRefusal, durationWords, outcomeWords, runLine } from "./checkWords";

const OID = "0123456789abcdef0123456789abcdef01234567";
const ENDED = "2026-10-04T01:02:03.456Z";
const STAMP = new Date(ENDED).toLocaleString();

const run = (over: Partial<CheckRunJson> = {}): CheckRunJson => ({
  run_id: "55555555-5555-4555-8555-555555555555",
  check_id: "44444444-4444-4444-8444-444444444444",
  workspace_id: "22222222-2222-4222-8222-222222222222",
  worktree_id: null,
  head_oid: OID,
  dirty: false,
  outcome: "passed",
  exit_code: 0,
  duration_ms: 65_000,
  output_bytes: 0,
  started_at: "2026-10-04T01:00:00.000Z",
  ended_at: ENDED,
  ...over,
});

test("each outcome has its words", () => {
  const rows: [Partial<CheckRunJson>, string][] = [
    [{ outcome: "running" }, "Running"],
    [{ outcome: "passed" }, "Passed"],
    [{ outcome: "failed", exit_code: 3 }, "Failed (exit code 3)"],
    [{ outcome: "failed", exit_code: null }, "Failed"],
    [{ outcome: "cancelled" }, "Cancelled"],
    [{ outcome: "timed_out" }, "Stopped at the time limit"],
    [{ outcome: "interrupted" }, "Interrupted: VIKI closed while it ran"],
  ];
  for (const [over, words] of rows) {
    expect(outcomeWords(run(over))).toBe(words);
  }
});

test("durations", () => {
  expect(durationWords(0)).toBe("0 s");
  expect(durationWords(12_400)).toBe("12 s");
  expect(durationWords(65_000)).toBe("1 min 5 s");
  expect(durationWords(125_000)).toBe("2 min 5 s");
});

test("a run line names the commit and the state", () => {
  expect(runLine(run())).toBe(
    `Passed · 0123456789ab · no uncommitted changes · 1 min 5 s · ${STAMP}`,
  );
  expect(runLine(run({ dirty: true }))).toBe(
    `Passed · 0123456789ab · uncommitted changes · 1 min 5 s · ${STAMP}`,
  );
});

test("a null head's line says there is no Git state", () => {
  const bare = run({ head_oid: null, dirty: null });
  expect(runLine(bare)).toBe(`Passed · no Git state · 1 min 5 s · ${STAMP}`);
});

test("a running run shows its start time and no duration", () => {
  const live = run({ outcome: "running", ended_at: null, duration_ms: null });
  expect(runLine(live)).toBe(
    `Running · 0123456789ab · no uncommitted changes · ${new Date(live.started_at).toLocaleString()}`,
  );
});

test("each refusal has its words", () => {
  const rows: [string, string][] = [
    ["invalid_name", "Use 1 to 100 characters on one line."],
    ["invalid_command", "Write the command on one line, up to 8,000 characters."],
    ["invalid_timeout", "Set a time limit of 1 to 60 minutes."],
    ["check_limit", "A folder keeps at most 32 checks. Delete one first."],
    ["check_running", "This check is running. Cancel it or wait until it ends."],
    ["run_limit", "Four checks are running. Wait until one ends."],
    ["check_unreadable", "This check can't be read on this computer. Delete it and add it again."],
    ["unknown_check", "This check no longer exists. Refresh the list."],
    ["unknown_run", "This check no longer exists. Refresh the list."],
    ["start_failed", "The command could not start. Check that the folder still exists."],
    ["unsupported_platform", "Checks need Windows, macOS or Linux."],
    ["worktree_not_ready", "This worktree is not ready."],
    ["unknown_worktree", "This worktree is not ready."],
  ];
  for (const [code, words] of rows) {
    expect(checksRefusal(code)).toBe(words);
  }
});

test("other codes use the root refusals", () => {
  for (const code of ["trust_required", "unavailable", "toString", "nonsense"]) {
    expect(checksRefusal(code)).toBe(rootRefusal(code));
  }
  expect(checksRefusal("toString")).toBe("That did not work. Try again.");
});
