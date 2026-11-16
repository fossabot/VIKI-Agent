import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, expect, test, vi } from "vitest";
import type {
  ConversationSearchApi,
  NativeSearchHit,
} from "../../clients/conversations/conversationSearchApi";
import type { HistoryItem } from "../../protocol";
import { applyEnvelope, applyHistoryPage } from "../../state/sessionReducer";
import { emptyView, type SessionView } from "../../state/view";
import { Conversation } from "../Conversation/Conversation";
import { RevisionSearch } from "./RevisionSearch";

function liveView(text: string): SessionView {
  const built = applyHistoryPage(emptyView("s"), {
    events: [{ type: "user.message", data: { text, command: "", at: "" } }],
    next_before_uuid: null,
  });
  return {
    ...built,
    generation: "g",
    turns: built.turns.map((turn) => ({ ...turn, history: false })),
  };
}
const emptyResult = async (
  id: string,
): Promise<Awaited<ReturnType<ConversationSearchApi["search"]>>> => ({
  request_id: id,
  hits: [],
  state: "ready",
  reason: "",
  coverage: "complete",
});

// The real crypto.subtle.digest settles on its own native schedule, outside the fake-timer
// clock; this stub resolves as a plain microtask while staying content-dependent, so a test that
// checks two hashes still tell equal from changed input apart.
function contentDigest(data: BufferSource): ArrayBuffer {
  const bytes = new Uint8Array(data as ArrayBuffer);
  const digest = new Uint8Array(32);
  bytes.forEach((byte, index) => {
    const slot = index % 32;
    digest[slot] = ((digest[slot] ?? 0) + byte * (index + 1)) & 0xff;
  });
  return digest.buffer;
}

function stubContentDigest(): void {
  vi.spyOn(crypto.subtle, "digest").mockImplementation(async (_algorithm, data) =>
    contentDigest(data),
  );
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  // A digest spy created with vi.spyOn must not leak into the next test: restoring it only at
  // the end of a test body skips the restore when an earlier assertion in that body throws.
  vi.restoreAllMocks();
});

const hit: NativeSearchHit = {
  role: "user",
  at: "",
  snapshot: { before: "saved ", match: "needle", after: " snapshot" },
  anchor: {
    kind: "native_entry",
    entry_uuid: "id",
    mapped_index: 0,
    source_revision: "a".repeat(64),
  },
};

test("stale native selection retains the snapshot without replacing history", async () => {
  vi.useFakeTimers();
  const onPage = vi.fn();
  const api: ConversationSearchApi = {
    search: vi.fn<ConversationSearchApi["search"]>(async (_sid, _query, id) => ({
      hits: [hit],
      state: "ready",
      reason: "",
      coverage: "complete",
      request_id: id,
    })),
    anchor: vi.fn<ConversationSearchApi["anchor"]>(async (_sid, _anchor, id) => ({
      request_id: id,
      state: "changed",
      reason: "source_changed",
      events: [],
      anchor_turn_index: null,
      older_before_uuid: null,
      newer_after_uuid: null,
      coverage: "complete",
    })),
  };
  render(<RevisionSearch api={api} view={emptyView("s")} onNativePage={onPage} onJump={vi.fn()} />);
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "needle" } });
  await act(() => vi.advanceTimersByTimeAsync(250));
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "saved needle snapshot" }));
  });
  expect(screen.getByText("The source changed. The search snapshot is kept below.")).toBeTruthy();
  expect(onPage).not.toHaveBeenCalled();
  expect(screen.getAllByText("needle").length).toBe(2);
});

