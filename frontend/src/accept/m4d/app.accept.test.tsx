/**
 * Acceptance (frozen): Wrap up and the other-VIKI banner reach the screen.
 *
 * Both components are tested in isolation (Header.unit.test.tsx pins their wording and their
 * disabled state; wrapUp.accept.test.ts pins the sentences themselves), but those tests never
 * mount them in App, the same gap m4b/app.accept.test.tsx closes for the conversation body.
 * This file pins the seam and only the seam: that App mounts them, what it feeds them, and
 * what a click puts on the wire.
 *
 * It does not re-assert the wording — wrapUp.ts owns those words and its own frozen test pins
 * them. What it does assert is that the text the menu sends is WRAP_UP_PROMPT itself, because
 * the whole point of the item is that the user does not retype the convention.
 *
 * Same harness as m4b: the m3b fakes, real timers, a pinned clock.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { WRAP_UP_PROMPT } from "../../components/Header/wrapUp";
import type { Envelope, EventMap, OtherSession, Rails } from "../../protocol";
import { fakeDurable } from "../../testDurable";
import { fakeRest, fakeViki, localIso, sessionRow } from "../m3b/fakes";

afterEach(cleanup);

const day = (h: number, m: number) => localIso(2026, 9, 12, h, m);

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(day(20, 0)));
});

afterEach(() => {
  vi.useRealTimers();
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

function mount({ open = true }: { open?: boolean } = {}) {
  const rest = fakeRest({ sessions: rows, rails: rails() });
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  const utils = render(<App viki={viki} rest={rest} durable={durable} />);
  if (open) {
    act(() => socket.open());
    socket.sent.length = 0;
  }
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

function other(over: Partial<OtherSession> = {}): OtherSession {
  return {
    session_id: "v-1",
    entrypoint: "claude-vscode",
    pid: 4242,
    title: "retro H1–H4",
    ...over,
  };
}

/** vault.sessions is connection-wide; the store takes it from whatever session_id carries it. */
let globalSequence = 0;
function vaultSessions(socket: ReturnType<typeof mount>["socket"], others: OtherSession[]): void {
  act(() => socket.receive(frame("", ++globalSequence, "vault.sessions", { others, live: [] })));
}

async function openFirstRow(container: HTMLElement, socket: ReturnType<typeof mount>["socket"]) {
  const row = await waitFor(() => {
    const found = container.querySelector<HTMLElement>('[data-session-id="s-1"]');
    if (!found) throw new Error("the list has not arrived");
    return found;
  });
  fireEvent.click(row);
  socket.sent.length = 0;
}

const menuButton = () => screen.getByLabelText("Session actions");
const wrapUpItem = () => screen.getByRole("menuitem") as HTMLButtonElement;
const sentOf = (socket: ReturnType<typeof mount>["socket"], type: string) =>
  socket.sent.filter((message) => message.type === type);

// --- the menu -------------------------------------------------------------------------------

test("the header carries the ⋯ menu, and it opens the one item the gate's banner refers to", () => {
  const { container } = mount();
  const header = container.querySelector(".conversation-header") as HTMLElement;
  expect(header.querySelector(".header-menu-wrap")).not.toBeNull();
  expect(screen.queryByRole("menu")).toBeNull();
  fireEvent.click(menuButton());
  expect(screen.getByRole("menu")).not.toBeNull();
  expect(wrapUpItem().textContent).toContain("Wrap up");
});

test("Wrap up sends the convention as an ordinary turn on the open session", async () => {
  const { container, socket, durable } = mount();
  await openFirstRow(container, socket);
  fireEvent.click(menuButton());
  await act(async () => {
    fireEvent.click(wrapUpItem());
  });
  expect(sentOf(socket, "turn.send")).toEqual([]);
  expect(durable.submissions).toEqual([
    {
      session_id: "s-1",
      generation: expect.any(String),
      command_id: expect.stringMatching(/^[0-9a-f-]{36}$/),
      draft_revision: expect.any(Number),
    },
  ]);
  expect([...durable.commands.values()][0]?.text).toBe(WRAP_UP_PROMPT);
  // The menu closes behind it, like every other menu in this app.
  expect(screen.queryByRole("menu")).toBeNull();
});

test("with nothing selected it starts a session and sends into that", async () => {
  const { socket, durable } = mount();
  fireEvent.click(menuButton());
  await act(async () => {
    fireEvent.click(wrapUpItem());
  });
  expect(sentOf(socket, "turn.send")).toEqual([]);
  const sends = [...durable.commands.values()];
  expect(sends).toHaveLength(1);
  expect(sends[0]?.text).toBe(WRAP_UP_PROMPT);
  expect(typeof sends[0]?.session_id).toBe("string");
});

test("while another VIKI holds the vault the item refuses and names who to finish first", async () => {
  const { container, socket, durable } = mount();
  await openFirstRow(container, socket);
  vaultSessions(socket, [other()]);
  fireEvent.click(menuButton());
  const item = wrapUpItem();
  expect(item.disabled).toBe(true);
  expect(item.title).toContain("VS Code: retro H1–H4");
  fireEvent.click(item);
  expect(sentOf(socket, "turn.send")).toEqual([]);
  expect(durable.submissions).toEqual([]);
});

// --- the banner -----------------------------------------------------------------------------

test("the banner appears with vault.sessions and leaves with it", () => {
  const { container, socket } = mount();
  expect(container.querySelector(".vault-banner")).toBeNull();
  vaultSessions(socket, [other()]);
  const banner = container.querySelector(".vault-banner") as HTMLElement;
  expect(banner).not.toBeNull();
  expect(banner.getAttribute("role")).toBe("status");
  expect(banner.textContent).toContain("retro H1–H4");
  vaultSessions(socket, []);
  expect(container.querySelector(".vault-banner")).toBeNull();
});

test("a dropped socket and a busy vault are two banners, not a contest", () => {
  const { container, socket } = mount();
  vaultSessions(socket, [other()]);
  act(() => socket.onclose?.({ code: 1006 }));
  const conversation = container.querySelector(".conversation") as HTMLElement;
  const kids = [...conversation.children].map((el) => el.className);
  // Header, the reconnect banner, then the vault banner: the socket being down is the more
  // urgent of the two, so it keeps the line nearest the header.
  expect(kids.indexOf("banner jp")).toBe(kids.indexOf("conversation-header") + 1);
  expect(kids.indexOf("vault-banner jp")).toBe(kids.indexOf("banner jp") + 1);
});
