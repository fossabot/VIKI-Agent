/** Synthetic Needs you and approval-card captures in light and dark themes.
 *
 * Run after `npm run build`, with port 8765 free:
 * `npx vitest run --config vitest.shots.config.ts e2e/approvals.shots.ts`
 * The server and browser use synthetic fixtures only. PNGs land in `test-results/approvals/`.
 */
// @ts-expect-error this module owns its one Node file-system use, like shell.shots.ts.
import { mkdirSync } from "node:fs";
import type { Browser, Locator, Page } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, send, waitForText } from "./harness";
import { type Server, startServer } from "./server";
import { newSidebarSession } from "./sidebar-navigation";

const OUT = "test-results/approvals";
const FIXTURE = "tests/fixtures/claude/slow-tool-permission.jsonl";
const THEMES = ["light", "dark"] as const;
const SIZES = [
  { width: 1440, height: 1000, name: "1440x1000" },
  { width: 400, height: 700, name: "400x700" },
] as const;
type Theme = (typeof THEMES)[number];
type Size = (typeof SIZES)[number];

let browser: Browser | undefined;

beforeAll(async () => {
  mkdirSync(OUT, { recursive: true });
  browser = await launchEdge();
}, 60_000);

afterAll(async () => {
  await browser?.close();
});

async function open(size: Size, theme: Theme): Promise<Page> {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({
    viewport: { width: size.width, height: size.height },
    colorScheme: theme,
  });
  const settings = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v2/settings" &&
      response.request().method() === "GET",
  );
  try {
    await page.goto(appUrl());
    await page.locator('[data-region="shell"]').waitFor({ timeout: 15_000 });
    await settings;
    await page.evaluate(() => document.fonts.ready);
    return page;
  } catch (error) {
    await page.close();
    throw error;
  }
}

async function setTheme(page: Page, theme: Theme): Promise<void> {
  await page.keyboard.press("Control+Comma");
  const settings = page.locator('[data-region="settings"]');
  await settings.waitFor();
  await settings.getByRole("tab", { name: /Appearance/ }).click();
  await page.locator("#appearance-theme").selectOption(theme);
  await settings.getByRole("button", { name: "Apply", exact: true }).click();
  await page.waitForFunction((value) => document.documentElement.dataset.theme === value, theme);
  await page.evaluate(() => document.fonts.ready);

  const back = settings.getByRole("button", { name: "Back", exact: true });
  await back.focus();
  await page.keyboard.press("Escape");
  await settings.locator('[data-page="general"][aria-selected="true"]').waitFor();
  await settings.getByRole("button", { name: "Back", exact: true }).focus();
  await page.keyboard.press("Escape");
  await page.locator('[data-region="conversation"]').waitFor();
}

async function settledDrawer(page: Page): Promise<void> {
  await page.locator(".rail-drawer").evaluate(async (element) => {
    await Promise.all(
      element
        .getAnimations({ subtree: true })
        .map((animation) => animation.finished.catch(() => undefined)),
    );
  });
}

async function capture(page: Page, name: string): Promise<void> {
  await page.screenshot({ path: `${OUT}/${name}.png` });
}

async function stageCards(page: Page): Promise<void> {
  for (let index = 0; index < 3; index++) {
    await send(page, "count the notes");
    await waitForText(page, ".card .chip", "Waiting for approval", 30_000);
    if (index < 2) {
      await newSidebarSession(page);
      const navigationDrawer = page.getByRole("dialog", {
        name: "Sessions and agents",
        exact: true,
      });
      await navigationDrawer.waitFor({ state: "detached" });
    }
  }
}

async function verifyNeedsYou(page: Page): Promise<void> {
  await waitForText(page, ".right-rail-pinned .needs-you-band--urgent .attention-count", "3");
  await waitForText(page, ".needs-you-native-group .request-actions", "Allow once");
  await waitForText(page, ".needs-you-footer", "Turn on desktop notices");
}

async function captureTarget(page: Page, name: string, drawer?: Locator): Promise<void> {
  await page.locator(".needs-you-native-group .need").first().click();
  await drawer?.waitFor({ state: "detached" });
  const target = page.locator(".card.card-target");
  await target.waitFor({ timeout: 15_000 });
  await target.evaluate(async (element) => {
    await Promise.all(
      element.getAnimations().map((animation) => animation.finished.catch(() => undefined)),
    );
  });
  const measured = await target.evaluate((element) => {
    const box = element.getBoundingClientRect();
    return {
      active: document.activeElement === element,
      id: element.getAttribute("data-tool-use-id"),
      visible: box.width > 0 && box.height > 0 && box.top >= 0 && box.bottom <= innerHeight,
    };
  });
  expect(await page.locator(".card.card-target").count()).toBe(1);
  expect(measured.active).toBe(true);
  expect(measured.id).toBeTruthy();
  expect(measured.visible).toBe(true);
  await capture(page, name);
}

async function verifyFooterInDrawer(page: Page): Promise<void> {
  const drawer = page.getByRole("dialog", { name: "VIKI panel", exact: true });
  const footer = drawer.locator(".needs-you-footer");
  await footer.evaluate((element) => element.scrollIntoView({ block: "end" }));
  const measured = await drawer.evaluate((element) => {
    const footerElement = element.querySelector<HTMLElement>(".needs-you-footer");
    if (!footerElement) return null;
    const outer = element.getBoundingClientRect();
    const inner = footerElement.getBoundingClientRect();
    return {
      visible:
        inner.height > 0 &&
        inner.top >= outer.top - 1 &&
        inner.bottom <= outer.bottom + 1 &&
        inner.top >= 0 &&
        inner.bottom <= innerHeight,
    };
  });
  expect(measured).not.toBeNull();
  if (!measured) throw new Error("Needs you notice footer is missing from the drawer");
  expect(measured.visible).toBe(true);
}