test("a time-budget anchor error shows its own copy, not the generic failure", async () => {
  vi.useFakeTimers();
  const onPage = vi.fn();
  const api: ConversationSearchApi = {
    search: vi.fn<ConversationSearchApi["search"]>(async (_sid, _query, id) => ({
      hits: [hit],
      state: "ready",
      reason: "",
      coverage: "complete",
      request_id: id,
    })),
    anchor: vi.fn<ConversationSearchApi["anchor"]>(async (_sid, _anchor, id) => ({
      request_id: id,
      state: "error",
      reason: "time_budget",
      events: [],
      anchor_turn_index: null,
      older_before_uuid: null,
      newer_after_uuid: null,
      coverage: "complete",
    })),
  };
  render(<RevisionSearch api={api} view={emptyView("s")} onNativePage={onPage} onJump={vi.fn()} />);
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "needle" } });
  await act(() => vi.advanceTimersByTimeAsync(250));
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "saved needle snapshot" }));
  });
  expect(screen.getByText("This could not be loaded in time.")).toBeTruthy();
  expect(onPage).not.toHaveBeenCalled();
});

test("new query aborts the previous request and ignores its late result", async () => {
  vi.useFakeTimers();
  let complete: ((value: Awaited<ReturnType<ConversationSearchApi["search"]>>) => void) | undefined;
  let signal: AbortSignal | undefined;
  const api: ConversationSearchApi = {
    search: vi.fn<ConversationSearchApi["search"]>((_sid, query, id, supplied) => {
      if (query === "first") {
        signal = supplied;
        return new Promise((resolve) => {
          complete = resolve;
        });
      }
      return Promise.resolve({
        request_id: id,
        hits: [],
        state: "ready",
        reason: "",
        coverage: "complete",
      });
    }),
    anchor: vi.fn(),
  };
  render(
    <RevisionSearch api={api} view={emptyView("s")} onNativePage={vi.fn()} onJump={vi.fn()} />,
  );
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "first" } });
  await act(() => vi.advanceTimersByTimeAsync(250));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "second" } });
  expect(signal?.aborted).toBe(true);
  await act(() => vi.advanceTimersByTimeAsync(250));
  await act(async () =>
    complete?.({
      request_id: "stale",
      hits: [hit],
      state: "ready",
      reason: "",
      coverage: "complete",
    }),
  );
  expect(screen.queryByText("needle")).toBeNull();
});

test("a live envelope re-runs only the app-hit search, never the native request", async () => {
  vi.useFakeTimers();
  stubContentDigest();
  const api: ConversationSearchApi = {
    search: vi.fn<ConversationSearchApi["search"]>((_sid, _q, id) => emptyResult(id)),
    anchor: vi.fn(),
  };
  let view = liveView("a needle");
  const { rerender } = render(
    <RevisionSearch api={api} view={view} onNativePage={vi.fn()} onJump={vi.fn()} />,
  );
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "needle" } });
  await act(() => vi.advanceTimersByTimeAsync(250));
  expect(api.search).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("button", { name: /needle/ })).toBeTruthy();
  for (let tick = 0; tick < 10; tick += 1) {
    view = { ...view, seq: view.seq + 1 };
    rerender(<RevisionSearch api={api} view={view} onNativePage={vi.fn()} onJump={vi.fn()} />);
    await act(() => vi.advanceTimersByTimeAsync(100));
  }
  expect(api.search).toHaveBeenCalledTimes(1);
  // A single envelope followed by a quiet gap longer than the debounce: the native search must
  // not restart just because the app-hit search's own timer would have fired here.
  view = { ...view, seq: view.seq + 1 };
  rerender(<RevisionSearch api={api} view={view} onNativePage={vi.fn()} onJump={vi.fn()} />);
  await act(() => vi.advanceTimersByTimeAsync(300));
  expect(api.search).toHaveBeenCalledTimes(1);
});

