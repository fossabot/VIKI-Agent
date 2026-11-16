import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { RailDrawer } from "../Shell/RailDrawer";
import { OrganizedSessionMenu } from "./OrganizedSessionMenu";

const ID = "00000000-0000-0000-0000-000000000001";
const clipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  if (clipboard) Object.defineProperty(navigator, "clipboard", clipboard);
  else Reflect.deleteProperty(navigator, "clipboard");
});

function props(extra: Partial<ComponentProps<typeof OrganizedSessionMenu>> = {}) {
  return {
    sessionId: ID,
    pinned: false,
    archived: false,
    pending: false,
    onRename: vi.fn(),
    onPin: vi.fn(),
    onArchive: vi.fn(),
    ...extra,
  };
}

function open() {
  fireEvent.click(screen.getByRole("button", { name: "Session menu" }));
}

test("unheld rows offer only implemented organization actions in order", () => {
  render(<OrganizedSessionMenu {...props()} />);
  open();
  expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual([
    "Rename…",
    "Pin",
    "Copy ID",
    "Archive",
  ]);
});

test("held callbacks stay isolated from each other and from opening the row", () => {
  const callbacks = { onFork: vi.fn(), onOpenTranscript: vi.fn(), onClose: vi.fn() };
  const values = props({ pinned: true, archived: true, ...callbacks });
  const onRow = vi.fn();
  render(
    // biome-ignore lint/a11y/useSemanticElements: match SessionList's row with nested controls.
    <div role="button" tabIndex={0} onClick={onRow} onKeyDown={onRow}>
      <OrganizedSessionMenu {...values} />
    </div>,
  );
  const actions = [
    ["Rename…", values.onRename],
    ["Unpin", values.onPin],
    ["Fork", callbacks.onFork],
    ["Open transcript in VS Code", callbacks.onOpenTranscript],
    ["Unarchive", values.onArchive],
    ["Close session", callbacks.onClose],
  ] as const;
  for (const [name, callback] of actions) {
    open();
    fireEvent.click(screen.getByRole("menuitem", { name }));
    expect(callback).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("menu")).toBeNull();
  }
  expect(onRow).not.toHaveBeenCalled();
});

test("pending metadata actions refuse selection but Copy ID remains available", () => {
  const values = props({ pending: true });
  render(<OrganizedSessionMenu {...values} />);
  open();
  for (const name of ["Rename…", "Pin", "Archive"]) {
    const item = screen.getByRole("menuitem", { name });
    expect(item.getAttribute("aria-disabled")).toBe("true");
    fireEvent.click(item);
  }
  expect(values.onRename).not.toHaveBeenCalled();
  expect(values.onPin).not.toHaveBeenCalled();
  expect(values.onArchive).not.toHaveBeenCalled();
  expect(
    screen.getByRole("menuitem", { name: "Copy ID" }).getAttribute("aria-disabled"),
  ).toBeNull();
});

test("copy writes the exact session ID and announces success after the menu closes", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
  render(<OrganizedSessionMenu {...props()} />);
  open();
  await act(async () => fireEvent.click(screen.getByRole("menuitem", { name: "Copy ID" })));
  expect(writeText).toHaveBeenCalledExactlyOnceWith(ID);
  expect(screen.getByRole("status").textContent).toBe("ID copied");
  expect(screen.queryByRole("menu")).toBeNull();
});

test.each([
  undefined,
  {
    writeText: () => {
      throw Error("refused");
    },
  },
  { writeText: () => Promise.reject(Error("refused")) },
])("clipboard absence or refusal has visible feedback", async (value) => {
  Object.defineProperty(navigator, "clipboard", { configurable: true, value });
  render(<OrganizedSessionMenu {...props()} />);
  open();
  await act(async () => fireEvent.click(screen.getByRole("menuitem", { name: "Copy ID" })));
  expect(screen.getByRole("status").textContent).toBe("Copy failed");
});