async function verifyNoticeFooterDocked(page: Page, name: string): Promise<void> {
  const pinned = page.locator(".right-rail-pinned");
  const footer = pinned.locator(".needs-you-footer");
  await footer.evaluate((element) => element.scrollIntoView({ block: "end" }));
  const visible = await pinned.evaluate((element) => {
    const footerElement = element.querySelector<HTMLElement>(".needs-you-footer");
    if (!footerElement) return false;
    const clip = element.getBoundingClientRect();
    const box = footerElement.getBoundingClientRect();
    return box.height > 0 && box.top >= clip.top - 1 && box.bottom <= clip.bottom + 1;
  });
  expect(visible).toBe(true);
  await capture(page, name);
}

async function verifyContextFooterInDrawer(page: Page): Promise<void> {
  const drawer = page.getByRole("dialog", { name: "VIKI panel", exact: true });
  const context = drawer.locator(".right-rail-footer .context-summary");
  await context.waitFor();
  await context.evaluate((element) => element.scrollIntoView({ block: "end" }));
  const measured = await drawer.evaluate((element) => {
    const contextElement = element.querySelector<HTMLElement>(
      ".right-rail-footer .context-summary",
    );
    if (!contextElement) return null;
    const clip = element.getBoundingClientRect();
    const box = contextElement.getBoundingClientRect();
    return {
      scrollTop: element.scrollTop,
      overflows: element.scrollHeight > element.clientHeight,
      visible: box.height > 0 && box.top >= clip.top - 1 && box.bottom <= clip.bottom + 1,
    };
  });
  expect(measured).not.toBeNull();
  if (!measured) throw new Error("Context footer is missing from the drawer");
  expect(measured.overflows).toBe(true);
  expect(measured.scrollTop).toBeGreaterThan(0);
  expect(measured.visible).toBe(true);

  const tabCount = await drawer
    .locator(
      'a[href], area[href], button, input, select, textarea, [contenteditable="true"], details > summary, [tabindex]',
    )
    .count();
  const contextControl = await context.evaluate((element) => ({
    tag: element.tagName,
    name: element.getAttribute("aria-label") ?? element.textContent?.trim(),
    disabled: element.matches(":disabled"),
    tabIndex: element.tabIndex,
  }));
  expect(contextControl.tag).toBe("BUTTON");
  expect(contextControl.name).toMatch(/^Context & loadout:/);
  expect(contextControl.disabled).toBe(false);
  expect(contextControl.tabIndex).toBeGreaterThanOrEqual(0);

  await drawer.getByRole("button", { name: "Close", exact: true }).focus();
  for (let step = 0; step <= tabCount; step++) {
    if (await context.evaluate((element) => element === document.activeElement)) break;
    await page.keyboard.press("Tab");
  }
  const focus = await drawer.evaluate((element) => {
    const contextElement = element.querySelector<HTMLElement>(
      ".right-rail-footer .context-summary",
    );
    const active = element.contains(document.activeElement) ? document.activeElement : null;
    const label = (target: Element | null) =>
      target?.getAttribute("aria-label") ?? target?.textContent?.trim() ?? null;
    return {
      context: contextElement ? label(contextElement) : null,
      focused: document.activeElement === contextElement,
      activeTag: active?.tagName ?? null,
      activeClass: active instanceof HTMLElement ? active.className : null,
      activeLabel: label(active),
    };
  });
  if (!focus.focused) await capture(page, "keyboard-focus-failure");
  expect(focus.focused, JSON.stringify({ tabCount, focus })).toBe(true);
}

test("Needs you and targeted approval captures at both sizes and themes", async () => {
  for (const theme of THEMES) {
    for (const size of SIZES) {
      const server: Server = await startServer({ fixture: FIXTURE });
      let page: Page | undefined;
      try {
        page = await open(size, theme);
        await setTheme(page, theme);
        await stageCards(page);
        const prefix = `approvals-${theme}-${size.name}`;

        if (size.width === 1440) {
          await verifyNeedsYou(page);
          await capture(page, `${prefix}-pinned-inline`);
          await captureTarget(page, `${prefix}-target`);
          await verifyNoticeFooterDocked(page, `${prefix}-notice-footer`);
        } else {
          const toggle = page.locator('[data-region="right-rail-toggle"]');
          await toggle.waitFor();
          await toggle.click();
          const drawer = page.getByRole("dialog", { name: "VIKI panel", exact: true });
          await drawer.waitFor();
          await settledDrawer(page);
          await verifyNeedsYou(page);
          await waitForText(page, ".rail-section:has(.need-open) .rail-section-count", "3");
          expect(await page.locator(".rail-toggle-dot").count()).toBe(1);
          await capture(page, `${prefix}-drawer-open`);
          await verifyFooterInDrawer(page);
          await capture(page, `${prefix}-drawer-footer`);
          await verifyContextFooterInDrawer(page);
          await captureTarget(page, `${prefix}-target`, drawer);
          expect(
            await page.locator('[data-region="right-rail-toggle"] .rail-toggle-dot').count(),
          ).toBe(1);
          await capture(page, `${prefix}-drawer-closed-toggle-dot`);
        }
      } finally {
        await page?.close();
        await server.stop();
      }
    }
  }
}, 300_000);
