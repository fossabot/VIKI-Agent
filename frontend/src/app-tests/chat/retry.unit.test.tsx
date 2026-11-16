/**
 * App-level wiring for Retry review, the composer's destination label, New session from a
 * non-chat view, and the blocked-reason handed to MediaComposer. Conversation.unit.test.tsx and
 * RetryReview.unit.test.tsx already pin the review's own contents; this file pins only what App
 * itself is responsible for supplying.
 */
import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, GENERATION, sessionRow } from "../../accept/m3b/fakes";
import { DurableApiError } from "../../clients/conversations/durableApi";
import type { AssetsApi } from "../../clients/media/assetsApi";
import { useReportUnsavedWork } from "../../state/unsavedWork";
import type { VaultApi } from "../../state/vaultApi";
import { fakeDurable } from "../../testDurable";
import type { Attachment } from "../../turnInput";

afterEach(() => {
  cleanup();
  sessionStorage.clear();
});

const AT = "2026-09-25T00:00:00.000Z";
const USAGE = { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_creation_tokens: 0 };

function composerBox(): HTMLTextAreaElement {
  return screen.getByLabelText<HTMLTextAreaElement>("Message");
}

async function openRow(container: HTMLElement, id: string): Promise<void> {
  const row = await waitFor(() => {
    const found = container.querySelector<HTMLElement>(`[data-session-id="${id}"]`);
    if (!found) throw new Error(`no row ${id}`);
    return found;
  });
  await act(async () => {
    fireEvent.click(row);
  });
}

async function sendFromComposer(text: string): Promise<void> {
  fireEvent.change(composerBox(), { target: { value: text } });
  await act(async () => {
    fireEvent.keyDown(composerBox(), { key: "Enter" });
  });
}

/** Drives a live turn with one user message straight to a failed status: enough for Conversation
 * to offer Retry…, with no tool rows to keep the review's own content out of this file's way. */
function failLiveTurn(
  socket: ReturnType<typeof fakeViki>["socket"],
  sessionId: string,
  status: "error" | "cancelled" = "error",
  attachments?: readonly Attachment[],
): void {
  act(() =>
    socket.receive({
      v: 2,
      generation: GENERATION,
      request_id: "",
      type: "user.message",
      session_id: sessionId,
      seq: 1,
      data: { text: "hi", command: "", at: AT, ...(attachments ? { attachments } : {}) },
    }),
  );
  act(() =>
    socket.receive({
      v: 2,
      generation: GENERATION,
      request_id: "",
      type: "turn.done",
      session_id: sessionId,
      seq: 2,
      data: { status, usage: USAGE, context_percent: null, duration_ms: 1 },
    }),
  );
}

function fakeVaultApi(): VaultApi {
  const pending = () => new Promise<never>(() => {});
  return {
    entries: pending,
    note: pending,
    backlinks: pending,
    history: pending,
    bookmarks: async () => ({ bookmarks: [] }),
    addBookmark: pending,
    removeBookmark: pending,
    createNote: pending,
    saveNote: pending,
    recovery: async () => [],
  };
}

function fakeAssetsApi(): AssetsApi {
  return { upload: () => new Promise(() => {}), read: () => new Promise(() => {}) };
}

const IMAGE: Attachment = {
  asset_id: "22222222-2222-4222-8222-222222222222",
  media_type: "image/png",
  byte_size: 100,
  width: 10,
  height: 10,
  name: "shot.png",
  modified_at: null,
  source: "picker",
};

test("Retry… opens the review, fills only an empty editor, and sends nothing until the user's own Send", async () => {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Foo" })] });
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  const { container } = render(<App durable={durable} viki={viki} rest={rest} />);
  act(() => socket.open());
  await openRow(container, "s1");
  failLiveTurn(socket, "s1");

  fireEvent.click(screen.getByRole("button", { name: "Retry…" }));
  const dialog = screen.getByRole("dialog");
  expect(dialog.textContent).toContain("Before you retry");

  const fill = screen.getByRole("button", { name: "Put the prompt back in the message box" });
  expect((fill as HTMLButtonElement).disabled).toBe(false);
  fireEvent.click(fill);

  expect(screen.queryByRole("dialog")).toBeNull();
  expect(composerBox().value).toBe("hi");
  expect(durable.submissions).toHaveLength(0);

  await act(async () => {
    fireEvent.keyDown(composerBox(), { key: "Enter" });
  });
  expect(durable.submissions).toHaveLength(1);
  expect(durable.submissions[0]).toMatchObject({ session_id: "s1" });
});

