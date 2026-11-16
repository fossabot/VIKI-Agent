/**
 * The app's views at 400 px, at 900 px and under root zoom, in real Edge against the real
 * backend.
 *
 * Only a browser can answer any of this: jsdom lays nothing out, and `html { zoom: … }` — the
 * one mechanism the font-scale setting uses — has no jsdom implementation at all. The zoom is
 * set here by writing `--font-scale` on the root, which is exactly what `applyAppearance` does
 * (`appearance.ts`) and leaves `appearance.css`'s own rule to do the rest; that the settings
 * control really reaches it is proved end to end in `e2e/scenarios/shell/settings.test.ts`.
 *
 * Run from frontend/ (binds 8765; check it is free first):
 *   npx vitest run -c vitest.e2e.config.ts e2e/scenarios/shell/narrow-views.test.ts
 */
import type { Browser, Page } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import type { SettingsView } from "../../../src/clients/app/shellApi";
import { appUrl, launchEdge } from "../../harness";
import { ORIGIN, type Server, startServer } from "../../server";

let browser: Browser | undefined;
let server: Server | undefined;

beforeAll(async () => {
  browser = await launchEdge();
  server = await startServer({});
}, 90_000);

afterAll(async () => {
  await server?.stop();
  await browser?.close();
});

async function openApp(
  width: number,
  height = 900,
  deviceScaleFactor = 1,
  fontScale = 1,
): Promise<Page> {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor });
  // Keep the initial setting and the measured scale equal; its delayed commit must not undo zoom.
  await page.route(`${ORIGIN}/api/v2/settings`, async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const response = await route.fetch();
    const settings = (await response.json()) as SettingsView;
    return route.fulfill({
      response,
      json: {
        ...settings,
        effective: { ...settings.effective, "accessibility.font_scale": fontScale },
      } satisfies SettingsView,
    });
  });
  const settings = page.waitForResponse(
    (response) => new URL(response.url()).pathname === "/api/v2/settings",
  );
  // Capabilities are what the app menu's rows and the vault surfaces are drawn from. The status
  // bar has no readiness element to poll, so wait for the capabilities response itself
  // (registered here, before `page.goto`), then one animation frame so the render it triggers
  // has painted before anything here measures the top bar.
  const capabilities = page.waitForResponse(
    (response) => new URL(response.url()).pathname === "/api/v2/capabilities",
  );
  await page.goto(appUrl());
  await page.waitForSelector('[data-region="shell"]');
  await capabilities;
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
  await settings;
  await page.waitForFunction(
    (wanted) =>
      document.documentElement.style.getPropertyValue("--font-scale") === wanted &&
      getComputedStyle(document.documentElement).zoom === wanted,
    String(fontScale),
  );
  await page.evaluate(() => document.fonts.ready);
  return page;
}

/** The font-scale setting's own mechanism: one custom property on the root. The settings
 * commit applies its own scale in an effect that can run a frame after the response, so the
 * value is written until it survives two frames. */
async function setFontScale(page: Page, scale: number): Promise<void> {
  await page.evaluate(async (value: string) => {
    const root = document.documentElement;
    const frame = () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    for (let attempt = 0; attempt < 20; attempt += 1) {
      root.style.setProperty("--font-scale", value);
      await frame();
      await frame();
      if (root.style.getPropertyValue("--font-scale") === value) return;
    }
    throw new Error("--font-scale did not hold");
  }, String(scale));
}

async function composerGeometry(page: Page) {
  return page.evaluate(() => {
    const textarea = document.querySelector<HTMLTextAreaElement>('textarea[aria-label="Message"]');
    const composer = textarea?.closest(".composer");
    const slot = textarea?.closest(".composer-slot");
    const conversation = slot?.closest(".conversation");
    if (!textarea || !composer || !slot || !conversation)
      throw new Error("Composer geometry is missing");
    const bounds = (el: Element) => {
      const rect = el.getBoundingClientRect();
      return {
        left: rect.left,
        right: rect.right,
        top: rect.top,
        bottom: rect.bottom,
        height: rect.height,
      };
    };
    return {
      width: innerWidth,
      height: innerHeight,
      local: Number.parseFloat(textarea.style.height),
      slotMaximum: Number.parseFloat(
        getComputedStyle(slot).getPropertyValue("--composer-max-height"),
      ),
      scroll: textarea.scrollHeight,
      client: textarea.clientHeight,
      textarea: bounds(textarea),
      composer: bounds(composer),
      slot: bounds(slot),
      conversation: bounds(conversation),
    };
  });
}