test("an app hit revalidates against the current view instead of an object identity check", async () => {
  vi.useFakeTimers();
  stubContentDigest();
  const view = liveView("a needle");
  const api: ConversationSearchApi = {
    search: vi.fn<ConversationSearchApi["search"]>((_sid, _q, id) => emptyResult(id)),
    anchor: vi.fn(),
  };
  const onJump = vi.fn();
  const { rerender } = render(
    <RevisionSearch api={api} view={view} onNativePage={vi.fn()} onJump={onJump} />,
  );
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "needle" } });
  await act(() => vi.advanceTimersByTimeAsync(250));
  const button = screen.getByRole("button", { name: /needle/ });
  vi.useRealTimers();
  fireEvent.click(button);
  // A fresh view object with identical content commits before the hash settles — the same
  // shape a live envelope produces mid-lookup.
  rerender(<RevisionSearch api={api} view={{ ...view }} onNativePage={vi.fn()} onJump={onJump} />);
  await waitFor(() => expect(onJump).toHaveBeenCalledWith(view.turns[0]?.id));
  expect(screen.queryByText("The source changed. The search snapshot is kept below.")).toBeNull();
});

test("an app hit whose text actually changed still reports source_changed", async () => {
  vi.useFakeTimers();
  stubContentDigest();
  const view = liveView("a needle");
  const api: ConversationSearchApi = {
    search: vi.fn<ConversationSearchApi["search"]>((_sid, _q, id) => emptyResult(id)),
    anchor: vi.fn(),
  };
  const onJump = vi.fn();
  const { rerender } = render(
    <RevisionSearch api={api} view={view} onNativePage={vi.fn()} onJump={onJump} />,
  );
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "needle" } });
  await act(() => vi.advanceTimersByTimeAsync(250));
  const button = screen.getByRole("button", { name: /needle/ });
  const changed: SessionView = {
    ...view,
    turns: view.turns.map((turn) => ({
      ...turn,
      user: turn.user && { ...turn.user, text: "changed" },
    })),
  };
  rerender(<RevisionSearch api={api} view={changed} onNativePage={vi.fn()} onJump={onJump} />);
  await act(async () => {
    fireEvent.click(button);
    await vi.advanceTimersByTimeAsync(0);
  });
  expect(onJump).not.toHaveBeenCalled();
  expect(screen.getByText("The source changed. The search snapshot is kept below.")).toBeTruthy();
});

function liveEvents(items: HistoryItem[], start = emptyView("s1")): SessionView {
  return items.reduce(
    (current, item) =>
      applyEnvelope(current, {
        v: 2,
        session_id: current.sessionId,
        generation: "g",
        request_id: "r",
        seq: current.seq + 1,
        ...item,
      }),
    start,
  );
}

function SearchJumpHarness({ current, api }: { current: SessionView; api: ConversationSearchApi }) {
  const [jumpTo, setTurnJump] = useState<{ turnId: string; request: number }>();
  return (
    <>
      <RevisionSearch
        api={api}
        view={current}
        onNativePage={() => false}
        onJump={(turnId) => setTurnJump({ turnId, request: Date.now() })}
      />
      <div className="conversation-scroll">
        <Conversation view={current} jumpTo={jumpTo} nowMs={0} onOpenUrl={vi.fn()} />
      </div>
    </>
  );
}

