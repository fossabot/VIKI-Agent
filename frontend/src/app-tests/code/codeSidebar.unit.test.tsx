import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki } from "../../accept/m3b/fakes";
import { ShellApiError } from "../../clients/app/shellApi";
import type { CodeApi } from "../../clients/code/codeApi";
import type { CodePreviewProps } from "../../components/Workbench/CodePreview";
import { fakeDurable } from "../../testDurable";
import {
  capabilitiesFixture,
  fakeShellApi,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../testShell";

// Keep the real preview and viewers. Capture its callback so App's own guard can also be
// exercised with a stale callback that FileView's current guard would otherwise reject first.
let previewProps: CodePreviewProps | null = null;
vi.mock("../../components/Workbench/CodePreview", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../components/Workbench/CodePreview")>();
  return {
    ...actual,
    CodePreview: (props: CodePreviewProps) => {
      previewProps = props;
      return <actual.CodePreview {...props} />;
    },
  };
});

const A = "55555555-5555-4555-8555-555555555555";
const B = "66666666-6666-4666-8666-666666666666";
const codeFolder = (id: string, name: string) =>
  workspaceFixture({
    workspace_id: id,
    kind: "code",
    name,
    root: `D:\\Code\\${name}`,
    root_physical_id: `physical-${name}`,
  });
const alpha = codeFolder(A, "Alpha");
const beta = codeFolder(B, "Beta");

beforeEach(() => {
  sessionStorage.clear();
  previewProps = null;
});
afterEach(() => {
  cleanup();
  sessionStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function codeApi(): CodeApi {
  return {
    tree: vi.fn<CodeApi["tree"]>(async (_id, path) => ({
      path,
      entries: [{ name: "a.ts", type: "file", size: 10 }],
      truncated: false,
    })),
    file: vi.fn<CodeApi["file"]>(async (_id, path) => ({
      path,
      size: 10,
      sha256: "a".repeat(64),
      encoding: "utf-8",
      newline: "lf",
      text: "first line\nsecond line",
      reason: null,
    })),
    changes: vi.fn<CodeApi["changes"]>(async () => ({
      state: "ready",
      branch: "main",
      head: "abcdef123456",
      detached: false,
      upstream: null,
      ahead: null,
      behind: null,
      entries: [
        {
          path: "a.ts",
          orig_path: null,
          index: ".",
          worktree: "M",
          kind: "tracked",
          private: false,
        },
      ],
      truncated: false,
    })),
    diff: vi.fn<CodeApi["diff"]>(async (_id, path) => ({
      path,
      text: "@@ -1 +1 @@\n+changed line",
      reason: null,
    })),
    search: vi.fn(),
    save: vi.fn(),
  };
}

async function settled() {
  await act(async () => {
    for (let i = 0; i < 5; i++) await Promise.resolve();
  });
}
async function click(element: HTMLElement) {
  await act(async () => {
    element.focus();
    fireEvent.click(element);
  });
}
const railTabs = () => screen.getByRole("tablist", { name: "Right sidebar" });
const railTab = (name: string) => within(railTabs()).getByRole("tab", { name });
const composer = () => {
  const box = document.querySelector<HTMLTextAreaElement>('textarea[aria-label="Message"]');
  if (!box) throw new Error("missing retained composer");
  return box;
};
const codeContent = () => screen.getByRole("region", { name: "Code content" });
function observedPreview() {
  if (!previewProps) throw new Error("preview was not opened");
  return previewProps;
}

async function mount(
  options: {
    client?: boolean;
    empty?: boolean;
    narrow?: boolean;
    retryListing?: boolean;
    effective?: Record<string, unknown>;
    service?: CodeApi;
  } = {},
) {
  if (options.narrow)
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: false,
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
    }));
  const fake = fakeViki();
  const stored = settingsFixture({ "onboarding.completed": true });
  const shell = fakeShellApi({
    settings: { ...stored, effective: { ...stored.effective, ...options.effective } },
    capabilities: capabilitiesFixture({
      workspace: {
        shared_scratch: false,
        chat_only: false,
        active: {
          id: A,
          kind: "code",
          name: "Alpha",
          policy: "ordinary",
          layout: "unknown",
          source: "created",
          aliased: false,
          root_available: true,
        },
      },
    }),
    workspaces: {
      ...listingFixture(options.empty ? [] : [{ ...alpha }, { ...beta }, workspaceFixture()]),
      active_id: A,
      chat_only: false,
    },
  });
  if (options.retryListing) shell.fail("workspaces", new ShellApiError(503, "unavailable"));
  const code = options.service ?? codeApi();
  const appProps = {
    viki: fake.viki,
    rest: fakeRest(),
    durable: fakeDurable(),
    shell,
    ...(options.client === false ? {} : { codeApi: code }),
  };
  let result!: ReturnType<typeof render>;
  await act(async () => {
    result = render(<App {...appProps} />);
    fake.socket.open();
  });
  await settled();
  return { ...result, ...fake, code, shell, appProps };
}

