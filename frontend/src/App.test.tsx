// @ts-expect-error no @types/node in this browser-focused tsconfig; Node itself is present
// under Vitest, which runs tests through Node, not a bundled browser.
import { readFileSync } from "node:fs";
// @ts-expect-error see above
import { fileURLToPath } from "node:url";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import viteConfig from "../vite.config";
import { App } from "./App";
import {
  agent,
  emptyRails,
  failingRest,
  fakeRest,
  fakeViki,
  sessionRow,
  snapshotData,
} from "./accept/m3b/fakes";
import { RestError } from "./api";
import { DurableApiError } from "./clients/conversations/durableApi";
import { fakeDurable } from "./testDurable";

afterEach(cleanup);

const REGIONS = [
  "shell",
  "topbar",
  "wordmark",
  "new-session",
  "left-rail",
  "conversation",
  "conversation-header",
  "message-column",
  "composer",
  "right-rail",
  "statusbar",
];

test("renders every region the layout acceptance test looks for", () => {
  const { container } = render(
    <App durable={fakeDurable()} viki={fakeViki().viki} rest={fakeRest()} />,
  );
  for (const region of REGIONS) {
    expect(container.querySelector(`[data-region="${region}"]`)).not.toBeNull();
  }
});

test("shows the empty-state chrome, not any of the user's session data", () => {
  render(<App durable={fakeDurable()} viki={fakeViki().viki} rest={fakeRest()} />);
  expect(screen.getByText("New session")).toBeTruthy();
  expect(screen.getByText("Search sessions")).toBeTruthy();
  // Nothing waits on the user, so the rail has no Needs you section.
  expect(screen.queryByText("Needs you")).toBeNull();
});

test("the send arrow's stroke matches the design (2px, not the shared .ico 1.6px)", () => {
  render(<App durable={fakeDurable()} viki={fakeViki().viki} rest={fakeRest()} />);
  const sendSvg = screen.getByRole("button", { name: "Send" }).querySelector("svg");
  expect((sendSvg as SVGElement | null)?.style.strokeWidth).toBe("2");
});

test("the conversation title uses the JP font stack, for session names in Japanese", () => {
  const { container } = render(
    <App durable={fakeDurable()} viki={fakeViki().viki} rest={fakeRest()} />,
  );
  const title = container.querySelector(".conversation-title");
  expect(title?.classList.contains("jp")).toBe(true);
});

test("no routines client renders no Routines block", () => {
  const { container } = render(
    <App durable={fakeDurable()} viki={fakeViki().viki} rest={fakeRest()} />,
  );
  expect(screen.queryByText("Routines", { selector: ".lbl" })).toBeNull();
  expect(container.querySelector(".rail-section--routines")).toBeNull();
});

test("vite build never inlines fonts as data: URIs (blocked by the server's font-src 'self' CSP)", () => {
  expect(viteConfig.build?.assetsInlineLimit).toBe(0);
});

// Left implicit by app.accept.test.tsx (which only ever selects sessions that stay open): the
// App docstring's own rule that a closed session's selection clears rather than sticking to a
// header title from a view that no longer exists.
test("closing the selected session clears the selection back to Untitled session", async () => {
  const row = {
    session_id: "s1",
    title: "Hello there",
    first_prompt: "",
    entrypoint: "viki-agent",
    modified: "2026-09-12T08:00:00.000Z",
    cli_version: "2.1.259",
    other: false,
  };
  const { viki, socket } = fakeViki();
  const rest = fakeRest({ sessions: [row] });
  const { container } = render(<App durable={fakeDurable()} viki={viki} rest={rest} />);
  act(() => socket.open());
  const sessionRow = await waitFor(() => {
    const found = container.querySelector<HTMLElement>('[data-session-id="s1"]');
    if (!found) throw new Error("not yet");
    return found;
  });
  fireEvent.click(sessionRow);
  expect(container.querySelector(".conversation-title")?.textContent).toBe("Hello there");

  act(() => {
    socket.receive({
      v: 2,
      generation: "11111111-1111-4111-8111-111111111111",
      request_id: "",
      type: "session.closed",
      session_id: "s1",
      seq: 1,
      data: { reason: "requested" },
    });
  });
  expect(container.querySelector(".conversation-title")?.textContent).toBe("Untitled session");
});

