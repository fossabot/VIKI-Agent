/**
 * App-level reuse of an earlier prompt: ArrowUp recall and "Edit in a new chat". Both rejoin a
 * slash command with its argument text, and neither passes off a prompt this window holds only a
 * cut copy of (a snapshot item the server bounded) as the whole prompt.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, GENERATION, sessionRow } from "../../accept/m3b/fakes";
import type { ChatArchivesApi } from "../../clients/conversations/chatArchivesApi";
import type { AssetsApi } from "../../clients/media/assetsApi";
import { fakeDurable } from "../../testDurable";

afterEach(() => {
  cleanup();
  sessionStorage.clear();
});

const AT = "2026-09-25T00:00:00.000Z";
const USAGE = { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_creation_tokens: 0 };
const DONE = { status: "success", usage: USAGE, context_percent: null, duration_ms: 1 };
const IDLE_STATE = {
  live: false,
  info: null,
  mode: "",
  blocked: null,
  closed: false,
  error: null,
  compacting: false,
  context_percent: null,
  rate_limit: null,
};

function composerBox(): HTMLTextAreaElement {
  return screen.getByLabelText<HTMLTextAreaElement>("Message");
}

/** Edit in a new chat and Select for export moved off bare per-turn controls into each turn's
 * "More actions" menu — opens the nth turn's own menu before the real click. */
function openTurnMenu(turnIndex = 0): void {
  fireEvent.click(
    screen.getAllByRole("button", { name: "More actions" })[turnIndex] as HTMLElement,
  );
}

const archives: ChatArchivesApi = {
  list: async () => [],
  get: () => new Promise(() => {}),
  save: async () => {},
  remove: async () => {},
};
const assets: AssetsApi = {
  upload: () => new Promise(() => {}),
  read: () => new Promise(() => {}),
};

async function openSession(): Promise<ReturnType<typeof fakeViki>["socket"]> {
  return (await openSessionWithViki()).socket;
}

const S1 = sessionRow({ session_id: "s1", title: "Foo" });

/** Like `openSession`, but also hands back `viki` so a test can drive a second history page
 * (`loadOlder`) directly instead of simulating the scroll that triggers it in the real UI. */
async function openSessionWithViki(
  sessions: ReturnType<typeof sessionRow>[] = [S1],
): Promise<ReturnType<typeof fakeViki>> {
  const rest = fakeRest({ sessions });
  const { viki, socket } = fakeViki();
  const { container } = render(
    <App durable={fakeDurable()} viki={viki} rest={rest} archives={archives} assets={assets} />,
  );
  act(() => socket.open());
  const row = await waitFor(() => {
    const found = container.querySelector<HTMLElement>('[data-session-id="s1"]');
    if (!found) throw new Error("no row s1");
    return found;
  });
  await act(async () => {
    fireEvent.click(row);
  });
  return { viki, socket };
}

/** A live, finished turn whose prompt is a slash command with its argument text. */
async function wholeCommandTurn(): Promise<void> {
  const socket = await openSession();
  act(() =>
    socket.receive({
      v: 2,
      generation: GENERATION,
      request_id: "",
      type: "user.message",
      session_id: "s1",
      seq: 1,
      data: { text: "src/app.py", command: "/review", at: AT },
    }),
  );
  act(() =>
    socket.receive({
      v: 2,
      generation: GENERATION,
      request_id: "",
      type: "turn.done",
      session_id: "s1",
      seq: 2,
      data: DONE,
    }),
  );
}

/** A finished turn replayed from a snapshot whose prompt the server cut at its bound. */
async function cutTurn(sessions?: ReturnType<typeof sessionRow>[]): Promise<void> {
  const { socket } = await openSessionWithViki(sessions);
  act(() =>
    socket.synchronize("s1", GENERATION, 2, {
      events: [
        { type: "user.message", data: { text: "hi", command: "", at: AT }, original_bytes: 999 },
        { type: "turn.done", data: DONE },
      ],
      truncated: false,
      state: IDLE_STATE,
    } as unknown as Parameters<typeof socket.synchronize>[3]),
  );
}

