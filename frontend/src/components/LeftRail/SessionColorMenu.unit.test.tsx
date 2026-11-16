import * as Dropdown from "@radix-ui/react-dropdown-menu";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { RailDrawer } from "../Shell/RailDrawer";
import { SessionColorMenu } from "./SessionColorMenu";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function menu(extra: Partial<Parameters<typeof SessionColorMenu>[0]> = {}) {
  const onColor = vi.fn();
  render(
    <Dropdown.Root defaultOpen={true} modal={false}>
      <Dropdown.Trigger>Session menu</Dropdown.Trigger>
      <Dropdown.Content>
        <SessionColorMenu color={null} pending={false} onColor={onColor} {...extra} />
      </Dropdown.Content>
    </Dropdown.Root>,
  );
  fireEvent.click(screen.getByRole("menuitem", { name: "Color label" }));
  return onColor;
}

test.each([
  ["blue", "Blue"],
  ["green", "Green"],
  ["orange", "Orange"],
  ["purple", "Purple"],
  ["red", "Red"],
  ["slate", "Teal"],
] as const)("palette exposes %s as %s and selects its original key", (color, name) => {
  const onColor = menu({ color });
  expect(screen.getAllByRole("menuitemradio")).toHaveLength(7);
  const label = `${name} color label`;
  const item = screen.getByRole("menuitemradio", { name: label });
  expect(item.getAttribute("aria-checked")).toBe("true");
  expect(item.querySelector(".session-color-name")?.textContent).toBe(name);
  const swatch = item.querySelector<HTMLElement>("[data-color]");
  expect(swatch?.getAttribute("data-color")).toBe(color);
  expect(swatch?.style.backgroundColor).toBe(`var(--label-${color})`);
  expect(
    screen.getByRole("menuitemradio", { name: "No color label" }).getAttribute("aria-checked"),
  ).toBe("false");
  fireEvent.click(item);
  expect(onColor).toHaveBeenCalledExactlyOnceWith(color);
});

test("No color is selected by default and clears the label with null", () => {
  const onColor = menu();
  expect(
    screen.getAllByRole("menuitemradio").map((item) => item.getAttribute("aria-label")),
  ).toEqual([
    "No color label",
    "Blue color label",
    "Green color label",
    "Orange color label",
    "Purple color label",
    "Red color label",
    "Teal color label",
  ]);
  const item = screen.getByRole("menuitemradio", { name: "No color label" });
  expect(item.getAttribute("aria-checked")).toBe("true");
  fireEvent.click(item);
  expect(onColor).toHaveBeenCalledExactlyOnceWith(null);
});

test("pending refuses opening the palette", () => {
  const onColor = menu({ pending: true });
  expect(screen.getByRole("menuitem", { name: "Color label" }).getAttribute("aria-disabled")).toBe(
    "true",
  );
  expect(screen.queryByRole("menuitemradio")).toBeNull();
  expect(onColor).not.toHaveBeenCalled();
});

test("trigger holds one hidden color icon and keeps its accessible name", () => {
  menu();
  const trigger = screen.getByRole("menuitem", { name: "Color label" });
  const icons = trigger.querySelectorAll('svg[aria-hidden="true"][data-icon="color"]');
  expect(icons).toHaveLength(1);
});

test("drawer submenu stays inside the focus boundary and ArrowLeft returns to Color label", async () => {
  vi.useFakeTimers();
  render(
    <RailDrawer side="left" label="Sessions" open={true} onClose={vi.fn()}>
      <Dropdown.Root defaultOpen={true} modal={false}>
        <Dropdown.Trigger>Session menu</Dropdown.Trigger>
        <Dropdown.Content>
          <SessionColorMenu
            color={null}
            pending={false}
            onColor={vi.fn()}
            portalContainer={document.querySelector(".rail-drawer")}
          />
        </Dropdown.Content>
      </Dropdown.Root>
    </RailDrawer>,
  );
  const trigger = screen.getByRole("menuitem", { name: "Color label" });
  trigger.focus();
  fireEvent.keyDown(trigger, { key: "ArrowRight" });
  await act(() => vi.advanceTimersByTimeAsync(0));
  const first = screen.getByRole("menuitemradio", { name: "No color label" });
  expect(screen.getByRole("dialog").contains(first)).toBe(true);
  expect(document.activeElement).toBe(first);
  fireEvent.keyDown(first, { key: "ArrowDown" });
  await act(() => vi.advanceTimersByTimeAsync(0));
  const blue = screen.getByRole("menuitemradio", { name: "Blue color label" });
  expect(document.activeElement).toBe(blue);
  fireEvent.keyDown(blue, { key: "ArrowLeft" });
  await act(() => vi.advanceTimersByTimeAsync(0));
  expect(screen.queryByRole("menuitemradio")).toBeNull();
  expect(document.activeElement).toBe(trigger);
});
