import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, expect, test, vi } from "vitest";
import type { SessionRow } from "../../protocol";
import { emptyView, type SessionView, type Turn } from "../../state/view";
import { SessionList, type SessionOrganization } from "./SessionList";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});
const A = "00000000-0000-4000-8000-000000000001";
const B = "00000000-0000-4000-8000-000000000002";
const row = (id: string): SessionRow => ({
  session_id: id,
  title: "Synthetic session",
  first_prompt: "",
  entrypoint: "viki-agent",
  modified: "2026-10-12T12:00:00Z",
  cli_version: "",
  other: false,
});
const turn = (status: Turn["status"]): Turn => ({
  id: "synthetic-turn",
  user: null,
  blocks: [],
  status,
  usage: null,
  durationMs: 0,
  history: false,
});

function props(extra: Partial<SessionOrganization> = {}, view?: SessionView) {
  return {
    groups: [{ label: "Today", rows: [row(A), row(B)] }],
    other: [],
    selectedId: A,
    liveIds: new Set([A]),
    nowMs: new Date("2026-10-12T12:05:00Z").getTime(),
    onOpen: vi.fn(),
    archive: { isArchived: () => false, pending: new Set<string>(), onToggle: vi.fn() },
    organization: {
      isPinned: () => false,
      pending: new Set<string>(),
      onRename: vi.fn(),
      onPin: vi.fn(),
      ...extra,
    },
    ...(view
      ? {
          rowContext: {
            app: { sessions: { [A]: view }, others: [], status: "open" as const },
            elsewhere: new Map(),
          },
        }
      : {}),
  } satisfies ComponentProps<typeof SessionList>;
}

function session(container: HTMLElement, id: string) {
  const element = container.querySelector<HTMLElement>(`[data-session-id="${id}"]`);
  if (!element) throw Error("Synthetic session row is absent.");
  return element;
}

test("optional label accessors leave legacy rows without label chrome", () => {
  const { container } = render(<SessionList {...props()} />);
  expect(container.querySelector(".sess-unread-label")).toBeNull();
  expect(container.querySelector(".sess-color-label")).toBeNull();
  expect(session(container, A).getAttribute("data-unread")).toBeNull();
  expect(session(container, A).querySelector(".t")?.textContent).toBe("Synthetic session");
});

test.each([
  ["blue", "Blue"],
  ["green", "Green"],
  ["orange", "Orange"],
  ["purple", "Purple"],
  ["red", "Red"],
  ["slate", "Teal"],
] as const)(
  "%s announces %s alongside unread without replacing running status or selection",
  (color, label) => {
    const view = { ...emptyView(A), turns: [turn("running")] };
    const { container } = render(
      <SessionList
        {...props({ unreadOf: (id) => id === A, colorOf: (id) => (id === A ? color : null) }, view)}
      />,
    );
    const item = session(container, A);
    expect(item.classList.contains("sess-selected")).toBe(true);
    expect(item.querySelector(".sess-activity")?.getAttribute("data-activity")).toBe("running");
    expect(item.querySelector(".t")?.textContent).toBe("Synthetic session");
    expect(within(item).getByText("Unread")).toBeTruthy();
    const name = `${label} color label`;
    expect(within(item).getByRole("img", { name }).getAttribute("data-color")).toBe(color);
    expect(session(container, B).querySelector(".sess-color-label")).toBeNull();
    expect(session(container, B).querySelector(".sess-unread-label")).toBeNull();
  },
);