test("ArrowUp recalls a whole prompt rejoined with its slash command", async () => {
  await wholeCommandTurn();
  fireEvent.keyDown(composerBox(), { key: "ArrowUp" });
  expect(composerBox().value).toBe("/review src/app.py");
});

test("ArrowUp recalls nothing when the newest prompt is a cut copy", async () => {
  await cutTurn();
  fireEvent.keyDown(composerBox(), { key: "ArrowUp" });
  expect(composerBox().value).toBe("");
});

test("Edit in a new chat opens a whole prompt rejoined with its slash command", async () => {
  await wholeCommandTurn();
  openTurnMenu();
  fireEvent.click(screen.getByRole("menuitem", { name: "Edit in a new chat" }));
  expect(composerBox().value).toBe("/review src/app.py");
  expect(screen.queryByRole("alert")).toBeNull();
});

test("Edit in a new chat refuses a cut prompt and says where the full text is", async () => {
  await cutTurn();
  openTurnMenu();
  fireEvent.click(screen.getByRole("menuitem", { name: "Edit in a new chat" }));
  expect(composerBox().value).toBe("");
  const sent = new Date(AT).toLocaleString("en-US");
  expect(document.querySelector(".composer-refusal")?.textContent).toBe(
    `Only this window's copy of this prompt was cut short (sent ${sent}). Drafts and send history → Send history → Restore text opens the full text in a new conversation's message box.`,
  );
});

test("Edit in a new chat on a whole prompt clears an earlier cut prompt's refusal", async () => {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Foo" })] });
  const { viki, socket } = fakeViki();
  const { container } = render(
    <App durable={fakeDurable()} viki={viki} rest={rest} archives={archives} assets={assets} />,
  );
  act(() => socket.open());
  const row = await waitFor(() => {
    const found = container.querySelector<HTMLElement>('[data-session-id="s1"]');
    if (!found) throw new Error("no row s1");
    return found;
  });
  await act(async () => {
    fireEvent.click(row);
  });
  act(() =>
    socket.synchronize("s1", GENERATION, 4, {
      events: [
        { type: "user.message", data: { text: "hi", command: "", at: AT }, original_bytes: 999 },
        { type: "turn.done", data: DONE },
        { type: "user.message", data: { text: "whole", command: "", at: AT } },
        { type: "turn.done", data: DONE },
      ],
      truncated: false,
      state: IDLE_STATE,
    } as unknown as Parameters<typeof socket.synchronize>[3]),
  );

  openTurnMenu(0);
  fireEvent.click(screen.getByRole("menuitem", { name: "Edit in a new chat" }));
  expect(document.querySelector(".composer-refusal")).not.toBeNull();

  openTurnMenu(1);
  fireEvent.click(screen.getByRole("menuitem", { name: "Edit in a new chat" }));
  expect(composerBox().value).toBe("whole");
  expect(screen.queryByRole("alert")).toBeNull();
});

/** A finished history-page turn cut at the reader's own length limit (sessionReducer.ts's
 * HISTORY_TEXT_LIMIT), with no recorded send time — a transcript entry the mapper read with no
 * timestamp (`mapper.py`'s `str(entry.get("timestamp", ""))`). */
async function historyCutTurn(): Promise<ReturnType<typeof fakeViki>["socket"]> {
  const socket = await openSession();
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
      data: {
        events: [
          { type: "user.message", data: { text: "x".repeat(16384), command: "", at: "" } },
          { type: "turn.done", data: DONE },
        ],
        next_before_uuid: null,
      },
    }),
  );
  return socket;
}

test("Edit in a new chat refuses a cut history prompt, naming both routes with no invalid send time", async () => {
  await historyCutTurn();
  openTurnMenu();
  fireEvent.click(screen.getByRole("menuitem", { name: "Edit in a new chat" }));
  expect(composerBox().value).toBe("");
  expect(document.querySelector(".composer-refusal")?.textContent).toBe(
    "Only this window's copy of this prompt was cut short. Open its full text: Drafts and send history → Send history → Restore text for a prompt sent from this app, Open conversation record for an earlier conversation.",
  );
});