const fileTexts = (root: ParentNode = document.body) =>
  [...root.querySelectorAll(".code-text")].map((line) => line.textContent);

async function openCodeFile() {
  await click(railTab("Code"));
  await settled();
  await click(screen.getByRole("button", { name: "a.ts" }));
  await settled();
}

test.each([false, true])(
  "removing then restoring the same client cannot renew a code selection, held: %s",
  async (held) => {
    const code = codeApi();
    let reply!: (file: Awaited<ReturnType<CodeApi["file"]>>) => void;
    const pending = new Promise<Awaited<ReturnType<CodeApi["file"]>>>((resolve) => {
      reply = resolve;
    });
    if (held) vi.mocked(code.file).mockReturnValue(pending);
    const { container, rerender, appProps, socket, shell } = await mount({ service: code });
    const box = composer();
    const chat = container.querySelector('[data-region="conversation"]');
    fireEvent.change(box, { target: { value: "keep the same client draft" } });
    await openCodeFile();
    expect(code.file).toHaveBeenCalledTimes(1);
    const signal = vi.mocked(code.file).mock.calls[0]?.[2];
    socket.sent.length = 0;
    const withoutClient = { ...appProps };
    delete withoutClient.codeApi;
    await act(async () => rerender(<App {...withoutClient} />));
    expect(signal?.aborted).toBe(true);
    expect(screen.queryByRole("region", { name: "Code content" })).toBeNull();
    expect(screen.getByRole("textbox", { name: "Message" })).toBe(box);
    await act(async () => rerender(<App {...appProps} />));
    await settled();
    expect(screen.queryByRole("region", { name: "Code content" })).toBeNull();
    expect(code.file).toHaveBeenCalledTimes(1);
    if (held) {
      await act(async () =>
        reply({
          path: "a.ts",
          size: 4,
          sha256: "a".repeat(64),
          encoding: "utf-8",
          newline: "lf",
          text: "held client result",
          reason: null,
        }),
      );
      expect(fileTexts()).not.toContain("held client result");
    }
    expect(container.querySelector('[data-region="conversation"]')).toBe(chat);
    expect(composer()).toBe(box);
    expect(box.value).toBe("keep the same client draft");
    expect(socket.sent).toEqual([]);
    expect(shell.calls.filter((call) => call.method === "selectWorkspace")).toEqual([]);
    expect(code.save).not.toHaveBeenCalled();
    await openCodeFile();
    expect(code.file).toHaveBeenCalledTimes(2);
    expect(codeContent()).toBeTruthy();
  },
);

