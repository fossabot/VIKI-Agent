/**
 * Decision page states rendered for a person to inspect. Real bundle and Edge, synthetic API data
 * only; these shots are not part of scripts/check.py.
 *
 *   npx vitest run --config vitest.shots.config.ts e2e/decision.shots.ts
 *
 * Run from frontend/. The scratch server binds 8765 and must be free; build the app freshly first.
 * No real vault or provider home is used. PNGs go to test-results/decision as
 * decision-<view>-<width>-<theme>.png. Narrow charts and confirm views also get answer- and
 * action-focused frames so both parts have a readable viewport.
 */
// @ts-expect-error the shot only needs mkdirSync; e2e/node.d.ts intentionally stays narrow.
import { mkdirSync } from "node:fs";
import type { Browser, Page } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import { decisionLoopExamples } from "../src/clients/decisionLoop/fake";
import type {
  DataKind,
  DataRecordsView,
  DataRecordView,
  DataSummaryView,
} from "../src/clients/decisionLoop/types";
import { appUrl, launchEdge } from "./harness";
import { type Server, startServer } from "./server";
import { navigateSidebar } from "./sidebar-navigation";

const OUT = "test-results/decision";
const SIZES = [
  { width: 1440, height: 1000 },
  { width: 400, height: 700 },
] as const;
const THEMES = ["light", "dark"] as const;
type Theme = (typeof THEMES)[number];

let browser: Browser | undefined;
let server: Server | undefined;

beforeAll(async () => {
  mkdirSync(OUT, { recursive: true });
  browser = await launchEdge();
  server = await startServer({ fixture: "tests/fixtures/claude/text-two-turns.jsonl" });
}, 120_000);

afterAll(async () => {
  await browser?.close();
  await server?.stop();
});

function syntheticData() {
  const examples = decisionLoopExamples();
  const now = new Date();
  const today = now.toISOString().slice(0, 10);
  const asOf = now.toISOString();
  const summary: DataSummaryView = {
    ...examples.data_summary,
    as_of: asOf,
    today,
    days: Array.from({ length: 90 }, (_, index) => {
      const date = new Date(now);
      date.setUTCDate(date.getUTCDate() - 89 + index);
      const day = date.toISOString().slice(0, 10);
      return {
        day,
        held: 2 + (index % 4),
        listed: 1 + (index % 5),
        attention: 2 + (index % 6),
        reports: index % 3 === 0 ? 1 : 0,
        useful: 1 + (index % 3),
        wrong: index % 4 === 0 ? 1 : 0,
        later: index % 5 === 0 ? 1 : 0,
        mute: index % 7 === 0 ? 1 : 0,
        right_to_hold: index % 6 === 0 ? 1 : 0,
        missed: index % 8 === 0 ? 1 : 0,
      };
    }),
  };
  const decisions: DataRecordsView = structuredClone(examples.data_decisions);
  // The first example row is kept by an open suggestion; ensure a distinct deletable row exists.
  decisions.rows = decisions.rows.map((row, index) => ({
    ...row,
    at: new Date(now.getTime() - index * 3_600_000).toISOString(),
    kept: index === 0 ? "open_suggestion" : null,
  }));
  decisions.total = decisions.rows.length;
  decisions.deletable = decisions.rows.filter((row) => row.kept === null).length;
  const deletable = decisions.rows.find((row) => row.kept === null);
  const detail: DataRecordView = {
    ...examples.data_decision,
    id: deletable?.id ?? examples.data_decision.id,
    at: deletable?.at ?? asOf,
    kept: null,
  };
  return { examples, summary, decisions, detail };
}

