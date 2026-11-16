/**
 * Acceptance (frozen): what "live" means, and the two features that depend on it.
 *
 * Both features used a correct-looking expression,
 * `new Set(Object.keys(appSnapshot.sessions))`, every session this
 * browser has a VIEW of, as if it were every session with a claude process behind it.
 *
 * Those are different sets, and the app has both kinds. Opening a row to READ it seeds a view
 * synchronously before any request is answered, while the server deliberately starts nothing for
 * an existing transcript — "its process starts on the first send" (session.py). So:
 *
 * - The three-session cap counted transcripts the user had merely glanced at. Reading three old
 *   conversations refused a new one, and the toast named those three as the sessions they had to
 *   close, in an app running zero copies of claude. The server would have admitted a fourth; only
 *   the browser said no.
 * - Wrap up asks the same question with the vault at stake, since its prompt runs `git add -A`.
 *   It was fed `others`, which the server builds by subtracting its own processes — so a sibling
 *   session of the one being wrapped up could not appear in it by construction, while wrapUp.ts's
 *   own docstring promised "another window of this app's" was covered. The one case it could
 *   actually know about was the one case it could not see.
 *
 * A session of ours is not privileged over a VS Code one: both hold the vault, both finish first.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { wrapUpState } from "../../components/Header/wrapUp";
import type { Command, Envelope, EventMap, ModelOption, Rails } from "../../protocol";
import { fakeDurable } from "../../testDurable";
import { fakeRest, fakeViki, localIso, sessionRow } from "../m3b/fakes";

afterEach(cleanup);

const day = (h: number, m: number) => localIso(2026, 9, 13, h, m);

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
  sessionRow({ session_id: "s-3", title: "Buy milk", modified: day(15, 0) }),
];

function rails(): Rails {
  return {
    agents: [],
    gates: [],
    routines: [],
    counts: { knowledge: 132, sources: 202, inbox: 1 },
    git: {
      state: "known",
      ref_state: "branch",
      branch: "main",
      head: "c8868e6",
      committed_at: day(19, 16),
      dirty: 0,
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
  return { ...utils, socket };
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

const COMMANDS: Command[] = [];
const MODELS: ModelOption[] = [{ value: "sonnet", label: "Sonnet 5", description: "fast" }];

/** A real process reporting itself. This is the only thing that makes a session live. */
function started(socket: ReturnType<typeof mount>["socket"], id: string): void {
  act(() =>
    socket.receive(
      frame(id, 1, "session.started", {
        model: "sonnet",
        mode: "auto",
        effort: "low",
        commands: COMMANDS,
        agents: [],
        models: MODELS,
        cli_version: "2.1.259",
        resumed: true,
      }),
    ),
  );
}

async function openRow(container: HTMLElement, id: string): Promise<void> {
  const row = await waitFor(() => {
    const found = container.querySelector<HTMLElement>(`[data-session-id="${id}"]`);
    if (!found) throw new Error(`no row ${id}`);
    return found;
  });
  fireEvent.click(row);
}

function newSessionButton(): HTMLElement {
  return screen.getByText("New session");
}

test("reading old conversations does not spend the live-session slots", async () => {
  const { container } = mount();
  // The exact workflow the scope calls out: "Clicking a row opens read-only history."
  await openRow(container, "s-1");
  await openRow(container, "s-2");
  await openRow(container, "s-3");

  fireEvent.click(newSessionButton());
  // No refusal: nothing they read has a process, so nothing they read can fill a cap on processes.
  expect(screen.queryByRole("alert")).toBeNull();
});

test("sessions that actually reported a process do fill the cap", async () => {
  const { container, socket } = mount();
  await openRow(container, "s-1");
  await openRow(container, "s-2");
  await openRow(container, "s-3");
  started(socket, "s-1");
  started(socket, "s-2");
  started(socket, "s-3");

  fireEvent.click(newSessionButton());
  const toast = await waitFor(() => screen.getByRole("alert"));
  // And it names them, so the refusal says what to do rather than only saying no.
  expect(toast.textContent).toContain("retro H1–H4");
});

test("a brand-new session holds its slot before session.started confirms it", async () => {
  // The server starts a new id eagerly on session.open, so the slot is taken from the click.
  // Counting only `info` would leave a window, one round trip wide, in which a fourth slipped in.
  const { socket } = mount();
  for (let i = 0; i < 3; i++) fireEvent.click(newSessionButton());
  expect(screen.queryByRole("alert")).toBeNull();
  expect(socket.sent.filter((f) => f.type === "session.open")).toHaveLength(3);

  fireEvent.click(newSessionButton());
  await waitFor(() => screen.getByRole("alert"));
  expect(socket.sent.filter((f) => f.type === "session.open")).toHaveLength(3);
});

// --- Wrap up -----------------------------------------------------------------------------------

test("wrapUpState treats our own live session exactly like someone else's", () => {
  expect(wrapUpState([], [])).toEqual({ enabled: true, tooltip: "" });

  const ours = wrapUpState([], ["retro H1–H4"]);
  expect(ours.enabled).toBe(false);
  expect(ours.tooltip).toContain("retro H1–H4");
  // Named as ours, because "viki-agent" as a badge would tell them nothing they cannot see.
  expect(ours.tooltip).toContain("This app");

  // An untitled sibling is still a reason to wait, and is worded like every other untitled row.
  expect(wrapUpState([], [""]).tooltip).toContain("Untitled session");
});

test("Wrap up is refused while another session of this app is live, and says which", async () => {
  const { container, socket } = mount();
  await openRow(container, "s-1");
  await openRow(container, "s-2");
  started(socket, "s-1");
  started(socket, "s-2");
  // Looking at s-1; s-2 is live behind it. Wrap up's prompt runs `git add -A`, which would sweep
  // whatever s-2 has half-written into a commit describing s-1's work.
  await openRow(container, "s-1");

  fireEvent.click(await waitFor(() => screen.getByLabelText("Session actions")));
  const item = await waitFor(() => screen.getByText("Wrap up (log + commit)"));
  const row = item.closest("button") ?? item;
  expect(row.getAttribute("aria-disabled") ?? String(row.hasAttribute("disabled"))).not.toBe(
    "false",
  );
  expect(row.getAttribute("title") ?? "").toContain("retro H1–H4");
});
