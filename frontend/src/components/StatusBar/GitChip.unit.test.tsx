import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { type CodeApi, CodeApiError, type CodeChanges } from "../../clients/code/codeApi";
import { GitChip, type GitStatus } from "./GitChip";

afterEach(cleanup);

const settle = () => act(async () => {});

function codeApi(over: Partial<CodeApi> = {}): CodeApi {
  return {
    tree: vi.fn(),
    file: vi.fn(),
    search: vi.fn(),
    save: vi.fn(),
    diff: vi.fn(),
    changes: vi.fn().mockResolvedValue({
      state: "ready",
      branch: "main",
      head: null,
      detached: false,
      upstream: null,
      ahead: null,
      behind: null,
      truncated: false,
      entries: [],
    } satisfies CodeChanges),
    ...over,
  };
}

function entry(path: string, over: Partial<CodeChanges["entries"][number]> = {}) {
  return {
    path,
    orig_path: null,
    index: ".",
    worktree: "M",
    kind: "tracked",
    private: false,
    ...over,
  } as CodeChanges["entries"][number];
}

function ready(paths: string[]): CodeChanges {
  return {
    state: "ready",
    branch: "main",
    head: "abcdef123456",
    detached: false,
    upstream: null,
    ahead: null,
    behind: null,
    truncated: false,
    entries: paths.map((path) => entry(path)),
  } satisfies CodeChanges;
}

/** Reads a popover's `<dl>` as {term: value}, the same shape every row test asserts against. */
function dlEntries(dialog: HTMLElement): Record<string, string> {
  const children = [...(dialog.querySelector("dl")?.children ?? [])];
  const result: Record<string, string> = {};
  for (let i = 0; i < children.length; i += 2) {
    result[children[i]?.textContent ?? ""] = children[i + 1]?.textContent ?? "";
  }
  return result;
}

// --- renders nothing without a target; code wins when both are given ---

test("renders nothing without a vault or a code folder", () => {
  const { container } = render(<GitChip />);
  expect(container.firstChild).toBeNull();
});

test("code wins over vault when both are given", () => {
  render(
    <GitChip
      vault={{ branch: "main", dirty: false, sha: "abc1234" }}
      code={{ api: codeApi(), workspaceId: "w1", name: "Alpha", onOpen: vi.fn() }}
    />,
  );
  screen.getByRole("button", { name: "Alpha · checking" });
});

// --- vault label ---

test.each<[GitStatus, string]>([
  [{ branch: "main", dirty: false, sha: "abc1234" }, "main · clean · abc1234"],
  [{ branch: "main", dirty: true, sha: "abc1234" }, "main · dirty · abc1234"],
  [{ branch: "main", dirty: null, sha: "" }, "main · status unknown"],
  [{ branch: "main", dirty: null, sha: "", checking: true }, "main · checking"],
  [{ branch: "", dirty: false, sha: "", refState: "detached" }, "detached HEAD · clean"],
])("vault label for %j is %j", (vault, label) => {
  render(<GitChip vault={vault} />);
  screen.getByRole("button", { name: label });
});

// --- the hidden description and the title ---

test("a last commit names a hidden description, pointed at by aria-describedby, folded into the title", () => {
  render(
    <GitChip
      vault={{ branch: "main", dirty: false, sha: "abc1234" }}
      lastCommit={{ sha: "abc1234", time: "19:16" }}
      vaultName="VIKI"
    />,
  );
  const button = screen.getByRole("button", { name: "main · clean · abc1234" });
  const descId = button.getAttribute("aria-describedby");
  expect(descId).toBeTruthy();
  const desc = descId ? document.getElementById(descId) : null;
  expect(desc?.textContent).toBe("last commit abc1234 19:16");
  expect(desc?.hasAttribute("hidden")).toBe(true);
  expect(button.getAttribute("title")).toBe("Git: VIKI · last commit abc1234 19:16");
});

test("without a last commit there is no description, and the title names only the vault", () => {
  render(<GitChip vault={{ branch: "main", dirty: false, sha: "abc1234" }} vaultName="VIKI" />);
  const button = screen.getByRole("button", { name: "main · clean · abc1234" });
  expect(button.hasAttribute("aria-describedby")).toBe(false);
  expect(button.getAttribute("title")).toBe("Git: VIKI");
});

test("without a vaultName the vault defaults to 'vault' in the title", () => {
  render(<GitChip vault={{ branch: "main", dirty: false, sha: "abc1234" }} />);
  expect(screen.getByRole("button", { name: /main/ }).getAttribute("title")).toBe("Git: vault");
});

// --- popover rows ---

