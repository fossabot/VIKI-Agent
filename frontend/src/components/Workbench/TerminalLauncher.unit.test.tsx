import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import type { WorktreeJson } from "../../clients/code/rootsApi";
import { type TerminalApi, TerminalApiError } from "../../clients/code/terminalApi";
import { noteWorktree, resetRoots } from "../../state/sessionRoots";
import { TerminalLauncher, terminalRefusal } from "./TerminalLauncher";

const W = "00000000-0000-4000-8000-0000000000a1";

afterEach(() => {
  cleanup();
  resetRoots();
  vi.restoreAllMocks();
});

const folder = (over: Partial<WorkspaceJson> = {}) =>
  ({
    workspace_id: W,
    kind: "code",
    name: "Alpha",
    policy: "ordinary",
    policy_revision: 3,
    root: "x",
    root_available: true,
    ...over,
  }) as WorkspaceJson;

let counter = 0;
const worktree = (over: Partial<WorktreeJson> = {}): WorktreeJson => {
  counter += 1;
  return {
    worktree_id: `00000000-0000-4000-8000-${String(counter).padStart(12, "0")}`,
    workspace_id: W,
    branch: `viki/b${counter}`,
    base_oid: "1".repeat(40),
    path: `C:\\area\\wt${counter}`,
    state: "ready",
    created_at: "2026-10-03T10:00:00Z",
    updated_at: "2026-10-03T10:00:00Z",
    ...over,
  };
};

const stub = (open: TerminalApi["open"] = vi.fn().mockResolvedValue(undefined)) => ({ open });
const show = (api: TerminalApi, props: { canOpen?: boolean; folder?: WorkspaceJson } = {}) =>
  render(
    <TerminalLauncher
      api={api}
      folder={props.folder ?? folder()}
      canOpen={props.canOpen ?? true}
    />,
  );
const settle = () => act(async () => {});
const openButton = () => screen.getByRole("button", { name: "Open terminal here" });

describe("TerminalLauncher", () => {
  test("Open terminal here sends the picked worktree", async () => {
    const row = worktree();
    act(() => noteWorktree(row));
    const api = stub();
    show(api);
    fireEvent.change(screen.getByLabelText("Open in"), { target: { value: row.worktree_id } });
    fireEvent.click(openButton());
    await settle();
    expect(api.open).toHaveBeenCalledWith(W, row.worktree_id, expect.any(AbortSignal));
    expect(screen.getByRole("status").textContent).toBe("Opened the terminal.");
  });

  test("the folder itself sends null; the picker lists only ready worktrees", async () => {
    const ready = worktree();
    act(() => {
      noteWorktree(ready);
      noteWorktree(worktree({ state: "creating" as WorktreeJson["state"] }));
      noteWorktree(worktree({ workspace_id: "00000000-0000-4000-8000-0000000000a2" }));
    });
    const api = stub();
    show(api);
    const options = [...screen.getByLabelText("Open in").querySelectorAll("option")];
    expect(options.map((option) => option.textContent)).toEqual([
      "This folder",
      `${ready.branch} (worktree)`,
    ]);
    fireEvent.click(openButton());
    await settle();
    expect(api.open).toHaveBeenCalledWith(W, null, expect.any(AbortSignal));
  });

  test("the note and the busy label show while the call is open", async () => {
    let finish: () => void = () => {};
    const api = stub(vi.fn(() => new Promise<void>((resolve) => (finish = resolve))));
    show(api);
    expect(
      screen.getByText(
        "Opens a terminal in its own window, in this folder or one of its worktrees, with your permissions. It keeps running after VIKI closes.",
      ),
    ).toBeTruthy();
    fireEvent.click(openButton());
    const busy = screen.getByRole("button", { name: "Opening…" }) as HTMLButtonElement;
    expect(busy.disabled).toBe(true);
    finish();
    await settle();
    expect(openButton()).toBeTruthy();
  });

  test("an unsupported platform shows its refusal without opening", () => {
    show(stub(), { canOpen: false });
    expect(screen.getByText("The terminal needs Windows, macOS or Linux.")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });

  test("an untrusted folder shows the trust words", () => {
    show(stub(), { folder: folder({ policy: "read_only" as WorkspaceJson["policy"] }) });
    expect(screen.getByText(terminalRefusal("trust_required"))).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });

  test("a refusal shows its words as an alert and clears on the next try", async () => {
    const open = vi
      .fn()
      .mockRejectedValueOnce(new TerminalApiError("start_failed"))
      .mockResolvedValueOnce(undefined);
    show(stub(open));
    fireEvent.click(openButton());
    await settle();
    expect(screen.getByRole("alert").textContent).toBe("The terminal could not start.");
    fireEvent.click(openButton());
    await settle();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("a failure without a code reads as unavailable, not as success", async () => {
    show(stub(vi.fn().mockRejectedValue(new Error("boom"))));
    fireEvent.click(openButton());
    await settle();
    expect(screen.getByRole("alert").textContent).toBe(terminalRefusal("unavailable"));
    expect(screen.getByRole("status").textContent).toBe("");
  });
});

describe("terminalRefusal", () => {
  test("its own codes, the shared ones, then the root refusals", () => {
    expect(terminalRefusal("start_failed")).toBe("The terminal could not start.");
    expect(terminalRefusal("unsupported_platform")).toBe(
      "The terminal needs Windows, macOS or Linux.",
    );
    expect(terminalRefusal("worktree_not_ready")).toBe("This worktree is not ready.");
    expect(terminalRefusal("unknown_worktree")).toBe("This worktree is not ready.");
    expect(terminalRefusal("trust_required")).not.toBe("");
    // No route answers terminal_limit, so it reads as any unknown code.
    expect(terminalRefusal("terminal_limit")).toBe("That did not work. Try again.");
  });
});
