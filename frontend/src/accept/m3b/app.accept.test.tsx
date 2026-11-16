/** Acceptance (frozen): the App wires the rails, the list, the palette and the status bar
 * to a fake socket and a scripted REST client. Real timers: the fakes resolve at once. */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { RestError } from "../../api";
import type { Envelope, Rails } from "../../protocol";
import { fakeDurable } from "../../testDurable";
import { agent, failingRest, fakeRest, fakeViki, gate, localIso, sessionRow } from "./fakes";

afterEach(cleanup);

const today = (h: number, m: number) => localIso(2026, 9, 12, h, m);

/* AMENDMENT (2026-09-13). The fixtures below
   are dated 2026-09-12 and call it "today", but App reads the real clock, so "Today" and the
   relative times became wrong the moment a session crossed midnight — two tests failed for that
   reason alone. The contract is unchanged; only Date is pinned to the day the fixtures use.
   `toFake: ["Date"]` leaves setTimeout and friends real, so the file's "Real timers: the fakes
   resolve at once" promise still holds. */
beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(today(20, 0)));
});

afterEach(() => {
  vi.useRealTimers();
});

function rails(): Rails {
  return {
    agents: [
      agent({ name: "librarian", last_run: localIso(2026, 8, 31, 8, 0) }),
      agent({ name: "researcher" }),
    ],
    gates: [
      gate({ id: "H1", item: "/recall skill", label: "medium" }),
      gate({
        id: "H4",
        item: "CLAUDE.md containment line",
        label: "owner applies",
        owner_applies: true,
      }),
    ],
    routines: [{ when: "07:30", name: "/daily · morning brief", planned: true }],
    counts: { knowledge: 116, sources: 202, inbox: 1 },
    git: {
      state: "known",
      ref_state: "branch",
      branch: "main",
      head: "c8868e6",
      committed_at: today(19, 16),
      dirty: 3,
    },
  };
}

const rows = [
  sessionRow({
    session_id: "s-old",
    title: "Dashboard v0 at-a-glance",
    modified: localIso(2026, 9, 11, 9, 0),
  }),
  sessionRow({ session_id: "s-today", title: "Dashboard v1 → reverted", modified: today(17, 20) }),
];

function mount(rest = fakeRest({ rails: rails(), sessions: rows })) {
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  const utils = render(<App viki={viki} rest={rest} durable={durable} />);
  return { ...utils, durable, viki, socket, rest };
}

function openSocket(socket: ReturnType<typeof fakeViki>["socket"]): void {
  act(() => socket.open());
  socket.sent.length = 0;
}

/* AMENDMENT (2026-10-20). Retiring the retro
   feed retires two of this file's own frozen pins: the `.q` gate-id assertion below and the
   gate-pick test that followed it are both deleted here; every other case is unaffected. */
test("the rails, tiles, git chip and status bar fill in once REST answers", async () => {
  const { container } = mount();
  /* AMENDMENT (2026-10-20). The status bar carries the vault counts only while the
     right panel is off screen; docked, as this test mounts, that text never
     appears. Swapped the wait for the right rail's own knowledge tile, keeping the test's
     intent unchanged: wait until the rails REST answer lands. */
  await screen.findByText("116", { selector: '[data-tile="knowledge"] .tile-n' });
  expect(container.querySelector(".git-chip")?.textContent).toContain("main · dirty · c8868e6");
  expect(screen.getByText(`last commit c8868e6 19:16`)).toBeTruthy();
  expect([...container.querySelectorAll(".tile-n")].map((el) => el.textContent)).toEqual([
    "116",
    "202",
    "1",
  ]);
  /* AMENDMENT (2026-10-20). Without a routines client — this mount never had one —
     Up next now renders no Routines block at all; RoutinesSection's fallback branch is deleted,
     not relocated. This pin of its `.rt .h` row has nothing left to read; retired outright. */
  expect(
    [...container.querySelectorAll(".agent")].map((el) => el.getAttribute("data-agent")),
  ).toEqual(["VIKI", "librarian", "researcher"]);
});