test.each([true, false])(
  "a committed live update during hashing focuses only unchanged searched content (changed=%s)",
  async (changed) => {
    vi.useFakeTimers();
    stubContentDigest();
    const api: ConversationSearchApi = {
      search: vi.fn<ConversationSearchApi["search"]>((_sid, _query, id) => emptyResult(id)),
      anchor: vi.fn(),
    };
    const original = liveEvents([
      {
        type: "user.message",
        data: { text: "prompt", command: "", at: "2026-09-12T09:05:00.000Z" },
      },
      { type: "turn.started", data: { turn_id: "t1" } },
      { type: "text.delta", data: { block_id: "b1", text: "assistant needle" } },
    ]);
    const ui = render(<SearchJumpHarness api={api} current={original} />);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "needle" } });
    await act(() => vi.advanceTimersByTimeAsync(250));
    let release: (() => void) | undefined;
    vi.mocked(crypto.subtle.digest).mockImplementationOnce(
      (_algorithm, data) =>
        new Promise<ArrayBuffer>((resolve) => {
          release = () => resolve(contentDigest(data));
        }),
    );
    fireEvent.click(screen.getByRole("button", { name: "assistant needle" }));
    expect(release).toBeTypeOf("function");
    const turnElement = ui.container.querySelector<HTMLElement>('[data-turn-id="t1"]');
    if (!turnElement) throw new Error("missing live turn");
    const focus = vi.spyOn(turnElement, "focus");
    const text = changed ? "The final answer has changed." : "assistant needle";
    const next = liveEvents([{ type: "text.done", data: { block_id: "b1", text } }], original);
    expect(next.seq).toBe(original.seq + 1);
    expect(next.turns[0]).not.toBe(original.turns[0]);
    ui.rerender(<SearchJumpHarness api={api} current={next} />);
    expect(turnElement.querySelector(".md")?.textContent).toBe(text);
    await act(async () => {
      release?.();
      await Promise.resolve();
      await Promise.resolve();
    });
    const notice = screen.queryByText("The source changed. The search snapshot is kept below.");
    if (changed) {
      expect(focus).not.toHaveBeenCalled();
      expect(turnElement.classList.contains("turn-search-target")).toBe(false);
      expect(notice).toBeTruthy();
      expect(screen.getAllByText("needle")).toHaveLength(2);
    } else {
      expect(focus).toHaveBeenCalledExactlyOnceWith({ preventScroll: true });
      expect(turnElement.classList.contains("turn-search-target")).toBe(true);
      expect(notice).toBeNull();
    }
  },
);

test("a click while already composing does nothing", async () => {
  vi.useFakeTimers();
  const api: ConversationSearchApi = {
    search: vi.fn<ConversationSearchApi["search"]>(async (_sid, _q, id) => ({
      request_id: id,
      hits: [hit],
      state: "ready",
      reason: "",
      coverage: "complete",
    })),
    anchor: vi.fn<ConversationSearchApi["anchor"]>(async (_sid, _anchor, id) => ({
      request_id: id,
      state: "changed",
      reason: "source_changed",
      events: [],
      anchor_turn_index: null,
      older_before_uuid: null,
      newer_after_uuid: null,
      coverage: "complete",
    })),
  };
  render(
    <RevisionSearch api={api} view={emptyView("s")} onNativePage={vi.fn()} onJump={vi.fn()} />,
  );
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "needle" } });
  await act(() => vi.advanceTimersByTimeAsync(250));
  await act(async () =>
    fireEvent.click(screen.getByRole("button", { name: "saved needle snapshot" })),
  );
  expect(api.anchor).toHaveBeenCalledTimes(1);
  // The retry button, unlike the hit list, survives composition clearing the search results.
  const retry = screen.getByRole("button", { name: "Try again" });
  fireEvent.compositionStart(screen.getByRole("textbox"));
  await act(async () => fireEvent.click(retry));
  expect(api.anchor).toHaveBeenCalledTimes(1);
});

test("composition starting mid-lookup discards a native anchor result too", async () => {
  vi.useFakeTimers();
  let resolveAnchor:
    | ((value: Awaited<ReturnType<ConversationSearchApi["anchor"]>>) => void)
    | undefined;
  const api: ConversationSearchApi = {
    search: vi.fn<ConversationSearchApi["search"]>(async (_sid, _q, id) => ({
      request_id: id,
      hits: [hit],
      state: "ready",
      reason: "",
      coverage: "complete",
    })),
    anchor: vi.fn(
      () =>
        new Promise<Awaited<ReturnType<ConversationSearchApi["anchor"]>>>((resolve) => {
          resolveAnchor = resolve;
        }),
    ),
  };
  const onJump = vi.fn();
  const onPage = vi.fn();
  render(<RevisionSearch api={api} view={emptyView("s")} onNativePage={onPage} onJump={onJump} />);
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "needle" } });
  await act(() => vi.advanceTimersByTimeAsync(250));
  const button = screen.getByRole("button", { name: "saved needle snapshot" });
  vi.useRealTimers();
  fireEvent.click(button);
  // Composition starts while the lookup is still in flight.
  fireEvent.compositionStart(screen.getByRole("textbox"));
  resolveAnchor?.({
    request_id: "x",
    state: "ready",
    reason: "",
    events: [{ type: "user.message", data: { text: "found", command: "", at: "" } }],
    anchor_turn_index: 0,
    older_before_uuid: null,
    newer_after_uuid: null,
    coverage: "complete",
  });
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(onPage).not.toHaveBeenCalled();
  expect(onJump).not.toHaveBeenCalled();
});

