import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import { decisionLoopExamples } from "../../clients/decisionLoop/fake";
import {
  FEEDBACK_REASONS,
  type ItemAction,
  type ItemEvidence,
  type ItemView,
} from "../../clients/decisionLoop/types";
import { ItemCard } from "./ItemCard";
import { ACTION_LABELS, KIND_LABELS, REASON_LABELS } from "./wording";

afterEach(cleanup);

const EXAMPLE_ITEMS = decisionLoopExamples().launcher.items;

function baseItem(overrides: Partial<ItemView> = {}): ItemView {
  // items[0]: a "suggest" item with a note evidence chip and three actions, primary already
  // first — a representative, fully-populated item to start every synthetic test from.
  return { ...EXAMPLE_ITEMS[0], ...overrides } as ItemView;
}

function mount(item: ItemView) {
  const onAction = vi.fn();
  const onFeedback = vi.fn();
  const view = render(<ItemCard item={item} onAction={onAction} onFeedback={onFeedback} />);
  return { ...view, onAction, onFeedback };
}

// --- kind chip, what, why ---

test("every example item shows its kind chip, its what line and a Why now line", () => {
  for (const item of EXAMPLE_ITEMS) {
    const { container, unmount } = mount(item);
    expect(within(container).getByText(KIND_LABELS[item.kind])).toBeTruthy();
    expect(within(container).getByText(item.what)).toBeTruthy();
    expect(container.textContent).toContain(`Why now: ${item.why}`);
    unmount();
  }
});

// --- evidence chips ---

test("an evidence chip naming a note is a button that calls onAction with an open_note action built from it", () => {
  const item = baseItem(); // evidence: [{ label, workspace_id, ref }]
  const [evidence] = item.evidence as [ItemEvidence];
  const { onAction } = mount(item);
  fireEvent.click(screen.getByRole("button", { name: evidence.label }));
  expect(onAction).toHaveBeenCalledWith({
    type: "open_note",
    label: evidence.label,
    primary: false,
    workspace_id: evidence.workspace_id,
    ref: evidence.ref,
  });
});

test("an evidence chip naming a chat is a button that calls onAction with an open_chat action built from it", () => {
  const item = EXAMPLE_ITEMS[1] as ItemView; // "approvals": evidence: [{ label, session_id }]
  const [evidence] = item.evidence as [ItemEvidence];
  const { onAction } = mount(item);
  fireEvent.click(screen.getByRole("button", { name: evidence.label }));
  expect(onAction).toHaveBeenCalledWith({
    type: "open_chat",
    label: evidence.label,
    primary: false,
    session_id: evidence.session_id,
  });
});

test("an evidence chip with neither a ref nor a session_id is plain text, not a button", () => {
  const item = EXAMPLE_ITEMS[4] as ItemView; // "agent_runs": evidence: [{ label: "Agent runs" }]
  const { container } = mount(item);
  expect(screen.getByText("Agent runs")).toBeTruthy();
  expect(within(container).queryByRole("button", { name: "Agent runs" })).toBeNull();
});

test("no evidence list renders when the item has none", () => {
  const item = EXAMPLE_ITEMS[6] as ItemView; // "routine": evidence: []
  const { container } = mount(item);
  expect(container.querySelector(".dloop-item-evidence")).toBeNull();
});

// --- actions ---

test("actions render primary first even when the server sends it out of order", () => {
  const actions: ItemAction[] = [
    { type: "open_inbox", label: "Second", primary: false },
    { type: "start_run", label: "First", primary: true },
  ];
  const { container } = mount(baseItem({ actions }));
  const labels = [...container.querySelectorAll(".dloop-item-action")].map((el) => el.textContent);
  expect(labels).toEqual(["First", "Second"]);
});

test("clicking an action button calls onAction with that exact action object", () => {
  const item = EXAMPLE_ITEMS[2] as ItemView; // open_chat (primary) + stop_mission
  const [first, second] = item.actions as [ItemAction, ItemAction];
  const { onAction } = mount(item);
  fireEvent.click(screen.getByRole("button", { name: first.label }));
  expect(onAction).toHaveBeenCalledWith(first);
  fireEvent.click(screen.getByRole("button", { name: second.label }));
  expect(onAction).toHaveBeenCalledWith(second);
});

test("two actions of one type render as two buttons with keys of their own (ui-1)", () => {
  const errors = vi.spyOn(console, "error").mockImplementation(() => {});
  const actions: ItemAction[] = [
    { type: "open_chat", label: "Open chat", primary: true, session_id: "session-1" },
    { type: "open_chat", label: "Open chat", primary: false, session_id: "sess-live" },
  ];
  const { onAction } = mount(baseItem({ actions }));
  const buttons = screen.getAllByRole("button", { name: "Open chat" });
  expect(buttons).toHaveLength(2);
  fireEvent.click(buttons[1] as HTMLElement);
  expect(onAction).toHaveBeenCalledWith(actions[1]);
  const keyWarnings = errors.mock.calls.filter((call) => String(call[0]).includes("same key"));
  errors.mockRestore();
  expect(keyWarnings).toEqual([]);
});