async function open(size: (typeof SIZES)[number], theme: Theme, enabled = true): Promise<Page> {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: size, colorScheme: theme });
  const data = syntheticData();
  await page.route("**/api/v2/decision/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    let body: unknown;
    if (method === "GET" && url.pathname.endsWith("/page")) {
      body = { ...data.examples.page, enabled };
    } else if (method === "GET" && url.pathname.endsWith("/data")) {
      body = data.summary;
    } else if (method === "POST" && url.pathname.endsWith("/data/records")) {
      const query = request.postDataJSON() as { kind?: DataKind };
      body =
        query.kind === "decisions"
          ? data.decisions
          : query.kind === "suggestions"
            ? data.examples.data_suggestions
            : query.kind === "calls"
              ? data.examples.data_calls
              : undefined;
    } else if (method === "POST" && url.pathname.endsWith("/data/record")) {
      const record = request.postDataJSON() as { kind?: DataKind };
      body =
        record.kind === "decisions"
          ? data.detail
          : record.kind === "suggestions"
            ? data.examples.data_suggestion
            : undefined;
    }
    if (body === undefined) return route.fallback();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });
  await page.goto(appUrl());
  await page.locator('[data-region="shell"]').waitFor({ timeout: 15_000 });
  await page.waitForFunction((value) => document.documentElement.dataset.theme === value, theme);
  await navigateSidebar(page, "Decision");
  await page.getByRole("heading", { name: "Decision", exact: true }).waitFor();
  const proactiveControl = enabled
    ? page.getByRole("button", { name: "Pause for 1 hour", exact: true })
    : page.getByRole("button", { name: "Turn proactive VIKI on", exact: true });
  await expect.poll(() => proactiveControl.isEnabled(), { timeout: 15_000 }).toBe(true);
  await page.evaluate(() => document.fonts.ready);
  return page;
}

/** Capture all size/theme combinations after the view-specific state has visibly settled. */
async function capture(
  view: string,
  ready: (page: Page) => Promise<void>,
  enabled = true,
  afterPrimary?: (page: Page, size: (typeof SIZES)[number], theme: Theme) => Promise<void>,
): Promise<void> {
  for (const size of SIZES) {
    for (const theme of THEMES) {
      const page = await open(size, theme, enabled);
      try {
        try {
          await ready(page);
          await page.screenshot({ path: `${OUT}/decision-${view}-${size.width}-${theme}.png` });
          await afterPrimary?.(page, size, theme);
        } catch (error) {
          await page.screenshot({ path: `${OUT}/failed-${view}-${size.width}-${theme}.png` });
          if (view === "charts") {
            const geometry = await page.evaluate(() => {
              const rect = (element: Element | null) => {
                if (!element) return null;
                const box = element.getBoundingClientRect();
                const style = getComputedStyle(element);
                const clipped = {
                  top: Math.max(0, box.top),
                  right: Math.min(window.innerWidth, box.right),
                  bottom: Math.min(window.innerHeight, box.bottom),
                  left: Math.max(0, box.left),
                };
                return {
                  top: box.top,
                  right: box.right,
                  bottom: box.bottom,
                  left: box.left,
                  width: box.width,
                  height: box.height,
                  overflowX: style.overflowX,
                  overflowY: style.overflowY,
                  clipPath: style.clipPath,
                  viewportIntersection: {
                    ...clipped,
                    width: Math.max(0, clipped.right - clipped.left),
                    height: Math.max(0, clipped.bottom - clipped.top),
                  },
                };
              };
              const activity = [...document.querySelectorAll("h3")].find(
                (heading) => heading.textContent?.trim() === "Activity",
              );
              const answers = document.getElementById("data-answers");
              const answerBars = document.querySelector(".dloop-answer-bars");
              const appView = document.querySelector<HTMLElement>(".app-view");
              const appViewBox = appView?.getBoundingClientRect();
              return {
                viewport: { width: window.innerWidth, height: window.innerHeight },
                scroll: { x: window.scrollX, y: window.scrollY },
                viewportClip: {
                  top: 0,
                  right: window.innerWidth,
                  bottom: window.innerHeight,
                  left: 0,
                },
                visualViewport: window.visualViewport
                  ? { width: window.visualViewport.width, height: window.visualViewport.height }
                  : null,
                zoom: {
                  devicePixelRatio: window.devicePixelRatio,
                  visualViewportScale: window.visualViewport?.scale ?? null,
                },
                appView:
                  appViewBox && appView
                    ? {
                        top: appViewBox.top,
                        right: appViewBox.right,
                        bottom: appViewBox.bottom,
                        left: appViewBox.left,
                        clientTop: appView.clientTop,
                        clientLeft: appView.clientLeft,
                        clientHeight: appView.clientHeight,
                        clientWidth: appView.clientWidth,
                      }
                    : null,
                activityHeading: rect(activity ?? null),
                activitySvg: rect(document.querySelector(".dloop-activity-svg")),
                answersHeading: rect(answers),
                answerBars: rect(answerBars),
                answerVisuals: [...document.querySelectorAll(".dloop-answer-visual")].map(
                  (visual) => rect(visual),
                ),
              };
            });
            console.error("[decision shots] charts frame geometry", JSON.stringify(geometry));
          }
          throw error;
        }
      } finally {
        await page.close();
      }
    }
  }
}