test("composition starting mid-lookup discards an app hit's result too", async () => {
  vi.useFakeTimers();
  stubContentDigest();
  const view = liveView("a needle");
  const api: ConversationSearchApi = {
    search: vi.fn<ConversationSearchApi["search"]>((_sid, _q, id) => emptyResult(id)),
    anchor: vi.fn(),
  };
  const onJump = vi.fn();
  render(<RevisionSearch api={api} view={view} onNativePage={vi.fn()} onJump={onJump} />);
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "needle" } });
  await act(() => vi.advanceTimersByTimeAsync(250));
  const button = screen.getByRole("button", { name: /needle/ });
  vi.useRealTimers();
  fireEvent.click(button);
  fireEvent.compositionStart(screen.getByRole("textbox"));
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(onJump).not.toHaveBeenCalled();
  expect(screen.queryByText("The source changed. The search snapshot is kept below.")).toBeNull();
});

test("compositionStart clears both the native and app hit lists", async () => {
  vi.useFakeTimers();
  stubContentDigest();
  const view = liveView("a needle");
  const api: ConversationSearchApi = {
    search: vi.fn<ConversationSearchApi["search"]>(async (_sid, _q, id) => ({
      request_id: id,
      hits: [hit],
      state: "ready",
      reason: "",
      coverage: "complete",
    })),
    anchor: vi.fn(),
  };
  render(<RevisionSearch api={api} view={view} onNativePage={vi.fn()} onJump={vi.fn()} />);
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "needle" } });
  await act(() => vi.advanceTimersByTimeAsync(250));
  expect(screen.getAllByRole("button", { name: /needle/ }).length).toBe(2);
  fireEvent.compositionStart(screen.getByRole("textbox"));
  expect(screen.queryByRole("button", { name: /needle/ })).toBeNull();
});

test("an unmount with a pending app-hit timer makes no digest call", async () => {
  vi.useFakeTimers();
  const digest = vi.spyOn(crypto.subtle, "digest");
  const view = liveView("a needle");
  const api: ConversationSearchApi = {
    search: vi.fn<ConversationSearchApi["search"]>((_sid, _q, id) => emptyResult(id)),
    anchor: vi.fn(),
  };
  const { unmount } = render(
    <RevisionSearch api={api} view={view} onNativePage={vi.fn()} onJump={vi.fn()} />,
  );
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "needle" } });
  unmount();
  await act(() => vi.advanceTimersByTimeAsync(250));
  expect(digest).not.toHaveBeenCalled();
});

test("a partial native result shows the tail, response-budget or time-budget copy", async () => {
  vi.useFakeTimers();
  let state: "tail" | "complete" | "time_budget" = "tail";
  const api: ConversationSearchApi = {
    search: vi.fn<ConversationSearchApi["search"]>(async (_sid, _q, id) => ({
      request_id: id,
      hits: [],
      state: "partial",
      reason:
        state === "tail" ? "tail_budget" : state === "complete" ? "response_budget" : "time_budget",
      coverage: state === "tail" ? "tail" : "complete",
    })),
    anchor: vi.fn(),
  };
  const { rerender } = render(
    <RevisionSearch api={api} view={emptyView("s")} onNativePage={vi.fn()} onJump={vi.fn()} />,
  );
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "needle" } });
  await act(() => vi.advanceTimersByTimeAsync(250));
  expect(screen.getByText("Older history is outside the bounded search window.")).toBeTruthy();
  state = "complete";
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "needle2" } });
  await act(() => vi.advanceTimersByTimeAsync(250));
  rerender(
    <RevisionSearch api={api} view={emptyView("s")} onNativePage={vi.fn()} onJump={vi.fn()} />,
  );
  expect(screen.getByText("More matches exist than this search can display.")).toBeTruthy();
  // A complete-coverage partial result claims more matches only when the reason is
  // response_budget: a time-budget cutoff gets its own copy even with coverage complete.
  state = "time_budget";
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "needle3" } });
  await act(() => vi.advanceTimersByTimeAsync(250));
  rerender(
    <RevisionSearch api={api} view={emptyView("s")} onNativePage={vi.fn()} onJump={vi.fn()} />,
  );
  expect(screen.getByText("Search stopped after 2 seconds; showing what it found.")).toBeTruthy();
  expect(screen.queryByText("More matches exist than this search can display.")).toBeNull();
});