// A "busy" history page keeps its cursor and offers an explicit reload instead of the
// generic note, and the button re-requests the page rather than retrying on its own.
test("a busy history page shows the reload note, and its button resends history.more", async () => {
  const row = {
    session_id: "s1",
    title: "Hello there",
    first_prompt: "",
    entrypoint: "viki-agent",
    modified: "2026-09-12T08:00:00.000Z",
    cli_version: "2.1.259",
    other: false,
  };
  const { viki, socket } = fakeViki();
  const rest = fakeRest({ sessions: [row] });
  const { container } = render(<App durable={fakeDurable()} viki={viki} rest={rest} />);
  act(() => socket.open());
  const sessionRow = await waitFor(() => {
    const found = container.querySelector<HTMLElement>('[data-session-id="s1"]');
    if (!found) throw new Error("not yet");
    return found;
  });
  fireEvent.click(sessionRow);

  const first = socket.sent.find((f) => f.type === "history.more") as {
    generation: string;
    request_id: string;
  };
  act(() =>
    socket.receive({
      v: 2,
      generation: first.generation,
      request_id: first.request_id,
      type: "history.page",
      session_id: "s1",
      seq: 1,
      data: { events: [], next_before_uuid: null, error: "busy" },
    }),
  );

  const note = container.querySelector(".history-note");
  expect(note?.textContent).toContain("Loading past conversations is busy right now.");
  const reload = screen.getByRole("button", { name: "Reload" });
  socket.sent.length = 0;
  fireEvent.click(reload);
  expect(socket.sent.some((f) => f.type === "history.more")).toBe(true);
  expect(viki.store.getSnapshot().sessions.s1?.historyError).toBeNull();
});

test("main.tsx imports base.css before App, so tokens don't lose to component rules", () => {
  // Resolved from this test file's own URL, not process.cwd() — main.tsx sits right next to
  // it (both directly in src/), so this holds wherever vitest is invoked from, and even if
  // this file were copied elsewhere for a probe: process.cwd() would silently resolve to
  // nothing there and fail with an unrelated ENOENT. `here` must be a variable, not inlined:
  // `new URL(literal, import.meta.url)` written directly is Vite's own special asset-URL
  // pattern, statically rewritten at build time into a dev-server URL instead of a real path.
  const here = import.meta.url;
  const mainPath = fileURLToPath(new URL("./main.tsx", here));
  const mainSource: string = readFileSync(mainPath, "utf-8");
  const baseCssImport = mainSource.indexOf('"./styles/base.css"');
  const appImport = mainSource.indexOf('"./App"');
  expect(baseCssImport).toBeGreaterThan(-1);
  expect(appImport).toBeGreaterThan(-1);
  expect(baseCssImport).toBeLessThan(appImport);
});

test("resolving main.tsx never falls back to process.cwd()", () => {
  // Stubbing the return value (not a real chdir(), which worker threads reject outright) is
  // enough to prove the code path never consults process.cwd(): if it did, this stub would
  // send it looking for main.tsx under a directory that does not exist.
  // @ts-expect-error no @types/node in this browser-focused tsconfig; see the top of the file
  const cwdSpy = vi.spyOn(process, "cwd").mockReturnValue("C:/nowhere/not-the-repo");
  try {
    const here = import.meta.url;
    const mainPath = fileURLToPath(new URL("./main.tsx", here));
    expect(() => readFileSync(mainPath, "utf-8")).not.toThrow();
  } finally {
    cwdSpy.mockRestore();
  }
});

/** Dispatches the event the window manager sends when the user closes the window. */
function closingIsQuestioned(): boolean {
  const event = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(event);
  return event.defaultPrevented;
}

test("closing the window asks while a turn runs, and stops asking once it ends", () => {
  const { viki, socket } = fakeViki();
  render(<App durable={fakeDurable()} viki={viki} rest={fakeRest()} />);
  act(() => socket.open());
  expect(closingIsQuestioned()).toBe(false);

  fireEvent.click(screen.getByText("New session"));
  const opened = socket.sent.find((frame) => frame.type === "session.open") as {
    session_id: string;
  };
  expect(opened).toBeTruthy();
  const sessionId = opened.session_id;
  act(() =>
    socket.receive({
      v: 2,
      generation: "11111111-1111-4111-8111-111111111111",
      request_id: "",
      type: "user.message",
      session_id: sessionId,
      seq: 1,
      data: { text: "hi", command: "", at: "2026-09-12T09:00:00.000Z" },
    }),
  );
  expect(closingIsQuestioned()).toBe(true);

  act(() =>
    socket.receive({
      v: 2,
      generation: "11111111-1111-4111-8111-111111111111",
      request_id: "",
      type: "turn.done",
      session_id: sessionId,
      seq: 2,
      data: {
        status: "success",
        usage: {
          input_tokens: 0,
          output_tokens: 0,
          cache_read_tokens: 0,
          cache_creation_tokens: 0,
        },
        context_percent: null,
        duration_ms: 1,
      },
    }),
  );
  expect(closingIsQuestioned()).toBe(false);
});