async function openData(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Data", exact: true }).click();
  await page.getByRole("article").first().waitFor({ timeout: 15_000 });
  await page.locator("[data-data-row]").first().waitFor({ timeout: 15_000 });
}

async function openInventory(page: Page): Promise<void> {
  await openData(page);
  const heading = page.getByRole("heading", { level: 3, name: "What VIKI keeps", exact: true });
  await heading.evaluate((element) => element.scrollIntoView({ block: "start" }));
  await page.getByRole("article", { name: "Decision history" }).waitFor({ timeout: 15_000 });
  await page.waitForFunction(() => {
    const heading = [...document.querySelectorAll("h3")].find(
      (element) => element.textContent?.trim() === "What VIKI keeps",
    );
    const card = [...document.querySelectorAll("article")].find((element) =>
      element.textContent?.includes("Decision history"),
    );
    if (!heading || !card) return false;
    const headingBox = heading.getBoundingClientRect();
    const cardBox = card.getBoundingClientRect();
    return (
      headingBox.top >= 0 &&
      headingBox.bottom <= window.innerHeight &&
      cardBox.top >= 0 &&
      cardBox.bottom <= window.innerHeight
    );
  });
}

async function showRowAndDetails(page: Page): Promise<void> {
  const { checkbox, row } = await deletableRow(page);
  await checkbox.check();
  if (!(await checkbox.evaluate((input) => (input as HTMLInputElement).checked)))
    throw new Error("The deletable decision row was not selected");
  await row.getByRole("button", { name: "Details", exact: true }).click();
  const detail = row.locator(".dloop-data-detail");
  await detail.waitFor({ timeout: 15_000 });
  await row.evaluate((element) => element.scrollIntoView({ block: "start" }));
  await page.waitForFunction(() => {
    const row = document.querySelector<HTMLElement>(
      "[data-data-row]:has(input[type=checkbox]:checked)",
    );
    const detail = row?.querySelector(".dloop-data-detail");
    const checkbox = row?.querySelector<HTMLInputElement>('input[type="checkbox"]');
    if (!row || !detail || !checkbox) return false;
    const rowBox = row.getBoundingClientRect();
    const detailBox = detail.getBoundingClientRect();
    const checkboxBox = checkbox.getBoundingClientRect();
    const detailsButtonBox = row.querySelector(".dloop-data-row > button")?.getBoundingClientRect();
    if (!detailsButtonBox) return false;
    return (
      rowBox.top < window.innerHeight &&
      rowBox.bottom > 0 &&
      detailBox.top < window.innerHeight &&
      detailBox.bottom > 0 &&
      checkboxBox.top >= 0 &&
      checkboxBox.bottom <= window.innerHeight &&
      detailsButtonBox.top >= 0 &&
      detailsButtonBox.bottom <= window.innerHeight
    );
  });
}

async function deletableRow(page: Page) {
  const checkbox = page.getByRole("checkbox").first();
  await checkbox.waitFor({ timeout: 15_000 });
  const row = checkbox.locator("xpath=ancestor::li[@data-data-row]");
  return { checkbox, row };
}