test("Retry rejoins a live command with its argument text, the way the live path split them", async () => {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Foo" })] });
  const { viki, socket } = fakeViki();
  const { container } = render(<App durable={fakeDurable()} viki={viki} rest={rest} />);
  act(() => socket.open());
  await openRow(container, "s1");
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
      data: { status: "error", usage: USAGE, context_percent: null, duration_ms: 1 },
    }),
  );

  fireEvent.click(screen.getByRole("button", { name: "Retry…" }));
  fireEvent.click(screen.getByRole("button", { name: "Put the prompt back in the message box" }));
  expect(composerBox().value).toBe("/review src/app.py");
});

test("Retry offers no silent fill for a snapshot-replayed prompt cut at the server's bound", async () => {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Foo" })] });
  const { viki, socket } = fakeViki();
  const { container } = render(<App durable={fakeDurable()} viki={viki} rest={rest} />);
  act(() => socket.open());
  await openRow(container, "s1");
  act(() =>
    socket.synchronize("s1", GENERATION, 2, {
      events: [
        { type: "user.message", data: { text: "hi", command: "", at: AT }, original_bytes: 999 },
        {
          type: "turn.done",
          data: { status: "error", usage: USAGE, context_percent: null, duration_ms: 1 },
        },
      ],
      truncated: false,
      state: {
        live: false,
        info: null,
        mode: "",
        blocked: null,
        closed: false,
        error: null,
        compacting: false,
        context_percent: null,
        rate_limit: null,
      },
    } as unknown as Parameters<typeof socket.synchronize>[3]),
  );

  fireEvent.click(screen.getByRole("button", { name: "Retry…" }));
  const fill = screen.getByRole("button", {
    name: "Put the prompt back in the message box",
  }) as HTMLButtonElement;
  expect(fill.disabled).toBe(true);
  expect(screen.getByRole("dialog").textContent).toContain(
    "Drafts and send history → Send history → Restore text",
  );
});

test("Retry names the incomplete notice when App's own view is truncated", async () => {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Foo" })] });
  const { viki, socket } = fakeViki();
  const { container } = render(<App durable={fakeDurable()} viki={viki} rest={rest} />);
  act(() => socket.open());
  await openRow(container, "s1");
  act(() =>
    socket.synchronize("s1", GENERATION, 2, {
      events: [
        { type: "user.message", data: { text: "hi", command: "", at: AT } },
        {
          type: "turn.done",
          data: { status: "error", usage: USAGE, context_percent: null, duration_ms: 1 },
        },
      ],
      truncated: true,
      state: {
        live: false,
        info: null,
        mode: "",
        blocked: null,
        closed: false,
        error: null,
        compacting: false,
        context_percent: null,
        rate_limit: null,
      },
    } as unknown as Parameters<typeof socket.synchronize>[3]),
  );

  fireEvent.click(screen.getByRole("button", { name: "Retry…" }));
  const dialog = screen.getByRole("dialog");
  expect(dialog.textContent).toContain("This list may be incomplete");
  expect(dialog.textContent).not.toContain("No tools ran in this turn.");
});

