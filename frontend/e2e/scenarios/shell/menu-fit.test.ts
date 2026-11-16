/** Actual App menus with synthetic REST/socket data; no backend, provider or screenshots. */
import type { Browser, Locator, Page } from "playwright-core";
import { createServer, type ViteDevServer } from "vite";
import { afterAll, beforeAll, expect, test } from "vitest";
import { capabilitiesFixture, settingsFixture } from "../../../src/testShell";
import {
  ACCEPTED_MEDIA_TYPES,
  MAX_ATTACHMENTS,
  MAX_FILE_BYTES,
  MAX_TURN_BYTES,
} from "../../../src/turnInput";
import { launchEdge } from "../../harness";

const PORT = 5422;
const LONG_NAME = "A".repeat(32);
let browser: Browser | undefined;
let server: ViteDevServer | undefined;

beforeAll(async () => {
  server = await createServer({
    server: { host: "127.0.0.1", port: PORT, strictPort: true },
    logLevel: "silent",
  });
  await server.listen();
  browser = await launchEdge();
});
afterAll(async () => {
  await browser?.close();
  await server?.close();
});

async function settle(page: Page): Promise<void> {
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );
}

async function open(width: number, height: number, scale: number, assistantName: string) {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: { width, height }, reducedMotion: "reduce" });
  await page.routeWebSocket("**/ws", (socket) => socket.onMessage(() => {}));
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    const key = /^\/api\/v2\/drafts\/(.+)$/.exec(path)?.[1];
    const bodies: Record<string, unknown> = {
      "/api/rails": {
        agents: [],
        gates: [],
        routines: [],
        counts: { knowledge: 0, sources: 0, inbox: 0 },
        git: {
          state: "known",
          ref_state: "branch",
          branch: "feature/menu-fit-regression",
          head: "1234567",
          committed_at: "",
          dirty: 1,
        },
      },
      "/api/sessions": { sessions: [], state: "ready", reason: "", omitted: 0 },
      "/api/v2/capabilities": capabilitiesFixture(),
      "/api/v2/settings": settingsFixture({
        "identity.assistant_name": assistantName,
        "accessibility.font_scale": scale,
        "onboarding.completed": true,
      }),
      "/api/v2/attachment-capabilities": {
        provider: "claude",
        media_types: ACCEPTED_MEDIA_TYPES,
        max_files: MAX_ATTACHMENTS,
        max_file_bytes: MAX_FILE_BYTES,
        max_turn_bytes: MAX_TURN_BYTES,
        max_dimension: 8192,
      },
      "/api/v2/routines": {
        routines: [],
        scheduler: { state: "running", attended: true, paused: false },
      },
      "/api/v2/drafts": { drafts: [], next_after: null },
    };
    const body = key ? { key, revision: 0, text: "", saved_at: null } : bodies[path];
    return route.fulfill({
      status: body === undefined ? 404 : 200,
      contentType: "application/json",
      body: JSON.stringify(body ?? {}),
    });
  });
  // The status bar has no readiness element to poll: register the capabilities
  // response wait before `page.goto` so a reply that lands early is never missed.
  const capabilities = page.waitForResponse(
    (response) => new URL(response.url()).pathname === "/api/v2/capabilities",
  );
  await page.goto(`http://127.0.0.1:${PORT}/#token=menu-fit-synthetic`);
  await page.waitForFunction(
    (name) => document.querySelector(".wordmark")?.textContent === name,
    assistantName === "VIKI" ? "VIKI Agent" : assistantName,
  );
  // Wait for the capabilities response itself, then one animation frame so the render it
  // triggers has painted.
  await capabilities;
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
  await page.evaluate(() => document.fonts.ready);
  await page.evaluate((value) => {
    document.documentElement.style.setProperty("--font-scale", String(value));
  }, scale);
  await settle(page);
  return page;
}