function expectComposerContained(measured: Awaited<ReturnType<typeof composerGeometry>>): void {
  for (const region of [
    measured.textarea,
    measured.composer,
    measured.slot,
    measured.conversation,
  ]) {
    expect(region.left).toBeGreaterThanOrEqual(-1);
    expect(region.right).toBeLessThanOrEqual(measured.width + 1);
    expect(region.top).toBeGreaterThanOrEqual(-1);
    expect(region.bottom).toBeLessThanOrEqual(measured.height + 1);
  }
  for (const [inner, outer] of [
    [measured.textarea, measured.composer],
    [measured.composer, measured.slot],
    [measured.slot, measured.conversation],
  ] as const) {
    expect(inner.left).toBeGreaterThanOrEqual(outer.left - 1);
    expect(inner.right).toBeLessThanOrEqual(outer.right + 1);
    expect(inner.top).toBeGreaterThanOrEqual(outer.top - 1);
    expect(inner.bottom).toBeLessThanOrEqual(outer.bottom + 1);
  }
  expect(measured.scroll).toBeGreaterThan(measured.client);
  expect(measured.textarea.height).toBeLessThanOrEqual(0.4 * measured.height + 2);
}

/** True when anything inside `selector` paints past the window's own width. */
function noHorizontalOverflow(page: Page): Promise<boolean> {
  return page.evaluate(() => {
    const root = document.documentElement;
    return root.scrollWidth <= root.clientWidth + 1;
  });
}

/** Opens the app menu and picks the first row whose name matches `row`. */
async function openMenuAnd(page: Page, row: RegExp): Promise<void> {
  await page.locator('[data-region="app-menu"]').click();
  await page.getByRole("menuitem", { name: row }).first().click();
}

const MENU = {
  settings: /^Settings/,
  data: /Data locations|Where data lives/,
  diagnostics: /^Diagnostics$/,
} as const;

test("the top bar fits at 400 px with capabilities known", async () => {
  const page = await openApp(400);

  const overflow = await page.evaluate(() => {
    const bar = document.querySelector<HTMLElement>(".topbar");
    if (!bar) return null;
    return { scroll: bar.scrollWidth, client: bar.clientWidth };
  });
  expect(overflow).not.toBeNull();
  // The app-menu button joins the two toggles, the git chip and New session here. The
  // chip is the one field allowed to truncate; the 32px menu button never is.
  expect(overflow?.scroll).toBeLessThanOrEqual((overflow?.client ?? 0) + 1);
  const menu = await page.locator('[data-region="app-menu"]').boundingBox();
  expect(Math.round(menu?.width ?? 0)).toBe(32);

  await page.close();
});

