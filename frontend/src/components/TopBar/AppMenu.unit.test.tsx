import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { WorkspacesListing } from "../../clients/app/shellApi";
import type { ShellSnapshot } from "../../state/shell";
import { capabilitiesFixture, listingFixture } from "../../testShell";
import { AppMenu } from "./AppMenu";

afterEach(cleanup);

function snapshotWith(
  capabilitiesOverrides: Parameters<typeof capabilitiesFixture>[0] = {},
  workspaces: WorkspacesListing | null = listingFixture(),
): ShellSnapshot {
  return {
    capabilities: capabilitiesFixture(capabilitiesOverrides),
    capabilitiesStatus: "ready",
    settings: null,
    settingsStatus: "ready",
    workspaces,
    workspacesStatus: workspaces === null ? "unavailable" : "ready",
    errors: { capabilities: "", settings: "", workspaces: "" },
    recovery: false,
    checkRunning: null,
  };
}

const ROW_LABELS = [
  "Settings",
  "Switch workspace",
  "Diagnostics",
  "Data locations",
  "Export diagnostics",
  "Import settings",
  "Reset",
  "Search conversations",
  "Search the vault",
];

function open(): void {
  fireEvent.click(screen.getByRole("button", { name: "App menu" }));
}

describe("AppMenu", () => {
  it("the toggle starts closed and opens a role=menu panel on click", () => {
    render(<AppMenu snapshot={snapshotWith()} view="chat" onCommand={vi.fn()} />);
    const toggle = screen.getByRole("button", { name: "App menu" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("menu")).toBeNull();
    open();
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    screen.getByRole("menu"); // throws if absent
  });

  it("renders every row in the documented order, and no provider.setup row when the provider can start", () => {
    render(<AppMenu snapshot={snapshotWith()} view="chat" onCommand={vi.fn()} />);
    open();
    const menu = screen.getByRole("menu");
    const rows = within(menu)
      .getAllByRole("menuitem")
      .map((row) => row.querySelector(".app-menu-row-label")?.textContent);
    expect(rows).toEqual(ROW_LABELS);
    expect(screen.queryByText("Set up provider")).toBeNull();
  });

  it("adds the provider.setup row only when the provider cannot start", () => {
    render(
      <AppMenu
        snapshot={snapshotWith({ provider: { can_start: false } })}
        view="chat"
        onCommand={vi.fn()}
      />,
    );
    open();
    const menu = screen.getByRole("menu");
    const rows = within(menu)
      .getAllByRole("menuitem")
      .map((row) => row.querySelector(".app-menu-row-label")?.textContent);
    expect(rows).toEqual([...ROW_LABELS, "Set up provider"]);
  });

  it("shows the settings.open shortcut", () => {
    render(<AppMenu snapshot={snapshotWith()} view="chat" onCommand={vi.fn()} />);
    open();
    const settingsRow = screen.getByText("Settings").closest('[role="menuitem"]');
    expect(settingsRow?.textContent).toContain("Ctrl+,");
  });

  it("clicking an enabled row closes the menu and dispatches its command", () => {
    const onCommand = vi.fn();
    render(<AppMenu snapshot={snapshotWith()} view="chat" onCommand={onCommand} />);
    open();
    fireEvent.click(screen.getByText("Settings"));
    expect(onCommand).toHaveBeenCalledWith("settings.open");
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("a refused row is focusable, aria-disabled, names its reason, and its click is a no-op", () => {
    const onCommand = vi.fn();
    render(
      <AppMenu snapshot={snapshotWith({}, listingFixture([]))} view="chat" onCommand={onCommand} />,
    );
    open();
    const row = screen.getByText("Switch workspace").closest("button") as HTMLButtonElement;
    expect(row.getAttribute("aria-disabled")).toBe("true");
    expect(row.tabIndex).toBe(0);
    const describedBy = row.getAttribute("aria-describedby");
    expect(describedBy).not.toBeNull();
    expect(document.getElementById(describedBy as string)?.textContent).toBe(
      "No workspace is registered yet.",
    );
    fireEvent.click(row);
    expect(onCommand).not.toHaveBeenCalled();
  });

  it("Escape closes the menu", () => {
    render(<AppMenu snapshot={snapshotWith()} view="chat" onCommand={vi.fn()} />);
    open();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("Escape returns focus to the toggle", () => {
    render(<AppMenu snapshot={snapshotWith()} view="chat" onCommand={vi.fn()} />);
    const toggle = screen.getByRole("button", { name: "App menu" });
    open();
    expect(document.activeElement).not.toBe(toggle);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(document.activeElement).toBe(toggle);
  });

  it("clicking an enabled row returns focus to the toggle, same as Escape", () => {
    render(<AppMenu snapshot={snapshotWith()} view="chat" onCommand={vi.fn()} />);
    const toggle = screen.getByRole("button", { name: "App menu" });
    open();
    const settingsRow = screen.getByText("Settings").closest("button") as HTMLButtonElement;
    settingsRow.focus();
    expect(document.activeElement).toBe(settingsRow);
    fireEvent.click(settingsRow);
    expect(document.activeElement).toBe(toggle);
  });

  it("Escape is ignored while composing (IME guard)", () => {
    render(<AppMenu snapshot={snapshotWith()} view="chat" onCommand={vi.fn()} />);
    open();
    fireEvent.keyDown(window, { key: "Escape", isComposing: true });
    screen.getByRole("menu"); // throws if absent
  });

  it("clicking outside the menu closes it", () => {
    render(
      <div>
        <AppMenu snapshot={snapshotWith()} view="chat" onCommand={vi.fn()} />
        <button type="button">elsewhere</button>
      </div>,
    );
    open();
    fireEvent.mouseDown(screen.getByText("elsewhere"));
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("ArrowDown/ArrowUp/Home/End move focus among the rows", () => {
    render(<AppMenu snapshot={snapshotWith()} view="chat" onCommand={vi.fn()} />);
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
    // The last search row remains reachable even when disabled.
    expect(
      screen.getByText("Search the vault").closest("button")?.getAttribute("aria-disabled"),
    ).toBe("true");
  });

  it("End, and ArrowUp from the first row, land on the last row not hidden by display: none", () => {
    render(<AppMenu snapshot={snapshotWith()} view="chat" onCommand={vi.fn()} />);
    open();
    const menu = screen.getByRole("menu");
    const rows = within(menu).getAllByRole("menuitem");
    const lastRow = rows[rows.length - 1] as HTMLButtonElement;
    lastRow.style.display = "none";
    const lastVisible = rows[rows.length - 2] as HTMLElement;

    fireEvent.keyDown(rows[0] as HTMLElement, { key: "End" });
    expect(document.activeElement).toBe(lastVisible);

    act(() => (rows[0] as HTMLElement).focus());
    fireEvent.keyDown(rows[0] as HTMLElement, { key: "ArrowUp" });
    expect(document.activeElement).toBe(lastVisible);
  });

  it.each(["ArrowDown", "ArrowUp", "Home", "End"])(
    "Tab closes the menu and leaves %s to the destination",
    (key) => {
      const onKeyDown = vi.fn();
      render(
        <div>
          <AppMenu snapshot={snapshotWith()} view="chat" onCommand={vi.fn()} />
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
        <AppMenu snapshot={snapshotWith()} view="chat" onCommand={vi.fn()} />
      </div>,
    );
    open();
    const destination = screen.getByRole("button", { name: "elsewhere" });
    act(() => destination.focus());
    expect(document.activeElement).toBe(destination);
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("navigation keys are ignored while composing and for keyCode 229", () => {
    render(<AppMenu snapshot={snapshotWith()} view="chat" onCommand={vi.fn()} />);
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

  it("does not repeat the top bar's Help entry", () => {
    render(<AppMenu snapshot={snapshotWith()} view="chat" onCommand={vi.fn()} />);
    open();
    expect(screen.queryByRole("menuitem", { name: "Help" })).toBeNull();
  });
});
