/** ArchiveBar's own tests. */
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ArchiveBar } from "./ArchiveBar";

afterEach(cleanup);

function mount(props?: { showArchived?: boolean; archivedCount?: number }) {
  const onToggle = vi.fn();
  const showArchived = props?.showArchived ?? false;
  const archivedCount = props?.archivedCount ?? 0;
  const { container } = render(
    <ArchiveBar showArchived={showArchived} archivedCount={archivedCount} onToggle={onToggle} />,
  );
  return { container, onToggle };
}

test("renders nothing when there is nothing archived and the archived list is not showing", () => {
  const { container } = mount({ showArchived: false, archivedCount: 0 });
  expect(container.firstChild).toBeNull();
});

test("renders the chip with the count, unpressed, and no note when there is something archived", () => {
  const { container } = mount({ showArchived: false, archivedCount: 3 });
  const bar = container.querySelector("div.archive-bar");
  expect(bar).not.toBeNull();
  const chip = container.querySelector("button.archive-chip");
  expect(chip?.textContent).toBe("Archived (3)");
  expect(chip?.getAttribute("aria-pressed")).toBe("false");
  expect(container.querySelector("p.archive-note")).toBeNull();
});

test("shows the retention note, pressed, while the archived list is showing", () => {
  const { container } = mount({ showArchived: true, archivedCount: 3 });
  const chip = container.querySelector("button.archive-chip");
  expect(chip?.getAttribute("aria-pressed")).toBe("true");
  const note = container.querySelector("p.archive-note");
  expect(note?.textContent).toBe(
    "Archiving closes an open chat and hides it from this list. Claude Code still deletes each transcript 30 days " +
      "after its last activity by default (cleanupPeriodDays).",
  );
});

test("stays visible, pressed, with Archived (0) when showArchived is true but the count is 0", () => {
  // The empty-but-pressed case: the user just unarchived the last row while looking at the
  // archived list, and still needs the chip to toggle back off.
  const { container } = mount({ showArchived: true, archivedCount: 0 });
  expect(container.querySelector("div.archive-bar")).not.toBeNull();
  expect(container.querySelector("button.archive-chip")?.textContent).toBe("Archived (0)");
  expect(container.querySelector("p.archive-note")).not.toBeNull();
});

test("a click calls onToggle, once, and nothing else", () => {
  const { container, onToggle } = mount({ showArchived: false, archivedCount: 2 });
  const chip = container.querySelector("button.archive-chip") as HTMLElement;
  fireEvent.click(chip);
  expect(onToggle).toHaveBeenCalledTimes(1);
});
