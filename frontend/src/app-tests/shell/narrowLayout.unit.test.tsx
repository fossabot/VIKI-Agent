/**
 * The drawers as the app drives them: App's own six edits, with matchMedia stubbed to a
 * 400px window so both rails are drawers. What a drawer IS — focus, inert, Tab, Escape, backdrop —
 * is pinned in components/Shell/RailDrawer.unit.test.tsx and not repeated here. What is pinned
 * here is that a drawer opens from the top bar and closes on every action that takes the user
 * somewhere else, without ever coexisting with another modal.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, gate, localIso, sessionRow } from "../../accept/m3b/fakes";
import type { Envelope, EventMap, Rails } from "../../protocol";
import { fakeDurable } from "../../testDurable";

afterEach(cleanup);

const day = (h: number, m: number) => localIso(2026, 9, 13, h, m);

/** One matchMedia for the whole App, answering `(min-width: Npx)` for a viewport of `width`,
 * able to announce a change the way Edge does when the window is resized or zoomed. */
function stubViewport(width: number) {
  let current = width;
  const listeners = new Map<string, Set<(event: MediaQueryListEvent) => void>>();
  const matches = (query: string) => {
    const min = /min-width:\s*(\d+)px/.exec(query)?.[1];
    return min !== undefined && current >= Number(min);
  };
  vi.stubGlobal("matchMedia", (query: string) => {
    return {
      media: query,
      get matches(): boolean {
        return matches(query);
      },
      addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => {
        const registered = listeners.get(query) ?? new Set();
        registered.add(listener);
        listeners.set(query, registered);
      },
      removeEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => {
        listeners.get(query)?.delete(listener);
      },
    };
  });
  return {
    resizeTo(next: number): void {
      const previous = new Map([...listeners.keys()].map((query) => [query, matches(query)]));
      current = next;
      act(() => {
        for (const [query, registered] of listeners) {
          if (previous.get(query) === matches(query)) continue;
          const event = Object.assign(new Event("change"), {
            media: query,
            matches: matches(query),
          });
          for (const listener of [...registered]) listener(event);
        }
      });
    },
  };
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(day(20, 0)));
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  window.sessionStorage.clear();
});

const rows = [sessionRow({ session_id: "s-1", title: "retro H1–H4", modified: day(17, 20) })];

function rails(dirty: number): Rails {
  return {
    agents: [],
    gates: [gate({ id: "H1", item: "/recall skill", label: "medium" })],
    routines: [],
    counts: { knowledge: 132, sources: 202, inbox: 1 },
    git: {
      state: "known",
      ref_state: "branch",
      branch: "main",
      head: "c8868e6",
      committed_at: day(19, 16),
      dirty,
    },
  };
}

function frame<K extends keyof EventMap>(
  session: string,
  seq: number,
  type: K,
  data: EventMap[K],
): Envelope {
  return {
    v: 2,
    generation: "11111111-1111-4111-8111-111111111111",
    request_id: "",
    type,
    session_id: session,
    seq,
    data,
  } as Envelope;
}

function mount(dirty = 0) {
  const viewport = stubViewport(400);
  const rest = fakeRest({ sessions: rows, rails: rails(dirty) });
  const { viki, socket } = fakeViki();
  const utils = render(<App viki={viki} rest={rest} durable={fakeDurable()} />);
  act(() => socket.open());
  socket.sent.length = 0;
  return { ...utils, socket, viewport };
}

/** Open a drawer by its top-bar toggle and wait for the panel. */
async function openDrawer(label: string): Promise<HTMLElement> {
  fireEvent.click(await screen.findByRole("button", { name: label }));
  return screen.getByRole("dialog", { name: label });
}

/** The session row, inside whichever drawer is open. */
async function rowInDrawer(dialog: HTMLElement): Promise<HTMLElement> {
  return waitFor(() => {
    const found = dialog.querySelector<HTMLElement>('[data-session-id="s-1"]');
    if (!found) throw new Error("no row in the drawer");
    return found;
  });
}