test.each([
  [400, 1],
  [440, 1.15],
  [620, 1.5],
])("Help stays visible and clickable at %i px and font scale %s", async (width, scale) => {
  const page = await openApp(width, 900, 1, scale);
  try {
    await setFontScale(page, scale);
    const help = page.getByRole("button", { name: "Help", exact: true });
    expect(await help.count()).toBe(1);
    expect(await help.isVisible()).toBe(true);
    expect(Math.round((await help.boundingBox())?.width ?? 0)).toBe(Math.round(32 * scale));

    // A synthetic long name exercises the wordmark's layout without changing stored settings.
    await page.locator(".wordmark").evaluate((el) => {
      el.textContent = "An unusually long assistant name for the narrow window";
    });
    const layout = await page.locator(".topbar").evaluate((bar) => {
      const rect = bar.getBoundingClientRect();
      const wordmark = bar.querySelector<HTMLElement>(".wordmark");
      if (!wordmark) throw new Error("Assistant name is missing");
      const nameRect = wordmark.getBoundingClientRect();
      const range = document.createRange();
      range.selectNodeContents(wordmark);
      return {
        left: rect.left,
        right: rect.right,
        top: rect.top,
        bottom: rect.bottom,
        scroll: bar.scrollWidth,
        client: bar.clientWidth,
        wordmark: {
          text: wordmark.textContent,
          left: nameRect.left,
          right: nameRect.right,
          top: nameRect.top,
          bottom: nameRect.bottom,
          scroll: wordmark.scrollWidth,
          client: wordmark.clientWidth,
          textRects: [...range.getClientRects()].map((box) => ({
            left: box.left,
            right: box.right,
            top: box.top,
            bottom: box.bottom,
          })),
        },
        controls: [...bar.querySelectorAll("button, select")]
          .map((el) => el.getBoundingClientRect())
          .filter((box) => box.width > 0)
          .map((box) => ({
            left: box.left,
            right: box.right,
            top: box.top,
            bottom: box.bottom,
          })),
      };
    });
    expect(layout.scroll).toBeLessThanOrEqual(layout.client + 1);
    expect(layout.wordmark.text).toBe("An unusually long assistant name for the narrow window");
    expect(layout.wordmark.scroll).toBeLessThanOrEqual(layout.wordmark.client + 1);
    expect(layout.wordmark.textRects.length).toBeGreaterThan(1);
    for (const box of [layout.wordmark, ...layout.wordmark.textRects]) {
      expect(box.left).toBeGreaterThanOrEqual(layout.wordmark.left - 1);
      expect(box.right).toBeLessThanOrEqual(layout.wordmark.right + 1);
      expect(box.top).toBeGreaterThanOrEqual(layout.wordmark.top - 1);
      expect(box.bottom).toBeLessThanOrEqual(layout.wordmark.bottom + 1);
      expect(box.left).toBeGreaterThanOrEqual(layout.left - 1);
      expect(box.right).toBeLessThanOrEqual(layout.right + 1);
      expect(box.top).toBeGreaterThanOrEqual(layout.top - 1);
      expect(box.bottom).toBeLessThanOrEqual(layout.bottom + 1);
    }
    for (const [index, control] of layout.controls.entries()) {
      expect(control.left).toBeGreaterThanOrEqual(layout.left - 1);
      expect(control.right).toBeLessThanOrEqual(layout.right + 1);
      expect(control.top).toBeGreaterThanOrEqual(layout.top - 1);
      expect(control.bottom).toBeLessThanOrEqual(layout.bottom + 1);
      for (const other of layout.controls.slice(index + 1)) {
        expect(
          control.left < other.right &&
            control.right > other.left &&
            control.top < other.bottom &&
            control.bottom > other.top,
        ).toBe(false);
      }
    }

    await help.click();
    await page.getByRole("menuitem", { name: "Keyboard shortcuts" }).click();
    await page.getByRole("dialog", { name: "Help", exact: true }).waitFor();
    await page.getByRole("button", { name: "Close help" }).click();
    expect(await help.isVisible()).toBe(true);
    await page.locator('[data-region="app-menu"]').click();
    expect(await page.getByRole("menuitem", { name: "Help", exact: true }).count()).toBe(0);
    await page.keyboard.press("Escape");
  } finally {
    await page.close();
  }
});

