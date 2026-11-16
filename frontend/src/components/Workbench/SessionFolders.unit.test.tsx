import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import type { CodeApi } from "../../clients/code/codeApi";
import { RootsApiError } from "../../clients/code/rootsApi";
import { resetRoots } from "../../state/sessionRoots";
import { Workbench, type WorkbenchProps, type WorkbenchRoots } from "./Workbench";

const A = "00000000-0000-4000-8000-00000000000a";
const B = "00000000-0000-4000-8000-00000000000b";
const C = "00000000-0000-4000-8000-00000000000c";

afterEach(() => {
  cleanup();
  resetRoots();
});

const folder = (id: string, name: string, over: Partial<WorkspaceJson> = {}) =>
  ({
    workspace_id: id,
    kind: "code",
    name,
    root: `C:/projects/${name}`,
    root_available: true,
    root_physical_id: id,
    policy: "ordinary",
    policy_revision: 3,
    persistent: true,
    ...over,
  }) as WorkspaceJson;

function rootsStub(over: Partial<WorkbenchRoots> = {}): WorkbenchRoots {
  return {
    api: {
      roots: vi.fn().mockResolvedValue({ roots: [], truncated: false }),
      worktrees: vi.fn().mockResolvedValue({ worktrees: [], truncated: false }),
      record: vi.fn(),
      create: vi.fn(),
      remove: vi.fn(),
      archives: vi.fn(async () => {
        throw new RootsApiError("unavailable");
      }),
      cleanup: vi.fn(async () => {
        throw new RootsApiError("unavailable");
      }),
      restore: vi.fn(async () => {
        throw new RootsApiError("unavailable");
      }),
    },
    onStartSession: vi.fn().mockResolvedValue(null),
    ...over,
  };
}

function setup(
  folders = [folder(A, "Alpha"), folder(B, "Beta"), folder(C, "Gamma")],
  roots = rootsStub(),
) {
  const props: WorkbenchProps = {
    api: {
      tree: vi.fn().mockResolvedValue({ path: "", entries: [], truncated: false }),
    } as unknown as CodeApi,
    folders,
    activeId: A,
    onQuote: vi.fn(),
    onOpenInEditor: vi.fn(),
    onOpenSettings: vi.fn(),
    onBack: vi.fn(),
    fontSize: 13,
    lineWrap: false,
    roots,
  };
  return { ...render(<Workbench {...props} />), props, roots };
}

const settle = () => act(async () => {});
const checkbox = (name: string) => screen.getByRole<HTMLInputElement>("checkbox", { name });
const startButton = () => screen.getByRole("button", { name: /New session/ });

test("selected code folders start one conversation with the browsed folder as primary", async () => {
  const { roots } = setup();
  const choices = screen.getByRole("group", { name: "Folders for this conversation" });
  expect(within(choices).getByText("Primary folder (working directory)")).toBeTruthy();
  expect(within(choices).getByText("C:/projects/Alpha")).toBeTruthy();
  fireEvent.click(checkbox("Beta C:/projects/Beta"));
  fireEvent.click(checkbox("Gamma C:/projects/Gamma"));
  fireEvent.click(startButton());
  await settle();
  expect(roots.onStartSession).toHaveBeenCalledExactlyOnceWith({
    workspace_id: A,
    worktree_id: null,
    additional_workspace_ids: [B, C],
  });
});

test("starting without extra folders preserves the single-folder callback", async () => {
  const { roots } = setup();
  fireEvent.click(startButton());
  await settle();
  expect(roots.onStartSession).toHaveBeenCalledExactlyOnceWith({
    workspace_id: A,
    worktree_id: null,
  });
});

test("changing the primary clears extra folder choices before starting", async () => {
  const { roots } = setup();
  fireEvent.click(checkbox("Beta C:/projects/Beta"));
  fireEvent.change(screen.getByLabelText("Code folder"), { target: { value: C } });
  expect(checkbox("Beta C:/projects/Beta").checked).toBe(false);
  fireEvent.click(startButton());
  await settle();
  expect(roots.onStartSession).toHaveBeenCalledExactlyOnceWith({
    workspace_id: C,
    worktree_id: null,
  });
});

test("extra folders show exact names and roots as text, with native permission wording", () => {
  const name = "<script>code</script>";
  const root = "C:/projects/<img onerror=code>";
  const { container } = setup([
    folder(A, "Alpha"),
    folder(B, name, { root }),
    folder(C, "Vault", { kind: "vault" }),
  ]);
  expect(checkbox(`${name} ${root}`)).toBeTruthy();
  expect(
    screen.getByText(
      "Selected folders add context to one conversation. Claude may ask before accessing additional folders.",
    ),
  ).toBeTruthy();
  expect(screen.queryByRole("checkbox", { name: /Alpha|Vault/ })).toBeNull();
  expect(container.querySelector("script, img")).toBeNull();
});