test("a narrow explorer selection closes its drawer and Close defeats a held file reply", async () => {
  const code = codeApi();
  let answer!: (file: Awaited<ReturnType<CodeApi["file"]>>) => void;
  const held = new Promise<Awaited<ReturnType<CodeApi["file"]>>>((resolve) => {
    answer = resolve;
  });
  vi.mocked(code.file).mockReturnValue(held);
  const { container, socket, shell } = await mount({ narrow: true, service: code });
  const box = composer();
  fireEvent.change(box, { target: { value: "retained narrow draft" } });
  const chat = container.querySelector('[data-region="conversation"]');
  socket.sent.length = 0;
  await click(screen.getByRole("button", { name: "VIKI panel" }));
  await openCodeFile();
  expect(screen.queryByRole("dialog", { name: "VIKI panel" })).toBeNull();
  expect(within(codeContent()).getByText("Loading file…")).toBeTruthy();
  expect(container.querySelector('[data-region="conversation"]')).toBe(chat);
  expect(composer()).toBe(box);
  const signal = vi.mocked(code.file).mock.calls[0]?.[2];
  await click(screen.getByRole("button", { name: "Back to chat" }));
  expect(signal?.aborted).toBe(true);
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "VIKI panel" }));
  await act(async () =>
    answer({
      path: "a.ts",
      size: 4,
      sha256: "a".repeat(64),
      encoding: "utf-8",
      newline: "lf",
      text: "late answer",
      reason: null,
    }),
  );
  expect(screen.queryByRole("region", { name: "Code content" })).toBeNull();
  expect(fileTexts()).not.toContain("late answer");
  expect(screen.getByRole("textbox", { name: "Message" })).toBe(box);
  expect(box.value).toBe("retained narrow draft");
  expect(socket.sent).toEqual([]);
  expect(shell.calls.filter((call) => call.method === "selectWorkspace")).toEqual([]);
  expect(code.save).not.toHaveBeenCalled();
});

test("eligible Escape closes code but composition and modal ownership retain it", async () => {
  await mount();
  await openCodeFile();
  const close = screen.getByRole("button", { name: "Close code preview" });
  close.focus();
  fireEvent.keyDown(close, { key: "Escape", isComposing: true });
  expect(codeContent()).toBeTruthy();
  fireEvent.keyDown(close, { key: "Escape", ctrlKey: true });
  expect(codeContent()).toBeTruthy();
  fireEvent.keyDown(close, { key: "Escape" });
  await settled();
  expect(screen.queryByRole("region", { name: "Code content" })).toBeNull();
  await openCodeFile();
  fireEvent.keyDown(window, { key: "k", ctrlKey: true });
  await settled();
  expect(screen.getByRole("dialog")).toBeTruthy();
  fireEvent.keyDown(window, { key: "Escape" });
  expect(codeContent()).toBeTruthy();
});

test("a new chat clears code selection while right rail tab changes retain it", async () => {
  const { socket } = await mount();
  await openCodeFile();
  await click(railTab("Overview"));
  expect(codeContent()).toBeTruthy();
  await click(railTab("Code"));
  socket.sent.length = 0;
  await click(screen.getByRole("button", { name: "New session" }));
  expect(screen.queryByRole("region", { name: "Code content" })).toBeNull();
  expect(socket.sent.filter((message) => message.type === "session.open")).toHaveLength(1);
  expect(screen.getByRole("textbox", { name: "Message" })).toBeTruthy();
});

test("explicit workbench Back restores its current opener without reopening the center selection", async () => {
  await mount();
  await openCodeFile();
  await click(screen.getByRole("button", { name: "Open workbench" }));
  expect(screen.queryByRole("region", { name: "Code content" })).toBeNull();
  await click(screen.getByRole("button", { name: "Back" }));
  expect(screen.getByRole("textbox", { name: "Message" })).toBeTruthy();
  expect(screen.queryByRole("region", { name: "Code content" })).toBeNull();
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Open workbench" }));
});

