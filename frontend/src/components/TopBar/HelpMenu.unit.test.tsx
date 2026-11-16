import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resetHelp, useHelpTarget } from "../Help/helpStore";
import { HelpMenu } from "./HelpMenu";

afterEach(cleanup);
beforeEach(resetHelp);

const ROW_LABELS = [
  "Welcome to VIKI Agent",
  "Take the tour",
  "Keyboard shortcuts",
  "What's new",
  "Troubleshooting",
  "Glossary",
  "Ask about VIKI Agent",
];

function open(): void {
  fireEvent.click(screen.getByRole("button", { name: "Help" }));
}

describe("HelpMenu", () => {
  it("the toggle starts closed and opens a role=menu panel labelled Help on click", () => {
    render(<HelpMenu />);
    const toggle = screen.getByRole("button", { name: "Help" });
    expect(toggle.getAttribute("aria-haspopup")).toBe("menu");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("menu")).toBeNull();
    open();
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    screen.getByRole("menu", { name: "Help" }); // throws if absent
  });

  it("renders every row in the documented order, with one separator before the last", () => {
    render(<HelpMenu />);
    open();
    const menu = screen.getByRole("menu");
    const rows = within(menu)
      .getAllByRole("menuitem")
      .map((row) => row.querySelector(".help-menu-row-label")?.textContent);
    expect(rows).toEqual(ROW_LABELS);
    expect(menu.querySelectorAll(".help-menu-separator")).toHaveLength(1);
    const lastRow = within(menu).getByText("Ask about VIKI Agent").closest(".help-menu-row-wrap");
    expect(lastRow?.querySelector(".help-menu-separator")).not.toBeNull();
  });

  it("with onSetupGuide, Setup guide follows Welcome, calls it once and opens no help target", () => {
    const onSetupGuide = vi.fn();
    render(<HelpMenu onSetupGuide={onSetupGuide} />);
    const toggle = screen.getByRole("button", { name: "Help" });
    const { result } = renderHook(() => useHelpTarget());
    open();
    const menu = screen.getByRole("menu");
    const rows = within(menu)
      .getAllByRole("menuitem")
      .map((row) => row.querySelector(".help-menu-row-label")?.textContent);
    expect(rows).toEqual([ROW_LABELS[0], "Setup guide", ...ROW_LABELS.slice(1)]);
    expect(menu.querySelectorAll(".help-menu-separator")).toHaveLength(1);
    const lastRow = within(menu).getByText("Ask about VIKI Agent").closest(".help-menu-row-wrap");
    expect(lastRow?.querySelector(".help-menu-separator")).not.toBeNull();
    fireEvent.click(screen.getByText("Setup guide"));
    expect(onSetupGuide).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(toggle);
    expect(result.current).toBeNull();
  });

  it("shows the ? key next to Keyboard shortcuts, and no other row", () => {
    render(<HelpMenu />);
    open();
    const shortcutsRow = screen.getByText("Keyboard shortcuts").closest('[role="menuitem"]');
    expect(shortcutsRow?.querySelector(".kbd")?.textContent).toBe("?");
    const otherRow = screen.getByText("Glossary").closest('[role="menuitem"]');
    expect(otherRow?.querySelector(".kbd")).toBeNull();
  });

  it.each([
    ["Welcome to VIKI Agent", { kind: "topic", id: "welcome" }],
    ["Take the tour", { kind: "tour" }],
    ["Keyboard shortcuts", { kind: "topic", id: "keyboard-shortcuts" }],
    ["What's new", { kind: "whats-new" }],
    ["Troubleshooting", { kind: "topic", id: "troubleshooting" }],
    ["Glossary", { kind: "topic", id: "glossary" }],
    ["Ask about VIKI Agent", { kind: "search", query: "" }],
  ])(
    "selecting %s closes the menu, refocuses the toggle, and opens its target",
    (label, target) => {
      render(<HelpMenu />);
      const toggle = screen.getByRole("button", { name: "Help" });
      const { result } = renderHook(() => useHelpTarget());
      open();
      fireEvent.click(screen.getByText(label));
      expect(screen.queryByRole("menu")).toBeNull();
      expect(document.activeElement).toBe(toggle);
      expect(result.current).toEqual(target);
    },
  );

  it("Escape closes the menu", () => {
    render(<HelpMenu />);
    open();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("Escape returns focus to the toggle", () => {
    render(<HelpMenu />);
    const toggle = screen.getByRole("button", { name: "Help" });
    open();
    expect(document.activeElement).not.toBe(toggle);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(document.activeElement).toBe(toggle);
  });

  it("Escape is ignored while composing (IME guard)", () => {
    render(<HelpMenu />);
    open();
    fireEvent.keyDown(window, { key: "Escape", isComposing: true });
    screen.getByRole("menu"); // throws if absent
  });

  it("clicking outside the menu closes it", () => {
    render(
      <div>
        <HelpMenu />
        <button type="button">elsewhere</button>
      </div>,
    );
    open();
    fireEvent.mouseDown(screen.getByText("elsewhere"));
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("ArrowDown/ArrowUp/Home/End move focus among the rows", () => {
    render(<HelpMenu />);
    open();
    const menu = screen.getByRole("menu");
    const rows = within(menu).getAllByRole("menuitem");
    expect(document.activeElement).toBe(rows[0]);

    fireEvent.keyDown(rows[0] as HTMLElement, { key: "ArrowDown" });
    expect(document.activeElement).toBe(rows[1]);

    fireEvent.keyDown(rows[1] as HTMLElement, { key: "ArrowUp" });
    expect(document.activeElement).toBe(rows[0]);

    fireEvent.keyDown(rows[0] as HTMLElement, { key: "ArrowUp" });
    expect(document.activeElement).toBe(rows[rows.length - 1]);

    fireEvent.keyDown(rows[rows.length - 1] as HTMLElement, { key: "Home" });
    expect(document.activeElement).toBe(rows[0]);

    fireEvent.keyDown(rows[0] as HTMLElement, { key: "End" });
    expect(document.activeElement).toBe(rows[rows.length - 1]);
  });

  it.each(["ArrowDown", "ArrowUp", "Home", "End"])(
    "Tab closes the menu and leaves %s to the destination",
    (key) => {
      const onKeyDown = vi.fn();
      render(
        <div>
          <HelpMenu />
          <textarea aria-label="destination" onKeyDown={onKeyDown} />
        </div>,
      );
      open();
      const firstRow = screen.getAllByRole("menuitem")[0] as HTMLElement;
      expect(fireEvent.keyDown(firstRow, { key: "Tab" })).toBe(true);
      expect(screen.queryByRole("menu")).toBeNull();
      const destination = screen.getByRole("textbox", { name: "destination" });
      act(() => destination.focus());

      expect(fireEvent.keyDown(destination, { key })).toBe(true);
      expect(onKeyDown).toHaveBeenCalledOnce();
      expect(document.activeElement).toBe(destination);
      expect(screen.queryByRole("menu")).toBeNull();
    },
  );

  it("focus leaving the menu closes it without returning focus to the toggle", () => {
    render(
      <div>
        <button type="button">elsewhere</button>
        <HelpMenu />
      </div>,
    );
    open();
    const destination = screen.getByRole("button", { name: "elsewhere" });
    act(() => destination.focus());
    expect(document.activeElement).toBe(destination);
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("navigation keys are ignored while composing and for keyCode 229", () => {
    render(<HelpMenu />);
    open();
    const firstRow = screen.getAllByRole("menuitem")[0] as HTMLElement;
    for (const guard of [{ isComposing: true }, { keyCode: 229 }]) {
      for (const key of ["ArrowDown", "ArrowUp", "Home", "End", "Tab", "Escape"]) {
        expect(fireEvent.keyDown(firstRow, { key, ...guard })).toBe(true);
        expect(document.activeElement).toBe(firstRow);
        screen.getByRole("menu");
      }
    }
  });
});
