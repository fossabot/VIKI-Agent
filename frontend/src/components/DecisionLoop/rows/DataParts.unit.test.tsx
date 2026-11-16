import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { CANDIDATES, type DataDay, type DataTopic } from "../../../clients/decisionLoop/types";
import { t, tf } from "../../../i18n/t";
import { readCss } from "../../../testing/css";
import {
  ActivityChart,
  AnswerBars,
  InventoryCard,
  localDate,
  shiftDay,
  TopicTable,
  topicLabel,
} from "./DataParts";

afterEach(() => {
  cleanup();
});

function inData(children: ReactNode) {
  return <div className="dloop-data">{children}</div>;
}

function dataDay(day: string, values: Partial<Omit<DataDay, "day">> = {}): DataDay {
  return {
    day,
    held: 0,
    listed: 0,
    attention: 0,
    reports: 0,
    useful: 0,
    wrong: 0,
    later: 0,
    mute: 0,
    right_to_hold: 0,
    missed: 0,
    ...values,
  };
}

function activityTable() {
  fireEvent.click(screen.getByRole("button", { name: t("decision_page.data.table.show") }));
  const table = screen.getByRole("table", { name: /Activity/ });
  return within(table).getAllByRole("row").slice(1);
}

function rowCells(row: HTMLElement): string[] {
  return Array.from(row.querySelectorAll("th, td"), (cell) => cell.textContent ?? "");
}

test("inventory card names its article, shows its details, meter values, and actions", () => {
  const onExplore = vi.fn();
  const { container } = render(
    inData(
      <InventoryCard
        name="Decision history"
        meaning="Decisions and reports"
        count={12}
        dates="May 1–3"
        limit="365 days"
        meters={[{ value: 12, max: 100, label: "Stored decisions" }]}
        note="Oldest records are removed first."
        actions={[{ label: "Explore", onClick: onExplore }]}
      />,
    ),
  );

  const card = screen.getByRole("article", { name: "Decision history" });
  expect(card.querySelector("h4")?.textContent).toBe("Decision history");
  expect(card.getAttribute("aria-labelledby")).toBe(card.querySelector("h4")?.id);
  expect(card.textContent).toContain("Decisions and reports");
  expect(card.textContent).toContain("12");
  expect(card.textContent).toContain("May 1–3");
  expect(card.textContent).toContain("365 days");
  expect(card.textContent).toContain("Oldest records are removed first.");

  const meter = screen.getByRole("meter", { name: "Stored decisions" });
  expect(meter.tagName).toBe("SPAN");
  expect(meter.getAttribute("aria-valuemin")).toBe("0");
  expect(meter.getAttribute("aria-valuemax")).toBe("100");
  expect(meter.getAttribute("aria-valuenow")).toBe("12");
  expect(meter.textContent).toContain(tf("decision_page.data.meter", { value: 12, max: 100 }));

  const action = screen.getByRole("button", { name: "Explore" });
  expect(action.classList.contains("btn")).toBe(true);
  expect(action.classList.contains("alt")).toBe(true);
  expect(action.classList.contains("sm")).toBe(true);
  fireEvent.click(action);
  expect(onExplore).toHaveBeenCalledOnce();
  expect(container.querySelector(".dloop-data")).not.toBeNull();
});

