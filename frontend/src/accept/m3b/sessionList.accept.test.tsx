/** Acceptance (frozen): the session list's groups, rows, live marker and Other toggle. */
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { SessionList } from "../../components/LeftRail/SessionList";
import type { SessionGroup } from "../../state/rows";
import { localIso, sessionRow } from "./fakes";

afterEach(cleanup);

const NOW = new Date(2026, 8, 12, 18, 0).getTime();
const live = sessionRow({
  session_id: "live",
  title: "Plan a weekly review of my notes in VIKI",
  modified: localIso(2026, 9, 12, 18, 10),
});
const today = sessionRow({
  session_id: "today",
  title: "",
  first_prompt: "Dashboard v1 → reverted",
  modified: localIso(2026, 9, 12, 17, 20),
});
const old = sessionRow({
  session_id: "old",
  title: "Dashboard v0 at-a-glance",
  modified: localIso(2026, 9, 11, 9, 0),
});
const other = sessionRow({
  session_id: "other",
  title: "Some other tool",
  modified: localIso(2026, 9, 12, 12, 0),
  other: true,
});
const groups: SessionGroup[] = [
  { label: "Today", rows: [live, today] },
  { label: "Yesterday · 09-11", rows: [old] },
];

function mount(onOpen = vi.fn(), selectedId: string | null = "live") {
  const utils = render(
    <SessionList
      groups={groups}
      other={[other]}
      selectedId={selectedId}
      liveIds={new Set(["live"])}
      nowMs={NOW}
      onOpen={onOpen}
    />,
  );
  return { ...utils, onOpen };
}

const rowsOf = (container: HTMLElement) => [...container.querySelectorAll<HTMLElement>(".sess")];

test("groups render their label then their rows, titles falling back to the first prompt", () => {
  const { container } = mount();
  const labels = [...container.querySelectorAll(".lbl")].map((el) => el.textContent);
  expect(labels).toEqual(["Today", "Yesterday · 09-11", "Other (1)"]);
  const titles = rowsOf(container).map((row) => row.querySelector(".t")?.textContent);
  expect(titles).toEqual([
    "Plan a weekly review of my notes in VIKI",
    "Dashboard v1 → reverted",
    "Dashboard v0 at-a-glance",
  ]);
  for (const row of rowsOf(container))
    expect(row.querySelector(".t")?.classList.contains("jp")).toBe(true);
});

test("Today rows show HH:MM, older rows nothing, the live row a breathing dot and · live", () => {
  const { container } = mount();
  const [liveRow, todayRow, oldRow] = rowsOf(container);
  expect(liveRow?.querySelector(".m")?.textContent).toBe("18:10 · live");
  expect(liveRow?.querySelector(".sess-dot")?.classList.contains("breathe")).toBe(true);
  expect(todayRow?.querySelector(".m")?.textContent).toBe("17:20");
  expect(todayRow?.querySelector(".sess-dot")).toBeNull();
  expect(oldRow?.querySelector(".m")).toBeNull();
});

test("the selected row and the live row carry their classes", () => {
  const { container } = mount(vi.fn(), "today");
  const [liveRow, todayRow] = rowsOf(container);
  expect(liveRow?.classList.contains("sess-live")).toBe(true);
  expect(liveRow?.classList.contains("sess-selected")).toBe(false);
  expect(todayRow?.classList.contains("sess-selected")).toBe(true);
  expect(todayRow?.classList.contains("sess-live")).toBe(false);
  expect(todayRow?.getAttribute("data-session-id")).toBe("today");
});

test("clicking a row, or pressing Enter on it, opens that session", () => {
  const { container, onOpen } = mount();
  const [, todayRow, oldRow] = rowsOf(container);
  if (!todayRow || !oldRow) throw new Error("rows missing");
  fireEvent.click(todayRow);
  expect(onOpen).toHaveBeenLastCalledWith("today");
  fireEvent.keyDown(oldRow, { key: "Enter" });
  expect(onOpen).toHaveBeenLastCalledWith("old");
  expect(onOpen).toHaveBeenCalledTimes(2);
  expect(todayRow.getAttribute("role")).toBe("button");
});

test("Other (N) is collapsed until clicked, then shows those rows without a time", () => {
  const { container, onOpen } = mount();
  expect(rowsOf(container)).toHaveLength(3);
  const toggle = container.querySelector<HTMLElement>(".sess-other");
  expect(toggle?.textContent).toBe("Other (1)");
  if (!toggle) throw new Error("toggle missing");
  fireEvent.click(toggle);
  const rows = rowsOf(container);
  expect(rows).toHaveLength(4);
  expect(rows[3]?.querySelector(".t")?.textContent).toBe("Some other tool");
  expect(rows[3]?.querySelector(".m")).toBeNull();
  fireEvent.click(rows[3] as HTMLElement);
  expect(onOpen).toHaveBeenLastCalledWith("other");
  fireEvent.click(toggle);
  expect(rowsOf(container)).toHaveLength(3);
});

test("no Other toggle when there are no other sessions", () => {
  const { container } = render(
    <SessionList
      groups={groups}
      other={[]}
      selectedId={null}
      liveIds={new Set()}
      nowMs={NOW}
      onOpen={vi.fn()}
    />,
  );
  expect(container.querySelector(".sess-other")).toBeNull();
  expect(container.querySelector(".sess-selected")).toBeNull();
});