test("the popover reports the vault's repository, branch, state, HEAD and last commit", () => {
  render(
    <GitChip
      vault={{ branch: "main", dirty: true, sha: "abc1234" }}
      lastCommit={{ sha: "abc1234", time: "19:16" }}
      vaultName="VIKI"
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "main · dirty · abc1234" }));
  const dialog = screen.getByRole("dialog", { name: "Git" });
  expect(dlEntries(dialog)).toEqual({
    Repository: "VIKI (vault)",
    Branch: "main",
    State: "dirty",
    HEAD: "abc1234",
    "Last commit": "19:16",
  });
});

test("the popover omits HEAD and Last commit rows when there is nothing to show", () => {
  render(<GitChip vault={{ branch: "main", dirty: false, sha: "" }} />);
  fireEvent.click(screen.getByRole("button", { name: "main · clean" }));
  const dialog = screen.getByRole("dialog", { name: "Git" });
  expect(dlEntries(dialog)).toEqual({
    Repository: "vault (vault)",
    Branch: "main",
    State: "clean",
  });
});

test("the popover never shows a Last commit row for a code folder", async () => {
  const api = codeApi({
    changes: vi.fn().mockResolvedValue({
      state: "ready",
      branch: "main",
      head: "abcdef123456",
      detached: false,
      upstream: null,
      ahead: null,
      behind: null,
      truncated: false,
      entries: [],
    } satisfies CodeChanges),
  });
  render(<GitChip code={{ api, workspaceId: "w1", name: "Alpha", onOpen: vi.fn() }} />);
  await settle();
  fireEvent.click(screen.getByRole("button", { name: "main · clean · abcdef1" }));
  const dialog = screen.getByRole("dialog", { name: "Git" });
  expect(dlEntries(dialog)).toEqual({
    Repository: "Alpha (code folder)",
    Branch: "main",
    State: "clean",
    HEAD: "abcdef1",
  });
});

// --- Escape, outside click ---

test("Escape closes the popover and refocuses the chip", () => {
  render(<GitChip vault={{ branch: "main", dirty: false, sha: "abc1234" }} />);
  const button = screen.getByRole("button", { name: "main · clean · abc1234" });
  fireEvent.click(button);
  expect(document.activeElement).not.toBe(button);
  fireEvent.keyDown(window, { key: "Escape" });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(button);
});

test("a mousedown outside the popover closes it", () => {
  render(
    <div>
      <GitChip vault={{ branch: "main", dirty: false, sha: "abc1234" }} />
      <button type="button">elsewhere</button>
    </div>,
  );
  fireEvent.click(screen.getByRole("button", { name: "main · clean · abc1234" }));
  fireEvent.mouseDown(screen.getByText("elsewhere"));
  expect(screen.queryByRole("dialog")).toBeNull();
});

// --- focus moving inside the popover keeps it open; leaving the wrap closes it (keyboard users
// must be able to reach the file buttons, not just mouse users) ---

test("focus moving between the popover's own controls keeps it open; leaving the wrap closes it", async () => {
  const api = codeApi({ changes: vi.fn().mockResolvedValue(ready(["src/a.ts", "src/b.ts"])) });
  render(
    <div>
      <GitChip code={{ api, workspaceId: "w1", name: "Alpha", onOpen: vi.fn() }} />
      <button type="button">elsewhere</button>
    </div>,
  );
  await settle();
  fireEvent.click(screen.getByRole("button", { name: "main · 2 changed · abcdef1" }));
  const dialog = screen.getByRole("dialog", { name: "Git" });
  const [first, second] = within(dialog).getAllByRole("button", { name: /\.ts/ });
  if (!first || !second) throw new Error("expected two file buttons");

  act(() => first.focus());
  expect(screen.queryByRole("dialog", { name: "Git" })).not.toBeNull();

  fireEvent.keyDown(first, { key: "Tab" });
  expect(screen.queryByRole("dialog", { name: "Git" })).not.toBeNull();

  act(() => second.focus());
  expect(screen.queryByRole("dialog", { name: "Git" })).not.toBeNull();

  act(() => screen.getByText("elsewhere").focus());
  expect(screen.queryByRole("dialog", { name: "Git" })).toBeNull();
});

// --- code label ---

test("the code chip shows checking, then the branch, change count and short head", async () => {
  const pending = (() => {
    let resolve!: (value: CodeChanges) => void;
    const promise = new Promise<CodeChanges>((res) => {
      resolve = res;
    });
    return { promise, resolve };
  })();
  const api = codeApi({ changes: vi.fn().mockReturnValue(pending.promise) });
  render(<GitChip code={{ api, workspaceId: "w1", name: "Alpha", onOpen: vi.fn() }} />);
  screen.getByRole("button", { name: "Alpha · checking" });

  await act(async () => {
    pending.resolve({
      state: "ready",
      branch: "main",
      head: "abcdef123456",
      detached: false,
      upstream: null,
      ahead: null,
      behind: null,
      truncated: true,
      entries: [entry("a.ts"), entry("b.ts")],
    });
  });
  screen.getByRole("button", { name: "main · 2+ changed · abcdef1" });
});