test("activity chart shows localized summary, full 30-day window, totals, and table toggle", () => {
  const today = "2026-05-30";
  const days = [
    dataDay(shiftDay(today, -30), { held: 9, reports: 8 }),
    dataDay(shiftDay(today, -29), { held: 1 }),
    dataDay(today, { listed: 2, attention: 3, reports: 4 }),
  ];
  render(inData(<ActivityChart days={days} today={today} range={30} />));

  const summary = tf("decision_page.data.activity.summary", {
    range: t("decision_page.data.range.30"),
    decisions: 6,
    held: 1,
    listed: 2,
    attention: 3,
    reports: 4,
  });
  expect(screen.getByRole("img", { name: summary })).toBeTruthy();
  expect(screen.getByText("Held back: 1")).toBeTruthy();
  expect(screen.getByText("Listed: 2")).toBeTruthy();
  expect(screen.getByText("Got your attention: 3")).toBeTruthy();
  expect(screen.getByText("Reports: 4")).toBeTruthy();
  expect(screen.getByText(t("decision_page.data.activity.days"))).toBeTruthy();
  const chart = screen.getByRole("img", { name: summary });
  expect(chart.querySelector("rect")).not.toBeNull();
  expect(chart.querySelector("rect")?.hasAttribute("tabindex")).toBe(false);

  const rows = activityTable();
  const dates = rows.map((row) => rowCells(row)[0]);
  expect(rows).toHaveLength(30);
  expect(dates[0]).toBe(shiftDay(today, -29));
  expect(dates.at(-1)).toBe(today);
  expect(new Set(dates).size).toBe(30);
  expect(dates).not.toContain(shiftDay(today, -30));
  expect(
    screen
      .getByRole("button", { name: t("decision_page.data.table.hide") })
      .getAttribute("aria-expanded"),
  ).toBe("true");

  fireEvent.click(screen.getByRole("button", { name: t("decision_page.data.table.hide") }));
  expect(screen.queryByRole("table")).toBeNull();
  expect(
    screen
      .getByRole("button", { name: t("decision_page.data.table.show") })
      .getAttribute("aria-expanded"),
  ).toBe("false");
});

test.each([
  [90, -89, -90],
  [30, -29, -30],
] as const)(
  "daily range %i includes its first day and today, excludes the prior day",
  (range, first, excluded) => {
    const today = "2026-05-30";
    const days = [
      dataDay(shiftDay(today, excluded), { listed: 50 }),
      dataDay(shiftDay(today, first), { listed: 2 }),
      dataDay(today, { attention: 3 }),
    ];
    render(inData(<ActivityChart days={days} today={today} range={range} />));
    const rows = activityTable();
    const dates = rows.map((row) => rowCells(row)[0]);
    expect(rows).toHaveLength(range);
    expect(dates[0]).toBe(shiftDay(today, first));
    expect(dates.at(-1)).toBe(today);
    expect(new Set(dates).size).toBe(range);
    expect(dates).not.toContain(shiftDay(today, excluded));
    if (range === 90) {
      expect(
        screen.getByRole("img", {
          name: tf("decision_page.data.activity.summary", {
            range: t("decision_page.data.range.90"),
            decisions: 5,
            held: 0,
            listed: 2,
            attention: 3,
            reports: 0,
          }),
        }),
      ).toBeTruthy();
    }
  },
);

test("365-day activity uses 53 weekly columns, sums the bounded days, and ends today", () => {
  const today = "2026-05-30";
  const days = Array.from({ length: 365 }, (_, index) =>
    dataDay(shiftDay(today, index - 364), { held: 1 }),
  );
  render(inData(<ActivityChart days={days} today={today} range={365} />));

  expect(screen.getByText(t("decision_page.data.activity.weeks"))).toBeTruthy();
  const rows = activityTable();
  const endings = rows.map((row) => rowCells(row)[0] ?? "");
  const held = rows.map((row) => Number(rowCells(row)[1]));
  expect(rows).toHaveLength(53);
  expect(endings.at(-1)).toBe(today);
  expect(new Set(endings).size).toBe(53);
  expect(held.reduce((sum, count) => sum + count, 0)).toBe(365);
  expect(held.every((count) => count >= 1 && count <= 7)).toBe(true);
  expect(held.slice(1).every((count) => count === 7)).toBe(true);
});

test("empty activity range has a message and no chart rows", () => {
  render(inData(<ActivityChart days={[]} today="2026-05-30" range={30} />));
  expect(screen.getByText(t("decision_page.data.empty_range"))).toBeTruthy();
  expect(screen.queryByRole("img")).toBeNull();
  expect(screen.getByRole("button", { name: t("decision_page.data.table.show") })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: t("decision_page.data.table.show") }));
  expect(within(screen.getByRole("table")).getAllByRole("row")).toHaveLength(1);
});

