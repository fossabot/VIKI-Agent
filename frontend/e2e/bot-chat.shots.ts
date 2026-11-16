/**
 * A Bot's main chat and task thread, with its opening, header and right panel. Needs you holds
 * one waiting card from another chat. Synthetic data only: the e2e server's scratch Start with
 * VIKI vault, its Chief seat and the fake CLI's Write permission script; never a real vault.
 *
 *   npx vitest run --config vitest.shots.config.ts e2e/bot-chat.shots.ts
 *
 * Run from frontend/. It binds 8765 (check it is free first) and needs a fresh build. Never part
 * of scripts/check.py. PNGs land in `test-results/bot-chat/`.
 */
import type { Browser, Page } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, send } from "./harness";
import { type Server, startServer } from "./server";
import { navigateSidebar } from "./sidebar-navigation";

const OUT = "test-results/bot-chat";

interface Size {
  width: number;
  height: number;
}

const SIZES: readonly Size[] = [
  { width: 1440, height: 1000 },
  { width: 400, height: 700 },
];
const THEMES = ["light", "dark"] as const;
type Theme = (typeof THEMES)[number];

let browser: Browser | undefined;
let server: Server | undefined;

beforeAll(async () => {
  browser = await launchEdge();
  server = await startServer({
    bots: true,
    team: true,
    fixture: "tests/fixtures/claude/write-permission.jsonl",
  });
}, 120_000);

afterAll(async () => {
  await browser?.close();
  await server?.stop();
});

async function botReadiness(page: Page): Promise<string> {
  const view = page.locator(".bots-view");
  const brief = (value: string | null) => value?.trim().slice(0, 120) || "none";
  const loading = await view.locator(":scope > .loading").allTextContents();
  const alerts = await view.getByRole("alert").allTextContents();
  const selected = view.getByRole("region", { name: "Selected Bot", exact: true });
  const kind =
    (await selected.count()) === 0
      ? "none"
      : (await selected.locator(".bot-chat-actions").count()) > 0
        ? "Bot detail"
        : "seat";
  const selectedText =
    kind === "none"
      ? (await view.locator(".bots-pane > p").allTextContents())[0]
      : (await selected.getByRole("heading", { level: 2 }).allTextContents())[0];
  const chief = view
    .getByRole("group", { name: "Bot roster", exact: true })
    .getByRole("button", { name: "Chief", exact: true });
  const chiefRow = chief.locator("xpath=ancestor::li[1]");
  return JSON.stringify({
    loading: brief(loading[0] ?? null),
    alerts: alerts.slice(0, 3).map(brief),
    selected: { kind, text: brief(selectedText ?? null) },
    chief: {
      current: (await chief.count()) > 0 ? await chief.getAttribute("aria-current") : "missing",
      status: brief((await chiefRow.locator(".bots-status").allTextContents())[0] ?? null),
    },
  });
}

async function open(size: Size, theme: Theme): Promise<Page> {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({
    viewport: { width: size.width, height: size.height },
    colorScheme: theme,
  });
  await page.goto(appUrl());
  await page.locator('[data-region="shell"]').waitFor({ timeout: 15_000 });
  await page.waitForFunction((t) => document.documentElement.dataset.theme === t, theme);
  await navigateSidebar(page, "Bots");
  await page.locator(".bots-view").waitFor();
  try {
    await page.locator(".bots-view > .loading").waitFor({ state: "hidden", timeout: 15_000 });
  } catch {
    throw new Error(`Bots list did not settle: ${await botReadiness(page)}`);
  }
  await page
    .getByRole("group", { name: "Bot roster", exact: true })
    .getByRole("button", { name: "Chief", exact: true })
    .click();
  return page;
}

async function openChat(page: Page, thread: boolean): Promise<void> {
  const newThread = page
    .getByRole("region", { name: "Selected Bot", exact: true })
    .getByRole("button", { name: "New task thread", exact: true });
  try {
    await newThread.waitFor({ timeout: 15_000 });
    await expect.poll(() => newThread.isEnabled(), { timeout: 15_000 }).toBe(true);
  } catch {
    throw new Error(`Selected Bot is not ready for capture: ${await botReadiness(page)}`);
  }
  if (thread) {
    await newThread.click();
    await page
      .getByRole("navigation", { name: "Bot chat", exact: true })
      .getByText("Task thread", { exact: true })
      .waitFor();
  } else {
    await page.getByRole("button", { name: "Open conversation", exact: true }).click();
  }
  await page.locator(".empty-brand-face").waitFor();
  await page.evaluate(() => document.fonts.ready);
}