test("test_chat_remains_mounted_while_code_rail_opens", async () => {
  const { container, code, socket, shell } = await mount();
  const chat = container.querySelector('[data-region="conversation"]');
  const box = composer();
  fireEvent.change(box, { target: { value: "keep this draft" } });
  expect(code.tree).not.toHaveBeenCalled();
  const overview = container.querySelector(".rail-agents");
  const footer = container.querySelector(".right-rail-summary");
  const persisted = sessionStorage.getItem("viki.view");
  await openCodeFile();
  expect(container.querySelector('[data-region="conversation"]')).toBe(chat);
  expect(chat?.getAttribute("hidden")).toBe("");
  expect(chat?.hasAttribute("inert")).toBe(true);
  expect(screen.queryByRole("textbox", { name: "Message" })).toBeNull();
  expect(container.querySelector('[data-region="center-pane"]')?.contains(codeContent())).toBe(
    true,
  );
  expect(fileTexts(screen.getByRole("region", { name: "Code sidebar" }))).not.toContain(
    "first line",
  );
  expect(composer()).toBe(box);
  expect(box.value).toBe("keep this draft");
  expect(fileTexts(codeContent())).toContain("first line");
  expect(container.querySelector(".rail-agents")).toBe(overview);
  expect(container.querySelector(".right-rail-summary")).toBe(footer);
  await click(railTab("Overview"));
  expect(container.querySelector(".rail-agents")).toBe(overview);
  expect(container.querySelector('[data-region="conversation"]')).toBe(chat);
  expect(sessionStorage.getItem("viki.view")).toBe(persisted);
  await click(railTab("Code"));
  expect(fileTexts(codeContent())).toContain("first line");
  expect(code.tree).toHaveBeenCalledTimes(1);
  expect(shell.calls.filter((call) => call.method === "workspaces")).toHaveLength(1);
  expect(socket.sent.some((message) => message.type === "turn.send")).toBe(false);
  expect(code.save).not.toHaveBeenCalled();
  await click(screen.getByRole("button", { name: "Close code preview" }));
  expect(screen.queryByRole("region", { name: "Code content" })).toBeNull();
  expect(screen.getByRole("textbox", { name: "Message" })).toBe(box);
  expect(chat?.hasAttribute("hidden")).toBe(false);
  expect(box.value).toBe("keep this draft");
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "a.ts" }));
});

test("the existing footer and its direct children stay mounted while Code hides and restores it", async () => {
  const { container } = await mount();
  const summary = container.querySelector<HTMLDivElement>(".right-rail-summary");
  if (!summary) throw new Error("missing rail footer");
  const children = [...summary.children];
  expect(children).toHaveLength(1);
  const skills = summary.querySelector<HTMLElement>(".skills-summary");
  if (!skills) throw new Error("missing Skills summary");
  expect(children[0]).toBe(skills);
  expect(within(skills).getByText("Skills catalog unavailable.")).toBeTruthy();
  expect(summary.hidden).toBe(false);
  await openCodeFile();
  expect(container.querySelector(".right-rail-summary")).toBe(summary);
  expect(summary.hidden).toBe(true);
  expect([...summary.children]).toEqual(children);
  expect(children.every((child) => child.isConnected)).toBe(true);
  await click(railTab("Overview"));
  expect(container.querySelector(".right-rail-summary")).toBe(summary);
  expect(summary.hidden).toBe(false);
  expect([...summary.children]).toEqual(children);
  expect(children.every((child) => child.isConnected)).toBe(true);
});

test("browsing another folder neither changes the active workspace nor quotes into its draft", async () => {
  const { shell, socket } = await mount();
  fireEvent.change(composer(), { target: { value: "original draft" } });
  await click(railTab("Code"));
  fireEvent.change(screen.getByLabelText("Code folder"), { target: { value: B } });
  await settled();
  await click(screen.getByRole("button", { name: "a.ts" }));
  await settled();
  fireEvent.click(within(codeContent()).getByText("1"));
  await click(screen.getByRole("button", { name: "Quote in chat" }));
  expect(composer().value).toBe("original draft");
  expect(shell.data.capabilities.workspace.active?.id).toBe(A);
  expect(shell.calls.filter((call) => call.method === "selectWorkspace")).toHaveLength(0);
  expect(socket.sent.some((message) => message.type === "turn.send")).toBe(false);
  expect(composer().value).not.toContain(beta.root);
});