test("a detached, clean code folder reads 'detached HEAD · clean', with no head segment", async () => {
  const api = codeApi({
    changes: vi.fn().mockResolvedValue({
      state: "ready",
      branch: null,
      head: null,
      detached: true,
      upstream: null,
      ahead: null,
      behind: null,
      truncated: false,
      entries: [],
    } satisfies CodeChanges),
  });
  render(<GitChip code={{ api, workspaceId: "w1", name: "Alpha", onOpen: vi.fn() }} />);
  await settle();
  screen.getByRole("button", { name: "detached HEAD · clean" });
});

test("an error code shows status unknown on the chip, and the matching errorWord text in the popover", async () => {
  const api = codeApi({ changes: vi.fn().mockRejectedValue(new CodeApiError("trust_required")) });
  render(<GitChip code={{ api, workspaceId: "w1", name: "Alpha", onOpen: vi.fn() }} />);
  await settle();
  const button = screen.getByRole("button", { name: "Alpha · status unknown" });
  fireEvent.click(button);
  screen.getByText("Trust this folder in Settings → Workspaces to see its Git changes.");
});

test("a ready but unknown git state shows status unknown and a generic unavailable message", async () => {
  const api = codeApi({
    changes: vi.fn().mockResolvedValue({
      state: "unknown",
      branch: null,
      head: null,
      detached: false,
      upstream: null,
      ahead: null,
      behind: null,
      truncated: false,
      entries: [],
    } satisfies CodeChanges),
  });
  render(<GitChip code={{ api, workspaceId: "w1", name: "Alpha", onOpen: vi.fn() }} />);
  await settle();
  const button = screen.getByRole("button", { name: "Alpha · status unknown" });
  fireEvent.click(button);
  screen.getByText("Git is not available.");
});

// --- the entries list ---

test("the popover lists only the first 10 entries, with a count for the rest", async () => {
  const entries = Array.from({ length: 12 }, (_, i) => entry(`file${i}.ts`));
  const api = codeApi({
    changes: vi.fn().mockResolvedValue({
      state: "ready",
      branch: "main",
      head: null,
      detached: false,
      upstream: null,
      ahead: null,
      behind: null,
      truncated: false,
      entries,
    } satisfies CodeChanges),
  });
  render(<GitChip code={{ api, workspaceId: "w1", name: "Alpha", onOpen: vi.fn() }} />);
  await settle();
  fireEvent.click(screen.getByRole("button", { name: "main · 12 changed" }));
  const dialog = screen.getByRole("dialog", { name: "Git" });
  expect(within(dialog).getAllByRole("listitem")).toHaveLength(11);
  within(dialog).getByText("and 2 more");
});

test("a private entry renders as text, never a button", async () => {
  const api = codeApi({
    changes: vi.fn().mockResolvedValue({
      state: "ready",
      branch: "main",
      head: null,
      detached: false,
      upstream: null,
      ahead: null,
      behind: null,
      truncated: false,
      entries: [entry("secret.ts", { private: true })],
    } satisfies CodeChanges),
  });
  render(<GitChip code={{ api, workspaceId: "w1", name: "Alpha", onOpen: vi.fn() }} />);
  await settle();
  fireEvent.click(screen.getByRole("button", { name: "main · 1 changed" }));
  const dialog = screen.getByRole("dialog", { name: "Git" });
  expect(within(dialog).queryByRole("button")).toBeNull();
  expect(
    within(dialog)
      .getByText(/secret\.ts/)
      .closest("li")?.textContent,
  ).toContain("Private");
});

test("a file click closes the popover and calls onOpen with its path", async () => {
  const onOpen = vi.fn();
  const api = codeApi({
    changes: vi.fn().mockResolvedValue({
      state: "ready",
      branch: "main",
      head: null,
      detached: false,
      upstream: null,
      ahead: null,
      behind: null,
      truncated: false,
      entries: [entry("src/a.ts")],
    } satisfies CodeChanges),
  });
  render(<GitChip code={{ api, workspaceId: "w1", name: "Alpha", onOpen }} />);
  await settle();
  fireEvent.click(screen.getByRole("button", { name: "main · 1 changed" }));
  fireEvent.click(screen.getByRole("button", { name: /a\.ts/ }));
  expect(onOpen).toHaveBeenCalledExactlyOnceWith("src/a.ts");
  expect(screen.queryByRole("dialog")).toBeNull();
});

