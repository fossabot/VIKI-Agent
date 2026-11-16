/**
 * The chat tab's own "New window" action: `tabMenu` offers it beside Split and Move
 * whenever a session-window launch is available, reusing the rail's `openSessionWindow` for the
 * pending state and the POST, and surfacing a failure above the chat since the tab menu has no
 * inline slot for it (unlike the rail's `OrganizedSessionMenu`). It changes no
 * route, token, Origin, CSP or window lifetime, and must leave a handoff window's `layout.panes`
 * untouched, including across a remount that keeps the same storage.
 */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App, type AppProps } from "../../App";
import { FakeSocket, fakeRest, sessionRow } from "../../accept/m3b/fakes";
import type { SessionMetadataApi } from "../../clients/conversations/sessionMetadataApi";
import { SessionWindowsError } from "../../clients/conversations/sessionWindowsApi";
import { createViki, type Viki } from "../../state/viki";
import { fakeDurable } from "../../testDurable";
import { fakeShellApi } from "../../testShell";

const ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const TITLE = "Target";
const clients: Viki[] = [];

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  for (const client of clients.splice(0)) client.client.close();
  sessionStorage.clear();
  localStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function metadata(): SessionMetadataApi {
  return {
    list: vi.fn(async () => [
      {
        session_id: ID,
        revision: 1,
        title: TITLE,
        pinned: false,
        archived: false,
        unread: false,
        color: null,
      },
    ]),
    patch: vi.fn(async (id, revision, patch) => ({
      session_id: id,
      revision: revision + 1,
      title: TITLE,
      pinned: false,
      archived: false,
      unread: false,
      color: null,
      ...patch,
    })),
    remove: vi.fn(),
  };
}

/** Mounts a handoff window already showing `ID` as its one, selected tab: the shortest route to
 * a tab whose "Tab actions" menu exists, and the one the layout.panes regression needs anyway.
 * The new-window item's gate (`sessionWindowsApi && !recovery && !staleWindow`) does not exclude
 * a handoff window, so the same mount serves every test here. */
async function mountTab(props: Partial<AppProps> = {}) {
  const socket = new FakeSocket(false);
  const viki = createViki({
    url: "ws://127.0.0.1:8765/ws",
    token: "synthetic",
    createSocket: () => socket,
  });
  clients.push(viki);
  viki.client.connect();
  let view!: ReturnType<typeof render>;
  await act(async () => {
    view = render(
      <App
        viki={viki}
        rest={fakeRest({ sessions: [sessionRow({ session_id: ID, title: TITLE })] })}
        durable={fakeDurable()}
        organization={metadata()}
        inventoryApi={{ get: vi.fn() }}
        sessionHandoff={{ kind: "target", sessionId: ID }}
        {...props}
      />,
    );
    socket.open();
  });
  await act(async () => {
    socket.synchronize(ID);
  });
  return { ...view, socket, viki };
}

async function click(target: HTMLElement) {
  await act(async () => {
    fireEvent.click(target);
  });
}

function tabActionsButton() {
  return screen.getByRole("button", { name: `Tab actions: ${TITLE}` });
}

async function openTabMenu() {
  await click(tabActionsButton());
  return screen.getByRole("menu");
}

test("the tab's New window posts the session id once and shows the pending label while it opens", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  let resolve!: () => void;
  const open = vi.fn(
    (_id: string, _signal?: AbortSignal) =>
      new Promise<void>((done) => {
        resolve = done;
      }),
  );
  await mountTab({ sessionWindowsApi: { open } });
  await click(within(await openTabMenu()).getByRole("menuitem", { name: "New window" }));
  expect(open).toHaveBeenCalledOnce();
  expect(open.mock.calls[0]?.[0]).toBe(ID);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1);
  });
  await act(async () => {
    fireEvent.keyDown(tabActionsButton(), { key: "ArrowDown" });
  });
  const pending = screen.getByRole("menuitem", { name: "Opening window…" });
  expect(pending.getAttribute("aria-disabled")).toBe("true");
  await act(async () => {
    resolve();
  });
});

test.each([
  ["closed Sessions drawer", 600],
  ["collapsed Sessions rail", 1440],
] as const)("a tab's window refusal stays visible with a %s", async (_mode, width) => {
  vi.stubGlobal("matchMedia", (query: string) => ({
    media: query,
    matches: width >= Number(/min-width:\s*([\d.]+)px/.exec(query)?.[1] ?? 0),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }));
  const open = vi.fn().mockRejectedValueOnce(new SessionWindowsError("window_limit"));
  await mountTab({ sessionWindowsApi: { open } });
  if (width === 1440) {
    await act(async () => {
      fireEvent.keyDown(screen.getByRole("separator", { name: "Resize left rail" }), {
        key: "Enter",
      });
    });
  }
  expect(screen.queryByRole("navigation", { name: "Navigation and sessions" })).toBeNull();
  await click(within(await openTabMenu()).getByRole("menuitem", { name: "New window" }));
  expect(open).toHaveBeenCalledExactlyOnceWith(ID, expect.any(AbortSignal));
  const alert = screen.getByRole("alert");
  expect(alert.textContent).toBe("Too many session windows are open. Close one and try again.");
  expect(alert.closest("[hidden], [inert]")).toBeNull();
  await click(within(alert.parentElement as HTMLElement).getByRole("button", { name: "Dismiss" }));
  expect(screen.queryByRole("alert")).toBeNull();
});

test("a tab retry clears the old refusal while pending and after a successful open", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  let finish!: () => void;
  const open = vi
    .fn()
    .mockRejectedValueOnce(new SessionWindowsError("window_limit"))
    .mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
    );
  await mountTab({ sessionWindowsApi: { open } });
  await click(within(await openTabMenu()).getByRole("menuitem", { name: "New window" }));
  const refusal = "Too many session windows are open. Close one and try again.";
  expect(screen.getAllByText(refusal)).toHaveLength(2);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1);
  });
  await click(within(await openTabMenu()).getByRole("menuitem", { name: "New window" }));
  expect(open).toHaveBeenCalledTimes(2);
  expect(screen.queryByText(refusal)).toBeNull();
  await act(async () => {
    finish();
  });
  expect(screen.queryByText(refusal)).toBeNull();
});

test("a handoff window's New window never patches layout.panes, also after a remount with the same storage", async () => {
  const shell = fakeShellApi();
  const open = vi.fn().mockResolvedValue(undefined);
  for (let i = 0; i < 2; i++) {
    await mountTab({ shell, sessionWindowsApi: { open } });
    await click(within(await openTabMenu()).getByRole("menuitem", { name: "New window" }));
    expect(shell.calls.some((call) => call.method === "patchSettings")).toBe(false);
    cleanup();
  }
});
