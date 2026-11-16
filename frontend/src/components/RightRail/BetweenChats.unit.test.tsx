/**
 * BetweenChats: a fake `BetweenChatsApi` whose `list()` returns a controllable (deferred)
 * promise, and fake timers, so each poll can be driven one step at a time — the same technique
 * RunPeek.unit.test.tsx uses for its own polling component.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type {
  BetweenChatsApi,
  BetweenChatsResult,
  ChatMessage,
} from "../../clients/conversations/betweenChatsApi";
import { BETWEEN_CHATS_POLL_MS, BetweenChats } from "./BetweenChats";

const A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"; // the receiving chat, "Alpha"
const B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"; // a listed sender, "Beta"

function labelOf(id: string): string {
  if (id === A) return "Alpha";
  if (id === B) return "Beta";
  return "Unknown chat";
}

interface Deferred {
  resolve: (value: BetweenChatsResult) => void;
  reject: (error: unknown) => void;
}

interface Call {
  signal: AbortSignal | undefined;
}

function fakeApi(): { api: BetweenChatsApi; calls: Call[]; pending: Deferred[] } {
  const calls: Call[] = [];
  const pending: Deferred[] = [];
  const api: BetweenChatsApi = {
    list(signal) {
      calls.push({ signal });
      return new Promise<BetweenChatsResult>((resolve, reject) =>
        pending.push({ resolve, reject }),
      );
    },
  };
  return { api, calls, pending };
}

function message(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    at: "2026-10-20T10:00:00.000Z",
    from_name: "viki-9c21",
    from_session_id: null,
    to_session_id: A,
    text: "Status update.",
    ...overrides,
  };
}

function open(): void {
  fireEvent.click(screen.getByText("Between chats"));
}

async function settle(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

test("closed by default: no request, even after time passes", async () => {
  const { api, calls } = fakeApi();
  render(<BetweenChats api={api} labelOf={labelOf} onOpen={vi.fn()} nowMs={0} />);
  expect(screen.getByText("Between chats")).toBeTruthy();

  await advance(BETWEEN_CHATS_POLL_MS * 3);
  expect(calls).toHaveLength(0);
});

test("opening reads at once, shows Loading, then the empty sentence once it resolves empty", async () => {
  const { api, calls, pending } = fakeApi();
  render(<BetweenChats api={api} labelOf={labelOf} onOpen={vi.fn()} nowMs={0} />);
  open();
  await settle();
  expect(calls).toHaveLength(1);
  expect(screen.getByText("Loading…")).toBeTruthy();

  pending[0]?.resolve({ messages: [], partial: false });
  await settle();

  expect(screen.queryByText("Loading…")).toBeNull();
  expect(screen.getByText("No messages between chats.")).toBeTruthy();
});

test("a failed read shows the unavailable sentence", async () => {
  const { api, pending } = fakeApi();
  render(<BetweenChats api={api} labelOf={labelOf} onOpen={vi.fn()} nowMs={0} />);
  open();
  await settle();

  pending[0]?.reject(new Error("boom"));
  await settle();

  expect(screen.getByText("Between chats is unavailable.")).toBeTruthy();
  expect(screen.queryByText("Loading…")).toBeNull();
});

test("rows show a listed sender's chat label, an unknown sender's name, the receiving chat, the time and the text", async () => {
  const { api, pending } = fakeApi();
  const { container } = render(
    <BetweenChats api={api} labelOf={labelOf} onOpen={vi.fn()} nowMs={0} />,
  );
  open();
  await settle();

  pending[0]?.resolve({
    messages: [
      message({ from_session_id: B, to_session_id: A, text: "From a listed chat." }),
      message({
        from_session_id: null,
        from_name: "viki-9c21",
        to_session_id: A,
        text: "From an unknown name.",
      }),
    ],
    partial: false,
  });
  await settle();

  const rows = container.querySelectorAll(".between-chats-row");
  expect(rows).toHaveLength(2);
  expect(rows[0]?.querySelectorAll(".between-chats-line")).toHaveLength(2);
  expect(rows[0]?.textContent).toContain("Beta → Alpha");
  expect(rows[0]?.textContent).toContain("From a listed chat.");
  expect(rows[0]?.querySelector("time")?.getAttribute("dateTime")).toBe("2026-10-20T10:00:00.000Z");
  expect(rows[1]?.textContent).toContain("viki-9c21 → Alpha");
  expect(rows[1]?.textContent).toContain("From an unknown name.");
});

test("partial with messages shows the list and the partial sentence under it", async () => {
  const { api, pending } = fakeApi();
  const { container } = render(
    <BetweenChats api={api} labelOf={labelOf} onOpen={vi.fn()} nowMs={0} />,
  );
  open();
  await settle();

  pending[0]?.resolve({ messages: [message()], partial: true });
  await settle();

  expect(container.querySelectorAll(".between-chats-row")).toHaveLength(1);
  expect(screen.getByText("Some chats could not be read.")).toBeTruthy();
});

test("hostile markup and a URL in a message's text render literally: no img or anchor is created", async () => {
  const { api, pending } = fakeApi();
  const { container } = render(
    <BetweenChats api={api} labelOf={labelOf} onOpen={vi.fn()} nowMs={0} />,
  );
  open();
  await settle();

  const hostile = "<img src=x onerror=alert(1)> [x](http://e.test) **b**";
  pending[0]?.resolve({ messages: [message({ text: hostile })], partial: false });
  await settle();

  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("a")).toBeNull();
  expect(screen.getByText(hostile)).toBeTruthy();
});

test("selecting a row calls onOpen with the receiving chat's session id", async () => {
  const { api, pending } = fakeApi();
  const onOpen = vi.fn();
  const { container } = render(
    <BetweenChats api={api} labelOf={labelOf} onOpen={onOpen} nowMs={0} />,
  );
  open();
  await settle();

  pending[0]?.resolve({ messages: [message({ to_session_id: A })], partial: false });
  await settle();

  const row = container.querySelector(".between-chats-row");
  expect(row).not.toBeNull();
  fireEvent.click(row as Element);
  expect(onOpen).toHaveBeenCalledWith(A);
});

test("re-reads every BETWEEN_CHATS_POLL_MS while open", async () => {
  const { api, calls, pending } = fakeApi();
  render(<BetweenChats api={api} labelOf={labelOf} onOpen={vi.fn()} nowMs={0} />);
  open();
  await settle();
  expect(calls).toHaveLength(1);

  pending[0]?.resolve({ messages: [], partial: false });
  await settle();
  expect(calls).toHaveLength(1);

  await advance(BETWEEN_CHATS_POLL_MS);
  expect(calls).toHaveLength(2);

  pending[1]?.resolve({ messages: [], partial: false });
  await settle();
  await advance(BETWEEN_CHATS_POLL_MS);
  expect(calls).toHaveLength(3);
});

test("closing aborts an in-flight read and stops further polling", async () => {
  const { api, calls } = fakeApi();
  render(<BetweenChats api={api} labelOf={labelOf} onOpen={vi.fn()} nowMs={0} />);
  open();
  await settle();
  expect(calls).toHaveLength(1);
  expect(calls[0]?.signal?.aborted).toBe(false);

  open(); // the same summary click closes it again
  await settle();
  expect(calls[0]?.signal?.aborted).toBe(true);

  await advance(BETWEEN_CHATS_POLL_MS * 3);
  expect(calls).toHaveLength(1);
});

test("unmounting aborts the in-flight read and issues no further reads", async () => {
  const { api, calls } = fakeApi();
  const { unmount } = render(
    <BetweenChats api={api} labelOf={labelOf} onOpen={vi.fn()} nowMs={0} />,
  );
  open();
  await settle();
  expect(calls).toHaveLength(1);
  expect(calls[0]?.signal?.aborted).toBe(false);

  unmount();
  expect(calls[0]?.signal?.aborted).toBe(true);

  await advance(BETWEEN_CHATS_POLL_MS * 3);
  expect(calls).toHaveLength(1);
});

function hasDuplicateKeyWarning(spy: ReturnType<typeof vi.spyOn>): boolean {
  return spy.mock.calls.some((args: unknown[]) => String(args[0]).includes("same key"));
}

test("two messages equal in to/at/sender but different text render as two rows with no duplicate-key warning", async () => {
  const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
  const { api, pending } = fakeApi();
  const { container } = render(
    <BetweenChats api={api} labelOf={labelOf} onOpen={vi.fn()} nowMs={0} />,
  );
  open();
  await settle();

  pending[0]?.resolve({
    messages: [message({ text: "First text." }), message({ text: "Second text." })],
    partial: false,
  });
  await settle();

  expect(container.querySelectorAll(".between-chats-row")).toHaveLength(2);
  expect(hasDuplicateKeyWarning(errorSpy)).toBe(false);
  errorSpy.mockRestore();
});

test("two fully identical messages render as two rows with no duplicate-key warning", async () => {
  const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
  const { api, pending } = fakeApi();
  const { container } = render(
    <BetweenChats api={api} labelOf={labelOf} onOpen={vi.fn()} nowMs={0} />,
  );
  open();
  await settle();

  pending[0]?.resolve({ messages: [message(), message()], partial: false });
  await settle();

  expect(container.querySelectorAll(".between-chats-row")).toHaveLength(2);
  expect(hasDuplicateKeyWarning(errorSpy)).toBe(false);
  errorSpy.mockRestore();
});
