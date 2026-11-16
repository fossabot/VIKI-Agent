/**
 * Acceptance (frozen): a reload comes back to the session the user was looking at.
 *
 * The end-to-end reload test could not reach this. It forces the live socket closed, which is a
 * reconnect rather than a navigation, so React's own state — including the selection — survives
 * and the question never arises. A real F5 throws that away: the socket's reconnect re-acquires
 * the live sessions on its own (state/viki.ts reopens every id in vault.sessions' `live` that it
 * does not track), but `selectedId` starts null on a fresh page, so the user would come back to
 * "New conversation" while a reply streamed into a session they could not see until they
 * found its row.
 *
 * What is pinned here is only the seam: that the selection is remembered, that the restore waits
 * for the session to actually arrive rather than reading the snapshot once, and that it never
 * overrides the user. A remount with the same sessionStorage is what a reload is, from App's
 * point of view.
 */
import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import type { Envelope, EventMap, Rails } from "../../protocol";
import { fakeDurable } from "../../testDurable";
import { fakeRest, fakeViki, localIso, sessionRow } from "../m3b/fakes";

afterEach(cleanup);

const day = (h: number, m: number) => localIso(2026, 9, 12, h, m);

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(day(20, 0)));
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
  window.sessionStorage.clear();
});

const rows = [
  sessionRow({ session_id: "s-1", title: "Dashboard v1 → reverted", modified: day(17, 20) }),
  sessionRow({ session_id: "s-2", title: "retro H1–H4", modified: day(16, 0) }),
];

function rails(): Rails {
  return {
    agents: [],
    gates: [],
    routines: [],
    counts: { knowledge: 116, sources: 202, inbox: 1 },
    git: {
      state: "known",
      ref_state: "branch",
      branch: "main",
      head: "c8868e6",
      committed_at: day(19, 16),
      dirty: 3,
    },
  };
}

function mount() {
  const rest = fakeRest({ sessions: rows, rails: rails() });
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  const utils = render(<App viki={viki} rest={rest} durable={durable} />);
  act(() => socket.open());
  socket.sent.length = 0;
  return { ...utils, durable, viki, socket };
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

/** What the server sends a reconnected page: the sessions this app still holds. */
function live(socket: ReturnType<typeof mount>["socket"], ids: string[]): void {
  act(() => socket.receive(frame("s-1", 0, "vault.sessions", { others: [], live: ids })));
}

async function openRow(container: HTMLElement, id: string): Promise<void> {
  const row = await waitFor(() => {
    const found = container.querySelector<HTMLElement>(`[data-session-id="${id}"]`);
    if (!found) throw new Error("the list has not arrived");
    return found;
  });
  fireEvent.click(row);
}

test("opening a session remembers it, and the next page load comes back to it", async () => {
  const first = mount();
  await openRow(first.container, "s-2");
  act(() =>
    first.socket.receive(
      frame("s-2", 1, "user.message", { text: "milk", command: "", at: day(18, 0) }),
    ),
  );
  expect(first.container.querySelector(".user-turn")?.textContent).toContain("milk");
  cleanup();

  // A reload: a fresh App over the same sessionStorage, with nothing selected and no session
  // known yet. The column greets, because at this instant the app really does hold nothing.
  const second = mount();
  expect(second.container.querySelector(".empty")).not.toBeNull();

  // Then the reconnect re-acquires the session, and the user is back where they were.
  live(second.socket, ["s-2"]);
  act(() =>
    second.socket.receive(
      frame("s-2", 1, "user.message", { text: "milk", command: "", at: day(18, 0) }),
    ),
  );
  await waitFor(() => {
    expect(second.container.querySelector(".user-turn")?.textContent).toContain("milk");
  });
  expect(second.container.querySelector(".empty")).toBeNull();
});

test("a selection the owner makes first wins over the remembered one", async () => {
  window.sessionStorage.setItem("viki.selected", "s-2");
  const { container, socket } = mount();
  await openRow(container, "s-1");
  // s-2 arrives after the user has already chosen s-1: the restore is abandoned, not queued.
  live(socket, ["s-1", "s-2"]);
  act(() =>
    socket.receive(
      frame("s-2", 1, "user.message", { text: "the other one", command: "", at: day(18, 0) }),
    ),
  );
  act(() =>
    socket.receive(
      frame("s-1", 1, "user.message", { text: "this one", command: "", at: day(18, 0) }),
    ),
  );
  expect(container.querySelector(".user-turn")?.textContent).toContain("this one");
});

test("a remembered session that never comes back leaves the empty state alone", async () => {
  window.sessionStorage.setItem("viki.selected", "s-9");
  const { container, socket } = mount();
  live(socket, ["s-1"]);
  await waitFor(() => {
    expect(container.querySelector('[data-session-id="s-1"]')).not.toBeNull();
  });
  expect(container.querySelector(".empty")).not.toBeNull();
});

test("closing the session on screen forgets it, so the next load does not chase it", async () => {
  const { container, viki } = mount();
  await openRow(container, "s-1");
  expect(window.sessionStorage.getItem("viki.selected")).toBe("s-1");
  act(() => viki.actions.closeSession("s-1"));
  await waitFor(() => {
    expect(window.sessionStorage.getItem("viki.selected")).toBeNull();
  });
});