async function expectFit(menu: Locator, focused: Locator): Promise<void> {
  const measured = await menu.evaluate((panel) => {
    const rect = (element: Element) => {
      const box = element.getBoundingClientRect();
      return { left: box.left, right: box.right, top: box.top, bottom: box.bottom };
    };
    const rows = [...panel.querySelectorAll<HTMLElement>('[role="menuitem"]')]
      .filter((row) => row.getClientRects().length > 0)
      .map((row) => ({
        box: rect(row),
        focused: document.activeElement === row,
        text: [...row.querySelectorAll(".app-menu-row-label, .help-menu-row-label, .kbd")].flatMap(
          (label) => {
            const range = document.createRange();
            range.selectNodeContents(label);
            return [...range.getClientRects()].map((box) => ({
              label: label.textContent,
              host: rect(label),
              left: box.left,
              right: box.right,
              top: box.top,
              bottom: box.bottom,
            }));
          },
        ),
      }));
    return {
      panel: rect(panel),
      client: panel.clientWidth,
      scroll: panel.scrollWidth,
      width: innerWidth,
      height: innerHeight,
      roots: [
        document.documentElement,
        document.body,
        ...document.querySelectorAll(".shell, .shell-body, .app-view"),
      ].map((root) => ({ x: root.scrollLeft, y: root.scrollTop })),
      window: { x: scrollX, y: scrollY },
      rows,
    };
  });
  expect(measured.scroll).toBeLessThanOrEqual(measured.client + 1);
  expect(measured.panel.left).toBeGreaterThanOrEqual(0);
  expect(measured.panel.right).toBeLessThanOrEqual(measured.width);
  expect(measured.panel.top).toBeGreaterThanOrEqual(0);
  expect(measured.panel.bottom).toBeLessThanOrEqual(measured.height);
  expect(measured.window).toEqual({ x: 0, y: 0 });
  for (const root of measured.roots) expect(root).toEqual({ x: 0, y: 0 });
  expect(await focused.evaluate((row) => document.activeElement === row)).toBe(true);
  expect(
    await focused.evaluate((row) => {
      const box = row.getBoundingClientRect();
      return row.contains(
        document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2),
      );
    }),
  ).toBe(true);
  for (const row of measured.rows) {
    const boxes = [row.box, ...row.text];
    for (const box of boxes) {
      expect(box.left).toBeGreaterThanOrEqual(measured.panel.left - 1);
      expect(box.right).toBeLessThanOrEqual(measured.panel.right + 1);
      if (row.focused) {
        expect(box.top).toBeGreaterThanOrEqual(measured.panel.top - 1);
        expect(box.bottom).toBeLessThanOrEqual(measured.panel.bottom + 1);
      }
    }
    for (const text of row.text) {
      expect(text.left, text.label ?? "Menu text").toBeGreaterThanOrEqual(text.host.left - 1);
      expect(text.right, text.label ?? "Menu text").toBeLessThanOrEqual(text.host.right + 1);
      expect(text.top, text.label ?? "Menu text").toBeGreaterThanOrEqual(text.host.top - 1);
      expect(text.bottom, text.label ?? "Menu text").toBeLessThanOrEqual(text.host.bottom + 1);
    }
  }
}

async function exercise(page: Page, kind: "app" | "help", wide: boolean, scale: number) {
  const toggle = page.getByRole("button", {
    name: kind === "app" ? "App menu" : "Help",
    exact: true,
  });
  const menu = page.getByRole("menu", { name: kind === "app" ? "App menu" : "Help", exact: true });
  await toggle.click();
  await menu.waitFor();
  const rows = menu.getByRole("menuitem");
  const count = await rows.count();
  const first = rows.first();
  const last = rows.last();
  await expectFit(menu, first);
  if (wide) {
    const panel = await menu.boundingBox();
    const anchor = await toggle.boundingBox();
    if (!panel || !anchor) throw new Error("Menu geometry is missing");
    expect(panel.width).toBeCloseTo((kind === "app" ? 260 : 240) * scale, 0);
    expect(panel.x + panel.width).toBeCloseTo(anchor.x + anchor.width, 0);
    expect(panel.y).toBeCloseTo(anchor.y + anchor.height + 6 * scale, 0);
    expect((await first.boundingBox())?.height).toBeCloseTo(34 * scale, 0);
  }
  await page.keyboard.press("End");
  await expectFit(menu, last);
  await page.keyboard.press("Home");
  await expectFit(menu, first);
  // Every row can receive focus and reveal its full label, including refused actions.
  for (let index = 1; index < count; index++) {
    await page.keyboard.press("ArrowDown");
    await expectFit(menu, rows.nth(index));
  }
  await page.keyboard.press("Home");
  const before = await menu.evaluate((panel) => ({
    scroll: panel.scrollHeight,
    client: panel.clientHeight,
  }));
  const panel = await menu.boundingBox();
  if (!panel) throw new Error("Menu geometry is missing");
  await page.mouse.move(panel.x + panel.width / 2, panel.y + panel.height / 2);
  await page.mouse.wheel(0, 900);
  if (before.scroll > before.client + 1) {
    await page.waitForFunction(
      (className) => (document.querySelector<HTMLElement>(className)?.scrollTop ?? 0) > 0,
      `.${kind}-menu-panel`,
    );
  }
  await page.keyboard.press("End");
  await expectFit(menu, last);
  // Use the measured center so Playwright cannot rescue a clipped row by scrolling ancestors.
  const lastBox = await last.boundingBox();
  if (!lastBox) throw new Error("Last menu action is missing");
  const refused = (await last.getAttribute("aria-disabled")) === "true";
  await page.mouse.click(lastBox.x + lastBox.width / 2, lastBox.y + lastBox.height / 2);
  if (refused) {
    expect(await menu.count()).toBe(1);
    await expectFit(menu, last);
    await page.keyboard.press("Escape");
  } else {
    await menu.waitFor({ state: "hidden" });
    await page.locator('.help-panel[role="dialog"]').waitFor();
    await page.keyboard.press("Escape");
    await page.locator('.help-panel[role="dialog"]').waitFor({ state: "hidden" });
  }
  await toggle.click();
  await menu.waitFor();
  await page.keyboard.press("Home");
  await expectFit(menu, first);
  const firstBox = await first.boundingBox();
  if (!firstBox) throw new Error("First menu action is missing");
  await page.mouse.click(firstBox.x + firstBox.width / 2, firstBox.y + firstBox.height / 2);
  await menu.waitFor({ state: "hidden" });
  if (kind === "app") {
    await page.locator('[data-region="settings"]').waitFor();
    await page.getByRole("button", { name: "Back", exact: true }).click();
    await page.locator('[data-region="conversation"]').waitFor();
  } else {
    // The first Help row is Welcome to VIKI Agent, a Help topic, not the tour (c522dd43).
    await page.locator('.help-panel[role="dialog"]').waitFor();
    await page.keyboard.press("Escape");
    await page.locator('.help-panel[role="dialog"]').waitFor({ state: "hidden" });
  }
  await toggle.click();
  await menu.waitFor();
  await page.keyboard.press("Escape");
  await menu.waitFor({ state: "hidden" });
  expect(await toggle.evaluate((button) => document.activeElement === button)).toBe(true);
  await toggle.click();
  await menu.waitFor();
  await page.mouse.click(1, 1);
  await menu.waitFor({ state: "hidden" });
}