test("Decision page states in light and dark", async () => {
  await capture(
    "off",
    async (page) => {
      await page.getByRole("button", { name: "Turn proactive VIKI on", exact: true }).waitFor();
    },
    false,
  );

  await capture("on", async (page) => {
    await page.getByText(/Pause: VIKI stops checking/i).waitFor();
  });

  await capture("do", async (page) => {
    await page.getByRole("button", { name: "What VIKI may do", exact: true }).click();
    await page
      .getByText(/daily limit|plan brake/i)
      .first()
      .waitFor({ timeout: 15_000 });
  });

  await capture("data", openInventory);

  await capture(
    "charts",
    async (page) => {
      await openData(page);
      const activity = page.getByRole("heading", { level: 3, name: "Activity", exact: true });
      await activity.evaluate((element) => element.scrollIntoView({ block: "start" }));
      await page
        .getByRole("heading", { level: 3, name: "How you answered", exact: true })
        .waitFor();
      await page.locator("#data-answers").waitFor({ state: "attached" });
      await page.waitForFunction(() => {
        const appView = document.querySelector<HTMLElement>(".app-view");
        const activity = [...document.querySelectorAll("h3")].find(
          (heading) => heading.textContent?.trim() === "Activity",
        );
        const chart = document.querySelector(".dloop-activity-svg");
        if (!appView || !activity || !chart) return false;
        const viewBox = appView.getBoundingClientRect();
        const clip = {
          top: Math.max(0, viewBox.top),
          bottom: Math.min(window.innerHeight, viewBox.bottom),
          left: Math.max(0, viewBox.left),
          right: Math.min(window.innerWidth, viewBox.right),
        };
        const within = (element: Element) => {
          const box = element.getBoundingClientRect();
          const intersection = {
            top: Math.max(box.top, clip.top),
            bottom: Math.min(box.bottom, clip.bottom),
            left: Math.max(box.left, clip.left),
            right: Math.min(box.right, clip.right),
          };
          return (
            intersection.bottom > intersection.top &&
            intersection.right > intersection.left &&
            box.top >= clip.top - 1 &&
            box.bottom <= clip.bottom + 1 &&
            box.left >= clip.left - 1 &&
            box.right <= clip.right + 1
          );
        };
        if (!within(activity) || !within(chart)) return false;
        if (window.innerWidth === 400) return true;
        const answers = document.getElementById("data-answers");
        const bars = document.querySelector(".dloop-answer-bars");
        return Boolean(answers && bars && within(answers) && within(bars));
      });
    },
    true,
    async (page, size, theme) => {
      if (size.width !== 400) return;
      await page
        .locator("#data-answers")
        .evaluate((element) => element.scrollIntoView({ block: "start" }));
      await page.waitForFunction(() => {
        const appView = document.querySelector<HTMLElement>(".app-view");
        const answers = document.getElementById("data-answers");
        const bars = document.querySelector(".dloop-answer-bars");
        if (!appView || !answers || !bars) return false;
        const viewBox = appView.getBoundingClientRect();
        const clip = {
          top: Math.max(0, viewBox.top),
          bottom: Math.min(window.innerHeight, viewBox.bottom),
          left: Math.max(0, viewBox.left),
          right: Math.min(window.innerWidth, viewBox.right),
        };
        const within = (element: Element) => {
          const box = element.getBoundingClientRect();
          const intersection = {
            top: Math.max(box.top, clip.top),
            bottom: Math.min(box.bottom, clip.bottom),
            left: Math.max(box.left, clip.left),
            right: Math.min(box.right, clip.right),
          };
          return (
            intersection.bottom > intersection.top &&
            intersection.right > intersection.left &&
            box.top >= clip.top - 1 &&
            box.bottom <= clip.bottom + 1 &&
            box.left >= clip.left - 1 &&
            box.right <= clip.right + 1
          );
        };
        const visualGroups = [...document.querySelectorAll(".dloop-answer-group")];
        return (
          within(answers) &&
          within(bars) &&
          visualGroups.length === 2 &&
          visualGroups.every((group) =>
            [
              group.querySelector("h4"),
              group.querySelector(".dloop-answer-visual"),
              group.querySelector(".dloop-answer-legend"),
            ].every((element) => element !== null && within(element)),
          )
        );
      });
      await page.screenshot({
        path: `${OUT}/decision-charts-answers-${size.width}-${theme}.png`,
      });
    },
  );

  await capture("browse", async (page) => {
    await openData(page);
    await showRowAndDetails(page);
  });

  await capture(
    "confirm",
    async (page) => {
      await openData(page);
      const { checkbox } = await deletableRow(page);
      await checkbox.check();
      await page.getByRole("button", { name: "Delete selected (1)", exact: true }).click();
      const dialog = page.getByRole("alertdialog");
      await dialog.waitFor({ state: "visible", timeout: 15_000 });
      await dialog.evaluate((element) => element.scrollIntoView({ block: "start" }));
      await dialog.locator("p").first().waitFor({ state: "visible" });
      await dialog.locator(".confirm-dialog-heading").first().waitFor({ state: "visible" });
      await page.waitForFunction(() => {
        const appView = document.querySelector<HTMLElement>(".app-view");
        const dialog = document.querySelector<HTMLElement>('[role="alertdialog"]');
        if (!appView || !dialog) return false;
        const view = appView.getBoundingClientRect();
        const clip = {
          top: Math.max(0, view.top),
          bottom: Math.min(window.innerHeight, view.bottom),
          left: Math.max(0, view.left),
          right: Math.min(window.innerWidth, view.right),
        };
        const within = (element: Element) => {
          const box = element.getBoundingClientRect();
          const visible = {
            top: Math.max(box.top, clip.top),
            bottom: Math.min(box.bottom, clip.bottom),
            left: Math.max(box.left, clip.left),
            right: Math.min(box.right, clip.right),
          };
          return (
            visible.bottom > visible.top &&
            visible.right > visible.left &&
            box.top >= clip.top - 1 &&
            box.bottom <= clip.bottom + 1 &&
            box.left >= clip.left - 1 &&
            box.right <= clip.right + 1
          );
        };
        const body = dialog.querySelector("p");
        const heading = dialog.querySelector(".confirm-dialog-heading");
        if (!body || !heading || !within(body) || !within(heading)) return false;
        if (window.innerWidth < 800) return true;
        const actions = dialog.querySelector(".confirm-dialog-actions");
        return within(dialog) && actions !== null && within(actions);
      });
    },
    true,
    async (page, size, theme) => {
      if (size.width !== 400) return;
      const dialog = page.getByRole("alertdialog");
      await dialog
        .getByRole("button", { name: "Delete", exact: true })
        .waitFor({ state: "visible" });
      await dialog
        .getByRole("button", { name: "Cancel", exact: true })
        .waitFor({ state: "visible" });
      await page
        .locator('[role="alertdialog"] .confirm-dialog-actions')
        .evaluate((element) => element.scrollIntoView({ block: "center" }));
      await page.waitForFunction(() => {
        const appView = document.querySelector<HTMLElement>(".app-view");
        const irreversible = document.querySelector(".confirm-dialog-irreversible");
        const actions = document.querySelector(".confirm-dialog-actions");
        if (!appView || !irreversible || !actions) return false;
        const view = appView.getBoundingClientRect();
        const clip = {
          top: Math.max(0, view.top),
          bottom: Math.min(window.innerHeight, view.bottom),
          left: Math.max(0, view.left),
          right: Math.min(window.innerWidth, view.right),
        };
        const within = (element: Element) => {
          const box = element.getBoundingClientRect();
          const visible = {
            top: Math.max(box.top, clip.top),
            bottom: Math.min(box.bottom, clip.bottom),
            left: Math.max(box.left, clip.left),
            right: Math.min(box.right, clip.right),
          };
          return (
            visible.bottom > visible.top &&
            visible.right > visible.left &&
            box.top >= clip.top - 1 &&
            box.bottom <= clip.bottom + 1 &&
            box.left >= clip.left - 1 &&
            box.right <= clip.right + 1
          );
        };
        return (
          within(irreversible) &&
          within(actions) &&
          actions.querySelectorAll("button").length === 2 &&
          [...actions.querySelectorAll("button")].every(within)
        );
      });
      await page.screenshot({ path: `${OUT}/decision-confirm-actions-${size.width}-${theme}.png` });
    },
  );
}, 300_000);