async function expectBotHeader(page: Page, thread: boolean): Promise<void> {
  const bar = page.getByRole("navigation", { name: "Bot chat", exact: true });
  await bar.waitFor();
  await expect
    .poll(() => bar.locator(".bot-chat-name strong").textContent(), { timeout: 15_000 })
    .toBe("Chief");
  await expect
    .poll(() => bar.locator(".bot-chat-meta").textContent(), { timeout: 15_000 })
    .toBe(`${thread ? "Task thread" : "Main Bot chat"} · r1`);
  await expect.poll(() => page.locator(".conversation-header > .chip").count()).toBe(0);
}

async function expectPanelOrder(page: Page): Promise<void> {
  const needsYou = page.locator(".rail-section:has(.need-open)");
  const activity = page.getByRole("region", { name: "Bot activity", exact: true });
  await needsYou.waitFor();
  await activity.waitFor();
  expect(
    await page.evaluate(() => {
      const first = document.querySelector(".rail-section:has(.need-open)");
      const second = document.querySelector('[aria-label="Bot activity"]');
      return Boolean(
        first && second && first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING,
      );
    }),
  ).toBe(true);
}

async function expectPanelIntersection(page: Page): Promise<void> {
  const measured = await page
    .getByRole("dialog", { name: "VIKI panel", exact: true })
    .evaluate((drawer) => {
      const needs = drawer.querySelector(".right-rail-panel .rail-section:has(.need-open)");
      const activity = drawer.querySelector(".right-rail-panel .bot-chat-activity");
      const panel = drawer.querySelector<HTMLElement>(".right-rail-panel:not([hidden])");
      const details = drawer.querySelector<HTMLElement>(".right-rail-details");
      const targets = [
        ["Needs you", needs?.querySelector(".rail-section-head .lbl")],
        ["waiting card", needs?.querySelector(".rail-section-body .need")],
        ["Bot activity", activity?.querySelector(".bot-chat-activity-head h2")],
        [
          "Morning brief",
          [...(activity?.querySelectorAll(".routines-rail-row") ?? [])].find((row) =>
            row.textContent?.includes("Morning brief"),
          ),
        ],
      ] as const;
      const atTop = [drawer, details, panel].every((node) => node && node.scrollTop <= 1);
      return {
        atTop,
        targets: targets.map(([name, node]) => {
          if (!node) return { name, missing: true, fits: false };
          const box = node.getBoundingClientRect();
          const clip = { left: 0, top: 0, right: innerWidth, bottom: innerHeight };
          for (let parent = node.parentElement; parent; parent = parent.parentElement) {
            const style = getComputedStyle(parent);
            const bounds = parent.getBoundingClientRect();
            if (/(auto|scroll|hidden|clip)/.test(style.overflowX)) {
              clip.left = Math.max(clip.left, bounds.left + parent.clientLeft);
              clip.right = Math.min(
                clip.right,
                bounds.left + parent.clientLeft + parent.clientWidth,
              );
            }
            if (/(auto|scroll|hidden|clip)/.test(style.overflowY)) {
              clip.top = Math.max(clip.top, bounds.top + parent.clientTop);
              clip.bottom = Math.min(
                clip.bottom,
                bounds.top + parent.clientTop + parent.clientHeight,
              );
            }
          }
          const rect = { left: box.left, top: box.top, right: box.right, bottom: box.bottom };
          return {
            name,
            rect,
            clip,
            fits:
              box.width > 0 &&
              box.height > 0 &&
              rect.left >= clip.left - 1 &&
              rect.right <= clip.right + 1 &&
              rect.top >= clip.top - 1 &&
              rect.bottom <= clip.bottom + 1,
          };
        }),
      };
    });
  expect(measured.atTop, JSON.stringify(measured)).toBe(true);
  for (const target of measured.targets) expect(target.fits, JSON.stringify(target)).toBe(true);
}