test("pointer dismissal's trailing click does not reopen the nonmodal menu", () => {
  render(<OrganizedSessionMenu {...props()} />);
  const trigger = screen.getByRole("button", { name: "Session menu" });
  function clickPointer() {
    fireEvent(
      trigger,
      new MouseEvent("pointerdown", { bubbles: true, cancelable: true, button: 0 }),
    );
    fireEvent.click(trigger);
  }
  clickPointer();
  expect(screen.getByRole("menu")).toBeTruthy();
  clickPointer();
  expect(screen.queryByRole("menu")).toBeNull();
});

test("drawer menu portal remains in the focus boundary and Escape restores its trigger", async () => {
  const onClose = vi.fn();
  render(
    <RailDrawer side="left" label="Sessions" open={true} onClose={onClose}>
      <OrganizedSessionMenu {...props()} />
    </RailDrawer>,
  );
  const trigger = screen.getByRole("button", { name: "Session menu" });
  trigger.focus();
  fireEvent.keyDown(trigger, { key: "ArrowDown" });
  expect(screen.getByRole("dialog").contains(screen.getByRole("menu"))).toBe(true);
  const rename = screen.getByRole("menuitem", { name: "Rename…" });
  rename.focus();
  expect(document.activeElement).toBe(rename);
  fireEvent.keyDown(rename, { key: "ArrowDown" });
  await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
  expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "Pin" }));
  fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
  await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
  expect(screen.queryByRole("menu")).toBeNull();
  expect(onClose).not.toHaveBeenCalled();
  expect(document.activeElement).toBe(trigger);
});

test.each([false, true])(
  "unread actions use their own callback and current label (%s)",
  (unread) => {
    const onUnread = vi.fn();
    const values = props({ unread, onUnread });
    render(<OrganizedSessionMenu {...values} />);
    open();
    fireEvent.click(
      screen.getByRole("menuitem", { name: unread ? "Mark as read" : "Mark as unread" }),
    );
    expect(onUnread).toHaveBeenCalledExactlyOnceWith();
    expect(values.onPin).not.toHaveBeenCalled();
    expect(values.onArchive).not.toHaveBeenCalled();
    expect(screen.queryByRole("menu")).toBeNull();
  },
);

test("pending metadata refuses unread and color edits", () => {
  const onUnread = vi.fn();
  const onColor = vi.fn();
  render(<OrganizedSessionMenu {...props({ pending: true, onUnread, onColor })} />);
  open();
  for (const name of ["Mark as unread", "Color label"]) {
    const item = screen.getByRole("menuitem", { name });
    expect(item.getAttribute("aria-disabled")).toBe("true");
    fireEvent.click(item);
  }
  expect(onUnread).not.toHaveBeenCalled();
  expect(onColor).not.toHaveBeenCalled();
  expect(screen.queryByRole("menuitemradio")).toBeNull();
});

test("optional services invoke only their exact callback and do not open the row", () => {
  const callbacks = {
    onNewWindow: vi.fn(),
    onExport: vi.fn(),
    onOpenTerminal: vi.fn(),
    onNewChatInWorkspace: vi.fn(),
  };
  const onRow = vi.fn();
  render(
    // biome-ignore lint/a11y/useSemanticElements: match the session row's nested controls.
    <div role="button" tabIndex={0} onClick={onRow} onKeyDown={onRow}>
      <OrganizedSessionMenu {...props(callbacks)} />
    </div>,
  );
  for (const [name, callback] of [
    ["New window", callbacks.onNewWindow],
    ["Select turns to export", callbacks.onExport],
    ["Open in terminal", callbacks.onOpenTerminal],
    ["New chat in workspace…", callbacks.onNewChatInWorkspace],
  ] as const) {
    open();
    fireEvent.click(screen.getByRole("menuitem", { name }));
    expect(callback).toHaveBeenCalledExactlyOnceWith();
    expect(screen.queryByRole("menu")).toBeNull();
  }
  expect(onRow).not.toHaveBeenCalled();
});