// --- polling: re-armed only once the previous read settles, and unmount aborts it ---

test("the code read re-arms every 60s once it settles, and a new refreshKey forces another", async () => {
  vi.useFakeTimers();
  try {
    const changes = vi.fn().mockResolvedValue({
      state: "ready",
      branch: "main",
      head: null,
      detached: false,
      upstream: null,
      ahead: null,
      behind: null,
      truncated: false,
      entries: [],
    } satisfies CodeChanges);
    const api = codeApi({ changes });
    const { rerender } = render(
      <GitChip code={{ api, workspaceId: "w1", name: "Alpha", onOpen: vi.fn() }} refreshKey="1" />,
    );
    await act(async () => {});
    expect(changes).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(59_999);
    });
    expect(changes).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(changes).toHaveBeenCalledTimes(2);

    rerender(
      <GitChip code={{ api, workspaceId: "w1", name: "Alpha", onOpen: vi.fn() }} refreshKey="2" />,
    );
    await act(async () => {});
    expect(changes).toHaveBeenCalledTimes(3);
  } finally {
    vi.useRealTimers();
  }
});

// --- the last answer stays on screen across a refreshKey bump; a new workspaceId never shows it ---

test("a refreshKey bump keeps the last answer on screen; a new workspaceId never shows it", async () => {
  let call = 0;
  const changes = vi.fn(() => {
    call += 1;
    return call === 1 ? Promise.resolve(ready(["src/a.ts"])) : new Promise<CodeChanges>(() => {});
  });
  const code = { api: codeApi({ changes }), workspaceId: "w1", name: "Alpha", onOpen: vi.fn() };
  const { rerender } = render(<GitChip code={code} refreshKey="t1" />);
  await settle();
  fireEvent.click(screen.getByRole("button", { name: "main · 1 changed · abcdef1" }));
  expect(within(screen.getByRole("dialog")).queryAllByRole("button")).toHaveLength(1);

  // Same workspace, a new refreshKey: the re-read is in flight (it never settles here), but the
  // label and the popover's file list must not drop back to "checking" while it is.
  rerender(<GitChip code={{ ...code }} refreshKey="t2" />);
  await settle();
  expect(changes).toHaveBeenCalledTimes(2);
  screen.getByRole("button", { name: "main · 1 changed · abcdef1" });
  expect(within(screen.getByRole("dialog")).queryAllByRole("button")).toHaveLength(1);

  // A different workspace, the same refreshKey: this is a genuinely new target, so it must never
  // show "w1"'s stored answer under "w2"'s name, even before its own read settles.
  rerender(<GitChip code={{ ...code, workspaceId: "w2", name: "Beta" }} refreshKey="t2" />);
  screen.getByRole("button", { name: "Beta · checking" });
});

test("unmount aborts the in-flight read", () => {
  const calls: (AbortSignal | undefined)[] = [];
  const changes = vi.fn((_id: string, signal?: AbortSignal) => {
    calls.push(signal);
    return new Promise<CodeChanges>(() => {});
  });
  const api = codeApi({ changes });
  const { unmount } = render(
    <GitChip code={{ api, workspaceId: "w1", name: "Alpha", onOpen: vi.fn() }} />,
  );
  unmount();
  expect(calls[0]?.aborted).toBe(true);
});

test("a read that settles after unmount never re-arms the poll", async () => {
  vi.useFakeTimers();
  try {
    let release!: (value: CodeChanges) => void;
    const changes = vi.fn(
      () =>
        new Promise<CodeChanges>((resolve) => {
          release = resolve;
        }),
    );
    const api = codeApi({ changes });
    const { unmount } = render(
      <GitChip code={{ api, workspaceId: "w1", name: "Alpha", onOpen: vi.fn() }} />,
    );
    await act(async () => {});
    unmount();
    await act(async () => {
      release(ready([]));
      await vi.advanceTimersByTimeAsync(5 * 60_000);
    });
    expect(changes).toHaveBeenCalledTimes(1);
  } finally {
    vi.useRealTimers();
  }
});

test("a pending re-arm timer is cleared on unmount", async () => {
  vi.useFakeTimers();
  try {
    const changes = vi.fn().mockResolvedValue(ready([]));
    const api = codeApi({ changes });
    const { unmount } = render(
      <GitChip code={{ api, workspaceId: "w1", name: "Alpha", onOpen: vi.fn() }} />,
    );
    await act(async () => {});
    expect(changes).toHaveBeenCalledTimes(1);
    unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5 * 60_000);
    });
    expect(changes).toHaveBeenCalledTimes(1);
  } finally {
    vi.useRealTimers();
  }
});
