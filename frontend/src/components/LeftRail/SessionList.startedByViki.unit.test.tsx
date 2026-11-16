/**
 * SessionList's optional `startedByViki` prop. The frozen m3b acceptance test and
 * SessionList.unit.test.tsx already pin every row without it; this covers what the prop adds,
 * and — the thing that matters most — that a session it does not (or cannot) flag renders the
 * exact single-span title the frozen test pins, not a lookalike.
 */
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { sessionRow } from "../../accept/m3b/fakes";
import type { SessionGroup } from "../../state/rows";
import { SessionList } from "./SessionList";

afterEach(cleanup);

const NOW = new Date(2026, 8, 12, 19, 30).getTime();

const groups: SessionGroup[] = [
  { label: "Today", rows: [sessionRow({ session_id: "s1", title: "Dashboard v1" })] },
];

function mount(over: Partial<Parameters<typeof SessionList>[0]> = {}) {
  return render(
    <SessionList
      groups={groups}
      other={[]}
      selectedId={null}
      liveIds={new Set()}
      nowMs={NOW}
      onOpen={vi.fn()}
      {...over}
    />,
  );
}

test("without the prop, the title stays the one plain span M3-b pins: no chip, no extra class", () => {
  const { container } = mount();
  const title = container.querySelector(".sess .t") as HTMLElement;
  expect(title.className).toBe("t jp");
  expect(title.textContent).toBe("Dashboard v1");
  expect(title.querySelector(".chip")).toBeNull();
  expect(title.querySelector(".t-label")).toBeNull();
});

test("a prop that returns false for this session renders byte-for-byte what omitting it renders", () => {
  const without = mount().container.querySelector(".sess .t") as HTMLElement;
  const withoutHtml = without.outerHTML;
  cleanup();
  const never = mount({ startedByViki: () => false }).container.querySelector(
    ".sess .t",
  ) as HTMLElement;
  expect(never.outerHTML).toBe(withoutHtml);
});

test("a prop returning true shows a dim chip reading exactly Started by VIKI, after the title", () => {
  const { container } = mount({ startedByViki: (id) => id === "s1" });
  const title = container.querySelector(".sess .t") as HTMLElement;
  expect(title.classList.contains("sess-title-chip")).toBe(true);
  const label = title.querySelector(".t-label");
  expect(label?.textContent).toBe("Dashboard v1");
  const children = [...title.children];
  expect(children.map((el) => el.className)).toEqual(["t-label", "chip dim"]);
  const chip = title.querySelector(".chip") as HTMLElement;
  expect(chip.textContent).toBe("Started by VIKI");
  expect(chip.children).toHaveLength(0); // a text node, not markup
  // No separator is written into the text itself: like rowState.ts's chip + text join
  // elsewhere in this row, the gap is CSS-only (sess-title-chip's `gap`).
  expect(title.textContent).toBe("Dashboard v1Started by VIKI");
});

test("the callback is asked for this row's own id, and a false for it still renders the plain span", () => {
  const startedByViki = vi.fn().mockReturnValue(false);
  const { container } = mount({ startedByViki });
  expect(startedByViki).toHaveBeenCalledWith("s1");
  const title = container.querySelector(".sess .t") as HTMLElement;
  expect(title.className).toBe("t jp");
  expect(title.querySelector(".chip")).toBeNull();
});

test("across two rows, only the one the callback flags gets the chip", () => {
  const twoRows: SessionGroup[] = [
    {
      label: "Today",
      rows: [
        sessionRow({ session_id: "s1", title: "Dashboard v1" }),
        sessionRow({ session_id: "s2", title: "Dashboard v2" }),
      ],
    },
  ];
  const { container } = mount({ groups: twoRows, startedByViki: (id) => id === "s2" });
  const rows = [...container.querySelectorAll<HTMLElement>(".sess")];
  const titles = rows.map((row) => row.querySelector(".t") as HTMLElement);
  expect(titles[0]?.classList.contains("sess-title-chip")).toBe(false);
  expect(titles[0]?.querySelector(".chip")).toBeNull();
  expect(titles[1]?.classList.contains("sess-title-chip")).toBe(true);
  expect(titles[1]?.querySelector(".chip")?.textContent).toBe("Started by VIKI");
});
