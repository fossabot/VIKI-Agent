/**
 * App-level wiring for steering from the chat composer: while a turn runs, Composer's own
 * `steer` prop (there is a selected chat and no fork armed) decides whether Enter and a Steer
 * button dispatch into it right away, through the same durable command path as an ordinary
 * send, instead of being refused. A profile chat steers the same way: its own Claude Code
 * version decides, server-side, whether that lands as a steer or a `turn_running` refusal that
 * keeps the draft with its note. Composer.unit.test.tsx and sessionReducer.steer.unit.test.ts
 * already pin the button's own gating and the reducer's steer.pending/steer.ended cases; this
 * file pins only what App itself is responsible for: which sessions get `steer`, and the socket
 * round trip from a steered send through its waiting row to a user turn.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, GENERATION, sessionRow } from "../../accept/m3b/fakes";
import { DurableApiError } from "../../clients/conversations/durableApi";
import {
  type PreparedProfilePreview,
  type ProfileLaunchApi,
  ProfileLaunchApiError,
} from "../../clients/profiles/profileLaunchApi";
import type { Envelope, EventMap } from "../../protocol";
import { fakeDurable } from "../../testDurable";

afterEach(() => {
  cleanup();
  sessionStorage.clear();
});

const AT = "2026-10-30T00:00:00.000Z";

function frame<K extends keyof EventMap>(
  sessionId: string,
  seq: number,
  type: K,
  data: EventMap[K],
): Envelope {
  return {
    v: 2,
    generation: GENERATION,
    request_id: "",
    type,
    session_id: sessionId,
    seq,
    data,
  } as Envelope;
}

function started(sessionId: string, seq: number): Envelope {
  return frame(sessionId, seq, "session.started", {
    model: "sonnet",
    mode: "auto",
    effort: "low",
    commands: [],
    agents: [],
    models: [],
    cli_version: "2.1.259",
    resumed: true,
  });
}

function userMessage(sessionId: string, seq: number, text: string): Envelope {
  return frame(sessionId, seq, "user.message", { text, command: "", at: AT });
}

function steerPending(sessionId: string, seq: number, steerId: string, text: string): Envelope {
  return frame(sessionId, seq, "steer.pending", {
    steer_id: steerId,
    text,
    command: "",
    images: 0,
    at: AT,
  });
}

function steerEnded(
  sessionId: string,
  seq: number,
  steerId: string,
  outcome: "started" | "dropped",
): Envelope {
  return frame(sessionId, seq, "steer.ended", { steer_id: steerId, outcome });
}

function composerBox(): HTMLTextAreaElement {
  return screen.getByLabelText<HTMLTextAreaElement>("Message");
}

async function sendFromComposer(text: string): Promise<void> {
  fireEvent.change(composerBox(), { target: { value: text } });
  await act(async () => {
    fireEvent.keyDown(composerBox(), { key: "Enter" });
  });
}

async function openRow(container: HTMLElement, sessionId: string): Promise<void> {
  const row = await waitFor(() => {
    const found = container.querySelector<HTMLElement>(`[data-session-id="${sessionId}"]`);
    if (!found) throw new Error(`no row ${sessionId}`);
    return found;
  });
  await act(async () => {
    fireEvent.click(row);
  });
}

/** Opens an already-listed row and drives it to a running turn: `started`, then a first
 * `user.message` (newTurn's own default status is "running", with no turn.done to close it). */
async function openRunningSession(
  container: HTMLElement,
  socket: { receive: (message: unknown) => void },
  sessionId: string,
): Promise<void> {
  await openRow(container, sessionId);
  act(() => socket.receive(started(sessionId, 1)));
  act(() => socket.receive(userMessage(sessionId, 2, "first turn")));
}

function preparedPreview(sessionId: string): PreparedProfilePreview {
  return {
    launchable: true,
    fields: [],
    blockers: [],
    warnings: [],
    prefs: { mode: "default", effort: "high", model: "sonnet" },
    session_id: sessionId,
    profile_id: "profile",
    profile_revision: 1,
    generation: null,
    workspace_id: "workspace",
    workspace_revision: 1,
    policy: "ordinary",
    policy_revision: 1,
    settings_revision: 1,
    runtime: "claude",
    capability_key: "a".repeat(64),
    draft: { key: sessionId, revision: 1, text: "", saved_at: null, attachments: [] },
  };
}