test.each([
  ["protected", { policy: "protected" }, "Trust this folder in Workspaces settings first."],
  ["read-only", { policy: "existing_readonly" }, "Trust this folder in Workspaces settings first."],
  ["unavailable", { root_available: false }, "This folder is unavailable."],
  ["temporary", { persistent: false }, "Register this folder in Workspaces settings first."],
] as const)(
  "%s extra folders stay visible, disabled, and explain their reason",
  (_name, over, reason) => {
    setup([folder(A, "Alpha"), folder(B, "Beta", over)]);
    const choice = checkbox("Beta C:/projects/Beta");
    expect(choice.disabled).toBe(true);
    expect(choice.getAttribute("aria-describedby")).toBe(screen.getByText(reason).id);
    fireEvent.click(choice);
    expect(choice.checked).toBe(false);
  },
);

test("eight folders is the limit, and selected extras remain removable", async () => {
  const extra = Array.from({ length: 9 }, (_, index) =>
    folder(`00000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`, `Folder${index}`),
  );
  const { roots } = setup([folder(A, "Alpha"), ...extra]);
  for (const current of extra.slice(0, 7)) {
    fireEvent.click(checkbox(`${current.name} ${current.root}`));
  }
  expect(
    screen.getAllByRole<HTMLInputElement>("checkbox").filter((input) => input.checked),
  ).toHaveLength(7);
  const blocked = checkbox("Folder7 C:/projects/Folder7");
  expect(blocked.disabled).toBe(true);
  expect(document.getElementById(blocked.getAttribute("aria-describedby") ?? "")?.textContent).toBe(
    "A conversation can include up to 8 folders. Remove an additional folder first.",
  );
  expect(checkbox("Folder0 C:/projects/Folder0").disabled).toBe(false);
  fireEvent.click(checkbox("Folder0 C:/projects/Folder0"));
  expect(blocked.disabled).toBe(false);
  fireEvent.click(blocked);
  fireEvent.click(startButton());
  await settle();
  expect(roots.onStartSession).toHaveBeenCalledExactlyOnceWith({
    workspace_id: A,
    worktree_id: null,
    additional_workspace_ids: extra.slice(1, 8).map((current) => current.workspace_id),
  });
});

test.each([
  ["revoked trust", { policy: "existing_readonly" }],
  ["unavailable root", { root_available: false }],
  ["removed registration", { persistent: false }],
  ["changed path", { root: "C:/projects/Different" }],
  ["changed identity", { root_physical_id: "different" }],
  ["changed policy revision", { policy_revision: 4 }],
  ["changed kind", { kind: "vault" }],
] as const)(
  "%s drops a selected extra and shows the change without restoring stale picks",
  async (_name, over) => {
    const { props, roots, rerender } = setup();
    fireEvent.click(checkbox("Beta C:/projects/Beta"));
    rerender(<Workbench {...props} folders={[folder(A, "Alpha"), folder(B, "Beta", over)]} />);
    expect(screen.getByRole("status").textContent).toBe(
      "Selected folders changed. Review the folders before starting a conversation.",
    );
    rerender(<Workbench {...props} />);
    expect(checkbox("Beta C:/projects/Beta").checked).toBe(false);
    fireEvent.click(startButton());
    await settle();
    expect(roots.onStartSession).toHaveBeenCalledExactlyOnceWith({
      workspace_id: A,
      worktree_id: null,
    });
  },
);

