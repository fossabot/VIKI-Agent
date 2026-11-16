import { expect, test } from "vitest";
import { rootRefusal } from "../../components/Workbench/Worktrees";
import { checksRefusal } from "./checkWords";
import type { PreviewJson } from "./previewsApi";
import { previewRefusal, previewStateWords } from "./previewWords";

const preview = (over: Partial<PreviewJson> = {}): PreviewJson => ({
  preview_id: "66666666-6666-4666-8666-666666666666",
  workspace_id: "22222222-2222-4222-8222-222222222222",
  worktree_id: null,
  command: "npm run dev",
  port: 5173,
  state: "starting",
  pid: 4242,
  listener_pid: null,
  exit_code: null,
  started_at: "2026-10-04T01:00:00.000Z",
  ended_at: null,
  ...over,
});

test("each state has its words", () => {
  const rows: [Partial<PreviewJson>, string][] = [
    [{ state: "starting" }, "Waiting for port 5173…"],
    [{ state: "listening", listener_pid: 4300 }, "Listening on port 5173 (process 4300)"],
    [{ state: "port_taken" }, "Port 5173 is used by another program"],
    [{ state: "stopped", ended_at: "2026-10-04T01:02:03.456Z" }, "Stopped"],
    [
      { state: "exited", exit_code: 3, ended_at: "2026-10-04T01:02:03.456Z" },
      "Ended (exit code 3)",
    ],
  ];
  for (const [over, words] of rows) {
    expect(previewStateWords(preview(over))).toBe(words);
  }
});

test("each refusal has its words", () => {
  const rows: [string, string][] = [
    ["invalid_port", "Use a port from 1024 to 65535."],
    ["port_reserved", "Port 8765 is VIKI's own. Choose another."],
    ["port_in_use", "Another preview uses this port."],
    ["port_taken", "Another program uses this port, so VIKI does not open it."],
    ["preview_limit", "Two previews are running. Stop one first."],
    ["window_limit", "Four preview windows are open. Close one first."],
    ["not_listening", "Nothing listens on this port yet. Wait for the server, then try again."],
    ["preview_running", "Stop the preview first."],
    ["unknown_preview", "This preview no longer exists. Refresh the list."],
    ["browser_missing", "No browser for preview windows was found."],
    ["start_failed", "The command could not start. Check that the folder still exists."],
    ["unsupported_platform", "Previews work on Windows in this version."],
  ];
  for (const [code, words] of rows) {
    expect(previewRefusal(code)).toBe(words);
  }
});

test("the codes shared with checks use the checks' words", () => {
  for (const code of ["invalid_command", "worktree_not_ready", "unknown_worktree"]) {
    expect(previewRefusal(code)).toBe(checksRefusal(code));
  }
  expect(previewRefusal("invalid_command")).toBe(
    "Write the command on one line, up to 8,000 characters.",
  );
});

test("other codes, the inert set's among them, use the root refusals", () => {
  const codes = [
    "trust_required",
    "unavailable",
    "toString",
    "nonsense",
    "check_running",
    "unsupported_file",
    "file_too_large",
    "private_path",
    "not_found",
    "not_a_file",
    "invalid_path",
  ];
  for (const code of codes) {
    expect(previewRefusal(code)).toBe(rootRefusal(code));
  }
});