test("Ctrl+Shift+K (event.key resolves upper-case 'K') still opens the search palette", () => {
  render(<App durable={fakeDurable()} viki={fakeViki().viki} rest={fakeRest()} />);
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  fireEvent.keyDown(window, { key: "K", ctrlKey: true, shiftKey: true });
  expect(document.querySelector('[role="dialog"]')).not.toBeNull();
});

const USAGE = { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_creation_tokens: 0 };

/** The envelope sequence for a turn that runs one Edit tool and finishes. */
function editTurnFrames(sessionId: string): Record<string, unknown>[] {
  return [
    {
      v: 2,
      generation: "11111111-1111-4111-8111-111111111111",
      request_id: "",
      type: "user.message",
      session_id: sessionId,
      seq: 1,
      data: { text: "hi", command: "", at: "2026-09-12T09:00:00.000Z" },
    },
    {
      v: 2,
      generation: "11111111-1111-4111-8111-111111111111",
      request_id: "",
      type: "turn.started",
      session_id: sessionId,
      seq: 2,
      data: { turn_id: "t1" },
    },
    {
      v: 2,
      generation: "11111111-1111-4111-8111-111111111111",
      request_id: "",
      type: "tool.started",
      session_id: sessionId,
      seq: 3,
      data: {
        block_id: "b1",
        tool: "Edit",
        row: { head: "Edit", rest: "x", tail: "" },
        parent_id: "",
      },
    },
    {
      v: 2,
      generation: "11111111-1111-4111-8111-111111111111",
      request_id: "",
      type: "turn.done",
      session_id: sessionId,
      seq: 4,
      data: { status: "success", usage: USAGE, context_percent: null, duration_ms: 1 },
    },
  ];
}

test("a write in this window plus a dirty vault (from rails) does not make closing the window ask once the turn is idle", async () => {
  const dirtyRails = {
    ...emptyRails(),
    git: {
      state: "known" as const,
      ref_state: "branch" as const,
      branch: "main",
      head: "abc1234",
      committed_at: "2026-09-12T08:00:00.000Z",
      dirty: 3,
    },
  };
  const rest = fakeRest({ rails: dirtyRails });
  const { viki, socket } = fakeViki();
  render(<App durable={fakeDurable()} viki={viki} rest={rest} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));
  expect(closingIsQuestioned()).toBe(false); // dirty vault alone, nothing written here yet

  fireEvent.click(screen.getByText("New session"));
  const opened = socket.sent.find((frame) => frame.type === "session.open") as {
    session_id: string;
  };
  const sessionId = opened.session_id;
  for (const frame of editTurnFrames(sessionId)) act(() => socket.receive(frame));

  // Idle turn: the window close no longer reads the vault's git state.
  expect(closingIsQuestioned()).toBe(false);
});