test("color labels preserve both observed completion and attention activity", () => {
  const values = props({ colorOf: () => "blue" }, { ...emptyView(A), turns: [turn("success")] });
  const { container, rerender } = render(<SessionList {...values} />);
  expect(session(container, A).querySelector(".sess-activity")?.getAttribute("data-activity")).toBe(
    "done",
  );
  const waiting: SessionView = {
    ...emptyView(A),
    cards: [
      {
        kind: "permission" as const,
        nativeRequest: null,
        data: {
          tool_use_id: "p1",
          kind: "tool",
          tool: "Read",
          summary: "",
          question: "",
          reason: "",
          agent: "",
          plan: "",
        },
      },
    ],
  };
  rerender(<SessionList {...props({ colorOf: () => "blue" }, waiting)} />);
  expect(session(container, A).querySelector(".sess-activity")?.getAttribute("data-activity")).toBe(
    "needs-you",
  );
  expect(within(session(container, A)).getByRole("img", { name: "Blue color label" })).toBeTruthy();
});

test("unread and color callbacks receive the exact row ID without opening it", async () => {
  vi.useFakeTimers();
  const onUnread = vi.fn();
  const onColor = vi.fn();
  const values = props({ unreadOf: (id) => id === B, onUnread, onColor });
  const { container } = render(<SessionList {...values} />);
  const item = session(container, B);
  fireEvent.click(within(item).getByRole("button", { name: "Session menu" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Mark as read" }));
  expect(onUnread).toHaveBeenCalledExactlyOnceWith(B);
  await act(() => vi.advanceTimersByTimeAsync(0));
  fireEvent.click(within(item).getByRole("button", { name: "Session menu" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Color label" }));
  await act(() => vi.advanceTimersByTimeAsync(0));
  fireEvent.click(screen.getByRole("menuitemradio", { name: "Orange color label" }));
  expect(onColor).toHaveBeenCalledExactlyOnceWith(B, "orange");
  expect(values.onOpen).not.toHaveBeenCalled();
});

test("new-window pending and errors belong only to their exact row", () => {
  const onNewWindow = vi.fn();
  const values = props({
    onNewWindow,
    newWindowPending: new Set([A]),
    newWindowErrorOf: (id) => (id === A ? "The session is unavailable." : null),
  });
  const { container } = render(<SessionList {...values} />);
  expect(within(session(container, A)).getByRole("alert").textContent).toBe(
    "The session is unavailable.",
  );
  expect(within(session(container, B)).queryByRole("alert")).toBeNull();
  fireEvent.click(within(session(container, A)).getByRole("button", { name: "Session menu" }));
  const pending = screen.getByRole("menuitem", { name: "Opening window…" });
  expect(pending.getAttribute("aria-disabled")).toBe("true");
  fireEvent.click(pending);
  expect(onNewWindow).not.toHaveBeenCalled();
  fireEvent.keyDown(pending, { key: "Escape" });
  fireEvent.click(within(session(container, B)).getByRole("button", { name: "Session menu" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "New window" }));
  expect(onNewWindow).toHaveBeenCalledExactlyOnceWith(B);
  expect(values.onOpen).not.toHaveBeenCalled();
});

test("terminal availability is per ID while export and fresh-workspace callbacks remain isolated", () => {
  const callbacks = { onExport: vi.fn(), onOpenTerminal: vi.fn(), onNewChatInWorkspace: vi.fn() };
  const values = props({ ...callbacks, canOpenTerminal: (id) => id === B });
  const { container } = render(<SessionList {...values} />);
  fireEvent.click(within(session(container, A)).getByRole("button", { name: "Session menu" }));
  expect(screen.queryByRole("menuitem", { name: "Open in terminal" })).toBeNull();
  fireEvent.keyDown(screen.getByRole("menuitem", { name: "Pin" }), { key: "Escape" });
  for (const [name, callback] of [
    ["Open in terminal", callbacks.onOpenTerminal],
    ["Select turns to export", callbacks.onExport],
    ["New chat in workspace…", callbacks.onNewChatInWorkspace],
  ] as const) {
    fireEvent.click(within(session(container, B)).getByRole("button", { name: "Session menu" }));
    fireEvent.click(screen.getByRole("menuitem", { name }));
    expect(callback).toHaveBeenCalledExactlyOnceWith(B);
  }
  expect(values.onOpen).not.toHaveBeenCalled();
});