async function expectDrawerScroll(page: Page): Promise<void> {
  const drawer = page.getByRole("dialog", { name: "VIKI panel", exact: true });
  await drawer
    .locator(".right-rail-summary")
    .evaluate((summary) => summary.scrollIntoView({ block: "start" }));
  const summaryVisible = await drawer.evaluate((element) => {
    const summary = element.querySelector(".right-rail-summary");
    if (!summary) return false;
    const outer = element.getBoundingClientRect();
    const inner = summary.getBoundingClientRect();
    return inner.height > 0 && inner.top >= outer.top - 1 && inner.top < outer.bottom;
  });
  expect(summaryVisible).toBe(true);
  await drawer
    .locator(".right-rail-footer")
    .evaluate((footer) => footer.scrollIntoView({ block: "end" }));
  const footer = await drawer.evaluate((element) => {
    const footer = element.querySelector(".right-rail-footer");
    if (!footer) return { scrollTop: element.scrollTop, visible: false };
    const outer = element.getBoundingClientRect();
    const inner = footer.getBoundingClientRect();
    return {
      scrollTop: element.scrollTop,
      visible: inner.height > 0 && inner.top >= outer.top - 1 && inner.bottom <= outer.bottom + 1,
    };
  });
  expect(footer.scrollTop).toBeGreaterThan(0);
  expect(footer.visible).toBe(true);
}

test("Chief's Bot, one manual routine and one waiting card", async () => {
  if (!browser) throw new Error("Edge did not launch");
  const staged = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  await staged.goto(appUrl());
  await staged.locator('[data-region="shell"]').waitFor({ timeout: 15_000 });
  await send(staged, "Write a short note.");
  await staged.locator(".card").waitFor({ timeout: 30_000 });
  await staged.close();

  const page = await open({ width: 1440, height: 1000 }, "light");
  await page.getByRole("region", { name: "Selected Bot", exact: true }).waitFor();
  const conversation = page.getByRole("button", { name: "Open conversation", exact: true });
  await expect.poll(() => conversation.isEnabled()).toBe(true);
  await conversation.click();
  const activity = page.getByRole("region", { name: "Bot activity", exact: true });
  await activity.waitFor();
  await page.getByRole("button", { name: "Chats & routines", exact: true }).click();
  await page.getByRole("button", { name: "New routine", exact: true }).click();
  const editor = page.getByRole("region", { name: "New routine", exact: true });
  await editor.getByLabel("Name", { exact: true }).fill("Morning brief");
  await editor
    .getByLabel("Prompt", { exact: true })
    .fill("List today's three most important notes.");
  await editor.getByLabel("Manual only", { exact: true }).check();
  await editor.locator(".routine-preview-turn").waitFor();
  await editor.getByRole("button", { name: "Save", exact: true }).click();
  await page
    .locator(".bots-routines .routine-detail")
    .getByRole("heading", { name: "Morning brief", exact: true })
    .waitFor();
  await page.close();
}, 120_000);

for (const thread of [false, true]) {
  const view = thread ? "thread" : "main";
  test(`the ${view} chat`, async () => {
    for (const size of SIZES) {
      for (const theme of THEMES) {
        const page = await open(size, theme);
        await openChat(page, thread);
        await expectBotHeader(page, thread);
        if (size.width === 1440) {
          await page.locator('[data-region="right-rail"] .need-open').waitFor();
          await page
            .getByRole("region", { name: "Bot activity", exact: true })
            .getByText("Morning brief", { exact: true })
            .waitFor();
          await expectPanelOrder(page);
          await page.screenshot({
            path: `${OUT}/bot-chat-${view}-${size.width}-${theme}.png`,
          });
        } else {
          await page.screenshot({
            path: `${OUT}/bot-chat-${view}-${size.width}-${theme}.png`,
          });
          await page.getByRole("button", { name: "VIKI panel", exact: true }).click();
          const drawer = page.getByRole("dialog", { name: "VIKI panel", exact: true });
          await drawer.evaluate(async (element) => {
            await Promise.all(element.getAnimations().map((animation) => animation.finished));
          });
          await page.locator('[data-region="right-rail"] .need-open').waitFor();
          await page
            .getByRole("region", { name: "Bot activity", exact: true })
            .getByText("Morning brief", { exact: true })
            .waitFor();
          await expectPanelOrder(page);
          const left = await drawer.evaluate((element) => element.getBoundingClientRect().left);
          expect(Math.abs(left - 60)).toBeLessThanOrEqual(1);
          await expectPanelIntersection(page);
          await page.screenshot({
            path: `${OUT}/bot-chat-${view}-${size.width}-${theme}-panel.png`,
          });
          await expectDrawerScroll(page);
        }
        await page.close();
      }
    }
  });
}