/** Open the session (which closes the drawer) and start it. */
async function liveSession(socket: ReturnType<typeof mount>["socket"]) {
  const dialog = await openDrawer("Sessions and agents");
  fireEvent.click(await rowInDrawer(dialog));
  act(() =>
    socket.receive(
      frame("s-1", 1, "session.started", {
        model: "sonnet",
        mode: "auto",
        effort: "low",
        commands: [],
        agents: [],
        models: [],
        cli_version: "2.1.259",
        resumed: true,
      }),
    ),
  );
}

test("the sessions toggle opens the rail as a drawer, rows and all", async () => {
  mount();
  expect(document.querySelector('[data-region="left-rail"]')).toBeNull();
  const dialog = await openDrawer("Sessions and agents");
  expect((await rowInDrawer(dialog)).textContent).toContain("retro H1–H4");
  expect(
    screen.getByRole("button", { name: "Sessions and agents" }).getAttribute("aria-expanded"),
  ).toBe("true");
});

test("opening a row closes the drawer and selects the session", async () => {
  const { container, socket } = mount();
  const dialog = await openDrawer("Sessions and agents");
  fireEvent.click(await rowInDrawer(dialog));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(container.querySelector(".conversation-title")?.textContent).toBe("retro H1–H4");
  expect(socket.sent.some((f) => f.type === "session.open")).toBe(true);
});

test("Fork from the row menu closes the drawer and arms the fork", async () => {
  const { container, socket } = mount();
  await liveSession(socket);
  const dialog = await openDrawer("Sessions and agents");
  fireEvent.click(dialog.querySelector('[data-session-id="s-1"] .sess-menu') as HTMLElement);
  fireEvent.click(await screen.findByRole("menuitem", { name: "Fork" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(container.querySelector(".fork-note")?.textContent).toContain("starts a fork of");
});

test("Search sessions from the drawer closes it and opens the palette, focused", async () => {
  mount();
  const dialog = await openDrawer("Sessions and agents");
  fireEvent.click(dialog.querySelector(".rail-search") as HTMLElement);
  expect(screen.queryByRole("dialog", { name: "Sessions and agents" })).toBeNull();
  const palette = await waitFor(() => screen.getByRole("dialog", { name: "Search sessions" }));
  expect(palette.contains(document.activeElement)).toBe(true);
  expect(document.activeElement).toBe(screen.getByPlaceholderText("Search sessions"));
});

test("Escape with a row menu open inside the drawer closes the menu, not the drawer", async () => {
  const { socket } = mount();
  await liveSession(socket);
  const dialog = await openDrawer("Sessions and agents");
  fireEvent.click(dialog.querySelector('[data-session-id="s-1"] .sess-menu') as HTMLElement);
  await screen.findByRole("menuitem", { name: "Fork" });

  fireEvent.keyDown(window, { key: "Escape" });
  expect(screen.queryByRole("menuitem", { name: "Fork" })).toBeNull();
  expect(screen.queryByRole("dialog", { name: "Sessions and agents" })).not.toBeNull();

  fireEvent.keyDown(window, { key: "Escape" });
  expect(screen.queryByRole("dialog", { name: "Sessions and agents" })).toBeNull();
});

test("growing the window docks the rail and closes the drawer it was showing", async () => {
  const { container, viewport } = mount();
  await openDrawer("Sessions and agents");
  viewport.resizeTo(1440);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(container.querySelector('[data-region="left-rail"]')).not.toBeNull();
  expect(container.querySelector('[data-region="right-rail"]')).not.toBeNull();
  expect(screen.queryByRole("button", { name: "Sessions and agents" })).toBeNull();
  // The toggle that opened it went with the rail it named, so useModalFocus has no connected
  // opener to return to; Shell's fallback puts focus in the composer rather than letting it
  // fall to document.body.
  expect(document.activeElement).toBe(screen.getByLabelText("Message"));
});