test("a missing extra is removed, and a missing primary clears its surviving extra picks", async () => {
  const { props, roots, rerender } = setup();
  fireEvent.click(checkbox("Beta C:/projects/Beta"));
  fireEvent.click(checkbox("Gamma C:/projects/Gamma"));
  rerender(<Workbench {...props} folders={[folder(A, "Alpha"), folder(C, "Gamma")]} />);
  expect(screen.queryByRole("checkbox", { name: /Beta/ })).toBeNull();
  expect(checkbox("Gamma C:/projects/Gamma").checked).toBe(true);
  expect(screen.getByRole("status").textContent).toContain("Selected folders changed.");
  rerender(<Workbench {...props} folders={[folder(B, "Beta"), folder(C, "Gamma")]} />);
  expect(checkbox("Gamma C:/projects/Gamma").checked).toBe(false);
  fireEvent.click(startButton());
  await settle();
  expect(roots.onStartSession).toHaveBeenCalledExactlyOnceWith({
    workspace_id: B,
    worktree_id: null,
  });
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

test("an in-flight start snapshots the picks and disables both selectors and repeat starts", async () => {
  const waiting = deferred<string | null>();
  const onStartSession = vi.fn().mockReturnValue(waiting.promise);
  const { props, rerender } = setup(undefined, rootsStub({ onStartSession }));
  fireEvent.click(checkbox("Beta C:/projects/Beta"));
  const start = startButton();
  act(() => {
    fireEvent.click(start);
    fireEvent.click(start);
  });
  expect(onStartSession).toHaveBeenCalledExactlyOnceWith({
    workspace_id: A,
    worktree_id: null,
    additional_workspace_ids: [B],
  });
  expect((start as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByLabelText("Code folder") as HTMLSelectElement).disabled).toBe(true);
  expect(checkbox("Beta C:/projects/Beta").disabled).toBe(true);
  expect(checkbox("Gamma C:/projects/Gamma").disabled).toBe(true);
  fireEvent.change(screen.getByLabelText("Code folder"), { target: { value: C } });
  fireEvent.click(checkbox("Gamma C:/projects/Gamma"));
  expect(checkbox("Gamma C:/projects/Gamma").checked).toBe(false);
  rerender(<Workbench {...props} folders={[folder(A, "Alpha"), folder(C, "Gamma")]} />);
  expect(onStartSession.mock.calls[0]?.[0].additional_workspace_ids).toEqual([B]);
  await act(async () => waiting.resolve(null));
  expect((startButton() as HTMLButtonElement).disabled).toBe(false);
  expect(onStartSession).toHaveBeenCalledTimes(1);
});

test("a stale start refusal cannot return after the primary disappears and reappears", async () => {
  const waiting = deferred<string | null>();
  const { props, rerender } = setup(
    undefined,
    rootsStub({ onStartSession: vi.fn(() => waiting.promise) }),
  );
  fireEvent.click(startButton());
  rerender(<Workbench {...props} folders={[folder(B, "Beta")]} />);
  rerender(<Workbench {...props} />);
  await act(async () => waiting.resolve("too_many_sessions"));
  expect(screen.queryByRole("alert")).toBeNull();
  expect((startButton() as HTMLButtonElement).disabled).toBe(false);
});

test("a rejected start shows a refusal and allows another attempt", async () => {
  const onStartSession = vi
    .fn()
    .mockRejectedValueOnce(new Error("offline"))
    .mockResolvedValueOnce(null);
  setup(undefined, rootsStub({ onStartSession }));
  fireEvent.click(startButton());
  await settle();
  expect(screen.getByRole("alert").textContent).toBe("That did not work. Try again.");
  expect((startButton() as HTMLButtonElement).disabled).toBe(false);
  fireEvent.click(startButton());
  await settle();
  expect(onStartSession).toHaveBeenCalledTimes(2);
  expect(screen.queryByRole("alert")).toBeNull();
});

test("an unavailable primary refuses a start and explains why", async () => {
  const { roots } = setup([folder(A, "Alpha", { root_available: false }), folder(B, "Beta")]);
  const start = startButton();
  expect(start.getAttribute("aria-disabled")).toBe("true");
  expect(start.getAttribute("aria-describedby")).toBe(
    screen.getByText("This folder changed. Reopen the Code view and try again.").id,
  );
  fireEvent.click(start);
  await settle();
  expect(roots.onStartSession).not.toHaveBeenCalled();
});

test("a worktree start keeps its single-root callback even when extra folders are selected", async () => {
  const roots = rootsStub();
  const worktreeId = "00000000-0000-4000-8000-00000000001a";
  vi.mocked(roots.api.worktrees).mockResolvedValue({
    worktrees: [
      {
        workspace_id: A,
        worktree_id: worktreeId,
        branch: "viki/example",
        base_oid: "a".repeat(40),
        path: "C:/projects/example-worktree",
        state: "ready",
        created_at: "2026-10-11T00:00:00Z",
        updated_at: "2026-10-11T00:00:00Z",
      },
    ],
    truncated: false,
  });
  setup(undefined, roots);
  fireEvent.click(checkbox("Beta C:/projects/Beta"));
  fireEvent.click(screen.getByRole("tab", { name: "Worktrees" }));
  await settle();
  fireEvent.click(screen.getByRole("button", { name: "New session here" }));
  await settle();
  expect(roots.onStartSession).toHaveBeenCalledExactlyOnceWith({
    workspace_id: A,
    worktree_id: worktreeId,
  });
});