test("control case: a clean git observation also leaves the window closable once the turn is idle", async () => {
  const rest = fakeRest({
    rails: {
      ...emptyRails(),
      git: {
        state: "known",
        ref_state: "branch",
        branch: "main",
        head: "",
        committed_at: "",
        dirty: 0,
      },
    },
  });
  const { viki, socket } = fakeViki();
  render(<App durable={fakeDurable()} viki={viki} rest={rest} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  fireEvent.click(screen.getByText("New session"));
  const opened = socket.sent.find((frame) => frame.type === "session.open") as {
    session_id: string;
  };
  const sessionId = opened.session_id;
  for (const frame of editTurnFrames(sessionId)) act(() => socket.receive(frame));

  await waitFor(() => expect(closingIsQuestioned()).toBe(false));
});

test("a running agent absent from the roster (built-in subagent type, 'workflow', a user-scope agent) does not inflate the here-now count with a phantom row", async () => {
  const rest = fakeRest({ rails: { ...emptyRails(), agents: [agent({ name: "researcher" })] } });
  const { viki, socket } = fakeViki();
  const { container } = render(<App durable={fakeDurable()} viki={viki} rest={rest} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  fireEvent.click(screen.getByText("New session"));
  const opened = socket.sent.find((frame) => frame.type === "session.open") as {
    session_id: string;
  };
  const sessionId = opened.session_id;
  act(() =>
    socket.receive({
      v: 2,
      generation: "11111111-1111-4111-8111-111111111111",
      request_id: "",
      type: "user.message",
      session_id: sessionId,
      seq: 1,
      data: { text: "hi", command: "", at: "2026-09-12T09:00:00.000Z" },
    }),
  );
  act(() =>
    socket.receive({
      v: 2,
      generation: "11111111-1111-4111-8111-111111111111",
      request_id: "",
      type: "subagent.started",
      session_id: sessionId,
      seq: 2,
      data: { block_id: "a1", agent: "workflow", model: "", description: "", reason: "" },
    }),
  );

  const label = container.querySelector(".rail-agents-count")?.textContent;
  expect(label).toBe("1 here now"); // VIKI only — "workflow" has no roster row to account for it
  const hereNowRows = [...container.querySelectorAll(".agent")].filter((row) =>
    row.querySelector(".s-on"),
  );
  expect(hereNowRows).toHaveLength(1);
  expect(container.querySelector('[data-agent="workflow"]')).toBeNull();
});

test("control case: a running agent that IS in the roster keeps the count and its row in step", async () => {
  const rest = fakeRest({ rails: { ...emptyRails(), agents: [agent({ name: "researcher" })] } });
  const { viki, socket } = fakeViki();
  const { container } = render(<App durable={fakeDurable()} viki={viki} rest={rest} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  fireEvent.click(screen.getByText("New session"));
  const opened = socket.sent.find((frame) => frame.type === "session.open") as {
    session_id: string;
  };
  const sessionId = opened.session_id;
  act(() =>
    socket.receive({
      v: 2,
      generation: "11111111-1111-4111-8111-111111111111",
      request_id: "",
      type: "user.message",
      session_id: sessionId,
      seq: 1,
      data: { text: "hi", command: "", at: "2026-09-12T09:00:00.000Z" },
    }),
  );
  act(() =>
    socket.receive({
      v: 2,
      generation: "11111111-1111-4111-8111-111111111111",
      request_id: "",
      type: "subagent.started",
      session_id: sessionId,
      seq: 2,
      data: { block_id: "a1", agent: "researcher", model: "", description: "", reason: "" },
    }),
  );

  expect(container.querySelector(".rail-agents-count")?.textContent).toBe("2 here now");
  const hereNowRows = [...container.querySelectorAll(".agent")].filter((row) =>
    row.querySelector(".s-on"),
  );
  expect(hereNowRows).toHaveLength(2);
});

test("the beforeunload handler actually calls preventDefault() and sets a non-empty returnValue (jsdom's own defaultPrevented can't tell the two halves apart, so it checks each directly)", async () => {
  const rest = fakeRest();
  const { viki, socket } = fakeViki();
  render(<App durable={fakeDurable()} viki={viki} rest={rest} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));
  fireEvent.click(screen.getByText("New session"));
  const opened = socket.sent.find((frame) => frame.type === "session.open") as {
    session_id: string;
  };
  // Only the opening frames, so the turn stays running (busy) and blocks the unload.
  for (const frame of editTurnFrames(opened.session_id).slice(0, 2)) {
    act(() => socket.receive(frame));
  }

  const preventDefaultSpy = vi.spyOn(Event.prototype, "preventDefault");
  try {
    const event = new Event("beforeunload", { cancelable: true }) as BeforeUnloadEvent;
    window.dispatchEvent(event);
    expect(preventDefaultSpy).toHaveBeenCalled();
    expect(event.returnValue).not.toBe("");
  } finally {
    preventDefaultSpy.mockRestore();
  }
});

test("a failing REST call surfaces a status-bar notice instead of a silently stale shell", async () => {
  const rest = failingRest(new RestError(500, "boom"));
  const { viki } = fakeViki();
  render(<App durable={fakeDurable()} viki={viki} rest={rest} />);
  await waitFor(() => expect(rest.calls).toContain("rails"));
  expect(await screen.findByText("offline · retrying")).toBeTruthy();
});

test("a socket closed 4401 (a refused per-launch token) surfaces a notice and clears the stored token so a reload can't replay it", async () => {
  window.sessionStorage.setItem("viki.token", "stale-launch-1");
  const { viki, socket } = fakeViki();
  const { container } = render(<App durable={fakeDurable()} viki={viki} rest={fakeRest()} />);
  act(() => socket.open());
  act(() => socket.onclose?.({ code: 4401 }));
  expect(await screen.findByText("not signed in · relaunch VIKI Agent")).toBeTruthy();
  expect(window.sessionStorage.getItem("viki.token")).toBeNull();
  // The seam, not the card's words (accept/m4/staleWindow.accept.test.tsx owns those): the
  // column is the stale card and the composer is gone. Without this, moving the unauthorized
  // check below the empty-state return passes every other test, and the user gets
  // "New conversation" on a dead token with a box that can never send.
  expect(container.querySelector(".stale")).not.toBeNull();
  expect(container.querySelector(".composer")).toBeNull();
});

test("closing the palette inside the debounce fires no search, and it reopens clean", () => {
  vi.useFakeTimers();
  try {
    const rest = fakeRest({ sessions: [] });
    render(<App durable={fakeDurable()} viki={fakeViki().viki} rest={rest} />);
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    const input = document.querySelector('[role="dialog"] input') as HTMLInputElement;
    fireEvent.change(input, { target: { value: "vault" } });
    act(() => void vi.advanceTimersByTime(100));
    fireEvent.keyDown(input, { key: "Escape" });
    act(() => void vi.advanceTimersByTime(500));
    expect(rest.calls.filter((c) => c.startsWith("search:"))).toEqual([]);
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    const again = document.querySelector('[role="dialog"] input') as HTMLInputElement;
    expect(again.value).toBe("");
    expect(document.querySelectorAll(".hit, .palette-note")).toHaveLength(0);
  } finally {
    vi.useRealTimers();
  }
});

/* Durable receipts decide whether an unchanged editor clears. */

function composerBox(): HTMLTextAreaElement {
  return screen.getByLabelText<HTMLTextAreaElement>("Message");
}

async function openRow(container: HTMLElement, id: string): Promise<HTMLElement> {
  const row = await waitFor(() => {
    const found = container.querySelector<HTMLElement>(`[data-session-id="${id}"]`);
    if (!found) throw new Error(`no row ${id}`);
    return found;
  });
  fireEvent.click(row);
  return row;
}

async function sendFromComposer(text: string): Promise<void> {
  const box = composerBox();
  fireEvent.change(box, { target: { value: text } });
  await act(async () => {
    fireEvent.keyDown(box, { key: "Enter" });
  });
}

function capacityViki() {
  const fake = fakeViki();
  const synchronize = fake.socket.synchronize.bind(fake.socket);
  vi.spyOn(fake.socket, "synchronize").mockImplementation(
    (id, generation, seq, data = snapshotData()) => {
      data.state.max_live_sessions = 8;
      synchronize(id, generation, seq, data);
    },
  );
  return fake;
}

test.each(["button", "Alt+N"] as const)(
  "the %s start uses the advertised eight-session limit",
  async (control) => {
    const { viki, socket } = capacityViki();
    render(<App durable={fakeDurable()} viki={viki} rest={fakeRest()} />);
    act(() => socket.open());
    const start = () =>
      control === "button"
        ? fireEvent.click(screen.getByText("New session"))
        : fireEvent.keyDown(window, { key: "n", altKey: true });
    for (let i = 1; i <= 8; i++) {
      await act(async () => {
        start();
      });
      expect(socket.sent.filter((frame) => frame.type === "session.open")).toHaveLength(i);
    }
    start();
    expect(socket.sent.filter((frame) => frame.type === "session.open")).toHaveLength(8);
    expect(screen.getByRole("alert").textContent).toContain("Up to 8 sessions can run at once");
    expect(document.querySelectorAll(".toast-row")).toHaveLength(8);
  },
);

test.each([7, 8])(
  "a new-editor send uses the advertised limit with %i sessions live",
  async (liveCount) => {
    const ids = Array.from({ length: liveCount }, (_, i) => `capacity-${i}`);
    const { viki, socket } = capacityViki();
    const newSession = vi.spyOn(viki.actions, "newSession");
    const durable = fakeDurable();
    render(<App durable={durable} viki={viki} rest={fakeRest()} />);
    act(() => socket.open());
    act(() => {
      socket.receive({
        v: 2,
        generation: GEN,
        request_id: "",
        type: "vault.sessions",
        session_id: "",
        seq: 1,
        data: { others: [], live: ids },
      });
      for (const id of ids) {
        socket.receive({
          v: 2,
          generation: GEN,
          request_id: "",
          type: "session.started",
          session_id: id,
          seq: 2,
          data: {
            model: "sonnet",
            mode: "auto",
            effort: "low",
            commands: [],
            agents: [],
            models: [],
            cli_version: "2.1.259",
            resumed: true,
          },
        });
      }
    });

    const text = "Keep this new-session draft";
    await sendFromComposer(text);
    expect(socket.sent.filter((frame) => frame.type === "session.open")).toHaveLength(8);
    expect(socket.sent.filter((frame) => frame.type === "turn.send")).toEqual([]);
    if (liveCount === 7) {
      expect(newSession).toHaveBeenCalledTimes(1);
      expect(durable.submissions).toHaveLength(1);
      expect(composerBox().value).toBe("");
      expect(screen.queryByRole("alert")).toBeNull();
    } else {
      expect(newSession).not.toHaveBeenCalled();
      expect(durable.submissions).toEqual([]);
      expect(screen.getByRole("alert").textContent).toContain("Up to 8 sessions can run at once");
      expect(composerBox().value).toBe(text);
    }
  },
);

test("a rejected durable command retains the saved editor text even after a socket block", async () => {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "vault" })] });
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  durable.submitCommand = async (input) => {
    durable.submissions.push(input);
    throw new DurableApiError(409, "turn_running");
  };
  const { container } = render(<App durable={durable} viki={viki} rest={rest} />);
  act(() => socket.open());
  await openRow(container, "s1");

  await sendFromComposer("One thing to check before wrapping up");
  expect(durable.submissions).toHaveLength(1);
  expect(durable.submissions[0]).toMatchObject({ session_id: "s1", draft_revision: 1 });
  expect(durable.saves).toContainEqual({
    key: "s1",
    expectedRevision: 0,
    text: "One thing to check before wrapping up",
  });
  expect(durable.drafts.get("s1")?.text).toBe("One thing to check before wrapping up");
  expect(socket.sent.filter((f) => f.type === "turn.send")).toEqual([]);
  expect(composerBox().value).toBe("One thing to check before wrapping up");
  // The draft PUT above already committed, so the status region must say the send was
  // refused (a turn is running), not the false "cannot save" sentence the bug report opened with.
  expect(container.querySelector(".draft-status")?.textContent).toContain(
    "Stop it or wait for it to finish, then send.",
  );
  expect(container.querySelector(".draft-status")?.textContent).not.toContain(
    "Can't save the draft.",
  );
  // HIGH 2: the draft-status region already told the truth; the generic "check your send record"
  // alert must not also fire -- there is no record to check, the actor guard refused before
  // acceptance.
  expect(screen.queryByRole("alert")).toBeNull();

  act(() =>
    socket.receive({
      v: 2,
      generation: "11111111-1111-4111-8111-111111111111",
      request_id: "",
      type: "session.blocked",
      session_id: "s1",
      seq: 1,
      data: {
        reason: "live_elsewhere",
        holder: { session_id: "s1", entrypoint: "vscode", pid: 42, title: "vault" },
      },
    }),
  );
  expect(composerBox().value).toBe("One thing to check before wrapping up");
});

