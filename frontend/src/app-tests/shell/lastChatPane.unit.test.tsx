/**
 * Closing the only chat tab while a file pane is open must not strand the
 * Message box in a file pane, or lose the chat pane entirely. App already renders an empty
 * focused chat pane correctly (the new-conversation landing plus the Message box); this pins
 * the model-level fix (`state/panes.ts`'s `shape`/`canonicalize` keep one empty chat group alive
 * while a file group survives) through the real App. Pattern: `src/app-tests/shell/filePanes.unit.test.tsx`.
 */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, sessionRow } from "../../accept/m3b/fakes";
import type { CodeApi } from "../../clients/code/codeApi";
import type { SessionMetadataApi } from "../../clients/conversations/sessionMetadataApi";
import type { EditorHandle, EditorOptions } from "../../components/Workbench/codemirror";
import { serializeLayout } from "../../state/paneFormat";
import { openFile, openTab, type PaneLayout, singlePane, splitFile } from "../../state/panes";
import { PANES_STORAGE_KEY } from "../../state/usePanes";
import { fakeDurable } from "../../testDurable";
import {
  capabilitiesFixture,
  fakeShellApi,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../testShell";

// Keep CodePreview real; only the lazy, layout-dependent editor facade is faked, as
// src/app-tests/shell/filePanes.unit.test.tsx does.
vi.mock("../../components/Workbench/codemirror", () => ({
  createEditor: async (options: EditorOptions): Promise<EditorHandle> => {
    const area = document.createElement("textarea");
    area.setAttribute("aria-label", options.label);
    area.value = options.text;
    options.parent.append(area);
    return {
      text: () => area.value,
      setText: (text) => {
        area.value = text;
      },
      setBase: () => {},
      setWrap: () => {},
      setFontSize: () => {},
      focus: () => area.focus(),
      destroy: () => area.remove(),
    };
  },
  codeTokens: async () => [],
}));

const CHAT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const W = "55555555-5555-4555-8555-555555555555";
const alpha = workspaceFixture({
  workspace_id: W,
  kind: "code",
  name: "Alpha",
  root: "D:\\Code\\Alpha",
  root_physical_id: "physical-Alpha",
});

beforeEach(() => {
  window.sessionStorage.clear();
  vi.stubGlobal("matchMedia", (query: string) => ({
    media: query,
    matches: 1440 >= Number(/min-width:\s*(\d+)px/.exec(query)?.[1] ?? "0"),
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.sessionStorage.clear();
});

function organization(): SessionMetadataApi {
  return {
    list: async () => [],
    patch: async () => {
      throw new Error("unused");
    },
    remove: async () => true,
  };
}

function codeApi(): CodeApi {
  return {
    tree: vi.fn<CodeApi["tree"]>(async (_id, path) => ({
      path,
      entries: [{ name: "a.ts", type: "file" as const, size: 10 }],
      truncated: false,
    })),
    file: vi.fn<CodeApi["file"]>(async (_id, path) => ({
      path,
      size: 10,
      sha256: "a".repeat(64),
      encoding: "utf-8",
      newline: "lf",
      text: "first line",
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
      entries: [],
      truncated: false,
    })),
    diff: vi.fn<CodeApi["diff"]>(async (_id, path) => ({ path, text: "", reason: null })),
    search: vi.fn(),
    save: vi.fn(),
  };
}

async function settled(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 8; i++) await Promise.resolve();
  });
}

async function mount(stored: PaneLayout): Promise<void> {
  window.sessionStorage.setItem(PANES_STORAGE_KEY, serializeLayout(stored));
  const fake = fakeViki();
  const shell = fakeShellApi({
    settings: settingsFixture({ "onboarding.completed": true }),
    capabilities: capabilitiesFixture({
      workspace: {
        shared_scratch: false,
        chat_only: false,
        active: {
          id: W,
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
    workspaces: { ...listingFixture([{ ...alpha }]), active_id: W, chat_only: false },
  });
  await act(async () => {
    render(
      <App
        viki={fake.viki}
        rest={fakeRest({ sessions: [sessionRow({ session_id: CHAT, title: "Chat" })] })}
        durable={fakeDurable()}
        organization={organization()}
        shell={shell}
        codeApi={codeApi()}
      />,
    );
    fake.socket.open();
  });
  await settled();
  await settled();
}

async function click(element: Element): Promise<void> {
  await act(async () => {
    fireEvent.click(element);
  });
  await settled();
}

const paneSections = () => [...document.querySelectorAll<HTMLElement>("section.conversation.pane")];
const pane = (index: number) => paneSections()[index] as HTMLElement;

/** The chat alone in one pane. */
const chatAlone = () => openTab(singlePane(), CHAT);
/** The chat, and beside it a file pane holding `ref` of the code folder (file pane focused). */
function chatAndFile(ref = "src/a.ts"): PaneLayout {
  const layout = openFile(chatAlone(), { workspaceId: W, ref }, () => true);
  if (!layout) throw new Error("no file pane");
  return layout;
}
/** The chat, and two file panes beside it: a second view of the same file (as
 * src/app-tests/shell/filePanes.unit.test.tsx's "An Edit in one pane..." test builds its three panes). */
function chatAndTwoFiles(): PaneLayout {
  return splitFile(chatAndFile(), "f1", "bottom");
}

test("closing the chat tab with one file pane open keeps the Message box in the chat pane, now empty", async () => {
  await mount(chatAndFile());
  expect(paneSections()).toHaveLength(2);
  expect(screen.getByLabelText("Message").closest("section.pane")).toBe(pane(0));

  await click(screen.getByRole("button", { name: "Close: Chat" }));

  expect(paneSections()).toHaveLength(2); // the chat pane survives, empty, not pruned
  expect(within(pane(0)).queryAllByRole("tab")).toHaveLength(0);
  expect(within(pane(1)).getAllByRole("tab")).toHaveLength(1); // the file pane, untouched
  expect(screen.getAllByLabelText("Message")).toHaveLength(1); // exactly one Message box anywhere
  expect(screen.getByLabelText("Message").closest("section.pane")).toBe(pane(0)); // never the file pane

  await click(screen.getByRole("button", { name: "New session" }));
  expect(within(pane(0)).getAllByRole("tab")).toHaveLength(1); // the new chat lands back in pane 0
  expect(screen.getByLabelText("Message").closest("section.pane")).toBe(pane(0));
});

test("closing the chat tab with two file panes open keeps the Message box in the chat pane, now empty", async () => {
  await mount(chatAndTwoFiles());
  expect(paneSections()).toHaveLength(3);
  expect(screen.getByLabelText("Message").closest("section.pane")).toBe(pane(0));

  await click(screen.getByRole("button", { name: "Close: Chat" }));

  expect(paneSections()).toHaveLength(3); // the chat pane survives, empty, beside both file panes
  expect(within(pane(0)).queryAllByRole("tab")).toHaveLength(0);
  expect(screen.getAllByLabelText("Message")).toHaveLength(1);
  expect(screen.getByLabelText("Message").closest("section.pane")).toBe(pane(0));

  await click(screen.getByRole("button", { name: "New session" }));
  expect(within(pane(0)).getAllByRole("tab")).toHaveLength(1);
  expect(screen.getByLabelText("Message").closest("section.pane")).toBe(pane(0));
});
