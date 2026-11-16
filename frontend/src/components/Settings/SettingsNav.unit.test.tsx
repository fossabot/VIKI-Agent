import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { SettingsPage } from "../../commands";
import { SettingsNav } from "./SettingsNav";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const ORDER = [
  "General",
  "Appearance and Accessibility",
  "Chat",
  "Providers",
  "Workspaces and Vault",
  "Instructions and Persona",
  "Privacy and Storage",
  "Diagnostics and About",
];

function renderNav(page: SettingsPage = "general"): ReturnType<typeof vi.fn> {
  const onPage = vi.fn();
  render(<SettingsNav page={page} onPage={onPage} panelId="panel" />);
  return onPage;
}

/** jsdom has no matchMedia at all; `useLayoutMode` reads the absence as the widest layout. */
function stubWidth(narrow: boolean): void {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: narrow ? false : query.includes("800"),
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
}

test("eight tabs in the set order, one of them in the tab order", () => {
  renderNav("chat");
  const tabs = screen.getAllByRole("tab");
  expect(tabs.map((tab) => tab.querySelector(".settings-tab-label")?.textContent)).toEqual(ORDER);
  expect(
    tabs.every(
      (tab) => document.getElementById(tab.getAttribute("aria-describedby") ?? "")?.textContent,
    ),
  ).toBe(true);
  expect(tabs.map((tab) => tab.getAttribute("aria-selected"))).toEqual([
    "false",
    "false",
    "true",
    "false",
    "false",
    "false",
    "false",
    "false",
  ]);
  expect(tabs.map((tab) => tab.tabIndex)).toEqual([-1, -1, 0, -1, -1, -1, -1, -1]);
  expect(tabs.every((tab) => tab.getAttribute("aria-controls") === "panel")).toBe(true);
});

test("the arrows select and move focus, wrapping, and Home and End reach the ends", () => {
  const onPage = renderNav("general");
  const list = screen.getByRole("tablist");
  fireEvent.keyDown(list, { key: "ArrowDown" });
  expect(onPage).toHaveBeenLastCalledWith("appearance");
  expect(document.activeElement?.getAttribute("data-page")).toBe("appearance");
  // The selection is the caller's state, so the component still sees `general`: ArrowUp from
  // there wraps to the last tab.
  fireEvent.keyDown(list, { key: "ArrowUp" });
  expect(onPage).toHaveBeenLastCalledWith("diagnostics");
  fireEvent.keyDown(list, { key: "ArrowRight" });
  expect(onPage).toHaveBeenLastCalledWith("appearance");
  fireEvent.keyDown(list, { key: "End" });
  expect(onPage).toHaveBeenLastCalledWith("diagnostics");
  fireEvent.keyDown(list, { key: "Home" });
  expect(onPage).toHaveBeenLastCalledWith("general");
});

test("a key that belongs to the IME moves nothing", () => {
  const onPage = renderNav("general");
  const list = screen.getByRole("tablist");
  fireEvent.keyDown(list, { key: "ArrowDown", isComposing: true });
  fireEvent.keyDown(list, { key: "ArrowDown", keyCode: 229 });
  expect(onPage).not.toHaveBeenCalled();
});

test("the announced orientation follows the 800px breakpoint, not the component", () => {
  stubWidth(false);
  renderNav();
  expect(screen.getByRole("tablist").getAttribute("aria-orientation")).toBe("vertical");
  cleanup();
  stubWidth(true);
  renderNav();
  expect(screen.getByRole("tablist").getAttribute("aria-orientation")).toBe("horizontal");
});