test("active quotes append only the exact ref and use the existing external editor action", async () => {
  const { socket } = await mount();
  const box = composer();
  fireEvent.change(box, { target: { value: "look here" } });
  await openCodeFile();
  fireEvent.click(within(codeContent()).getByText("1"));
  fireEvent.click(within(codeContent()).getByText("2"), { shiftKey: true });
  await click(screen.getByRole("button", { name: "Open in VS Code" }));
  expect(socket.sent.filter((message) => message.type === "open.code")).toEqual([
    { type: "open.code", workspace_id: A, path: "a.ts", line: 1 },
  ]);
  await click(screen.getByRole("button", { name: "Quote in chat" }));
  expect(composer()).toBe(box);
  expect(box.value).toBe("look here\n`a.ts:1-2`\n");
  expect(box.value).not.toContain("first line");
  expect(box.value).not.toContain(alpha.root);
  expect(screen.queryByRole("region", { name: "Code content" })).toBeNull();
  expect(document.activeElement).toBe(box);
});

test("App rechecks the current workspace even through an older preview callback", async () => {
  const { shell, viki } = await mount();
  fireEvent.change(composer(), { target: { value: "retained draft" } });
  await openCodeFile();
  const oldQuote = observedPreview().onQuote;
  shell.data.capabilities = {
    ...shell.data.capabilities,
    workspace: {
      ...shell.data.capabilities.workspace,
      active: {
        id: B,
        kind: "code",
        name: "Beta",
        policy: "ordinary",
        layout: "unknown",
        source: "created",
        aliased: false,
        root_available: true,
      },
    },
  };
  await act(async () => {
    viki.store.setStatus("reconnecting");
    viki.store.setStatus("open");
  });
  await settled();
  await act(async () => oldQuote({ workspaceId: A, text: "`a.ts:1`" }));
  expect(composer().value).toBe("retained draft");
  expect(screen.getByRole("alert").textContent).toBe(
    "Only files in the active code folder can be quoted",
  );
});

test.each(["refresh", "close", "registration"] as const)(
  "older preview callbacks cannot quote or open the editor after %s",
  async (invalidated) => {
    const { shell, socket, viki } = await mount();
    fireEvent.change(composer(), { target: { value: "keep guarded draft" } });
    await openCodeFile();
    const stale = observedPreview();
    socket.sent.length = 0;
    if (invalidated === "refresh") {
      await click(screen.getByRole("button", { name: "Refresh" }));
      await settled();
      expect(observedPreview().request).not.toBe(stale.request);
    } else if (invalidated === "close") {
      await click(screen.getByRole("button", { name: "Close code preview" }));
    } else {
      const registered = shell.data.workspaces.workspaces.find((row) => row.workspace_id === A);
      if (!registered) throw new Error("missing registered folder");
      registered.root = "D:\\Other\\Alpha";
      await act(async () => {
        viki.store.setStatus("reconnecting");
        viki.store.setStatus("open");
      });
      await settled();
      expect(within(codeContent()).getByRole("alert").textContent).toMatch(/no longer available/);
    }
    await act(async () => {
      stale.onQuote({ workspaceId: A, text: "`a.ts:1`" });
      stale.onOpenInEditor(A, "a.ts", 1);
    });
    expect(composer().value).toBe("keep guarded draft");
    expect(
      socket.sent.filter((message) => message.type === "open.code" || message.type === "turn.send"),
    ).toEqual([]);
  },
);