test("an action whose onAction answers a promise stays disabled until it settles (ui-2)", async () => {
  let settle: () => void = () => {};
  const answered = new Promise<void>((resolve) => {
    settle = resolve;
  });
  const onAction = vi.fn((action: ItemAction) =>
    action.type === "start_seat" ? answered : undefined,
  );
  const actions: ItemAction[] = [
    { type: "start_seat", label: "Start", primary: true, routine_id: "routine-1" },
    { type: "open_runs", label: "Open runs", primary: false },
  ];
  render(<ItemCard item={baseItem({ actions })} onAction={onAction} onFeedback={vi.fn()} />);
  const start = screen.getByRole<HTMLButtonElement>("button", { name: "Start" });
  fireEvent.click(start);
  fireEvent.click(start);
  expect(onAction).toHaveBeenCalledTimes(1);
  expect(start.disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Open runs" }));
  expect(screen.getByRole<HTMLButtonElement>("button", { name: "Open runs" }).disabled).toBe(false);
  await act(async () => settle());
  expect(start.disabled).toBe(false);
});

test("an action with an empty label falls back to ACTION_LABELS by its type", () => {
  const actions: ItemAction[] = [{ type: "open_runs", label: "", primary: true }];
  mount(baseItem({ actions }));
  expect(screen.getByRole("button", { name: ACTION_LABELS.open_runs })).toBeTruthy();
});

test("no actions row renders when the item has none", () => {
  const { container } = mount(baseItem({ actions: [] }));
  expect(container.querySelector(".dloop-item-actions")).toBeNull();
});

test("the card examples show their kind and an Open card button that hands onAction the open_note action", () => {
  for (const [candidate, kind] of [
    ["card_needs_you", "ask"],
    ["card_stale", "report"],
  ] as const) {
    const item = EXAMPLE_ITEMS.find((example) => example.candidate === candidate) as ItemView;
    expect(item.kind).toBe(kind);
    const { container, onAction, unmount } = mount(item);
    expect(within(container).getByText(KIND_LABELS[kind])).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Open card" }));
    const open = item.actions.find((action) => action.type === "open_note") as ItemAction;
    expect(open.ref).toMatch(/^kanban\//);
    expect(open.workspace_id).toBeTruthy();
    expect(onAction).toHaveBeenCalledWith(open);
    unmount();
  }
});

// --- Not now / Useful / Not useful ---

test("Not now calls onFeedback with later and no reason", () => {
  const { onFeedback } = mount(baseItem());
  fireEvent.click(screen.getByRole("button", { name: "Not now" }));
  expect(onFeedback).toHaveBeenCalledWith("later");
  expect((onFeedback.mock.calls[0] as unknown[]).length).toBe(1);
});

test("Useful sits between Not now and Not useful, is unpressed by default, and calls onFeedback with useful and no reason", () => {
  const { container, onFeedback } = mount(baseItem());
  const labels = [...container.querySelectorAll(".dloop-item-feedback-btn")].map(
    (el) => el.textContent,
  );
  expect(labels).toEqual(["Not now", "Useful", "Not useful"]);
  const button = screen.getByRole("button", { name: "Useful" });
  expect(button.getAttribute("aria-pressed")).toBe("false");
  fireEvent.click(button);
  expect(onFeedback).toHaveBeenCalledWith("useful");
  expect((onFeedback.mock.calls[0] as unknown[]).length).toBe(1);
});

test("clicking Useful marks it pressed, reading Marked useful", () => {
  mount(baseItem());
  fireEvent.click(screen.getByRole("button", { name: "Useful" }));
  const button = screen.getByRole("button", { name: "Marked useful" });
  expect(button.getAttribute("aria-pressed")).toBe("true");
});

test("Useful reads Marked useful only once its onFeedback promise resolves", async () => {
  let settle: () => void = () => {};
  const answered = new Promise<void>((resolve) => {
    settle = resolve;
  });
  const onFeedback = vi.fn(() => answered);
  render(<ItemCard item={baseItem()} onAction={vi.fn()} onFeedback={onFeedback} />);
  fireEvent.click(screen.getByRole("button", { name: "Useful" }));
  const waiting = screen.getByRole("button", { name: "Useful" });
  expect(waiting.getAttribute("aria-pressed")).toBe("false");
  await act(async () => settle());
  const button = screen.getByRole("button", { name: "Marked useful" });
  expect(button.getAttribute("aria-pressed")).toBe("true");
});

test("Useful stays unpressed when its onFeedback promise rejects", async () => {
  const onFeedback = vi.fn(() => Promise.reject(new Error("offline")));
  render(<ItemCard item={baseItem()} onAction={vi.fn()} onFeedback={onFeedback} />);
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Useful" }));
  });
  const button = screen.getByRole("button", { name: "Useful" });
  expect(button.getAttribute("aria-pressed")).toBe("false");
});

