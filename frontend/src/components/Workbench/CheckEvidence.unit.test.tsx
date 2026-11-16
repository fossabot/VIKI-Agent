import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import {
  type CheckRunJson,
  type ChecksApi,
  ChecksApiError,
  type CodeCheckJson,
} from "../../clients/code/checksApi";
import { CheckEvidence } from "./CheckEvidence";

const W = "00000000-0000-4000-8000-0000000000a1";
const HEAD = "0123456789abcdef0123456789abcdef01234567";
const OTHER = "fedcba9876543210fedcba9876543210fedcba98";

afterEach(() => {
  cleanup();
});

let counter = 0;
const id = () => `00000000-0000-4000-8000-${String(++counter).padStart(12, "0")}`;
const run = (over: Partial<CheckRunJson> = {}): CheckRunJson => ({
  run_id: id(),
  check_id: id(),
  workspace_id: W,
  worktree_id: null,
  head_oid: HEAD,
  dirty: false,
  outcome: "passed",
  exit_code: 0,
  duration_ms: 1000,
  output_bytes: 0,
  started_at: "2026-10-04T10:00:00.000Z",
  ended_at: "2026-10-04T10:00:01.000Z",
  ...over,
});
const check = (name: string, last_run: CheckRunJson | null): CodeCheckJson => ({
  check_id: id(),
  name,
  command: "x",
  timeout_s: 600,
  created_at: "2026-10-04T10:00:00.000Z",
  readable: true,
  last_run,
});
const stub = (list: ChecksApi["list"]): ChecksApi => ({ list }) as ChecksApi;
const settle = () => act(async () => {});

async function show(checks: CodeCheckJson[], headOid = HEAD) {
  const api = stub(vi.fn().mockResolvedValue(checks));
  render(
    <CheckEvidence
      api={api}
      workspaceId={W}
      worktreeId={null}
      headOid={headOid}
      shown
      reloadKey={0}
    />,
  );
  await settle();
  return api;
}

test("matches only at the reviewed head with no uncommitted changes", async () => {
  await show([
    check("same", run()),
    check("dirty", run({ dirty: true })),
    check("old", run({ head_oid: OTHER })),
    check("bare", run({ head_oid: null, dirty: null })),
  ]);
  expect(screen.getByText("same — Passed · matches this commit")).toBeTruthy();
  expect(screen.getByText("dirty — Passed · uncommitted changes at the time")).toBeTruthy();
  expect(screen.getByText(`old — Passed · another commit (${OTHER.slice(0, 12)})`)).toBeTruthy();
  expect(screen.getByText("bare — Passed · no Git state")).toBeTruthy();
});

test("not run and running lines, and the empty words", async () => {
  await show([check("a", null), check("b", run({ outcome: "running", ended_at: null }))]);
  expect(screen.getByText("a — not run here yet")).toBeTruthy();
  expect(screen.getByText("b — running")).toBeTruthy();
  cleanup();
  await show([]);
  expect(screen.getByText("No checks yet. Add them in the Checks tab.")).toBeTruthy();
});

test("a new head asks again and aborts the earlier call", async () => {
  const list = vi.fn().mockResolvedValue([]);
  const api = stub(list);
  const view = (headOid: string) => (
    <CheckEvidence
      api={api}
      workspaceId={W}
      worktreeId={null}
      headOid={headOid}
      shown
      reloadKey={0}
    />
  );
  const { rerender } = render(view(HEAD));
  await settle();
  const first = list.mock.calls[0]?.[2] as AbortSignal;
  rerender(view(OTHER));
  await settle();
  expect(list).toHaveBeenCalledTimes(2);
  expect(first.aborted).toBe(true);
});

test.each(["unsupported_platform", "trust_required"])("%s renders nothing", async (code) => {
  const api = stub(vi.fn().mockRejectedValue(new ChecksApiError(code)));
  const { container } = render(
    <CheckEvidence
      api={api}
      workspaceId={W}
      worktreeId={null}
      headOid={HEAD}
      shown
      reloadKey={0}
    />,
  );
  await settle();
  expect(container.innerHTML).toBe("");
});

test("another failure shows the failure words", async () => {
  const api = stub(vi.fn().mockRejectedValue(new ChecksApiError("unavailable")));
  render(
    <CheckEvidence
      api={api}
      workspaceId={W}
      worktreeId={null}
      headOid={HEAD}
      shown
      reloadKey={0}
    />,
  );
  await settle();
  expect(screen.getByText("Checks could not be loaded.")).toBeTruthy();
});

test("a script name stays text", async () => {
  const { container } = render(
    <CheckEvidence
      api={stub(vi.fn().mockResolvedValue([check("<script>x</script>", null)]))}
      workspaceId={W}
      worktreeId={null}
      headOid={HEAD}
      shown
      reloadKey={0}
    />,
  );
  await settle();
  expect(container.querySelector("script")).toBeNull();
  expect(screen.getByText("<script>x</script> — not run here yet")).toBeTruthy();
});

const unreadable = (): CodeCheckJson => ({ ...check("x", null), name: null, readable: false });

test("the empty line shows only when no check is listed, not when all are unreadable", async () => {
  await show([unreadable()]);
  expect(screen.getByRole("heading", { name: "Checks" })).toBeTruthy();
  expect(screen.queryByText(/No checks yet/)).toBeNull();
  cleanup();
  await show([]);
  expect(screen.getByText(/No checks yet/)).toBeTruthy();
});

test("a new reloadKey lists again and shows the new answer", async () => {
  const list = vi
    .fn()
    .mockResolvedValueOnce([check("ok", null)])
    .mockResolvedValueOnce([check("ok", run())]);
  const api = stub(list);
  const view = (reloadKey: number) => (
    <CheckEvidence
      api={api}
      workspaceId={W}
      worktreeId={null}
      headOid={HEAD}
      shown
      reloadKey={reloadKey}
    />
  );
  const { rerender } = render(view(1));
  await settle();
  expect(screen.getByText("ok — not run here yet")).toBeTruthy();
  rerender(view(2));
  await settle();
  expect(list).toHaveBeenCalledTimes(2);
  expect(screen.getByText("ok — Passed · matches this commit")).toBeTruthy();
});

test("while not shown it makes no call, and showing it reads once", async () => {
  const list = vi.fn().mockResolvedValue([check("ok", run())]);
  const api = stub(list);
  const view = (shown: boolean) => (
    <CheckEvidence
      api={api}
      workspaceId={W}
      worktreeId={null}
      headOid={HEAD}
      shown={shown}
      reloadKey={0}
    />
  );
  const { rerender } = render(view(false));
  await settle();
  expect(list).not.toHaveBeenCalled();
  rerender(view(true));
  await settle();
  expect(list).toHaveBeenCalledTimes(1);
  expect(screen.getByText("ok — Passed · matches this commit")).toBeTruthy();
});