test("answer bars expose matching segment labels, exact percentages, no-answer remainder, and reports", () => {
  const today = "2026-05-30";
  const days = [
    dataDay(today, {
      listed: 10,
      held: 5,
      useful: 2,
      wrong: 1,
      later: 1,
      mute: 1,
      right_to_hold: 1,
      missed: 2,
      reports: 3,
    }),
  ];
  render(inData(<AnswerBars days={days} today={today} range={30} />));

  const shown = [
    tf("decision_page.data.answers.share", {
      label: t("decision_page.data.answer.useful"),
      n: 2,
      percent: 20,
    }),
    tf("decision_page.data.answers.share", {
      label: t("decision_page.data.answer.wrong"),
      n: 1,
      percent: 10,
    }),
    tf("decision_page.data.answers.share", {
      label: t("decision_page.data.answer.later"),
      n: 1,
      percent: 10,
    }),
    tf("decision_page.data.answers.share", {
      label: t("decision_page.data.answer.mute"),
      n: 1,
      percent: 10,
    }),
    tf("decision_page.data.answers.share", {
      label: t("decision_page.data.answer.none"),
      n: 5,
      percent: 50,
    }),
  ];
  const held = [
    tf("decision_page.data.answers.share", {
      label: t("decision_page.data.answer.right_to_hold"),
      n: 1,
      percent: 20,
    }),
    tf("decision_page.data.answers.share", {
      label: t("decision_page.data.answer.missed"),
      n: 2,
      percent: 40,
    }),
    tf("decision_page.data.answers.share", {
      label: t("decision_page.data.answer.none"),
      n: 2,
      percent: 40,
    }),
  ];
  const images = screen.getAllByRole("img");
  const shownImage = images[0] as HTMLElement;
  const heldImage = images[1] as HTMLElement;
  for (const share of shown) {
    expect(shownImage.getAttribute("aria-label")).toContain(share);
    expect(screen.getAllByText(share).length).toBeGreaterThan(0);
  }
  for (const share of held) {
    expect(heldImage.getAttribute("aria-label")).toContain(share);
    expect(screen.getAllByText(share).length).toBeGreaterThan(0);
  }
  for (const image of [shownImage, heldImage]) {
    const percentages = [...(image.getAttribute("aria-label") ?? "").matchAll(/\((\d+)%\)/g)];
    expect(percentages.reduce((sum, match) => sum + Number(match[1]), 0)).toBe(100);
    expect(image.querySelector("[aria-hidden='true']")).not.toBeNull();
  }
  expect(screen.getByText("Shown to you: 10")).toBeTruthy();
  expect(screen.getByText("Held back: 5")).toBeTruthy();
  expect(screen.getByText("3 reports of something VIKI missed")).toBeTruthy();
});

test("empty answer range has no bars", () => {
  const today = "2026-05-30";
  render(inData(<AnswerBars days={[]} today={today} range={30} />));
  expect(screen.getByText(t("decision_page.data.empty_range"))).toBeTruthy();
  expect(screen.getByText("0 reports of something VIKI missed")).toBeTruthy();
  expect(screen.queryByRole("img")).toBeNull();
});

test("answer range still reports missed items when no one answered", () => {
  const today = "2026-05-30";
  render(inData(<AnswerBars days={[dataDay(today, { reports: 3 })]} today={today} range={30} />));
  expect(screen.getByText(t("decision_page.data.empty_range"))).toBeTruthy();
  expect(screen.getByText("3 reports of something VIKI missed")).toBeTruthy();
  expect(screen.queryByRole("img")).toBeNull();
});