/** Answers `preparedPreview` for `sessionId` (bound), 404 `not_found` for any other id. */
function fakeProfileLaunchApiFor(sessionId: string): ProfileLaunchApi {
  return {
    capabilities: vi.fn(),
    models: vi.fn(),
    prepare: vi.fn(),
    inspect: vi.fn(async (id) =>
      id === sessionId
        ? preparedPreview(sessionId)
        : Promise.reject(new ProfileLaunchApiError(404, "not_found")),
    ),
  };
}

test("while a turn runs, Stop and Steer show and Send does not", async () => {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Chat" })] });
  const { viki, socket } = fakeViki();
  const { container } = render(<App viki={viki} rest={rest} durable={fakeDurable()} />);
  act(() => socket.open());
  await openRunningSession(container, socket, "s1");

  expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "Steer" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
});

test("Enter and Steer each send one durable command, never turn.send, and a pending steer shows a waiting row until it resolves into a user turn", async () => {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Chat" })] });
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  const { container } = render(<App viki={viki} rest={rest} durable={durable} />);
  act(() => socket.open());
  await openRunningSession(container, socket, "s1");

  // Enter steers: the running turn never refuses it, and nothing goes out as a native turn.send.
  await sendFromComposer("steer by enter");
  expect(durable.submissions).toHaveLength(1);
  expect(durable.submissions[0]).toMatchObject({ session_id: "s1" });
  expect(socket.sent.filter((f) => f.type === "turn.send")).toEqual([]);

  // The server has not folded it in yet: a waiting row shows the unread message.
  act(() => socket.receive(steerPending("s1", 3, "steer-1", "steer by enter")));
  expect(container.querySelector(".steer-waiting-text")?.textContent).toBe("steer by enter");

  // It reaches Claude Code: the waiting row goes, replaced by an ordinary user turn.
  act(() => socket.receive(steerEnded("s1", 4, "steer-1", "started")));
  act(() => socket.receive(userMessage("s1", 5, "steer by enter")));
  expect(container.querySelector(".steer-waiting")).toBeNull();
  expect(screen.getByText("steer by enter")).toBeTruthy();

  // The Steer button is the same control surface as Enter: it also steers, and also never
  // reaches turn.send.
  fireEvent.change(composerBox(), { target: { value: "steer by button" } });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Steer" }));
  });
  expect(durable.submissions).toHaveLength(2);
  expect(durable.submissions[1]).toMatchObject({ session_id: "s1" });
  expect(socket.sent.filter((f) => f.type === "turn.send")).toEqual([]);
});

test("a fork armed on a running session shows Stop but no Steer", async () => {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Chat" })] });
  const { viki, socket } = fakeViki();
  const { container } = render(<App viki={viki} rest={rest} durable={fakeDurable()} />);
  act(() => socket.open());
  await openRunningSession(container, socket, "s1");

  fireEvent.click(container.querySelector('[data-session-id="s1"] .sess-menu') as HTMLElement);
  fireEvent.click(await screen.findByRole("menuitem", { name: "Fork" }));

  expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Steer" })).toBeNull();
});

test("a running profile chat steers", async () => {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "p1", title: "Profile chat" })] });
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  const { container } = render(
    <App
      viki={viki}
      rest={rest}
      durable={durable}
      profileLaunchApi={fakeProfileLaunchApiFor("p1")}
    />,
  );
  act(() => socket.open());
  await openRow(container, "p1");
  await waitFor(() => expect(screen.getByText(/Profile instructions/)).toBeTruthy());
  act(() => socket.receive(started("p1", 1)));
  act(() => socket.receive(userMessage("p1", 2, "profile turn")));

  expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "Steer" })).toBeTruthy();

  // Enter steers like any other running chat: one durable command, never a native turn.send.
  await sendFromComposer("steer the profile chat");
  expect(durable.submissions).toHaveLength(1);
  expect(durable.submissions[0]).toMatchObject({ session_id: "p1" });
  expect(socket.sent.filter((f) => f.type === "turn.send")).toEqual([]);

  // Its own Claude Code version may still answer `turn_running`: the box keeps the draft with
  // its note instead of clearing it.
  durable.submitCommand = async (input) => {
    durable.submissions.push(input);
    throw new DurableApiError(409, "turn_running");
  };
  await sendFromComposer("unsent profile text");
  expect(composerBox().value).toBe("unsent profile text");
  expect(container.querySelector(".draft-status")?.textContent).toContain(
    "Stop it or wait for it to finish, then send.",
  );
});