test("a non-dispatch-refusal send failure still shows the generic send-result note", async () => {
  // HIGH 2 control: when the store has nothing true to say about why (no dispatchRefusal), the
  // generic note is still the right thing to show -- this must not regress to always-silent.
  // A distinct session ID: unlike the turn_running case above, a 5xx is ambiguous, so
  // pendingCommand survives and persists to the shared jsdom sessionStorage across tests in this
  // file -- reusing "s1" here would leak a stale pending command into a later "s1" test.
  const rest = fakeRest({
    sessions: [sessionRow({ session_id: "s-unavailable", title: "vault" })],
  });
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  durable.submitCommand = async (input) => {
    durable.submissions.push(input);
    throw new DurableApiError(503, "unavailable");
  };
  const { container } = render(<App durable={durable} viki={viki} rest={rest} />);
  act(() => socket.open());
  await openRow(container, "s-unavailable");

  await sendFromComposer("This one did not go through");
  expect(screen.getByRole("alert").textContent).toContain(
    "Can't confirm the send result. Check the draft's status and the send history.",
  );
  expect(container.querySelector(".draft-status")?.textContent).toContain("Can't save the draft.");
});

test("a socket error after durable acceptance cannot undo the submitted copy or restore text", async () => {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1" })] });
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  const { container } = render(<App durable={durable} viki={viki} rest={rest} />);
  act(() => socket.open());
  await openRow(container, "s1");

  await sendFromComposer("First message");
  expect(durable.submissions).toHaveLength(1);
  const commandId = durable.submissions[0]?.command_id;
  expect(durable.commands.get(String(commandId))?.text).toBe("First message");
  expect(durable.drafts.get("s1")?.text).toBe("");
  expect(composerBox().value).toBe("");
  act(() =>
    socket.receive({
      v: 2,
      generation: "11111111-1111-4111-8111-111111111111",
      request_id: "",
      type: "error",
      session_id: "s1",
      seq: 1,
      data: { code: "too_many_sessions", message: "", recoverable: true },
    }),
  );
  expect(composerBox().value).toBe("");
  expect(durable.commands.get(String(commandId))?.text).toBe("First message");
  expect(socket.sent.filter((f) => f.type === "turn.send")).toEqual([]);
});