/** A finished history-page turn at ordinary length: not cut, so the Open conversation record note
 * this card adds must not appear for it. */
async function historyWholeTurn(): Promise<ReturnType<typeof fakeViki>["socket"]> {
  const socket = await openSession();
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
      data: {
        events: [
          { type: "user.message", data: { text: "hi", command: "", at: AT } },
          { type: "turn.done", data: DONE },
        ],
        next_before_uuid: null,
      },
    }),
  );
  return socket;
}

test("App shows Open conversation record for a view holding a cut history turn", async () => {
  await historyCutTurn();
  expect(screen.getByRole("button", { name: "Open conversation record" })).not.toBeNull();
});

test("App shows no Open conversation record for a view whose history turn is not cut", async () => {
  await historyWholeTurn();
  expect(screen.queryByRole("button", { name: "Open conversation record" })).toBeNull();
});

/** History pages prepend (sessionReducer.ts's applyHistoryPage): a cut history turn from an
 * earlier page can still be in the view once a later, older page comes back transcript_missing.
 * That note already says the record is gone, so the button this card adds must not also offer to
 * open it. */
test("App shows the missing-record copy, not Open conversation record, once an older page is transcript_missing", async () => {
  const { viki, socket } = await openSessionWithViki();
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
      data: {
        events: [
          { type: "user.message", data: { text: "x".repeat(16384), command: "", at: "" } },
          { type: "turn.done", data: DONE },
        ],
        next_before_uuid: "cursor1",
      },
    }),
  );
  // Drives the older-page request directly (as state/viki.unit.test.ts does), rather than
  // simulating the scroll that triggers it in the real UI.
  act(() => viki.actions.loadOlder("s1"));
  const second = socket.sent.filter((f) => f.type === "history.more")[1] as {
    generation: string;
    request_id: string;
  };
  act(() =>
    socket.receive({
      v: 2,
      generation: second.generation,
      request_id: second.request_id,
      type: "history.page",
      session_id: "s1",
      seq: 2,
      data: { events: [], next_before_uuid: null, error: "transcript_missing" },
    }),
  );
  expect(screen.queryByRole("button", { name: "Open conversation record" })).toBeNull();
  expect(
    screen.getByText(
      "Can't find the conversation record. Claude Code may have deleted it or moved it.",
    ),
  ).not.toBeNull();
});

test("Edit in a new chat on a cut history turn drops Open conversation record once an older page is transcript_missing", async () => {
  const { viki, socket } = await openSessionWithViki();
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
      data: {
        events: [
          { type: "user.message", data: { text: "x".repeat(16384), command: "", at: "" } },
          { type: "turn.done", data: DONE },
        ],
        next_before_uuid: "cursor1",
      },
    }),
  );
  act(() => viki.actions.loadOlder("s1"));
  const second = socket.sent.filter((f) => f.type === "history.more")[1] as {
    generation: string;
    request_id: string;
  };
  act(() =>
    socket.receive({
      v: 2,
      generation: second.generation,
      request_id: second.request_id,
      type: "history.page",
      session_id: "s1",
      seq: 2,
      data: { events: [], next_before_uuid: null, error: "transcript_missing" },
    }),
  );
  openTurnMenu();
  fireEvent.click(screen.getByRole("menuitem", { name: "Edit in a new chat" }));
  expect(document.querySelector(".composer-refusal")?.textContent).toBe(
    "Only this window's copy of this prompt was cut short. Open its full text: Drafts and send history → Send history → Restore text for a prompt sent from this app.",
  );
});