test("topic table preserves order, scales hidden bars, expands all topics, and selects a topic", () => {
  const topics: DataTopic[] = Array.from({ length: 10 }, (_, index) => ({
    topic: index === 0 ? "unwatched" : (CANDIDATES[index - 1] as DataTopic["topic"]),
    held: 10 - index,
    shown: 20 - index,
    reports: index,
    useful: 5,
    wrong: 1,
  }));
  const onTopic = vi.fn();
  render(inData(<TopicTable topics={topics} onTopic={onTopic} />));

  const table = screen.getByRole("table", { name: /Topics/ });
  const rows = within(table).getAllByRole("row").slice(1);
  expect(rows).toHaveLength(8);
  expect(
    within(rows[0] as HTMLElement).getByRole("button", {
      name: t("decision_page.data.topic.unwatched"),
    }),
  ).toBeTruthy();
  expect(within(rows[1] as HTMLElement).getByRole("button", { name: "Approvals" })).toBeTruthy();
  expect(rows[0]?.querySelector("[aria-hidden='true']")).not.toBeNull();
  const firstBar = rows[0]?.querySelectorAll<HTMLElement>(".dloop-topic-track > span");
  const secondBar = rows[1]?.querySelectorAll<HTMLElement>(".dloop-topic-track > span");
  const firstWidth = Array.from(firstBar ?? []).reduce(
    (sum, segment) => sum + Number.parseFloat(segment.style.width),
    0,
  );
  const secondWidth = Array.from(secondBar ?? []).reduce(
    (sum, segment) => sum + Number.parseFloat(segment.style.width),
    0,
  );
  expect(firstWidth).toBeCloseTo(100);
  expect(secondWidth).toBeCloseTo((29 / 30) * 100);
  fireEvent.click(within(rows[1] as HTMLElement).getByRole("button", { name: "Approvals" }));
  expect(onTopic).toHaveBeenCalledWith("approvals");

  fireEvent.click(
    screen.getByRole("button", {
      name: tf("decision_page.data.topics.show_all", { n: 2 }),
    }),
  );
  const allRows = within(table).getAllByRole("row").slice(1);
  expect(allRows).toHaveLength(10);
  expect(allRows.map((row) => rowCells(row)[1])).toEqual(topics.map((topic) => String(topic.held)));
});

test("date and topic helpers handle local dates, month boundaries, and the unwatched catalog word", () => {
  const localMidnight = new Date(2026, 0, 31, 12).toISOString();
  expect(localDate(localMidnight)).toBe("2026-01-31");
  expect(localDate("not a timestamp")).toBe("");
  expect(shiftDay("2026-01-31", 1)).toBe("2026-02-01");
  expect(shiftDay("2026-02-01", -1)).toBe("2026-01-31");
  expect(topicLabel("unwatched")).toBe(t("decision_page.data.topic.unwatched"));
  expect(topicLabel("approvals")).toBe("Approvals");
  expect(topicLabel("future_topic")).toBe("Other");
});

test("the chart's table toggle reads Show as table", () => {
  const props = { days: [] as DataDay[], today: "2026-05-30", range: 30 as const };
  render(inData(<ActivityChart {...props} />));
  expect(screen.getByRole("button", { name: "Show as table" }).textContent).toBe("Show as table");
});

test("data charts use container and forced-color rules without motion", () => {
  const css = readCss(import.meta.url, "DataParts.css");
  expect(css.match(/@container\b/g)).toHaveLength(1);
  expect(css).toMatch(/@container\s*\(width\s*<\s*480px\)/);
  expect(css).toContain("@media (forced-colors: active)");
  expect(css).toContain("forced-color-adjust: none");
  for (const systemColor of [
    "CanvasText",
    "Highlight",
    "LinkText",
    "Mark",
    "SelectedItem",
    "GrayText",
    "ButtonText",
    "VisitedText",
    "Canvas",
  ]) {
    expect(css).toContain(systemColor);
  }
  expect(css).not.toMatch(/\b(?:transition|animation)\s*:/);
});