test("every view fits the narrow window, every Settings tab fits, and Escape returns", async () => {
  for (const [width, deviceScale, fontScale] of [
    [400, 1, 1],
    [720, 2, 1],
    [784, 1, 1],
    [800, 1, 1],
    [900, 1, 1],
    [900, 1, 1.5],
    [1000, 1, 1.5],
  ] as const) {
    const page = await openApp(width, 900, deviceScale, fontScale);
    await setFontScale(page, fontScale);

    for (const row of [MENU.settings, MENU.data, MENU.diagnostics] as const) {
      await openMenuAnd(page, row);
      await page.waitForSelector('[data-region="view"]');
      expect(await noHorizontalOverflow(page)).toBe(true);
      expect(await page.getByRole("button", { name: "Help", exact: true }).isVisible()).toBe(true);
      expect(await page.getByRole("button", { name: /^Help:/ }).count()).toBe(0);
      await page.keyboard.press("Escape");
      await page.waitForSelector('[data-region="conversation"]');
    }

    // Change root scale with Settings mounted, without a window resize.
    await setFontScale(page, 1);
    await openMenuAnd(page, MENU.settings);
    await page.waitForSelector('[data-region="settings"]');
    await setFontScale(page, fontScale);
    const fit = await page.evaluate(() => {
      const nav = document.querySelector<HTMLElement>(".settings-nav");
      if (!nav) throw new Error("Settings navigation is missing");
      const bounds = (el: Element) => {
        const rect = el.getBoundingClientRect();
        return { left: rect.left, right: rect.right };
      };
      return {
        width: innerWidth,
        nav: bounds(nav),
        orientation: nav.getAttribute("aria-orientation"),
        direction: getComputedStyle(nav).flexDirection,
        regions: [
          ...document.querySelectorAll<HTMLElement>(
            ".app-view, .settings-view, .settings-grid, .settings-nav, .settings-panel, .settings-row-control",
          ),
        ].map((el) => ({
          name: el.className,
          scroll: el.scrollWidth,
          client: el.clientWidth,
          ...bounds(el),
        })),
        tabs: [...nav.querySelectorAll<HTMLElement>('[role="tab"]')].map((tab) => {
          const range = document.createRange();
          range.selectNodeContents(tab);
          return {
            label: tab.textContent,
            boxes: [
              bounds(tab),
              ...[...range.getClientRects()].map((box) => ({
                left: box.left,
                right: box.right,
              })),
            ],
          };
        }),
        controls: [
          ...document.querySelectorAll<HTMLElement>(
            ".settings-search-input, .settings-text, .settings-select, .settings-switch",
          ),
        ].map((control) => {
          const host = control.closest(".settings-search, .settings-row-control");
          if (!host) throw new Error("Settings control host is missing");
          return { control: bounds(control), host: bounds(host) };
        }),
      };
    });
    const horizontal = width / fontScale < 800;
    expect(fit.orientation).toBe(horizontal ? "horizontal" : "vertical");
    expect(fit.direction).toBe(horizontal ? "row" : "column");
    for (const region of fit.regions) {
      expect(region.scroll, region.name).toBeLessThanOrEqual(region.client + 1);
      expect(region.left, region.name).toBeGreaterThanOrEqual(-1);
      expect(region.right, region.name).toBeLessThanOrEqual(fit.width + 1);
    }
    expect(fit.tabs).toHaveLength(8);
    for (const tab of fit.tabs) {
      for (const box of tab.boxes) {
        expect(box.left, tab.label ?? "tab").toBeGreaterThanOrEqual(fit.nav.left - 1);
        expect(box.right, tab.label ?? "tab").toBeLessThanOrEqual(fit.nav.right + 1);
      }
    }
    expect(fit.controls.length).toBeGreaterThan(0);
    for (const { control, host } of fit.controls) {
      expect(control.left).toBeGreaterThanOrEqual(host.left - 1);
      expect(control.right).toBeLessThanOrEqual(host.right + 1);
    }
    expect(await noHorizontalOverflow(page)).toBe(true);

    await page.close();
  }
});