test("a tail-coverage partial result adds the tail-window explanation as a second sentence", async () => {
  vi.useFakeTimers();
  let reason: "response_budget" | "time_budget" = "response_budget";
  let hits: NativeSearchHit[] = [hit];
  const api: ConversationSearchApi = {
    search: vi.fn<ConversationSearchApi["search"]>(async (_sid, _q, id) => ({
      request_id: id,
      hits,
      state: "partial",
      reason,
      coverage: "tail",
    })),
    anchor: vi.fn(),
  };
  const { rerender } = render(
    <RevisionSearch api={api} view={emptyView("s")} onNativePage={vi.fn()} onJump={vi.fn()} />,
  );
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "needle" } });
  await act(() => vi.advanceTimersByTimeAsync(250));
  expect(screen.getByRole("status").textContent).toBe(
    "More matches exist than this search can display. Older history is outside the bounded search window.",
  );
  expect(screen.getAllByRole("listitem")).toHaveLength(1);
  reason = "time_budget";
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "needle2" } });
  await act(() => vi.advanceTimersByTimeAsync(250));
  rerender(
    <RevisionSearch api={api} view={emptyView("s")} onNativePage={vi.fn()} onJump={vi.fn()} />,
  );
  expect(screen.getByRole("status").textContent).toBe(
    "Search stopped after 2 seconds; showing what it found. Older history is outside the bounded search window.",
  );
  // This case really has no hits, unlike the two above, so the empty state is exercised too.
  hits = [];
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "needle3" } });
  await act(() => vi.advanceTimersByTimeAsync(250));
  rerender(
    <RevisionSearch api={api} view={emptyView("s")} onNativePage={vi.fn()} onJump={vi.fn()} />,
  );
  expect(screen.getByRole("status").textContent).toBe(
    "Search stopped after 2 seconds; showing what it found. Older history is outside the bounded search window.",
  );
  expect(screen.queryAllByRole("listitem")).toHaveLength(0);
  expect(screen.queryByText("No matches.")).toBeNull();
});

test("a query typed during a live stream is throttled, not starved: the hit appears within 250 ms, and no-matches never shows", async () => {
  vi.useFakeTimers();
  // The revision hash itself is not under test here; a real crypto.subtle.digest settles on
  // its own native schedule, outside the fake timer clock, which is exactly the raciness this
  // stub removes.
  vi.spyOn(crypto.subtle, "digest").mockResolvedValue(new ArrayBuffer(32));
  const api: ConversationSearchApi = {
    search: vi.fn<ConversationSearchApi["search"]>((_sid, _q, id) => emptyResult(id)),
    anchor: vi.fn(),
  };
  let view = liveView("a needle");
  const { rerender } = render(
    <RevisionSearch api={api} view={view} onNativePage={vi.fn()} onJump={vi.fn()} />,
  );
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "needle" } });
  // One envelope every 100 ms for 3 s: a steady live stream that never gives a debounce a quiet
  // gap to fire in.
  for (let tick = 0; tick < 30; tick += 1) {
    view = { ...view, seq: view.seq + 1 };
    rerender(<RevisionSearch api={api} view={view} onNativePage={vi.fn()} onJump={vi.fn()} />);
    await act(() => vi.advanceTimersByTimeAsync(100));
    expect(screen.queryByText("No matches.")).toBeNull();
    if (tick === 2) expect(screen.getByRole("button", { name: /needle/ })).toBeTruthy();
  }
  expect(screen.getByRole("button", { name: /needle/ })).toBeTruthy();
  expect(api.search).toHaveBeenCalledTimes(1);
});