test("selecting a cut history turn for export drops Open conversation record once an older page is transcript_missing", async () => {
  const { viki, socket } = await openSessionWithViki();
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
      data: {
        events: [
          { type: "user.message", data: { text: "x".repeat(16384), command: "", at: "" } },
          { type: "turn.done", data: DONE },
        ],
        next_before_uuid: "cursor1",
      },
    }),
  );
  act(() => viki.actions.loadOlder("s1"));
  const second = socket.sent.filter((f) => f.type === "history.more")[1] as {
    generation: string;
    request_id: string;
  };
  act(() =>
    socket.receive({
      v: 2,
      generation: second.generation,
      request_id: second.request_id,
      type: "history.page",
      session_id: "s1",
      seq: 2,
      data: { events: [], next_before_uuid: null, error: "transcript_missing" },
    }),
  );
  openTurnMenu();
  fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Select for export" }));
  expect(
    screen.getByText(
      "A selected turn's prompt is cut short in this window, so this selection cannot be exported. Unselect that turn, or open its full text: Drafts and send history → Send history → Restore text for a prompt sent from this app.",
    ),
  ).not.toBeNull();
  expect(screen.queryByText(/Open conversation record for an earlier conversation/)).toBeNull();
});

test("selecting another session clears a cut-prompt refusal (a live cut turn shows no Open conversation record)", async () => {
  await cutTurn([S1, sessionRow({ session_id: "s2", title: "Bar" })]);
  openTurnMenu();
  fireEvent.click(screen.getByRole("menuitem", { name: "Edit in a new chat" }));
  expect(document.querySelector(".composer-refusal")).not.toBeNull();
  // The cut turn is live, not a history turn: the record-open note is for history pages only.
  expect(screen.queryByRole("button", { name: "Open conversation record" })).toBeNull();

  const row2 = document.querySelector<HTMLElement>('[data-session-id="s2"]');
  if (!row2) throw new Error("no row s2");
  await act(async () => {
    fireEvent.click(row2);
  });
  expect(document.querySelector(".composer-refusal")).toBeNull();
});

test("clicking a cut history page's Open conversation record opens that session's transcript", async () => {
  const socket = await historyCutTurn();
  fireEvent.click(screen.getByRole("button", { name: "Open conversation record" }));
  expect(
    socket.sent.some((frame) => frame.type === "open.transcript" && frame.session_id === "s1"),
  ).toBe(true);
});

test("a truncated snapshot plus a cut history page shows one button and no cut-prompt note", async () => {
  // The cut history page lands first (as in historyCutTurn): a snapshot resets historyRequestId,
  // which would drop a history.page sent for an earlier request.
  const socket = await historyCutTurn();
  act(() =>
    socket.synchronize("s1", GENERATION, 3, {
      events: [
        { type: "user.message", data: { text: "hi", command: "", at: AT } },
        { type: "turn.done", data: DONE },
      ],
      truncated: true,
      state: IDLE_STATE,
    } as unknown as Parameters<typeof socket.synchronize>[3]),
  );
  expect(screen.getAllByRole("button", { name: "Open conversation record" })).toHaveLength(1);
  expect(
    screen.queryByText("This window cuts off part of a long sent message partway through."),
  ).toBeNull();
});

test("a cut history page then a busy older page still shows one Open conversation record button", async () => {
  const { viki, socket } = await openSessionWithViki();
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
      data: {
        events: [
          { type: "user.message", data: { text: "x".repeat(16384), command: "", at: "" } },
          { type: "turn.done", data: DONE },
        ],
        next_before_uuid: "cursor1",
      },
    }),
  );
  act(() => viki.actions.loadOlder("s1"));
  const second = socket.sent.filter((f) => f.type === "history.more")[1] as {
    generation: string;
    request_id: string;
  };
  act(() =>
    socket.receive({
      v: 2,
      generation: second.generation,
      request_id: second.request_id,
      type: "history.page",
      session_id: "s1",
      seq: 2,
      data: { events: [], next_before_uuid: null, error: "busy" },
    }),
  );
  expect(screen.getAllByRole("button", { name: "Open conversation record" })).toHaveLength(1);
});