test("the session list groups the rows; opening one subscribes and asks for history", async () => {
  const { container, socket } = mount();
  openSocket(socket);
  const row = await waitFor(() => {
    const found = container.querySelector<HTMLElement>('[data-session-id="s-today"]');
    if (!found) throw new Error("not yet");
    return found;
  });
  expect(
    [...container.querySelectorAll(".rail-sessions .lbl")].map((el) => el.textContent),
  ).toEqual(["Today", "Yesterday · 09-11"]);
  fireEvent.click(row);
  expect(socket.sent).toEqual([
    { request_id: expect.any(String), type: "session.open", session_id: "s-today", after_seq: 0 },
    {
      generation: expect.any(String),
      request_id: expect.any(String),
      type: "history.more",
      session_id: "s-today",
    },
  ]);
  expect(container.querySelector(".conversation-title")?.textContent).toBe(
    "Dashboard v1 → reverted",
  );
  expect(row.classList.contains("sess-selected")).toBe(true);
  expect(row.classList.contains("sess-live")).toBe(true);
});

test("New session starts one, selects it, and the rail reports VIKI here now", async () => {
  const { container, socket } = mount();
  openSocket(socket);
  fireEvent.click(screen.getByText("New session"));
  expect(socket.sent).toHaveLength(1);
  const frame = socket.sent[0] as { type: string; session_id: string; after_seq: number };
  expect(frame.type).toBe("session.open");
  expect(frame.session_id).toMatch(/^[0-9a-f-]{36}$/);
  expect(container.querySelector(".conversation-title")?.textContent).toBe("Untitled session");
  expect(container.querySelector(".right-rail .chip")?.textContent).toBe("here now");
  expect(container.querySelector(".rail-agents-count")?.textContent).toBe("1 here now");
  const envelope = (
    seq: number,
    type: "user.message" | "text.delta" | "question.asked",
  ): Envelope => {
    if (type === "user.message")
      return {
        v: 2,
        generation: "11111111-1111-4111-8111-111111111111",
        request_id: "",
        type,
        session_id: frame.session_id,
        seq,
        data: { text: "hi", command: "", at: today(18, 0) },
      };
    if (type === "text.delta")
      return {
        v: 2,
        generation: "11111111-1111-4111-8111-111111111111",
        request_id: "",
        type,
        session_id: frame.session_id,
        seq,
        data: { block_id: "b1", text: "Hel" },
      };
    return {
      v: 2,
      generation: "11111111-1111-4111-8111-111111111111",
      request_id: "",
      type,
      session_id: frame.session_id,
      seq,
      data: { tool_use_id: "q1", questions: [] },
    };
  };
  act(() => socket.receive(envelope(1, "user.message")));
  act(() => socket.receive(envelope(2, "text.delta")));
  /* AMENDMENT (2026-10-20). The rail header's status line is deleted (`statusLineFor`
     and the `statusLine` prop are gone); the composer's own activity strip is the one writing
     signal now (app-tests/chat/composerActivity.unit.test.tsx pins it). The two assertions this
     drove — `.right-rail-status` through "writing a reply" and "waiting for you" — are retired
     outright. */
  act(() => socket.receive(envelope(3, "question.asked")));
});

test("Ctrl+K and the rail button open the palette; Escape closes it", () => {
  mount();
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  fireEvent.keyDown(window, { key: "k", ctrlKey: true });
  expect(document.querySelector('[role="dialog"]')).not.toBeNull();
  fireEvent.keyDown(document.querySelector("input") as HTMLInputElement, { key: "Escape" });
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  fireEvent.click(screen.getByText("Search sessions"));
  expect(document.querySelector('[role="dialog"]')).not.toBeNull();
});

test("with the socket down and every REST call failing, the shell still renders", async () => {
  const rest = failingRest(new RestError(401, "unauthorized"));
  const { container } = mount(rest);
  await waitFor(() => expect(rest.calls).toContain("rails"));
  for (const region of [
    "shell",
    "topbar",
    "left-rail",
    "conversation",
    "right-rail",
    "statusbar",
  ]) {
    expect(container.querySelector(`[data-region="${region}"]`)).not.toBeNull();
  }
  await waitFor(() =>
    expect(container.querySelector(".git-chip")?.textContent).toContain("status unknown"),
  );
  expect(container.querySelector(".git-chip")?.textContent).not.toContain("clean");
  expect(container.querySelector(".tile")).toBeNull();
  expect(container.querySelector(".right-rail .chip")).toBeNull();
  /* AMENDMENT (2026-10-20). This assertion
     pinned GatesSection's "Needs review" heading, itself a pin of the retired feature;
     GatesSection is deleted and nothing replaces that heading. Swapped for another right-rail
     text the same degraded shell still renders, keeping the test's intent unchanged: the rail
     shows real content, not an empty region, even with every REST call failing. */
  expect(container.querySelector('[data-region="right-rail"]')?.textContent).toContain(
    "Skills catalog unavailable.",
  );
});