test("a prompt over the WebSocket frame cap is refused here, not by killing the connection", async () => {
  // The durable service and its protected store have a one MiB draft limit.
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  render(<App durable={durable} viki={viki} rest={fakeRest()} />);
  act(() => socket.open());
  const huge = "x".repeat(1_100_000);

  await sendFromComposer(huge);
  expect(socket.sent.filter((f) => f.type === "turn.send")).toEqual([]);
  expect(socket.sent.filter((f) => f.type === "session.open")).toEqual([]);
  expect(durable.submissions).toEqual([]);
  expect(composerBox().value).toBe(huge);
  // And it is the one refusal that has to say so itself: no card can, because the server never
  // saw the frame.
  expect(screen.getByRole("alert").textContent).toContain("Too large to send");

  // The next attempt is saved and accepted through the durable route.
  await sendFromComposer("Short");
  expect(screen.queryByRole("alert")).toBeNull();
  expect(durable.submissions).toHaveLength(1);
  expect(durable.saves.some((save) => save.text === "Short")).toBe(true);
  expect(socket.sent.filter((f) => f.type === "turn.send")).toEqual([]);
});

test("the session-cap refusal retains the local text", async () => {
  // This one was already guarded, through `draft` — the nearest channel to hand at the time.
  // It goes through `refused` with the rest now, so the restore is pinned rather than assumed.
  // A reload is what leaves three sessions live with none selected: viki.ts reopens every id in
  // vault.sessions' `live`, and `selectedId` starts null on a fresh page.
  const ids = ["s-1", "s-2", "s-3"];
  const { viki, socket } = fakeViki();
  render(<App durable={fakeDurable()} viki={viki} rest={fakeRest()} />);
  act(() => socket.open());
  act(() =>
    socket.receive({
      v: 2,
      generation: "11111111-1111-4111-8111-111111111111",
      request_id: "",
      type: "vault.sessions",
      session_id: "",
      seq: 1,
      data: { others: [], live: ids },
    }),
  );
  for (const id of ids) {
    act(() =>
      socket.receive({
        v: 2,
        generation: "11111111-1111-4111-8111-111111111111",
        request_id: "",
        type: "session.started",
        session_id: id,
        seq: 2,
        data: {
          model: "sonnet",
          mode: "auto",
          effort: "low",
          commands: [],
          agents: [],
          models: [],
          cli_version: "2.1.259",
          resumed: true,
        },
      }),
    );
  }

  await sendFromComposer("Text written for the fourth");
  expect(socket.sent.filter((f) => f.type === "turn.send")).toEqual([]);
  expect(screen.getByRole("alert")).toBeTruthy(); // the toast naming the three to close
  expect(composerBox().value).toBe("Text written for the fourth");
});