test("new-window pending refuses another click and presents the supplied error", () => {
  const onNewWindow = vi.fn();
  render(
    <OrganizedSessionMenu
      {...props({
        onNewWindow,
        newWindowPending: true,
        newWindowError: "The session is unavailable.",
      })}
    />,
  );
  open();
  const item = screen.getByRole("menuitem", { name: "Opening window…" });
  expect(item.getAttribute("aria-disabled")).toBe("true");
  fireEvent.click(item);
  expect(onNewWindow).not.toHaveBeenCalled();
  expect(screen.getByRole("alert").textContent).toBe("The session is unavailable.");
});

test("choosing a color closes both menus and returns focus to the session trigger", async () => {
  vi.useFakeTimers();
  const onColor = vi.fn();
  render(<OrganizedSessionMenu {...props({ color: "orange", onColor })} />);
  const trigger = screen.getByRole("button", { name: "Session menu" });
  trigger.focus();
  fireEvent.keyDown(trigger, { key: "ArrowDown" });
  const color = screen.getByRole("menuitem", { name: "Color label" });
  color.focus();
  fireEvent.keyDown(color, { key: "ArrowRight" });
  await act(() => vi.advanceTimersByTimeAsync(0));
  const orange = screen.getByRole("menuitemradio", { name: "Orange color label" });
  expect(orange.getAttribute("aria-checked")).toBe("true");
  fireEvent.click(screen.getByRole("menuitemradio", { name: "Blue color label" }));
  await act(() => vi.advanceTimersByTimeAsync(0));
  expect(onColor).toHaveBeenCalledExactlyOnceWith("blue");
  expect(screen.queryByRole("menu")).toBeNull();
  expect(document.activeElement).toBe(trigger);
});

function expectItemIcon(name: string, icon: string) {
  const item = screen.getByRole("menuitem", { name });
  const icons = item.querySelectorAll('svg.ico[aria-hidden="true"]');
  expect(icons).toHaveLength(1);
  expect(icons[0]?.getAttribute("data-icon")).toBe(icon);
}

test("every item carries exactly one mapped icon before its text", () => {
  const callbacks = {
    onNewWindow: vi.fn(),
    onOpenTerminal: vi.fn(),
    onUnread: vi.fn(),
    onColor: vi.fn(),
    onFork: vi.fn(),
    onOpenTranscript: vi.fn(),
    onExport: vi.fn(),
    onNewChatInWorkspace: vi.fn(),
    onClose: vi.fn(),
  };
  render(<OrganizedSessionMenu {...props(callbacks)} />);
  open();
  for (const [name, icon] of [
    ["New window", "newWindow"],
    ["Open in terminal", "terminal"],
    ["Rename…", "rename"],
    ["Pin", "pin"],
    ["Mark as unread", "markUnread"],
    ["Color label", "color"],
    ["Copy ID", "copy"],
    ["Fork", "fork"],
    ["Open transcript in VS Code", "transcript"],
    ["Select turns to export", "export"],
    ["New chat in workspace…", "newChat"],
    ["Archive", "archive"],
    ["Close session", "close"],
  ] as const) {
    expectItemIcon(name, icon);
  }
});

test("conditional items swap their icon with their label", () => {
  const callbacks = { onUnread: vi.fn(), onNewWindow: vi.fn() };
  render(
    <OrganizedSessionMenu
      {...props({
        pinned: true,
        archived: true,
        unread: true,
        newWindowPending: true,
        ...callbacks,
      })}
    />,
  );
  open();
  expectItemIcon("Opening window…", "newWindow");
  expectItemIcon("Unpin", "unpin");
  expectItemIcon("Mark as read", "markRead");
  expectItemIcon("Unarchive", "unarchive");
});