test("a superseded app-hit search never lands a stale local result", async () => {
  vi.useFakeTimers();
  const view = liveView("a needle");
  const api: ConversationSearchApi = {
    search: vi.fn<ConversationSearchApi["search"]>((_sid, _q, id) => emptyResult(id)),
    anchor: vi.fn(),
  };
  let releaseDigest: (() => void) | undefined;
  vi.spyOn(crypto.subtle, "digest").mockImplementation(
    () =>
      new Promise<ArrayBuffer>((resolve) => (releaseDigest = () => resolve(new ArrayBuffer(32)))),
  );
  render(<RevisionSearch api={api} view={view} onNativePage={vi.fn()} onJump={vi.fn()} />);
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "needle" } });
  // The app-hit timer fires and starts hashing the match, but the digest is held.
  await act(() => vi.advanceTimersByTimeAsync(250));
  // The box is cleared while the hash of the superseded "needle" search is still in flight.
  // This takes the empty/composing branch, not the reset branch below.
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "" } });
  await act(async () => {
    releaseDigest?.();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(screen.queryByRole("button", { name: /needle/ })).toBeNull();

  // A new non-empty query (the reset branch, not the empty box) must supersede it too: its own
  // token bump must drop the still-hashing "needle" result. "zzz" has no match of its own, so
  // releasing the held digest again only exercises the superseded "needle" run.
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "needle" } });
  await act(() => vi.advanceTimersByTimeAsync(250));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "zzz" } });
  await act(async () => {
    releaseDigest?.();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(screen.queryByRole("button", { name: /needle/ })).toBeNull();
});

test("a seq change alone reruns the app-hit search and surfaces a hit that just went live", async () => {
  vi.useFakeTimers();
  vi.spyOn(crypto.subtle, "digest").mockResolvedValue(new ArrayBuffer(32));
  const api: ConversationSearchApi = {
    search: vi.fn<ConversationSearchApi["search"]>((_sid, _q, id) => emptyResult(id)),
    anchor: vi.fn(),
  };
  let view = liveView("nothing here");
  const { rerender } = render(
    <RevisionSearch api={api} view={view} onNativePage={vi.fn()} onJump={vi.fn()} />,
  );
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "needle" } });
  await act(() => vi.advanceTimersByTimeAsync(250));
  expect(screen.queryByRole("button", { name: /needle/ })).toBeNull();
  // Only `seq` changes here: the query, session and generation stay the same.
  view = {
    ...view,
    seq: view.seq + 1,
    turns: view.turns.map((turn) => ({
      ...turn,
      user: turn.user && { ...turn.user, text: "a needle" },
    })),
  };
  rerender(<RevisionSearch api={api} view={view} onNativePage={vi.fn()} onJump={vi.fn()} />);
  await act(() => vi.advanceTimersByTimeAsync(250));
  expect(screen.getByRole("button", { name: /needle/ })).toBeTruthy();
});

