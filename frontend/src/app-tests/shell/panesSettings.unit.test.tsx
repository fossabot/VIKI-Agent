/**
 * App-level wiring for `layout.panes`: a stored layout is adopted and its chats opened
 * existing-only, a structural change (Ctrl+\) reaches the settings document exactly once, an
 * ordinary tab switch never does, and a read-only settings document is never written to.
 * `usePanes.unit.test.tsx` covers the hook itself in isolation; this file only pins that App
 * wires it correctly end to end.
 */
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { FakeSocket, fakeRest, sessionRow } from "../../accept/m3b/fakes";
import type { KeySpec, SettingsView } from "../../clients/app/shellApi";
import type { SessionMetadataApi } from "../../clients/conversations/sessionMetadataApi";
import { parseLayout, serializeLayout } from "../../state/paneFormat";
import { groups, moveTab, openTab, singlePane } from "../../state/panes";
import { createViki } from "../../state/viki";
import { fakeDurable } from "../../testDurable";
import { type FakeShellApi, fakeShellApi, settingsFixture } from "../../testShell";

const A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const TITLES: Record<string, string> = { [A]: "Alpha", [B]: "Beta" };

beforeEach(() => {
  window.sessionStorage.clear();
  // Wide enough to dock both rails, so a new pane may open.
  vi.stubGlobal("matchMedia", (query: string) => ({
    media: query,
    matches: 1440 >= Number(/min-width:\s*(\d+)px/.exec(query)?.[1] ?? "0"),
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
});

afterEach(() => {
  vi.useRealTimers();
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

/** `testShell.ts`'s static fixture has no `layout.panes` key, so — exactly as
 * `src/app-tests/shell/layoutPreferences.unit.test.tsx` does for the rail widths — a test that needs it adds it
 * itself: a personal, app-scope string with no allowed-values list (src/viki_agent/app/settings.py's spec). */
function withPanesKey(view: SettingsView, text = ""): SettingsView {
  const spec: KeySpec = {
    scope: "app",
    applies: "now",
    kind: "string",
    allowed: null,
    default: "",
    personal: true,
  };
  return {
    ...view,
    keys: { ...view.keys, "layout.panes": spec },
    values: { ...view.values, "layout.panes": text },
    effective: { ...view.effective, "layout.panes": text },
    sources: { ...view.sources, "layout.panes": text === "" ? "default" : "setting" },
  };
}

function mountApp(shell: FakeShellApi, sessions: readonly string[]): FakeSocket {
  const socket = new FakeSocket(true);
  const viki = createViki({
    url: "ws://127.0.0.1:8765/ws",
    token: "t",
    createSocket: () => socket,
  });
  viki.client.connect();
  const rows = sessions.map((id) => sessionRow({ session_id: id, title: TITLES[id] ?? "" }));
  render(
    <App
      viki={viki}
      rest={fakeRest({ sessions: rows })}
      durable={fakeDurable()}
      organization={organization()}
      shell={shell}
    />,
  );
  act(() => socket.open());
  return socket;
}

async function settled(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 8; i++) await Promise.resolve();
  });
}
/** Advances the fake `setTimeout`/`clearTimeout` clock a test switches to after mounting, so the
 * 1s `layout.panes` debounce fires on command instead of a real sleep (AGENTS.md: inject timers,
 * never sleep). */
async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

const paneSections = () => [...document.querySelectorAll<HTMLElement>("section.conversation.pane")];
const pane = (index: number) => paneSections()[index] as HTMLElement;
const main = () => document.querySelector("main.conversation") as HTMLElement;
const tabIds = (scope: ParentNode = document) =>
  [...scope.querySelectorAll(".session-tab-select")].map((tab) =>
    tab.id.replace("session-tab-", ""),
  );
const opens = (socket: FakeSocket) =>
  socket.sent
    .filter((frame) => frame.type === "session.open")
    .map((frame) => [frame.session_id, frame.existing_only ?? false]);
/** Only the patches that actually touch `layout.panes`, so an unrelated settings write (none
 * expected here, but the point of these tests) never counts as one of ours. */
const layoutPatches = (shell: FakeShellApi) =>
  shell.calls.filter(
    (call) =>
      call.method === "patchSettings" &&
      Object.keys(call.args[2] as Record<string, unknown>).includes("layout.panes"),
  );

/** jsdom lays nothing out: give `element` a box so the Split room checks read it as large. */
function stubRect(element: HTMLElement, width: number, height: number): void {
  element.getBoundingClientRect = () =>
    ({ left: 0, top: 0, right: width, bottom: height, width, height, x: 0, y: 0 }) as DOMRect;
}
/** jsdom never measures the shell frame: stub it so the lone pane's row room comes out to
 * exactly `room`, as `src/app-tests/shell/panesKeys.unit.test.tsx` does for the same reason. */
function stubRoom(room: number): void {
  const frame = document.querySelector(".shell-body") as HTMLElement;
  Object.defineProperty(frame, "clientWidth", { configurable: true, value: room + 272 + 1 });
  Object.defineProperty(frame, "clientHeight", { configurable: true, value: 800 });
  act(() => {
    window.dispatchEvent(new Event("resize"));
  });
}
function press(target: Element, init: KeyboardEventInit): boolean {
  let notPrevented = true;
  act(() => {
    notPrevented = fireEvent.keyDown(target, init);
  });
  return notPrevented;
}
async function openTabs(ids: readonly string[]): Promise<void> {
  for (const id of ids) {
    await act(async () => {
      fireEvent.click(document.querySelector(`.sess[data-session-id="${id}"]`) as Element);
    });
  }
}

test("a stored layout.panes with two groups is adopted, each group's active chat opening existing-only", async () => {
  const twoGroups = moveTab(openTab(singlePane(), A), B, { group: "g1", zone: "right" });
  const shell = fakeShellApi({
    settings: withPanesKey(
      settingsFixture({ "onboarding.completed": true }),
      serializeLayout(twoGroups),
    ),
  });
  const socket = mountApp(shell, [A, B]);
  await settled();

  expect(paneSections()).toHaveLength(2);
  expect(tabIds(pane(0))).toEqual([A]);
  expect(tabIds(pane(1))).toEqual([B]);
  expect(opens(socket)).toEqual(
    expect.arrayContaining([
      [A, true],
      [B, true],
    ]),
  );
}, 20_000);

test("a structural change (Split right via Ctrl+\\) PATCHes exactly { layout.panes } once, about 1s later", async () => {
  const shell = fakeShellApi({
    settings: withPanesKey(settingsFixture({ "onboarding.completed": true })),
  });
  mountApp(shell, [A, B]);
  await settled();
  // Fake timers go on before any layout change, so the tabs' own pending save is a real, faked
  // timer: pressing Split below arrives inside its 1s debounce and supersedes it (the save
  // effect's cleanup cancels it), so only the split's own save — holding both changes — lands.
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  await openTabs([A, B]);
  stubRect(main(), 1000, 800);
  stubRoom(1000);
  expect(layoutPatches(shell)).toHaveLength(0);

  expect(press(document.body, { key: "\\", code: "Backslash", ctrlKey: true })).toBe(false);
  await advance(1000);

  const after = layoutPatches(shell);
  expect(after).toHaveLength(1);
  const changes = after[0]?.args[2] as Record<string, unknown>;
  expect(Object.keys(changes)).toEqual(["layout.panes"]);
  const parsed = parseLayout(changes["layout.panes"]);
  expect(parsed && groups(parsed).length).toBe(2);
});

test("tab switches and pane focus alone PATCH nothing", async () => {
  const shell = fakeShellApi({
    settings: withPanesKey(settingsFixture({ "onboarding.completed": true })),
  });
  mountApp(shell, [A, B]);
  await settled();
  // Fake timers go on before any layout change (see the Split-right test above): opening the
  // tabs and the split below land inside the same 1s debounce, as one combined save.
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  await openTabs([A, B]);
  stubRect(main(), 1000, 800);
  stubRoom(1000);

  // A second pane, so a pure pane-focus change (Ctrl+1/Ctrl+2 below) is actually exercised, not
  // just the single-pane tab switches.
  expect(press(document.body, { key: "\\", code: "Backslash", ctrlKey: true })).toBe(false);
  await advance(1000); // lets the combined (tabs + split) save land
  const before = layoutPatches(shell).length;

  for (const id of [A, B, A]) {
    await act(async () => {
      fireEvent.click(document.getElementById(`session-tab-${id}`) as HTMLElement);
    });
    await advance(1000);
  }
  expect(press(document.body, { key: "1", code: "Digit1", ctrlKey: true })).toBe(false);
  await advance(1000);
  expect(press(document.body, { key: "2", code: "Digit2", ctrlKey: true })).toBe(false);
  await advance(1000);

  expect(layoutPatches(shell).length).toBe(before);
});

test("a read_only settings document never PATCHes", async () => {
  const sv = withPanesKey(settingsFixture({ "onboarding.completed": true }));
  const shell = fakeShellApi({ settings: { ...sv, read_only: true, reason: "newer_version" } });
  mountApp(shell, [A, B]);
  await settled();
  // Fake timers go on before the layout change below, so its debounce is a real, controlled
  // timer — not a real-clock one `advance` can never reach — and actually gets the chance to
  // PATCH if the read-only gate were missing.
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  await openTabs([A, B]); // a structural change, same as any other, still never saved

  await advance(1000);
  expect(layoutPatches(shell)).toEqual([]);
});