/* Card answers go to the durable ledger, never over the socket: one recorded choice
   per native request, whatever this window, another window or a reload does. A new session is
   opened here rather than a rail row, so these two cases test the card seam and nothing else. */

const GEN = "11111111-1111-4111-8111-111111111111";
const BINDING = {
  runtime_id: "33333333-3333-4333-8333-333333333333",
  request_id: "44444444-4444-4444-8444-444444444444",
  turn_id: "55555555-5555-4555-8555-555555555555",
};

function permissionFrame(sessionId: string, binding?: typeof BINDING) {
  return {
    v: 2,
    generation: GEN,
    request_id: "",
    type: "permission.requested",
    session_id: sessionId,
    seq: 1,
    data: {
      tool_use_id: "tu-1",
      kind: "tool",
      tool: "Write",
      summary: "30-knowledge/x.md",
      question: "May I rewrite this file?",
      reason: "",
      agent: "",
      plan: "",
    },
    ...(binding ? { native_request: binding } : {}),
  };
}

/** Open a live session the way the button does, and answer with its own id. */
function openNewSession(socket: ReturnType<typeof fakeViki>["socket"]): string {
  fireEvent.click(screen.getByText("New session"));
  const opened = socket.sent.find((frame) => frame.type === "session.open") as {
    session_id: string;
  };
  return opened.session_id;
}

test("a card answer goes through the approval ledger and shows the recorded state", async () => {
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  const { container } = render(<App durable={durable} viki={viki} rest={fakeRest()} />);
  act(() => socket.open());
  const sessionId = openNewSession(socket);
  act(() => socket.receive(permissionFrame(sessionId)));
  expect(container.querySelector(".card")).not.toBeNull();

  await act(async () => {
    fireEvent.click(screen.getByText("Allow once"));
  });
  await waitFor(() => expect(durable.approvals.size).toBe(1));
  expect([...durable.approvals.values()][0]).toMatchObject({
    choice_id: expect.stringMatching(/^[0-9a-f-]{36}$/),
    session_id: sessionId,
    generation: GEN,
    tool_use_id: "tu-1",
    native_request: {
      runtime_id: expect.any(String),
      request_id: expect.any(String),
      turn_id: expect.any(String),
    },
    choice: { kind: "permission", allow: true },
  });
  expect(socket.sent.filter((frame) => frame.type === "permission.respond")).toEqual([]);

  await waitFor(() =>
    expect(container.querySelector(".card-record")?.textContent).toContain(
      "Recorded choice: Allow once",
    ),
  );
  expect(container.querySelector(".card-record")?.textContent).toContain("Sent to Claude Code");
  // The opposite answer is not reachable from a card whose choice is recorded.
  expect(container.querySelector(".card-actions")).toBeNull();
  expect(screen.queryByText("Deny")).toBeNull();
});