test.each([false, true])(
  "candidate quotes enforce the 1 MiB UTF-8 boundary, over limit: %s",
  async (overLimit) => {
    await mount();
    const draft = "é".repeat(((1 << 20) - 10) / 2) + (overLimit ? "a" : "");
    fireEvent.change(composer(), { target: { value: draft } });
    await openCodeFile();
    fireEvent.click(within(codeContent()).getByText("1"));
    await click(screen.getByRole("button", { name: "Quote in chat" }));
    expect(composer().value).toBe(overLimit ? draft : `${draft}\n\`a.ts:1\`\n`);
    if (overLimit)
      expect(screen.getByRole("alert").textContent).toBe(
        "Too large to send (up to 1 MiB per message). The text stays in the message box",
      );
    else expect(new TextEncoder().encode(composer().value).byteLength).toBe(1 << 20);
    expect(screen.queryByRole("region", { name: "Code content" }) !== null).toBe(overLimit);
  },
);

test("the Code rail is absent without a client and an empty registration set explains Settings", async () => {
  await mount({ client: false });
  expect(screen.queryByRole("tab", { name: "Code" })).toBeNull();
  cleanup();
  const { code } = await mount({ empty: true });
  await click(railTab("Code"));
  expect(screen.getByText("No code folders yet. Open one in Settings → Workspaces.")).toBeTruthy();
  expect(code.tree).not.toHaveBeenCalled();
  expect(composer()).toBeTruthy();
});

test("opening Code retries an unavailable listing once and reuses the existing settings", async () => {
  const { shell } = await mount({
    retryListing: true,
    effective: { "workbench.code_font_size": 17, "workbench.line_wrap": true },
  });
  await openCodeFile();
  expect(shell.calls.filter((call) => call.method === "workspaces")).toHaveLength(2);
  expect(shell.calls.filter((call) => call.method === "settings")).toHaveLength(1);
  expect(observedPreview().fontSize).toBe(17);
  expect(observedPreview().lineWrap).toBe(true);
  await click(railTab("Overview"));
  await click(railTab("Code"));
  expect(shell.calls.filter((call) => call.method === "workspaces")).toHaveLength(2);
});

test("changes open in center and the explicit full workbench remains reachable", async () => {
  await mount();
  await click(railTab("Code"));
  await click(screen.getByRole("tab", { name: "Changes" }));
  await settled();
  await click(screen.getByRole("button", { name: ".M a.ts" }));
  await settled();
  expect(within(codeContent()).getByText("+changed line")).toBeTruthy();
  expect(composer()).toBeTruthy();
  expect(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).queryByRole(
      "button",
      { name: "Code" },
    ),
  ).toBeNull();
  await click(screen.getByRole("button", { name: "Open workbench" }));
  expect(screen.getByRole("heading", { name: "Code" })).toBeTruthy();
  expect(screen.getAllByRole("tablist", { name: "Right sidebar" })).toHaveLength(1);
});

test("the same Code panel works through the existing drawer and rail tabs support keyboard selection", async () => {
  const { container } = await mount({ narrow: true });
  const chat = container.querySelector('[data-region="conversation"]');
  await click(screen.getByRole("button", { name: "VIKI panel" }));
  const dialog = screen.getByRole("dialog", { name: "VIKI panel" });
  const overview = railTab("Overview");
  overview.focus();
  fireEvent.keyDown(overview, { key: "End" });
  await settled();
  expect(document.activeElement).toBe(railTab("Code"));
  expect(railTab("Code").getAttribute("aria-selected")).toBe("true");
  expect(within(dialog).getByRole("region", { name: "Code sidebar" })).toBeTruthy();
  fireEvent.keyDown(railTab("Code"), { key: "Home" });
  expect(document.activeElement).toBe(overview);
  await click(within(dialog).getByRole("button", { name: "Close" }));
  expect(screen.queryByRole("dialog", { name: "VIKI panel" })).toBeNull();
  expect(container.querySelector('[data-region="conversation"]')).toBe(chat);
});