test("an open Retry review keeps the incomplete flag it captured when it opened", async () => {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Foo" })] });
  const { viki, socket } = fakeViki();
  const { container } = render(<App durable={fakeDurable()} viki={viki} rest={rest} />);
  act(() => socket.open());
  await openRow(container, "s1");
  const replay = (truncated: boolean) =>
    ({
      events: [
        { type: "user.message", data: { text: "hi", command: "", at: AT } },
        {
          type: "turn.done",
          data: { status: "error", usage: USAGE, context_percent: null, duration_ms: 1 },
        },
      ],
      truncated,
      state: {
        live: false,
        info: null,
        mode: "",
        blocked: null,
        closed: false,
        error: null,
        compacting: false,
        context_percent: null,
        rate_limit: null,
      },
    }) as unknown as Parameters<typeof socket.synchronize>[3];
  act(() => socket.synchronize("s1", GENERATION, 2, replay(false)));

  fireEvent.click(screen.getByRole("button", { name: "Retry…" }));
  const notice = "This list may be incomplete";
  expect(screen.getByRole("dialog").textContent).not.toContain(notice);

  act(() => socket.synchronize("s1", GENERATION, 2, replay(true)));
  expect(viki.store.getSnapshot().sessions.s1?.truncated).toBe(true);
  expect(screen.getByRole("dialog").textContent).not.toContain(notice);
});

test("a non-empty editor keeps the review's fill action disabled", async () => {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Foo" })] });
  const { viki, socket } = fakeViki();
  const { container } = render(<App durable={fakeDurable()} viki={viki} rest={rest} />);
  act(() => socket.open());
  await openRow(container, "s1");
  failLiveTurn(socket, "s1");
  fireEvent.change(composerBox(), { target: { value: "already writing something" } });

  fireEvent.click(screen.getByRole("button", { name: "Retry…" }));
  const fill = screen.getByRole("button", { name: "Put the prompt back in the message box" });
  expect((fill as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(fill);
  expect(composerBox().value).toBe("already writing something");
});

test("New session from the Vault view lands in Chat", async () => {
  const { viki, socket } = fakeViki();
  const { container } = render(
    <App durable={fakeDurable()} viki={viki} rest={fakeRest()} vaultApi={fakeVaultApi()} />,
  );
  act(() => socket.open());

  fireEvent.click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Vault" },
    ),
  );
  expect(container.querySelector('[data-region="conversation"]')).toBeNull();

  fireEvent.click(screen.getByText("New session"));
  expect(container.querySelector('[data-region="conversation"]')).not.toBeNull();
  expect(composerBox().value).toBe("");
});

test("the destination line names the selected session", async () => {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Foo" })] });
  const { viki, socket } = fakeViki();
  const { container } = render(
    <App durable={fakeDurable()} viki={viki} rest={rest} assets={fakeAssetsApi()} />,
  );
  act(() => socket.open());
  await waitFor(() =>
    expect(container.querySelector('[aria-label="Claude · New conversation"]')).not.toBeNull(),
  );

  await openRow(container, "s1");
  expect(container.querySelector(`[aria-label='Claude · "Foo"']`)).not.toBeNull();
});

test("the composer names the blocked reason while a previous send's result is unconfirmed", async () => {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Foo" })] });
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  durable.submitCommand = async (input) => {
    durable.submissions.push(input);
    throw new DurableApiError(503, "unavailable");
  };
  const { container } = render(<App durable={durable} viki={viki} rest={rest} />);
  act(() => socket.open());
  await openRow(container, "s1");

  await sendFromComposer("held up");
  expect(durable.submissions).toHaveLength(1);

  await act(async () => {
    fireEvent.keyDown(composerBox(), { key: "Enter" });
  });
  expect(container.querySelector(".composer-blocked")?.textContent).toBe(
    "The previous send is being confirmed. Use Check send result to see its state.",
  );
});

test("New session from a dirty Vault view asks first; cancelling sends nothing and stays", async () => {
  const { viki, socket } = fakeViki();
  render(<App durable={fakeDurable()} viki={viki} rest={fakeRest()} vaultApi={fakeVaultApi()} />);
  act(() => socket.open());
  fireEvent.click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Vault" },
    ),
  );
  const heading = screen.getByRole("heading", { level: 1 });

  // The Vault view reports its own dirty state through this same hook; this stands in for a
  // user mid-edit without needing the whole note-editing UI to raise it.
  renderHook(() => useReportUnsavedWork("vault", true));
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);

  fireEvent.click(screen.getByText("New session"));
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(viki.store.getSnapshot().sessions).toEqual({});
  expect(screen.getByRole("heading", { level: 1 })).toBe(heading);
});