for (const [width, height, scale, assistantName] of [
  [400, 450, 1.5, "VIKI"],
  [400, 450, 1.5, LONG_NAME],
  [900, 600, 1.5, "VIKI"],
  [900, 600, 1.5, LONG_NAME],
  [1440, 900, 1, "VIKI"],
  [1280, 900, 1.5, "VIKI"],
] as const) {
  test(`menus fit ${width}x${height} at ${scale} scale with ${assistantName.length}-character name`, async () => {
    const page = await open(width, height, scale, assistantName);
    try {
      for (const kind of ["app", "help"] as const) {
        await exercise(page, kind, width / scale >= 800, scale);
      }
    } finally {
      await page.close();
    }
  });
}

test("an open menu remains bounded through resizing and a font-scale change", async () => {
  const page = await open(900, 600, 1, LONG_NAME);
  try {
    for (const kind of ["app", "help"] as const) {
      const toggle = page.getByRole("button", {
        name: kind === "app" ? "App menu" : "Help",
        exact: true,
      });
      const menu = page.getByRole("menu", {
        name: kind === "app" ? "App menu" : "Help",
        exact: true,
      });
      await toggle.click();
      await menu.waitFor();
      await page.evaluate(() => document.documentElement.style.setProperty("--font-scale", "1.5"));
      await settle(page);
      await expectFit(menu, menu.getByRole("menuitem").first());
      await page.setViewportSize({ width: 400, height: 450 });
      await page.keyboard.press("End");
      await expectFit(menu, menu.getByRole("menuitem").last());
      await page.evaluate(() => document.documentElement.style.setProperty("--font-scale", "1"));
      await settle(page);
      await page.keyboard.press("Home");
      await expectFit(menu, menu.getByRole("menuitem").first());
      await page.keyboard.press("Escape");
      await menu.waitFor({ state: "hidden" });
      await page.setViewportSize({ width: 900, height: 600 });
    }
  } finally {
    await page.close();
  }
});

test("menus retain a bounded sheet when anchor positioning is unavailable", async () => {
  const page = await open(400, 450, 1.5, LONG_NAME);
  try {
    const removed = await page.evaluate(() => {
      let count = 0;
      for (const sheet of document.styleSheets) {
        for (let index = sheet.cssRules.length - 1; index >= 0; index--) {
          const rule = sheet.cssRules[index];
          if (rule instanceof CSSSupportsRule && rule.conditionText === "(top: anchor(bottom))") {
            sheet.deleteRule(index);
            count++;
          }
        }
      }
      return count;
    });
    expect(removed).toBe(2);
    for (const kind of ["app", "help"] as const) await exercise(page, kind, false, 1.5);
  } finally {
    await page.close();
  }
});
