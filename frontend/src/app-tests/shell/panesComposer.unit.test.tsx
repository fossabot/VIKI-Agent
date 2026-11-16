import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { type ComponentProps, useLayoutEffect } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, GENERATION, sessionRow } from "../../accept/m3b/fakes";
import type { CommandReceipt, DurableApi } from "../../clients/conversations/durableApi";
import type { AssetsApi } from "../../clients/media/assetsApi";
import * as MediaComposerModule from "../../components/Composer/MediaComposer";
import * as ConversationModule from "../../components/Conversation/Conversation";
import * as ShellModule from "../../components/Shell/Shell";
import * as EditorModule from "../../state/editors";
import type { VaultExcerptPart } from "../../state/excerptApi";
import { serializeLayout } from "../../state/paneFormat";
import { clearActive, focusGroup, moveTab, openTab, singlePane } from "../../state/panes";
import { PANES_STORAGE_KEY } from "../../state/usePanes";
import type { VaultApi } from "../../state/vaultApi";
import { fakeDurable } from "../../testDurable";
import {
  FAKE_WORKSPACE_ID,
  fakeShellApi,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../testShell";
import type { UploadedAttachment } from "../../turnInput";

const A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const C = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const D = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
const LEFT_GENERATION = "77777777-7777-4777-8777-777777777777";
const TITLES = ["Alpha", "Beta", "Gamma", "Delta"];
const EXCERPT: VaultExcerptPart = {
  kind: "vault_excerpt",
  vault_id: FAKE_WORKSPACE_ID,
  ref: "notes/example.md",
  start_line: 1,
  end_line: 1,
  sha256: "e".repeat(64),
};

// Synthetic handoff at the existing VaultBrowser boundary; no vault reads are needed here.
vi.mock("../../components/VaultBrowser/VaultBrowser", () => ({
  VaultBrowser: ({ onAttachExcerpt }: { onAttachExcerpt?: (part: VaultExcerptPart) => void }) => (
    <button type="button" onClick={() => onAttachExcerpt?.(EXCERPT)}>
      Attach test excerpt
    </button>
  ),
}));

beforeEach(() => {
  sessionStorage.clear();
  vi.stubGlobal("matchMedia", (media: string) => ({
    media,
    matches: media.includes("min-width"),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }));
  vi.stubGlobal(
    "URL",
    class extends URL {
      static override createObjectURL() {
        return "blob:synthetic";
      }
      static override revokeObjectURL() {}
    },
  );
});
afterEach(() => {
  cleanup();
  sessionStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

const settled = () =>
  act(async () => {
    for (let i = 0; i < 12; i++) await Promise.resolve();
  });
const pane = (index: number) =>
  document.querySelectorAll<HTMLElement>("section.conversation.pane")[index] as HTMLElement;
const box = (index: number) => within(pane(index)).getByLabelText("Message") as HTMLTextAreaElement;
const activate = async (node: Element) => {
  fireEvent.pointerDown(node);
  await settled();
};

function twoPanes() {
  return moveTab(openTab(singlePane(), A), B, { group: "g1", zone: "right" });
}

async function mount(props: Partial<ComponentProps<typeof App>> = {}, count = 2) {
  let layout = twoPanes();
  if (count === 4) {
    layout = moveTab(layout, C, { group: "g2", zone: "right" });
    layout = moveTab(layout, D, { group: "g3", zone: "right" });
  }
  if (!sessionStorage.getItem(PANES_STORAGE_KEY))
    sessionStorage.setItem(PANES_STORAGE_KEY, serializeLayout(layout));
  const { viki, socket } = fakeViki();
  const durable = props.durable ?? fakeDurable();
  render(
    <App
      viki={viki}
      rest={fakeRest({
        sessions: [A, B, C, D].map((id, i) =>
          sessionRow({ session_id: id, title: TITLES[i] ?? "" }),
        ),
      })}
      durable={durable}
      organization={{ list: async () => [], patch: vi.fn(), remove: async () => true }}
      {...props}
    />,
  );
  act(() => socket.open());
  await settled();
  return { viki, socket, durable };
}

test("four visible chats have real editors before activation and keep their original nodes and selections", async () => {
  const durable = fakeDurable();
  for (const id of [A, B, C, D])
    durable.drafts.set(id, { key: id, revision: 1, text: `draft ${id[0]}`, saved_at: null });
  const { socket } = await mount({ durable }, 4);
  const originals = [0, 1, 2, 3].map(box);
  expect(screen.getAllByLabelText("Message")).toHaveLength(4);
  expect(document.querySelectorAll(".composer-wrap")).toHaveLength(4);
  expect(originals.map((node) => node.value)).toEqual(["draft a", "draft b", "draft c", "draft d"]);
  await activate(originals[0] as Element);
  fireEvent.focus(originals[0] as Element);
  fireEvent.change(originals[0] as Element, {
    target: { value: "edited alpha", selectionStart: 2, selectionEnd: 5 },
  });
  await activate(originals[1] as Element);
  await activate(originals[0] as Element);
  expect([0, 1, 2, 3].map(box)).toEqual(originals);
  expect(box(0).value).toBe("edited alpha");
  expect(box(0).selectionStart).toBe(2);
  expect(box(0).selectionEnd).toBe(5);
  expect(box(1).value).toBe("draft b");
  expect(socket.sent.filter((frame) => frame.type === "session.open")).toHaveLength(4);
});

test("the inactive pane's original Send and Enter submit only that editor and generation", async () => {
  const durable = fakeDurable();
  durable.drafts.set(A, { key: A, revision: 1, text: "send Alpha", saved_at: null });
  durable.drafts.set(B, { key: B, revision: 1, text: "keep Beta", saved_at: null });
  const { socket } = await mount({ durable });
  act(() => socket.synchronize(A, LEFT_GENERATION));
  const left = box(0);
  const right = box(1);
  const send = within(pane(0)).getByRole("button", { name: "Send" });
  await activate(send);
  expect(within(pane(0)).getByRole("button", { name: "Send" })).toBe(send);
  await act(async () => {
    fireEvent.click(send);
  });
  expect(durable.submissions).toEqual([
    expect.objectContaining({ session_id: A, generation: LEFT_GENERATION }),
  ]);
  expect(box(0)).toBe(left);
  expect(right.value).toBe("keep Beta");
  await activate(right);
  await act(async () => {
    fireEvent.keyDown(right, { key: "Enter" });
  });
  expect(durable.submissions[1]).toMatchObject({ session_id: B, generation: GENERATION });
});

test.each(["focused", "background"] as const)(
  "an archiving %s pane refuses its original Send without changing either draft or selection",
  async (where) => {
    const patch = vi.fn();
    const durable = fakeDurable();
    const { viki, socket } = await mount({
      durable,
      organization: {
        list: async () =>
          [A, B].map((session_id) => ({
            session_id,
            revision: 1,
            title: "",
            pinned: false,
            archived: false,
          })),
        patch,
        remove: async () => true,
      },
    });
    if (where === "focused") await activate(box(0));
    const alpha = box(0);
    const beta = box(1);
    fireEvent.change(alpha, { target: { value: "keep Alpha draft" } });
    fireEvent.change(beta, { target: { value: "keep Beta draft" } });
    const send = within(pane(0)).getByRole("button", { name: "Send" });
    const selected = sessionStorage.getItem("viki.selected");
    const focused = document.querySelector(".pane[data-focused]")?.getAttribute("data-pane");
    const prepare = vi.spyOn(viki.actions, "prepareTarget");
    const start = vi.spyOn(viki.actions, "newSession");
    const acknowledge = socket.acknowledge.bind(socket);
    let closeFrame: Record<string, unknown> | undefined;
    vi.spyOn(socket, "acknowledge").mockImplementation((frame, status) => {
      if (frame.type === "session.close") closeFrame = frame;
      else acknowledge(frame, status);
    });
    await act(async () =>
      fireEvent.click(
        document.querySelector(`.sess[data-session-id="${A}"] .sess-archive`) as Element,
      ),
    );
    expect(closeFrame).toBeDefined();
    const before = socket.sent.length;
    try {
      fireEvent.pointerDown(send, { pointerId: 1, button: 0 });
      fireEvent.pointerUp(send, { pointerId: 1, button: 0 });
      await act(async () => fireEvent.click(send, { detail: 1 }));
      expect(within(pane(0)).getByRole("button", { name: "Send" })).toBe(send);
      expect(box(0)).toBe(alpha);
      expect(prepare).not.toHaveBeenCalled();
      expect(start).not.toHaveBeenCalled();
      expect(durable.submissions).toEqual([]);
      expect([alpha.value, beta.value]).toEqual(["keep Alpha draft", "keep Beta draft"]);
      expect(within(pane(0)).getByRole("alert").textContent).toContain("being archived");
      expect(within(pane(1)).queryByRole("alert")).toBeNull();
      expect(sessionStorage.getItem("viki.selected")).toBe(selected);
      expect(document.querySelector(".pane[data-focused]")?.getAttribute("data-pane")).toBe(
        focused,
      );
      expect(socket.sent.slice(before).filter((frame) => frame.type === "session.open")).toEqual(
        [],
      );
      expect(patch).not.toHaveBeenCalled();
    } finally {
      await act(async () => acknowledge(closeFrame as Record<string, unknown>, "rejected"));
    }
  },
);

test("pane actions and typed sends replace or clear their own refusals while preserving another pane's note", async () => {
  const { viki } = await mount();
  const wrapUp = () => {
    fireEvent.click(screen.getByRole("button", { name: "Session actions" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Wrap up (log + commit)" }));
  };
  fireEvent.change(box(1), { target: { value: "Beta draft" } });
  wrapUp();
  expect(within(pane(1)).getByRole("alert").textContent).toContain("draft in progress");
  await act(async () => fireEvent.click(within(pane(1)).getByRole("button", { name: "Send" })));
  expect(within(pane(1)).queryByRole("alert")).toBeNull();
  let finish: (target: null) => void = () => {};
  const prepare = vi.spyOn(viki.actions, "prepareTarget").mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  await activate(box(0));
  fireEvent.change(box(0), { target: { value: "Alpha draft" } });
  fireEvent.click(within(pane(0)).getByRole("button", { name: "Send" }));
  await settled();
  await activate(box(1));
  await act(async () => finish(null));
  expect(within(pane(0)).getByRole("alert").textContent).toContain("confirm the destination");
  prepare.mockResolvedValueOnce(null);
  fireEvent.change(box(1), { target: { value: "another Beta draft" } });
  await act(async () => fireEvent.click(within(pane(1)).getByRole("button", { name: "Send" })));
  expect(within(pane(1)).getByRole("alert").textContent).toContain("confirm the destination");
  wrapUp();
  expect(within(pane(1)).getByRole("alert").textContent).toContain("draft in progress");
  expect(within(pane(0)).getByRole("alert").textContent).toContain("confirm the destination");
});

test("each pane shows its own idle, active, waiting or error activity", async () => {
  const { socket } = await mount({}, 4);
  act(() => {
    socket.receive({
      v: 2,
      generation: GENERATION,
      request_id: "",
      session_id: A,
      seq: 1,
      type: "turn.started",
      data: { turn_id: "alpha-turn" },
    });
    socket.receive({
      v: 2,
      generation: GENERATION,
      request_id: "",
      session_id: B,
      seq: 1,
      type: "permission.requested",
      data: {
        tool_use_id: "beta-tool",
        kind: "tool",
        tool: "Read",
        summary: "example",
        question: "",
        reason: "",
        agent: "",
        plan: "",
      },
    });
    socket.receive({
      v: 2,
      generation: GENERATION,
      request_id: "",
      session_id: C,
      seq: 1,
      type: "error",
      data: { code: "provider_error", message: "Synthetic failure" },
    });
  });
  expect(
    [0, 1, 2, 3].map((i) => pane(i).querySelector(".composer-wrap")?.getAttribute("data-activity")),
  ).toEqual(["active", "waiting", "error", "idle"]);
});

test("a running pane denied an editor keeps its original header Stop", async () => {
  const create = EditorModule.createEditorStore;
  vi.spyOn(EditorModule, "createEditorStore").mockImplementation((...args) => {
    const editors = create(...args);
    return { ...editors, capacityAvailable: (key) => key !== A && editors.capacityAvailable(key) };
  });
  const { socket } = await mount();
  act(() =>
    socket.receive({
      v: 2,
      generation: GENERATION,
      request_id: "",
      session_id: A,
      seq: 1,
      type: "turn.started",
      data: { turn_id: "alpha-turn" },
    }),
  );
  expect(within(pane(0)).queryByLabelText("Message")).toBeNull();
  expect(within(pane(0)).getByRole("alert")).toBeTruthy();
  const stop = within(pane(0)).getByRole("button", { name: "Stop" });
  await activate(stop);
  expect(within(pane(0)).getByRole("button", { name: "Stop" })).toBe(stop);
  fireEvent.click(stop);
  expect(socket.sent.filter((frame) => frame.type === "turn.interrupt")).toEqual([
    expect.objectContaining({ session_id: A }),
  ]);
});

test("the inactive model trigger stays mounted through its first click and picks only its model", async () => {
  const { socket } = await mount();
  const models = [
    { value: "sonnet", label: "Sonnet", description: "Fast" },
    { value: "opus", label: "Opus", description: "Deep" },
  ];
  act(() => {
    for (const [id, model] of [
      [A, "sonnet"],
      [B, "opus"],
    ])
      socket.receive({
        v: 2,
        generation: GENERATION,
        request_id: "",
        session_id: id,
        seq: 1,
        type: "session.started",
        data: {
          model,
          effort: "low",
          mode: "auto",
          models,
          commands: [],
          agents: [],
          cli_version: "2.1.259",
          resumed: true,
        },
      });
  });
  const chip = pane(0).querySelector(".model-chip") as HTMLElement;
  expect(chip.textContent).toContain("Sonnet");
  expect(pane(1).querySelector(".model-chip")?.textContent).toContain("Opus");
  await activate(chip);
  expect(pane(0).querySelector(".model-chip")).toBe(chip);
  fireEvent.click(chip);
  fireEvent.click(within(screen.getByRole("menu")).getByText("Opus"));
  await settled();
  expect(socket.sent.filter((frame) => frame.type === "session.set_model")).toEqual([
    expect.objectContaining({ session_id: A, generation: GENERATION, model: "opus" }),
  ]);
});

test("quote actions focus the selected pane's box instead of the first textarea", async () => {
  vi.spyOn(ConversationModule, "Conversation").mockImplementation(({ onQuoteTurn }) =>
    onQuoteTurn ? (
      <button
        type="button"
        onClick={() =>
          onQuoteTurn({
            id: "synthetic-turn",
            user: { text: "quote Beta", command: "", at: "2026-10-23T00:00:00.000Z" },
            blocks: [],
            status: "success",
            usage: null,
            durationMs: 0,
            history: false,
          })
        }
      >
        Quote test turn
      </button>
    ) : (
      <div />
    ),
  );
  await mount({
    archives: {
      list: vi.fn(async () => []),
      get: vi.fn(),
      save: vi.fn(),
      remove: vi.fn(),
    },
  });
  const leftFocus = vi.spyOn(box(0), "focus");
  const rightFocus = vi.spyOn(box(1), "focus");
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Quote test turn" }));
  });
  expect(box(1).value).toContain("quote Beta");
  expect(rightFocus).toHaveBeenCalled();
  expect(leftFocus).not.toHaveBeenCalled();
});

test("an independent fork draft stays in its source pane while another pane edits", async () => {
  const { socket } = await mount();
  act(() =>
    socket.receive({
      v: 2,
      generation: GENERATION,
      request_id: "",
      session_id: A,
      seq: 1,
      type: "session.blocked",
      data: {
        reason: "live_elsewhere",
        holder: { session_id: A, entrypoint: "cli", pid: 123, title: "Alpha" },
      },
    }),
  );
  await act(async () => {
    fireEvent.click(within(pane(0)).getByRole("button", { name: "Continue in a fork" }));
  });
  const forkBox = box(0);
  fireEvent.change(forkBox, { target: { value: "fork Alpha" } });
  const betaBox = box(1);
  await activate(betaBox);
  fireEvent.change(betaBox, { target: { value: "keep Beta" } });
  expect(box(0)).toBe(forkBox);
  expect(forkBox.value).toBe("fork Alpha");
  expect(betaBox.value).toBe("keep Beta");
  await activate(forkBox);
  expect(forkBox.value).toBe("fork Alpha");
  expect(betaBox.value).toBe("keep Beta");
});

test("the original inactive attachment picker works on its first click and a late upload stays in its editor", async () => {
  let finish: (value: UploadedAttachment) => void = () => {};
  const assets: AssetsApi = {
    upload: vi.fn(
      () =>
        new Promise<UploadedAttachment>((resolve) => {
          finish = resolve;
        }),
    ),
    read: async () => new Blob(["image"], { type: "image/png" }),
  };
  await mount({ assets });
  const picker = within(pane(0)).getByRole("button", { name: "Add attachment" });
  const input = pane(0).querySelector<HTMLInputElement>('input[type="file"]') as HTMLInputElement;
  const openPicker = vi.spyOn(input, "click");
  await activate(picker);
  expect(within(pane(0)).getByRole("button", { name: "Add attachment" })).toBe(picker);
  fireEvent.click(picker);
  expect(openPicker).toHaveBeenCalledTimes(1);
  fireEvent.change(input, {
    target: { files: [new File(["image"], "alpha.png", { type: "image/png" })] },
  });
  await settled();
  expect(assets.upload).toHaveBeenCalledTimes(1);
  await activate(box(1));
  await act(async () => {
    finish({
      asset_id: C,
      media_type: "image/png",
      byte_size: 5,
      width: 2,
      height: 2,
      name: "alpha.png",
    });
  });
  await settled();
  expect(within(pane(0)).getByText("alpha.png")).toBeTruthy();
  expect(within(pane(1)).queryByText("alpha.png")).toBeNull();
  expect(within(pane(0)).getByRole("button", { name: "Add attachment" })).toBe(picker);
});

test("two empty panes keep distinct drafts and rendering them starts no provider session", async () => {
  const layout = clearActive(focusGroup(clearActive(twoPanes()), "g1"));
  sessionStorage.setItem(PANES_STORAGE_KEY, serializeLayout(layout));
  const durable = fakeDurable();
  const { socket } = await mount({ durable });
  const left = box(0);
  const right = box(1);
  expect(socket.sent.filter((frame) => frame.type === "session.open")).toHaveLength(0);
  await activate(right);
  fireEvent.change(right, { target: { value: "new Beta" } });
  await activate(left);
  fireEvent.change(left, { target: { value: "new Alpha" } });
  expect(left.value).toBe("new Alpha");
  expect(right.value).toBe("new Beta");
  expect(socket.sent.filter((frame) => frame.type === "session.open")).toHaveLength(0);
  await act(async () => {
    fireEvent.keyDown(left, { key: "Enter" });
  });
  await activate(right);
  await act(async () => {
    fireEvent.keyDown(right, { key: "Enter" });
  });
  expect(durable.submissions).toHaveLength(2);
  expect(new Set(durable.submissions.map((command) => command.session_id)).size).toBe(2);
  expect(
    durable.submissions.every((command) => command.session_id !== A && command.session_id !== B),
  ).toBe(true);
});

test("excerpts and a late refusal stay with their original editor after another pane sends", async () => {
  const service = fakeDurable();
  let refused = false;
  const saves: { key: string; parts: readonly VaultExcerptPart[] }[] = [];
  const refuse = (command: CommandReceipt): CommandReceipt =>
    refused ? { ...command, state: "not_dispatched", reason: "excerpt_stale" } : command;
  const durable: DurableApi = {
    ...service,
    saveDraft: async (key, revision, text, attachments, parts) => {
      saves.push({ key, parts: parts ?? [] });
      return service.saveDraft(key, revision, text, attachments, parts);
    },
    listCommands: async (options) => {
      const page = await service.listCommands(options);
      return { ...page, commands: page.commands.map(refuse) };
    },
  };
  const shell = fakeShellApi({
    settings: settingsFixture({ "onboarding.completed": true }),
    workspaces: {
      ...listingFixture([workspaceFixture()]),
      active_id: FAKE_WORKSPACE_ID,
      chat_only: false,
    },
  });
  const { socket } = await mount({
    durable,
    shell,
    vaultApi: {} as VaultApi,
    excerpts: {
      excerpt: async () => ({
        part: EXCERPT,
        text: "example",
        block: "example",
        block_bytes: 7,
        revision: "r1",
        modified_at: "2026-10-23T00:00:00Z",
      }),
    },
  });
  const left = box(0);
  await activate(left);
  fireEvent.click(screen.getByRole("button", { name: "Vault" }));
  await settled();
  fireEvent.click(screen.getByRole("button", { name: "Attach test excerpt" }));
  fireEvent.click(screen.getByRole("button", { name: "Chat" }));
  await settled();
  expect(within(pane(0)).getByLabelText("Excerpts to send")).toBeTruthy();
  expect(within(pane(1)).queryByLabelText("Excerpts to send")).toBeNull();
  fireEvent.change(box(0), { target: { value: "cite Alpha" } });
  await act(async () => {
    fireEvent.keyDown(box(0), { key: "Enter" });
  });
  await activate(box(1));
  fireEvent.change(box(1), { target: { value: "plain Beta" } });
  await act(async () => {
    fireEvent.keyDown(box(1), { key: "Enter" });
  });
  expect(saves.filter((save) => save.parts.length).map((save) => save.key)).toEqual([A]);
  refused = true;
  vi.useFakeTimers();
  await act(async () => {
    fireEvent.click(screen.getByText("Drafts and send history"));
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
  expect(within(pane(0)).getByLabelText("Excerpts to send")).toBeTruthy();
  expect(within(pane(1)).queryByLabelText("Excerpts to send")).toBeNull();
  expect(socket.sent.filter((frame) => frame.type === "turn.send")).toHaveLength(0);
});

test("64 unresolved excerpt sends preserve the next draft, allow plain sends and release on a settled receipt", async () => {
  // Keep App's composer and submission path; capacity seeding does not need the shell chrome.
  vi.spyOn(ShellModule, "Shell").mockImplementation(({ composer }) => <>{composer}</>);
  const create = EditorModule.createEditorStore;
  const editorListeners = new Set<() => void>();
  let seeding = false;
  vi.spyOn(EditorModule, "createEditorStore").mockImplementation((...args) => {
    const editors = create(...args);
    return {
      ...editors,
      subscribe(listener) {
        editorListeners.add(listener);
        const unsubscribe = editors.subscribe(() => {
          if (!seeding) listener();
        });
        return () => {
          editorListeners.delete(listener);
          unsubscribe();
        };
      },
    };
  });
  let current: MediaComposerModule.MediaComposerProps | undefined;
  vi.spyOn(MediaComposerModule, "MediaComposer").mockImplementation((props) => {
    if (props.editorKey === B) current = props;
    return <div />;
  });
  const service = fakeDurable();
  let settledCommand: string | undefined;
  const durable: DurableApi = {
    ...service,
    saveDraft: async (key, revision, text, attachments, parts) => {
      const saved = {
        ...(await service.saveDraft(key, revision, text)),
        attachments: attachments ?? [],
        parts: parts ?? [],
      };
      service.drafts.set(key, saved);
      return saved;
    },
    listCommands: async (options) => {
      const page = await service.listCommands(options);
      return {
        ...page,
        commands: page.commands.map((command) =>
          command.command_id === settledCommand
            ? { ...command, state: "not_dispatched", reason: "excerpt_stale" }
            : command,
        ),
      };
    },
  };
  async function send(text: string, parts: VaultExcerptPart[]): Promise<boolean> {
    act(() => {
      current?.onExcerptsChange?.(parts);
      current?.onEdit?.(text, text.length, text.length);
    });
    let sent = false;
    await act(async () => {
      sent = (await current?.onSend(text)) ?? false;
    });
    return sent;
  }
  // Supply excerpt capability without opening the vault: the mounted editor's boundary is real.
  const shell = fakeShellApi({
    settings: settingsFixture({ "onboarding.completed": true }),
    workspaces: {
      ...listingFixture([workspaceFixture()]),
      active_id: FAKE_WORKSPACE_ID,
      chat_only: false,
    },
  });
  sessionStorage.setItem(PANES_STORAGE_KEY, serializeLayout(openTab(singlePane(), B)));
  const mounted = await mount({
    durable,
    shell,
    excerpts: {
      excerpt: async () => ({
        part: EXCERPT,
        text: "example",
        block: "example",
        block_bytes: 7,
        revision: "r1",
        modified_at: "2026-10-23T00:00:00Z",
      }),
    },
  });
  expect(editorListeners.size).toBe(1);
  // Batch unrelated status renders while seeding all 64 real submissions, saves and clears.
  seeding = true;
  try {
    for (let i = 0; i < 64; i++) expect(await send(`excerpt ${i}`, [EXCERPT])).toBe(true);
  } finally {
    act(() => {
      seeding = false;
      for (const listener of editorListeners) listener();
    });
  }
  const prepare = vi.spyOn(mounted.viki.actions, "prepareTarget");
  const image = {
    asset_id: C,
    media_type: "image/png" as const,
    byte_size: 5,
    width: 2,
    height: 2,
    name: "kept.png",
    modified_at: null,
    source: "picker" as const,
  };
  act(() => current?.onAttachments([image]));
  expect(await send("keep this draft", [EXCERPT])).toBe(false);
  expect(prepare).not.toHaveBeenCalled();
  expect(current?.value).toBe("keep this draft");
  expect(current?.excerpts).toEqual([EXCERPT]);
  expect(current?.attachments).toEqual([image]);
  expect(screen.getByRole("alert").textContent).toContain("Check send history");
  expect(service.submissions).toHaveLength(64);
  expect(await send("plain message", [])).toBe(true);
  settledCommand = service.submissions
    .slice(0, 64)
    .map((command) => command.command_id)
    .sort()[0];
  vi.useFakeTimers();
  await act(async () => {
    fireEvent.click(screen.getByText("Drafts and send history"));
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
  expect(await send("excerpt after settlement", [EXCERPT])).toBe(true);
  expect(service.submissions).toHaveLength(66);
});

import { DictationContext } from "../../voice/dictationContext";
import type { DictationApi, VoiceSocketHandlers, VoiceStatus } from "../../voice/dictationTypes";
import { createReadAloud } from "../../voice/readAloud";
import * as ReadAloudModule from "../../voice/useReadAloudSettings";

test.each(["single group", "split"] as const)(
  "%s: changing pane focus or remounting the same editor never transfers listening",
  async (mode) => {
    const deliveries: { key: string | undefined; multi: boolean; listening: boolean }[] = [];
    const Original = MediaComposerModule.MediaComposer;
    vi.spyOn(MediaComposerModule, "MediaComposer").mockImplementation((props) => {
      useLayoutEffect(() => {
        deliveries.push({
          key: props.editorKey,
          multi: Boolean(document.querySelector(".conversation-panes")),
          listening: Boolean(props.handsFree?.active && props.handsFree.listening),
        });
      });
      return <Original {...props} />;
    });
    let width = 2000;
    const listeners = new Set<() => void>();
    vi.stubGlobal("matchMedia", (media: string) => ({
      media,
      get matches() {
        return width >= Number(/min-width:\s*(\d+)px/.exec(media)?.[1] ?? "Infinity");
      },
      addEventListener: (_: string, listener: () => void) => listeners.add(listener),
      removeEventListener: (_: string, listener: () => void) => listeners.delete(listener),
    }));
    const resize = (next: number) =>
      act(() => {
        width = next;
        for (const listener of listeners) listener();
      });
    const api: DictationApi = {
      status: vi.fn(
        async (): Promise<VoiceStatus> => ({
          state: "ready",
          running: false,
          model: "small-q8_0",
          backend: "cpu",
          device: null,
          languages: ["auto"],
          language: "auto",
          language_unavailable: false,
          models: ["small-q8_0"],
          backends: ["cpu"],
          fallback: null,
          cpu_model: "small-q8_0",
          slow_on_cpu: false,
        }),
      ),
      clearFallback: vi.fn(),
      socket: vi.fn((_handlers: VoiceSocketHandlers) => ({
        begin: vi.fn(),
        audio: vi.fn(),
        end: vi.fn(),
        cancel: vi.fn(),
        close: vi.fn(),
      })),
      startCapture: vi.fn(async () => ({ deviceId: null, stop: vi.fn(async () => {}) })),
      microphones: vi.fn(async () => []),
      workletUrl: "/worklet.js",
    };
    const controller = createReadAloud({
      synth: {
        paused: false,
        getVoices: () => [
          { name: "Zira", voiceURI: "Zira", lang: "en-US", localService: true, default: false },
        ],
        addEventListener: () => {},
        removeEventListener: () => {},
        speak: () => {},
        cancel: () => {},
        pause: () => {},
        resume: () => {},
      } as unknown as SpeechSynthesis,
      makeUtterance: (text) =>
        ({ text, voice: null, lang: "", rate: 1, onend: null, onerror: null }) as never,
      target: new EventTarget(),
      devices: null,
      setTimeout: () => 0,
      clearTimeout: () => {},
    });
    vi.spyOn(ReadAloudModule, "useReadAloudSettings").mockReturnValue({
      controller,
      rate: 1,
      voices: {},
      noticeSeen: true,
      acknowledge: () => {},
    });
    sessionStorage.setItem(
      PANES_STORAGE_KEY,
      serializeLayout(mode === "split" ? twoPanes() : openTab(singlePane(), A)),
    );
    const { viki, socket } = fakeViki();
    render(
      <DictationContext.Provider value={api}>
        <App
          viki={viki}
          rest={fakeRest({
            sessions: [
              sessionRow({ session_id: A, title: "Alpha" }),
              sessionRow({ session_id: B, title: "Beta" }),
            ],
          })}
          durable={fakeDurable()}
          organization={{ list: async () => [], patch: vi.fn(), remove: async () => true }}
        />
      </DictationContext.Provider>,
    );
    act(() => socket.open());
    await settled();
    if (mode === "single group") {
      const single = screen.getByLabelText("Message") as HTMLTextAreaElement;
      fireEvent.change(single, { target: { value: "single Alpha" } });
      fireEvent.click(document.querySelector(".hands-free-toggle") as Element);
      await settled();
      expect(api.startCapture).toHaveBeenCalledTimes(1);
      expect(single.value).toBe("single Alpha");
      return;
    }
    fireEvent.click(pane(1).querySelector(".hands-free-toggle") as Element);
    await settled();
    expect(api.startCapture).toHaveBeenCalledTimes(1);
    const originalBox = box(0);
    await activate(originalBox);
    expect(box(0)).toBe(originalBox);
    expect(api.startCapture).toHaveBeenCalledTimes(1);
    fireEvent.change(originalBox, { target: { value: "Alpha stays here" } });
    await settled();
    fireEvent.click(pane(0).querySelector(".hands-free-toggle") as Element);
    await settled();
    expect(api.startCapture).toHaveBeenCalledTimes(2);
    const frame = document.querySelector(".shell-body") as HTMLElement;
    Object.defineProperty(frame, "clientWidth", { configurable: true, value: 1100 });
    Object.defineProperty(frame, "clientHeight", { configurable: true, value: 800 });
    const foldAt = deliveries.length;
    fireEvent(window, new Event("resize"));
    await settled();
    expect(document.querySelector(".conversation-panes")).toBeNull();
    const single = screen.getByLabelText("Message") as HTMLTextAreaElement;
    expect(single).not.toBe(originalBox);
    expect(single.value).toBe("Alpha stays here");
    expect(api.startCapture).toHaveBeenCalledTimes(2);
    expect(
      deliveries.slice(foldAt).find((delivery) => delivery.key === A && !delivery.multi),
    ).toEqual({
      key: A,
      multi: false,
      listening: false,
    });
    expect(screen.getByRole("button", { name: "Hands-free" }).getAttribute("aria-pressed")).toBe(
      "false",
    );
    fireEvent.click(document.querySelector(".hands-free-toggle") as Element);
    await settled();
    expect(api.startCapture).toHaveBeenCalledTimes(3);
    Object.defineProperty(frame, "clientWidth", { configurable: true, value: 2000 });
    const unfoldAt = deliveries.length;
    fireEvent(window, new Event("resize"));
    await settled();
    expect(box(0)).not.toBe(single);
    expect(box(0).value).toBe("Alpha stays here");
    expect(api.startCapture).toHaveBeenCalledTimes(3);
    expect(
      deliveries.slice(unfoldAt).find((delivery) => delivery.key === A && delivery.multi),
    ).toEqual({
      key: A,
      multi: true,
      listening: false,
    });
    expect(pane(0).querySelector(".hands-free-toggle")?.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(pane(0).querySelector(".hands-free-toggle") as Element);
    await settled();
    expect(api.startCapture).toHaveBeenCalledTimes(4);
    resize(1000);
    await settled();
    expect(document.querySelector(".conversation-panes")).toBeNull();
    const narrow = screen.getByLabelText("Message") as HTMLTextAreaElement;
    expect(narrow.value).toBe("Alpha stays here");
    expect(api.startCapture).toHaveBeenCalledTimes(4);
    fireEvent.click(document.querySelector(".hands-free-toggle") as Element);
    await settled();
    expect(api.startCapture).toHaveBeenCalledTimes(5);
    resize(2000);
    await settled();
    expect(box(0).value).toBe("Alpha stays here");
    expect(api.startCapture).toHaveBeenCalledTimes(5);
    fireEvent.click(pane(0).querySelector(".hands-free-toggle") as Element);
    await settled();
    expect(api.startCapture).toHaveBeenCalledTimes(6);
  },
);

test.each([
  ["admitted", "physical"],
  ["admitted", "keyboard"],
  ["archiving", "physical"],
  ["archiving", "keyboard"],
] as const)(
  "%s pane mic: its first original %s gesture obeys capture admission",
  async (admission, gesture) => {
    const api: DictationApi = {
      status: vi.fn(
        async (): Promise<VoiceStatus> => ({
          state: "ready",
          running: false,
          model: "small-q8_0",
          backend: "cpu",
          device: null,
          languages: ["auto"],
          language: "auto",
          language_unavailable: false,
          models: ["small-q8_0"],
          backends: ["cpu"],
          fallback: null,
          cpu_model: "small-q8_0",
          slow_on_cpu: false,
        }),
      ),
      clearFallback: vi.fn(),
      socket: vi.fn((_handlers: VoiceSocketHandlers) => ({
        begin: vi.fn(),
        audio: vi.fn(),
        end: vi.fn(),
        cancel: vi.fn(),
        close: vi.fn(),
      })),
      startCapture: vi.fn(async () => ({ deviceId: null, stop: vi.fn(async () => {}) })),
      microphones: vi.fn(async () => []),
      workletUrl: "/worklet.js",
    };
    const controller = createReadAloud({
      synth: {
        paused: false,
        getVoices: () => [
          { name: "Zira", voiceURI: "Zira", lang: "en-US", localService: true, default: false },
        ],
        addEventListener: () => {},
        removeEventListener: () => {},
        speak: () => {},
        cancel: () => {},
        pause: () => {},
        resume: () => {},
      } as unknown as SpeechSynthesis,
      makeUtterance: (text) =>
        ({ text, voice: null, lang: "", rate: 1, onend: null, onerror: null }) as never,
      target: new EventTarget(),
      devices: null,
      setTimeout: () => 0,
      clearTimeout: () => {},
    });
    vi.spyOn(ReadAloudModule, "useReadAloudSettings").mockReturnValue({
      controller,
      rate: 1,
      voices: {},
      noticeSeen: true,
      acknowledge: () => {},
    });
    sessionStorage.setItem(PANES_STORAGE_KEY, serializeLayout(twoPanes()));
    const { viki, socket } = fakeViki();
    render(
      <DictationContext.Provider value={api}>
        <App
          viki={viki}
          rest={fakeRest({
            sessions: [
              sessionRow({ session_id: A, title: "Alpha" }),
              sessionRow({ session_id: B, title: "Beta" }),
            ],
          })}
          durable={fakeDurable()}
          organization={{
            list: async () =>
              [A, B].map((session_id) => ({
                session_id,
                revision: 1,
                title: "",
                pinned: false,
                archived: false,
              })),
            patch: vi.fn(),
            remove: async () => true,
          }}
        />
      </DictationContext.Provider>,
    );
    act(() => socket.open());
    await settled();
    const microphone = pane(0).querySelector(".voice-mic") as HTMLElement;
    const selected = sessionStorage.getItem("viki.selected");
    expect(document.getElementById(`session-tab-${B}`)?.getAttribute("aria-selected")).toBe("true");
    const acknowledge = socket.acknowledge.bind(socket);
    let closeFrame: Record<string, unknown> | undefined;
    if (admission === "archiving") {
      vi.spyOn(socket, "acknowledge").mockImplementation((frame, status) => {
        if (frame.type === "session.close") closeFrame = frame;
        else acknowledge(frame, status);
      });
      await act(async () =>
        fireEvent.click(
          document.querySelector(`.sess[data-session-id="${A}"] .sess-archive`) as Element,
        ),
      );
      expect(closeFrame).toBeDefined();
    }
    try {
      if (gesture === "physical") {
        fireEvent.pointerDown(microphone, { pointerId: 1, button: 0 });
        fireEvent.pointerUp(microphone, { pointerId: 1, button: 0 });
        fireEvent.click(microphone, { detail: 1 });
      } else {
        act(() => microphone.focus());
        await settled();
        fireEvent.keyDown(microphone, { key: " " });
        fireEvent.keyUp(microphone, { key: " " });
        fireEvent.click(microphone, { detail: 0 });
      }
      await settled();
      const admitted = admission === "admitted";
      expect(Boolean(pane(0).getAttribute("data-focused"))).toBe(admitted);
      expect(sessionStorage.getItem("viki.selected")).toBe(admitted ? A : selected);
      expect(pane(0).querySelector(".voice-mic")).toBe(microphone);
      expect(api.startCapture).toHaveBeenCalledTimes(admitted ? 1 : 0);
      expect(microphone.getAttribute("aria-pressed")).toBe(String(admitted));
      expect(pane(1).querySelector(".voice-mic")?.getAttribute("aria-pressed")).toBe("false");
    } finally {
      if (closeFrame)
        await act(async () => acknowledge(closeFrame as Record<string, unknown>, "rejected"));
    }
  },
);