test("Not useful calls onFeedback with wrong and no reason", () => {
  const { onFeedback } = mount(baseItem());
  fireEvent.click(screen.getByRole("button", { name: "Not useful" }));
  expect(onFeedback).toHaveBeenCalledWith("wrong");
  expect((onFeedback.mock.calls[0] as unknown[]).length).toBe(1);
});

// --- the "⋯" menu ---

describe("the ⋯ menu", () => {
  function openMenu() {
    const result = mount(baseItem());
    const toggle = screen.getByRole("button", { name: "More" });
    expect(toggle.getAttribute("aria-haspopup")).toBe("menu");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    return { ...result, toggle };
  }

  test("opens a role=menu of role=menuitem buttons: Stop these, then the four reasons, in order", () => {
    openMenu();
    const menu = screen.getByRole("menu", { name: "More" });
    const items = within(menu).getAllByRole("menuitem");
    expect(items.map((el) => el.textContent)).toEqual([
      "Stop these",
      "Out of date",
      "Already knew",
      "Wrong topic",
      "Bad timing",
    ]);
  });

  test("Stop these calls onFeedback with mute, closes the menu and returns focus to the toggle", () => {
    const { onFeedback, toggle } = openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Stop these" }));
    expect(onFeedback).toHaveBeenCalledWith("mute");
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(toggle);
  });

  test("each reason calls onFeedback with wrong and its own reason code", () => {
    for (const reason of FEEDBACK_REASONS) {
      const { onFeedback } = openMenu();
      fireEvent.click(screen.getByRole("menuitem", { name: REASON_LABELS[reason] }));
      expect(onFeedback).toHaveBeenCalledWith("wrong", reason);
      cleanup();
    }
  });

  test("Escape closes the menu and returns focus to the toggle", () => {
    const { toggle } = openMenu();
    fireEvent.keyDown(document.activeElement as Element, { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(toggle);
  });

  test("the first item takes focus as soon as the menu opens", () => {
    openMenu();
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "Stop these" }));
  });

  test("ArrowDown and ArrowUp cycle focus between the five items, wrapping at both ends", () => {
    openMenu();
    const items = screen.getAllByRole("menuitem");
    expect(document.activeElement).toBe(items[0]);
    // Dispatched from the focused item itself, as a real keypress would be, so it bubbles to the
    // window listener with a target the wrapper's `.contains()` guard recognizes as inside it —
    // firing straight on `window` would make the event's own target `window`, which nothing
    // contains, and the handler would ignore it.
    fireEvent.keyDown(document.activeElement as Element, { key: "ArrowUp" }); // wraps to the last
    expect(document.activeElement).toBe(items[4]);
    fireEvent.keyDown(document.activeElement as Element, { key: "ArrowDown" }); // wraps to the first
    expect(document.activeElement).toBe(items[0]);
    fireEvent.keyDown(document.activeElement as Element, { key: "ArrowDown" });
    expect(document.activeElement).toBe(items[1]);
  });

  test("a click outside the card closes the menu", () => {
    openMenu();
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("menu")).toBeNull();
  });
});

// --- every example item, and text-node safety ---

test("every example item's rendered card holds no jev text", () => {
  for (const item of EXAMPLE_ITEMS) {
    const { container, unmount } = mount(item);
    expect(container.textContent ?? "").not.toMatch(/jev/i);
    unmount();
  }
});

test("what/why/evidence/action text renders as a literal text node, never parsed as markup", () => {
  const item = baseItem({
    what: "<b>bold</b> is not actually bold",
    why: "<i>italic</i> is not actually italic",
    evidence: [{ label: "<u>underline</u> label" }],
    actions: [{ type: "open_note", label: "<u>underline</u> action", primary: true }],
  });
  const { container } = mount(item);
  expect(container.querySelector("b, i, u")).toBeNull();
  expect(screen.getByText("<b>bold</b> is not actually bold")).toBeTruthy();
  expect(container.textContent).toContain("<i>italic</i> is not actually italic");
  expect(screen.getByText("<u>underline</u> label")).toBeTruthy();
  expect(screen.getByRole("button", { name: "<u>underline</u> action" })).toBeTruthy();
});