test("document heights under root zoom", async () => {
  const page = await openApp(900, 900, 1, 1.5);
  await setFontScale(page, 1.5);

  const heights = await page.evaluate(() => ({
    innerHeight: window.innerHeight,
    clientHeight: document.documentElement.clientHeight,
    // Proof the zoom is really on: a measurement of two identical numbers means nothing if the
    // stylesheet never reached the bundle.
    zoom: getComputedStyle(document.documentElement).zoom,
    topBar: document.querySelector(".topbar")?.getBoundingClientRect().height ?? 0,
  }));
  expect(heights.zoom).toBe("1.5");
  // Measured rather than assumed: Chromium's standardized root zoom divides NEITHER height —
  // `innerHeight` and `clientHeight` both report the window's own pixels — while
  // `getBoundingClientRect()` and `style.height` are in the zoomed element's local pixels.
  // Anything that mixes the two is wrong by the scale, which is why the composer's cap carries a
  // `/ scale` term.
  console.log(`root zoom 1.5 at 900x900: ${JSON.stringify(heights)}`);
  expect(heights.clientHeight).toBe(heights.innerHeight);

  const box = page.getByLabel("Message");
  await page.evaluate(() => document.fonts.ready);
  expect(await page.locator(".draft-status").textContent()).not.toContain("Can't save");
  await box.fill(Array.from({ length: 80 }, (_, i) => `line ${i}`).join("\n"));
  const capped = await composerGeometry(page);
  // The slot's other controls can cap a short window before the window fraction does. The
  // roomy case below pins the scale division; this case pins containment and scrolling.
  expectComposerContained(capped);

  await page.close();
});

test("the window height cap divides by root scale when the composer has room", async () => {
  const page = await openApp(900, 1920, 1, 1.5);
  try {
    await setFontScale(page, 1.5);
    await page.evaluate(() => document.fonts.ready);
    expect(await page.locator(".draft-status").textContent()).not.toContain("Can't save");
    await page
      .getByLabel("Message")
      .fill(Array.from({ length: 80 }, (_, i) => `line ${i}`).join("\n"));
    const capped = await composerGeometry(page);
    expectComposerContained(capped);
    const expected = 0.4 * capped.height;
    const fixed = capped.slot.height - capped.textarea.height;
    expect(capped.slotMaximum * 1.5 - fixed).toBeGreaterThanOrEqual(expected);
    expect(Math.abs(capped.local - expected / 1.5)).toBeLessThanOrEqual(2);
    expect(Math.abs(capped.textarea.height - expected)).toBeLessThanOrEqual(2);
  } finally {
    await page.close();
  }
});

test("fixed layers and the composer at scale 1.5", async () => {
  const page = await openApp(900, 900, 1, 1.5);
  await setFontScale(page, 1.5);

  // The search palette is `position: fixed`, outside `.shell`; the app-menu panel below 800 CSS
  // px is fixed too. Root zoom must scale both with everything else, not leave them off-screen.
  await page.keyboard.press("Control+k");
  await page.waitForSelector(".palette");
  const palette = await page.evaluate(() => {
    const panel = document.querySelector<HTMLElement>(".palette");
    if (!panel) return null;
    const rect = panel.getBoundingClientRect();
    return {
      right: rect.right,
      bottom: rect.bottom,
      width: document.documentElement.clientWidth,
      height: document.documentElement.clientHeight,
    };
  });
  expect(palette?.right).toBeLessThanOrEqual((palette?.width ?? 0) + 1);
  expect(palette?.bottom).toBeLessThanOrEqual((palette?.height ?? 0) + 1);
  await page.keyboard.press("Escape");

  // Every drawer this width has opens and closes without leaving the shell scrolled sideways.
  // At 900 CSS px only the right rail is a drawer; below 800 both are.
  for (const toggle of ["left-rail-toggle", "right-rail-toggle"]) {
    const button = page.locator(`[data-region="${toggle}"]`);
    if ((await button.count()) === 0) continue;
    await button.click();
    await page.getByRole("button", { name: "Close" }).first().click();
  }
  expect(await page.evaluate(() => document.querySelector<HTMLElement>(".shell")?.scrollLeft)).toBe(
    0,
  );

  await page.close();
});

test("font scale at the M band: 900 px at 1.5 keeps its gutters and does not overflow", async () => {
  const page = await openApp(900, 900, 1, 1.5);
  await setFontScale(page, 1.5);

  const composer = await page.evaluate(() => {
    const el = document.querySelector<HTMLElement>('[data-region="composer"]');
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    return { left: rect.left, right: rect.right, width: document.documentElement.clientWidth };
  });
  expect(composer?.left).toBeGreaterThanOrEqual(16);
  expect((composer?.width ?? 0) - (composer?.right ?? 0)).toBeGreaterThanOrEqual(16);
  expect(await noHorizontalOverflow(page)).toBe(true);

  await page.close();
});