test("a card another window already answered opens recorded, and records nothing more", async () => {
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  const { container } = render(<App durable={durable} viki={viki} rest={fakeRest()} />);
  act(() => socket.open());
  const sessionId = openNewSession(socket);
  const choiceId = "66666666-6666-4666-8666-666666666666";
  durable.approvals.set(choiceId, {
    choice_id: choiceId,
    session_id: sessionId,
    generation: GEN,
    tool_use_id: "tu-1",
    native_request: BINDING,
    choice: { kind: "permission", allow: false },
    state: "delivered_locally",
    reason: "",
    created_at: "2026-09-16T00:00:00.000Z",
    updated_at: "2026-09-16T00:00:00.000Z",
  });
  act(() => socket.receive(permissionFrame(sessionId, BINDING)));

  await waitFor(() =>
    expect(container.querySelector(".card-record")?.textContent).toContain("Recorded choice: Deny"),
  );
  expect(screen.queryByText("Allow once")).toBeNull();
  expect(durable.approvals.size).toBe(1);
});

test("a fresh subscription asks the ledger about every answer whose result was never heard", async () => {
  // The only way back from an ambiguous POST short of a reload: App keys on the subscription
  // request ID, which socket.ts allocates afresh on every (re)subscription, and calls
  // approvals.resynced for that session. Recovery is a GET on the retained ID — never a POST.
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  durable.recordApproval = async () => {
    throw new DurableApiError(0, "network_error");
  };
  const getApproval = vi.fn(async () => {
    throw new DurableApiError(503, "unavailable");
  });
  durable.getApproval = getApproval;
  const { container } = render(<App durable={durable} viki={viki} rest={fakeRest()} />);
  try {
    act(() => socket.open());
    const sessionId = openNewSession(socket);
    act(() => socket.receive(permissionFrame(sessionId, BINDING)));
    await act(async () => {
      fireEvent.click(screen.getByText("Allow once"));
    });
    // Unheard, so the ID is retained and the card stays answerable rather than latching shut.
    expect(durable.approvals.size).toBe(0);
    expect(getApproval).not.toHaveBeenCalled();
    expect((screen.getByText("Allow once") as HTMLButtonElement).disabled).toBe(false);
    expect(container.querySelector('.card-note[role="status"]')?.textContent).toContain(
      "The response delivery result is unknown",
    );
    const retained = JSON.parse(
      String(window.sessionStorage.getItem(`viki.approval.pending.${BINDING.request_id}`)),
    );
    expect(retained.choice_id).toMatch(/^[0-9a-f-]{36}$/);

    // Drop the socket and let its own backoff redial: the new subscription is what triggers the
    // lookup. Only the timer is faked, so nothing else about the run changes.
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    act(() => socket.onclose?.({ code: 1006 }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    await act(async () => {
      socket.open();
    });
    expect(getApproval).toHaveBeenCalledTimes(1);
    expect(getApproval).toHaveBeenCalledWith(retained.choice_id);
  } finally {
    vi.useRealTimers();
    window.sessionStorage.removeItem(`viki.approval.pending.${BINDING.request_id}`);
  }
});

test("the durable fake refuses a second choice under one ID exactly as the service would", async () => {
  // A fixture-integrity check, here because this file is where fakeDurable() stands in for the
  // approval service: a fake that compared fewer identifiers than the service would let a test
  // pass on a body the real ledger answers with 409 conflict.
  const durable = fakeDurable();
  const input = {
    choice_id: "66666666-6666-4666-8666-666666666666",
    session_id: "s1",
    generation: GEN,
    tool_use_id: "tu-1",
    native_request: BINDING,
    choice: { kind: "permission", allow: true } as const,
  };
  await durable.recordApproval(input);
  await expect(durable.recordApproval(input)).resolves.toMatchObject({
    state: "delivered_locally",
  });
  for (const field of ["runtime_id", "request_id", "turn_id"] as const) {
    await expect(
      durable.recordApproval({
        ...input,
        native_request: { ...BINDING, [field]: "99999999-9999-4999-8999-999999999999" },
      }),
    ).rejects.toMatchObject({ status: 409, code: "conflict" });
  }
  await expect(
    durable.recordApproval({ ...input, choice: { kind: "permission", allow: false } }),
  ).rejects.toMatchObject({ status: 409, code: "conflict" });
  expect(durable.approvals.size).toBe(1);
});