test("typing several prefixes within the throttle interval only searches the query that settled", async () => {
  vi.useFakeTimers();
  vi.spyOn(crypto.subtle, "digest").mockResolvedValue(new ArrayBuffer(32));
  const view = liveView("a needle");
  const api: ConversationSearchApi = {
    search: vi.fn<ConversationSearchApi["search"]>((_sid, _q, id) => emptyResult(id)),
    anchor: vi.fn(),
  };
  render(<RevisionSearch api={api} view={view} onNativePage={vi.fn()} onJump={vi.fn()} />);
  const input = screen.getByRole("textbox");
  // Three prefixes, 100 ms apart, with no envelope: each keystroke must cancel and reschedule
  // the pending timer, not leave the first one running toward a stale query.
  fireEvent.change(input, { target: { value: "needle" } });
  await act(() => vi.advanceTimersByTimeAsync(100));
  fireEvent.change(input, { target: { value: "needlex" } });
  await act(() => vi.advanceTimersByTimeAsync(100));
  fireEvent.change(input, { target: { value: "needle" } });
  expect(screen.queryByRole("button", { name: /needle/ })).toBeNull();
  await act(() => vi.advanceTimersByTimeAsync(250));
  expect(screen.getByRole("button", { name: /needle/ })).toBeTruthy();
});

test("a typed query holds No matches until the live hit finishes hashing", async () => {
  vi.useFakeTimers();
  const view = liveView("a needle");
  const api: ConversationSearchApi = {
    search: vi.fn<ConversationSearchApi["search"]>((_sid, _q, id) => emptyResult(id)),
    anchor: vi.fn(),
  };
  let releaseDigest: (() => void) | undefined;
  vi.spyOn(crypto.subtle, "digest").mockImplementation(
    () =>
      new Promise<ArrayBuffer>((resolve) => (releaseDigest = () => resolve(new ArrayBuffer(32)))),
  );
  render(<RevisionSearch api={api} view={view} onNativePage={vi.fn()} onJump={vi.fn()} />);
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "needle" } });
  // The native reply lands (empty, ready) while the app-hit search is still hashing the match.
  await act(() => vi.advanceTimersByTimeAsync(250));
  expect(screen.queryByText("No matches.")).toBeNull();
  await act(async () => {
    releaseDigest?.();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(screen.queryByText("No matches.")).toBeNull();
  expect(screen.getByRole("button", { name: /needle/ })).toBeTruthy();
});

test("an IME commit holds No matches until the live hit finishes hashing", async () => {
  vi.useFakeTimers();
  const view = liveView("a needle");
  const api: ConversationSearchApi = {
    search: vi.fn<ConversationSearchApi["search"]>((_sid, _q, id) => emptyResult(id)),
    anchor: vi.fn(),
  };
  let releaseDigest: (() => void) | undefined;
  vi.spyOn(crypto.subtle, "digest").mockImplementation(
    () =>
      new Promise<ArrayBuffer>((resolve) => (releaseDigest = () => resolve(new ArrayBuffer(32)))),
  );
  render(<RevisionSearch api={api} view={view} onNativePage={vi.fn()} onJump={vi.fn()} />);
  const input = screen.getByRole("textbox");
  fireEvent.compositionStart(input);
  fireEvent.change(input, { target: { value: "needle" } });
  fireEvent.compositionEnd(input);
  // The commit re-arms both the native and the app-hit search; the native reply lands first.
  await act(() => vi.advanceTimersByTimeAsync(250));
  expect(screen.queryByText("No matches.")).toBeNull();
  await act(async () => {
    releaseDigest?.();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(screen.queryByText("No matches.")).toBeNull();
  expect(screen.getByRole("button", { name: /needle/ })).toBeTruthy();
});

test("a second query hides the first query's hits at once, before the new search settles", async () => {
  vi.useFakeTimers();
  vi.spyOn(crypto.subtle, "digest").mockResolvedValue(new ArrayBuffer(32));
  const view = liveView("a needle");
  const api: ConversationSearchApi = {
    search: vi.fn<ConversationSearchApi["search"]>((_sid, _q, id) => emptyResult(id)),
    anchor: vi.fn(),
  };
  render(<RevisionSearch api={api} view={view} onNativePage={vi.fn()} onJump={vi.fn()} />);
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "needle" } });
  await act(() => vi.advanceTimersByTimeAsync(250));
  expect(screen.getByRole("button", { name: /needle/ })).toBeTruthy();
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "zzz" } });
  expect(screen.queryByRole("button", { name: /needle/ })).toBeNull();
});
