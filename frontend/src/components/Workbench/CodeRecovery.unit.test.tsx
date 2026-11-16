import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { VaultRecoveryStatus } from "../../state/vaultApi";
import { VaultOpsError } from "../../state/vaultOpsApi";
import { CodeRecovery, type CodeRecoveryApi } from "./CodeRecovery";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function row(over: Partial<VaultRecoveryStatus> = {}): VaultRecoveryStatus {
  return {
    operation_id: "op1",
    workspace_id: "w1",
    ref: "src/a.py",
    state: "conflict",
    reason: "conflict",
    resolvable: ["forget"],
    ...over,
  };
}

function setup(rows: VaultRecoveryStatus[] | Error, forget = vi.fn().mockResolvedValue({})) {
  const list = vi.fn(() => (rows instanceof Error ? Promise.reject(rows) : Promise.resolve(rows)));
  const recovery: CodeRecoveryApi = { list, forget };
  const view = render(<CodeRecovery recovery={recovery} workspaceId="w1" refreshKey={0} />);
  return { list, forget, view };
}

const FORGET = "Forget the record";
const GENERIC = "That did not work. Try again in a moment.";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const settle = () => act(async () => {});

test.each(["success", "unknown_publication"])(
  "a late Forget %s refreshes the current workspace and keeps the next target there",
  async (outcome) => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const pending = deferred<unknown>();
    const old = row({ operation_id: "old", ref: "old.txt" });
    const oldOther = row({ operation_id: "old-other", ref: "old-other.txt" });
    const current = row({ workspace_id: "w2", operation_id: "current", ref: "current.txt" });
    const recovery: CodeRecoveryApi = {
      list: vi.fn().mockResolvedValue([old, oldOther, current]),
      forget: vi.fn().mockReturnValueOnce(pending.promise).mockResolvedValue({}),
    };
    const view = render(<CodeRecovery recovery={recovery} workspaceId="w1" refreshKey={0} />);
    await settle();
    fireEvent.click(screen.getAllByRole("button", { name: FORGET })[0] as HTMLElement);
    view.rerender(<CodeRecovery recovery={recovery} workspaceId="w2" refreshKey={0} />);
    await settle();
    expect(screen.getByText("current.txt")).toBeTruthy();
    if (outcome === "success") pending.resolve({});
    else pending.reject(new VaultOpsError("unknown_publication"));
    await settle();
    expect(screen.getByText("current.txt")).toBeTruthy();
    expect(screen.queryByText("old-other.txt")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: FORGET }));
    await settle();
    expect(recovery.forget).toHaveBeenNthCalledWith(1, "old");
    expect(recovery.forget).toHaveBeenNthCalledWith(2, "current");
  },
);

test("a failed old Forget leaves the current workspace's feedback alone", async () => {
  vi.spyOn(window, "confirm").mockReturnValue(true);
  const pending = deferred<unknown>();
  const recovery: CodeRecoveryApi = {
    list: vi.fn().mockResolvedValue([row(), row({ workspace_id: "w2", ref: "new.txt" })]),
    forget: vi.fn().mockReturnValue(pending.promise),
  };
  const view = render(<CodeRecovery recovery={recovery} workspaceId="w1" refreshKey={0} />);
  await settle();
  fireEvent.click(screen.getByRole("button", { name: FORGET }));
  view.rerender(<CodeRecovery recovery={recovery} workspaceId="w2" refreshKey={0} />);
  await settle();
  pending.reject(new VaultOpsError("overloaded"));
  await settle();
  expect(screen.getByText("new.txt")).toBeTruthy();
  expect(screen.queryByText(GENERIC)).toBeNull();
});

test("a Forget answered after unmount starts no read", async () => {
  vi.spyOn(window, "confirm").mockReturnValue(true);
  const pending = deferred<unknown>();
  const { view, list } = setup([row()], vi.fn().mockReturnValue(pending.promise));
  await settle();
  fireEvent.click(screen.getByRole("button", { name: FORGET }));
  view.unmount();
  pending.resolve({});
  await settle();
  expect(list).toHaveBeenCalledTimes(1);
});

test("keeps only the rows of the workspace, with the reason's words", async () => {
  setup([
    row(),
    row({ operation_id: "op2", workspace_id: "w2", ref: "other.py" }),
    row({ operation_id: "op3", ref: "b.py", reason: "recovery_required" }),
  ]);
  expect(await screen.findByText("Stopped saves")).toBeTruthy();
  expect(screen.getByText("Restarting VIKI Agent finishes most of them.")).toBeTruthy();
  expect(screen.getByText("src/a.py")).toBeTruthy();
  expect(screen.queryByText("other.py")).toBeNull();
  expect(screen.getByText("Stopped because the file changed before the save.")).toBeTruthy();
  expect(screen.getByText("The previous save did not finish and needs recovery.")).toBeTruthy();
  expect(screen.getAllByText(FORGET).length).toBe(2);
});

test("renders nothing when there are no rows or the read fails", async () => {
  const empty = setup([row({ workspace_id: "w2" })]);
  await waitFor(() => expect(empty.list).toHaveBeenCalled());
  expect(empty.view.container.innerHTML).toBe("");
  cleanup();
  const failed = setup(new Error("down"));
  await waitFor(() => expect(failed.list).toHaveBeenCalled());
  expect(failed.view.container.innerHTML).toBe("");
});

test("a declined confirm calls nothing", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const { forget, list } = setup([row()]);
  fireEvent.click(await screen.findByText(FORGET));
  expect(confirm).toHaveBeenCalledWith(
    "This drops the app's record only; the file does not change. Temporary files starting .viki-stage- or .viki-backup- may remain in the folder.",
  );
  expect(forget).not.toHaveBeenCalled();
  expect(list).toHaveBeenCalledTimes(1);
});

test("forget asks, then drops the record", async () => {
  vi.spyOn(window, "confirm").mockReturnValue(true);
  const { forget, list } = setup([row()]);
  fireEvent.click(await screen.findByText(FORGET));
  await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
  expect(forget).toHaveBeenCalledWith("op1");
});

test("an already gone record reads again without a message", async () => {
  vi.spyOn(window, "confirm").mockReturnValue(true);
  const { list } = setup(
    [row()],
    vi.fn().mockRejectedValue(new VaultOpsError("unknown_publication")),
  );
  fireEvent.click(await screen.findByText(FORGET));
  await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
  expect(screen.queryByText(GENERIC)).toBeNull();
});

test("any other refusal shows the generic words, not the code", async () => {
  vi.spyOn(window, "confirm").mockReturnValue(true);
  const { list } = setup([row()], vi.fn().mockRejectedValue(new VaultOpsError("overloaded")));
  fireEvent.click(await screen.findByText(FORGET));
  expect(await screen.findByText(GENERIC)).toBeTruthy();
  expect(screen.queryByText("overloaded")).toBeNull();
  expect(list).toHaveBeenCalledTimes(1);
});

test("a script in a ref stays text", async () => {
  const ref = "<script>alert(1)</script>.py";
  const { view } = setup([row({ ref })]);
  expect(await screen.findByText(ref)).toBeTruthy();
  expect(view.container.querySelector("script")).toBeNull();
});
