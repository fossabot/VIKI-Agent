/** RowMenu's own tests (no frozen acceptance test owns this component yet). */

import { cleanup, fireEvent, render } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { readCss, ruleIn } from "../../testing/css";
import { RowMenu } from "./RowMenu";

afterEach(cleanup);

function mount(props?: { onArchive?: () => void; archived?: boolean }) {
  const onFork = vi.fn();
  const onOpenTranscript = vi.fn();
  const onClose = vi.fn();
  const onArchive = props?.onArchive;
  const archived = props?.archived ?? false;
  const renderProps: ComponentProps<typeof RowMenu> = {
    onFork,
    onOpenTranscript,
    onClose,
    archived,
  };
  if (onArchive) {
    renderProps.onArchive = onArchive;
  }
  const { container } = render(<RowMenu {...renderProps} />);
  return { container, onFork, onOpenTranscript, onClose, onArchive, archived };
}

test("renders the three actions in the approved order and words (when onArchive is not set)", () => {
  const { container } = mount();
  const items = [...container.querySelectorAll(".mi")];
  expect(items.map((el) => el.textContent)).toEqual([
    "Fork",
    "Open transcript in VS Code",
    "Close session",
  ]);
});

test("renders four actions with Archive item in the correct position when onArchive is set", () => {
  const onArchive = vi.fn();
  const { container } = mount({ onArchive, archived: false });
  const items = [...container.querySelectorAll(".mi")];
  expect(items.map((el) => el.textContent)).toEqual([
    "Fork",
    "Open transcript in VS Code",
    "Archive",
    "Close session",
  ]);
});

test("shows Unarchive when archived is true", () => {
  const onArchive = vi.fn();
  const { container } = mount({ onArchive, archived: true });
  const items = [...container.querySelectorAll(".mi")];
  expect(items.map((el) => el.textContent)).toEqual([
    "Fork",
    "Open transcript in VS Code",
    "Unarchive",
    "Close session",
  ]);
});

test("each action calls its own callback, and only that one", () => {
  const onArchive = vi.fn();
  const { container, onFork, onOpenTranscript, onClose } = mount({ onArchive });
  const [fork, openTranscript, archive, close] = [
    ...container.querySelectorAll(".mi"),
  ] as HTMLElement[];
  fireEvent.click(fork as HTMLElement);
  expect(onFork).toHaveBeenCalledTimes(1);
  expect(onOpenTranscript).not.toHaveBeenCalled();
  expect(onArchive).not.toHaveBeenCalled();
  expect(onClose).not.toHaveBeenCalled();
  fireEvent.click(openTranscript as HTMLElement);
  expect(onOpenTranscript).toHaveBeenCalledTimes(1);
  fireEvent.click(archive as HTMLElement);
  expect(onArchive).toHaveBeenCalledTimes(1);
  fireEvent.click(close as HTMLElement);
  expect(onClose).toHaveBeenCalledTimes(1);
});

test("the panel chrome is base.css's shared .menu-panel, never .row-menu's own", () => {
  // RowMenu.css's header comment says the panel chrome comes from the shared `.menu-panel`, and
  // comments can rot: a stale one would send a maintainer to `.row-menu` to restyle the
  // dropdowns. Component CSS loads after base.css, so a fill added here would really take
  // effect and silently fork chrome that four menus share one rule to keep identical (see
  // base.css).
  const { container } = mount();
  expect(container.querySelector(".row-menu")?.classList.contains("menu-panel")).toBe(true);
  const rowMenu = ruleIn(readCss(import.meta.url, "RowMenu.css"), ".row-menu");
  expect(rowMenu).not.toMatch(/background|border(?!-box)|box-shadow/);
});

test("each action shows exactly one mapped icon before its text", () => {
  const onArchive = vi.fn();
  const { container } = mount({ onArchive, archived: false });
  const items = [...container.querySelectorAll(".mi")] as HTMLElement[];
  const expected = ["fork", "transcript", "archive", "close"];
  items.forEach((item, index) => {
    const icons = item.querySelectorAll('svg.ico[aria-hidden="true"]');
    expect(icons).toHaveLength(1);
    expect(icons[0]?.getAttribute("data-icon")).toBe(expected[index]);
  });
});

test("Unarchive keeps the unarchive icon when archived is true", () => {
  const onArchive = vi.fn();
  const { container } = mount({ onArchive, archived: true });
  const archiveItem = [...container.querySelectorAll(".mi")][2];
  expect(archiveItem?.querySelector("svg.ico")?.getAttribute("data-icon")).toBe("unarchive");
});

test("no row is highlighted by default; Close session carries the warm, warning tint", () => {
  // The design freezes a hover state, not the menu's opening state —
  // Fork must not read as permanently selected before the user has touched anything.
  const onArchive = vi.fn();
  const { container } = mount({ onArchive });
  const items = [...container.querySelectorAll(".mi")];
  expect(items.every((el) => !el.classList.contains("on"))).toBe(true);
  expect(items[3]?.classList.contains("row-menu-close")).toBe(true);
});