test("an editor holding only an attachment, no text, keeps the review's fill action disabled", async () => {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Foo" })] });
  const { viki, socket } = fakeViki();
  const { container } = render(
    <App durable={fakeDurable()} viki={viki} rest={rest} assets={fakeAssetsApi()} />,
  );
  act(() => socket.open());
  await openRow(container, "s1");
  failLiveTurn(socket, "s1", "error", [IMAGE]);

  fireEvent.click(screen.getByRole("button", { name: "Retry…" }));
  fireEvent.click(screen.getByRole("button", { name: "Put the prompt back in the message box" }));
  expect(composerBox().value).toBe("hi");
  fireEvent.change(composerBox(), { target: { value: "" } });

  fireEvent.click(screen.getByRole("button", { name: "Retry…" }));
  const fill = screen.getByRole("button", { name: "Put the prompt back in the message box" });
  expect((fill as HTMLButtonElement).disabled).toBe(true);
});

test("Retry… fills both the text and the attachment when the failed turn had an image", async () => {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Foo" })] });
  const { viki, socket } = fakeViki();
  const { container } = render(
    <App durable={fakeDurable()} viki={viki} rest={rest} assets={fakeAssetsApi()} />,
  );
  act(() => socket.open());
  await openRow(container, "s1");
  failLiveTurn(socket, "s1", "error", [IMAGE]);

  fireEvent.click(screen.getByRole("button", { name: "Retry…" }));
  fireEvent.click(screen.getByRole("button", { name: "Put the prompt back in the message box" }));

  expect(composerBox().value).toBe("hi");
  expect(container.querySelector(".attachment-item-name")?.textContent).toBe("shot.png");
});

test("the destination line names the session's model once it is known", async () => {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Foo" })] });
  const { viki, socket } = fakeViki();
  const { container } = render(
    <App durable={fakeDurable()} viki={viki} rest={rest} assets={fakeAssetsApi()} />,
  );
  act(() => socket.open());
  await openRow(container, "s1");
  act(() =>
    socket.receive({
      v: 2,
      generation: GENERATION,
      request_id: "",
      type: "session.started",
      session_id: "s1",
      seq: 1,
      data: {
        model: "claude-sonnet-5",
        mode: "auto",
        effort: "high",
        commands: [],
        agents: [],
        models: [],
        cli_version: "2.1.259",
        resumed: false,
      },
    }),
  );
  expect(container.querySelector(`[aria-label='Claude · claude-sonnet-5 · "Foo"']`)).not.toBeNull();
});

test("the composer names the conflict reason once another window's draft wins the save", async () => {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Foo" })] });
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  durable.saveDraft = async () => {
    throw new DurableApiError(409, "conflict", 2);
  };
  const { container } = render(<App durable={durable} viki={viki} rest={rest} />);
  act(() => socket.open());
  await openRow(container, "s1");

  // The first Enter hits the conflict; the draft status line already names it, with its own
  // review button. What stays empty is `.composer-blocked`, which names a reason only once a
  // later Enter is itself refused — the second, now-blocked one below.
  await sendFromComposer("clash");
  await act(async () => {
    fireEvent.keyDown(composerBox(), { key: "Enter" });
  });
  expect(container.querySelector(".composer-blocked")?.textContent).toBe(
    "The draft changed in another window. Review the saved draft.",
  );
});

test("the composer names Sending while the draft save is still out", async () => {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Foo" })] });
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  durable.saveDraft = () => new Promise<never>(() => {});
  const { container } = render(<App durable={durable} viki={viki} rest={rest} />);
  act(() => socket.open());
  await openRow(container, "s1");

  await sendFromComposer("typing");
  await act(async () => {
    fireEvent.keyDown(composerBox(), { key: "Enter" });
  });
  expect(container.querySelector(".composer-blocked")?.textContent).toBe("Sending…");
});
