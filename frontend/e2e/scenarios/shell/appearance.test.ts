/**
 * The theme end-to-end: the resolved theme a stored setting and the OS colour scheme produce
 * together, the OS's own "more contrast" preference, the page's earliest language and theme
 * before any settings document has loaded, and the OS's forced-colors mode, which strips author
 * colour from everything unless a rule opts an element back in (an explicit outline, a border in
 * a system colour, or `forced-color-adjust: none`).
 *
 * Run from frontend/ (binds 8765; check it is free first):
 *   npx vitest run -c vitest.e2e.config.ts e2e/scenarios/shell/appearance.test.ts
 */
import type { Browser, Page } from "playwright-core";
import { createServer, type ViteDevServer } from "vite";
import { afterAll, afterEach, beforeAll, describe, expect, test } from "vitest";
import { appUrl, launchEdge, send, waitForText } from "../../harness";
import { type Server, startServer } from "../../server";
import { sidebarNavigation } from "../../sidebar-navigation";

const VIEWPORT = { width: 1440, height: 900 };
type PageOptions = NonNullable<Parameters<Browser["newPage"]>[0]>;

let browser: Browser | undefined;
let server: Server | undefined;

beforeAll(async () => {
  browser = await launchEdge();
});

afterAll(async () => {
  await browser?.close();
});

afterEach(async () => {
  await server?.stop();
  server = undefined;
});

async function openApp(options: PageOptions = {}): Promise<Page> {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: VIEWPORT, ...options });
  await page.goto(appUrl());
  await page.waitForSelector('[data-region="shell"]');
  return page;
}

/** Ctrl+, is the shortcut the app menu's own row documents (`e2e/scenarios/shell/settings.test.ts`'s own
 * pattern), then the Appearance tab. */
async function openAppearance(page: Page): Promise<void> {
  await page.keyboard.press("Control+Comma");
  await page.waitForSelector('[data-region="settings"]');
  await page.getByRole("tab", { name: /Appearance/ }).click();
}

async function closeSettings(page: Page): Promise<void> {
  const settings = page.locator('[data-region="settings"]');
  const selected = await settings
    .locator('[role="tab"][aria-selected="true"]')
    .getAttribute("data-page");
  expect(["general", "appearance"]).toContain(selected);
  if (selected === "appearance") {
    await settings.locator('.settings-page[aria-busy="false"]').waitFor();
  }
  const back = settings.getByRole("button", { name: "Back", exact: true });
  expect(await back.isVisible()).toBe(true);
  expect(await back.isEnabled()).toBe(true);
  await back.focus();
  await page.keyboard.press("Escape");
  if (selected === "appearance") {
    await settings.locator('[data-page="general"][aria-selected="true"]').waitFor();
    expect(await settings.isVisible()).toBe(true);
    await back.focus();
    await page.keyboard.press("Escape");
  }
  await page.waitForSelector('[data-region="conversation"]');
}

/** Ctrl+, alone: Settings opens on its own default landing tab (General), which is where the
 * identity avatar fields live. */
async function openSettings(page: Page): Promise<void> {
  await page.keyboard.press("Control+Comma");
  await page.waitForSelector('[data-region="settings"]');
}

/** The computed `outline-style` of the first element `selector` matches. */
async function outlineStyle(page: Page, selector: string): Promise<string> {
  return page
    .locator(selector)
    .first()
    .evaluate((el) => getComputedStyle(el).outlineStyle);
}

/** The computed border-style on the side `side` names of the first element `selector` matches
 * (only three selection marks — the selected sidebar destination, the selected Settings tab and the
 * effort in effect — draw as a border under forced colors, so the outline channel stays free
 * for their own focus ring; the model menu's rows keep outlines). */
async function borderStyle(
  page: Page,
  selector: string,
  side: "bottom" | "left" | "top",
): Promise<string> {
  return page
    .locator(selector)
    .first()
    .evaluate((el, edge) => {
      const style = getComputedStyle(el);
      if (edge === "bottom") return style.borderBottomStyle;
      if (edge === "left") return style.borderLeftStyle;
      return style.borderTopStyle;
    }, side);
}

async function tokenValue(page: Page, name: string): Promise<string> {
  return page.evaluate(
    (property: string) =>
      getComputedStyle(document.documentElement).getPropertyValue(property).trim(),
    name,
  );
}

async function waitForTheme(page: Page, theme: "light" | "dark"): Promise<void> {
  await page.waitForFunction(
    (wanted: "light" | "dark") => document.documentElement.dataset.theme === wanted,
    theme,
  );
}

async function waitForFontScale(page: Page, scale: string): Promise<void> {
  await page.waitForFunction(
    (wanted: string) => document.documentElement.style.getPropertyValue("--font-scale") === wanted,
    scale,
  );
}

/** The value a CSS colour (a `var(--token)` or literal) resolves to when forced colors is
 * active but the probe element opts out with `forced-color-adjust: none` — the same escape
 * hatch `.avatar` and the top bar's `Face` ring use for their own accent borders, read the same
 * way a browser resolves it rather than a literal hex/rgb this file would have to keep in step
 * with tokens.css by hand. */
async function forcedColorAdjustNone(page: Page, color: string): Promise<string> {
  return page.evaluate((value: string) => {
    const probe = document.createElement("span");
    probe.style.color = value;
    probe.style.forcedColorAdjust = "none";
    document.body.appendChild(probe);
    const resolved = getComputedStyle(probe).color;
    probe.remove();
    return resolved;
  }, color);
}

const TOPBAR_TABBABLE = "button, [href], input, select, textarea, [tabindex]";

/** The number of `.topbar` descendants a Tab walk would actually stop at: visible, and not
 * pulled out of tab order with `tabindex="-1"`. */
async function topbarTabbableCount(page: Page): Promise<number> {
  return page.evaluate((selector: string) => {
    const bar = document.querySelector(".topbar");
    if (!bar) return 0;
    return Array.from(bar.querySelectorAll<HTMLElement>(selector)).filter(
      (el) => el.tabIndex >= 0 && el.offsetParent !== null,
    ).length;
  }, TOPBAR_TABBABLE);
}

/** Focuses the first such element, in document order — where a real Tab walk starting outside
 * the bar would land first. */
async function focusFirstTopbarTabbable(page: Page): Promise<void> {
  await page.evaluate((selector: string) => {
    const bar = document.querySelector(".topbar");
    const first = bar
      ? Array.from(bar.querySelectorAll<HTMLElement>(selector)).find(
          (el) => el.tabIndex >= 0 && el.offsetParent !== null,
        )
      : undefined;
    first?.focus();
  }, TOPBAR_TABBABLE);
}

/** The bounding box of the first element `selector` matches, thrown if it is not rendered. */
async function elementBox(page: Page, selector: string) {
  const found = await page.locator(selector).first().boundingBox();
  if (!found) throw new Error(`${selector} is not rendered`);
  return found;
}

/** Whether two axis-aligned boxes overlap; touching edges (equal x/y sums) is not an overlap. */
function boxesIntersect(
  a: { x: number; y: number; width: number; height: number },
  b: { x: number; y: number; width: number; height: number },
): boolean {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
}

test("Appearance applies the stored theme's own tokens", async () => {
  server = await startServer({});
  // A fresh store's `appearance.theme` is `system` (the settings default, src/viki_agent/app/settings.py's
  // appearance.theme spec), which this dark context also resolves
  // to `dark` (`prefers-color-scheme`). Starting dark makes the Light step below a real flip,
  // not a resolution the page already showed before Apply ran.
  const page = await openApp({ colorScheme: "dark" });
  await waitForTheme(page, "dark");

  await openAppearance(page);
  await page.locator("#appearance-theme").selectOption("light");
  await page.getByRole("button", { name: "Apply" }).click();
  await waitForTheme(page, "light");
  expect(await page.evaluate(() => getComputedStyle(document.documentElement).colorScheme)).toBe(
    "light",
  );
  expect(await tokenValue(page, "--g0")).toBe("#f4f4f4");
  await closeSettings(page);

  await openAppearance(page);
  await page.locator("#appearance-theme").selectOption("dark");
  await page.getByRole("button", { name: "Apply" }).click();
  await waitForTheme(page, "dark");
  expect(await page.evaluate(() => getComputedStyle(document.documentElement).colorScheme)).toBe(
    "dark",
  );
  expect(await tokenValue(page, "--g0")).toBe("#0b0b0b");

  await page.close();
});

test("a stored system theme follows the OS colour scheme, live", async () => {
  server = await startServer({});

  const dark = await openApp({ colorScheme: "dark" });
  await waitForTheme(dark, "dark");
  await dark.close();

  const light = await openApp({ colorScheme: "light" });
  await waitForTheme(light, "light");
  // No reload between the two: App subscribes to the OS colour scheme through one `matchMedia`
  // listener, so a live change must flip the resolved theme by itself, with no navigation.
  await light.emulateMedia({ colorScheme: "dark" });
  await waitForTheme(light, "dark");
  await light.close();
});

test("the OS's more-contrast preference raises the text and border tokens", async () => {
  server = await startServer({});

  const dark = await openApp({ colorScheme: "dark", contrast: "more" });
  await waitForTheme(dark, "dark");
  expect(await tokenValue(dark, "--t3")).toBe("#b0b0b0");
  expect(await tokenValue(dark, "--line")).toBe("#7a7a7a");
  await dark.close();

  const light = await openApp({ colorScheme: "light", contrast: "more" });
  await waitForTheme(light, "light");
  expect(await tokenValue(light, "--t3")).toBe("#383838");
  expect(await tokenValue(light, "--line")).toBe("#6e6e6e");
  await light.close();
});

test("forced colors leave every erasable indicator visible", async () => {
  server = await startServer({ fixture: "tests/fixtures/claude/subagent-explore.jsonl" });
  const page = await openApp({ forcedColors: "active", colorScheme: "dark" });

  // The current Chat destination keeps a border so the UA's own
  // focus ring stays free to mark keyboard focus on its own.
  expect(await borderStyle(page, '.rail-nav-button[aria-current="page"]', "bottom")).not.toBe(
    "none",
  );

  // Settings' own current-tab indicator (Settings.css) and the identity avatar's accent border
  // (Avatar.css) both sit on the General page, Settings' own default landing tab.
  await openSettings(page);
  expect(await borderStyle(page, ".settings-tab-on", "left")).not.toBe("none");
  const avatarBorder = await page
    .locator(".avatar")
    .first()
    .evaluate((el) => getComputedStyle(el).borderColor);
  // The probe needs its own `forced-color-adjust: none` — without it, forced colors would
  // repaint the probe (which owns no indicator or image rule of its own) while leaving the real
  // `.avatar` alone.
  const accentSlate = await forcedColorAdjustNone(page, "var(--accent-slate)");
  expect(avatarBorder).toBe(accentSlate);
  await closeSettings(page);

  // This default identity has no custom avatar, so the right rail omits its old portrait.
  expect(await page.locator('[data-region="right-rail"]').count()).toBe(1);
  expect(await page.locator('[data-region="right-rail"] .face').count()).toBe(0);
  expect(await page.locator('img[src^="/api/face/"]').count()).toBe(0);

  // A subagent turn (fixture "06") gives the routing row's AgentFace, and the command and model
  // lists the slash menu and the model chip both read from the same session.
  await send(page, "Which file in _templates/ is the daily note template?");
  await page.locator(".reply .md").first().waitFor({ timeout: 30_000 });

  // AgentFace.tsx fills its shape with `var(--g3)`, rgb(22, 22, 22) only in the dark theme this
  // page is opened with. Chromium does not force SVG `fill`/`stroke` paint (probed separately),
  // so its shape retains the original fill in forced colors.
  expect(
    await page
      .locator(".routing-head .face svg g")
      .first()
      .evaluate((el) => getComputedStyle(el).fill),
  ).toBe("rgb(22, 22, 22)");

  // RowMenu.css's own indicator is not reached here: the row's `⋯` needs a listed session
  // (SessionList.tsx), but the fake CLI writes no transcript at all (tests/replay.py replays
  // frames only, and the scratch home's `.claude` starts empty), so the rail's REST listing never
  // carries this session and no row ever gets one. The permission card's own agent chip (fixture
  // subagent-permission.jsonl) is reachable, but it renders the same AgentFace the routing row
  // above already covers.

  // The header's own `⋯` menu (HeaderMenu.css); its one row is enabled with no other session
  // holding the vault.
  await page.locator('[aria-label="Session actions"]').click();
  const headerItem = page.locator(".header-menu .mi").first();
  await headerItem.hover();
  expect(await headerItem.evaluate((el) => getComputedStyle(el).outlineStyle)).not.toBe("none");
  await page.keyboard.press("Escape");

  // The app menu (AppMenu.css): an enabled row.
  await page.locator('[data-region="app-menu"]').click();
  const appItem = page.locator('.app-menu-panel .mi:not([aria-disabled="true"])').first();
  await appItem.hover();
  expect(await appItem.evaluate((el) => getComputedStyle(el).outlineStyle)).not.toBe("none");
  await page.keyboard.press("Escape");

  // The model chip's own menu (ModelChip.css): the current model's row keeps a dashed outline
  // forced colors cannot erase, distinct from the keyboard-highlighted row's solid one, so the
  // two rows stay apart once forced colors drop the fills and the inset bar.
  // Hovering a row other than the current model first keeps the two apart — the current model's
  // row would otherwise be both `.on` and `[data-highlighted]` at once, and the highlighted rule
  // (same specificity, later in the sheet) would win, hiding the dashed rule this checks.
  //
  // Wait for the turn above to actually end (Send re-enabled), not only for its reply text: the
  // chip stays disabled while a turn runs (ModelChip.tsx), and a click during that gap is a
  // no-op.
  await page.locator(".composer-submit:not([disabled])").waitFor({ timeout: 30_000 });
  await page.locator('.model-chip[aria-haspopup="menu"]').click();
  await page.locator(".chip-menu").waitFor();
  await page.locator(".chip-menu .mi:not(.on)").first().hover();
  expect(await outlineStyle(page, ".chip-menu .mi.on")).toBe("dashed");
  expect(await outlineStyle(page, ".chip-menu .mi[data-highlighted]")).toBe("solid");
  // The effort in effect draws as a border, not an outline, so its own focus ring
  // (`.chip-effort[data-highlighted]`'s outline) stays free to mark keyboard focus on its own.
  expect(await borderStyle(page, ".chip-effort.on", "top")).toBe("solid");
  await page.keyboard.press("Escape");

  // The slash menu's highlighted row: SlashMenu.css has no copy of this rule, so base.css's
  // `.mi.on` is the only one that sets it — only the computed outline is asserted, never which
  // sheet sets it.
  await page.getByLabel("Message").fill("/");
  await page.locator(".menu .mi.on").first().waitFor();
  expect(await outlineStyle(page, ".menu .mi.on")).not.toBe("none");
  await page.keyboard.press("Escape");

  await page.close();
});

test("forced colors keep a focus outline on every top bar control a Tab walk reaches", async () => {
  server = await startServer({});
  const page = await openApp({ forcedColors: "active", colorScheme: "dark" });

  const tabbable = await topbarTabbableCount(page);
  await focusFirstTopbarTabbable(page);
  let steps = 0;
  for (; steps < tabbable; steps++) {
    const style = await page.evaluate(() => {
      const el = document.activeElement as HTMLElement | null;
      if (!el?.closest(".topbar")) return null;
      return getComputedStyle(el).outlineStyle;
    });
    if (style === null) break;
    expect(style).not.toBe("none");
    await page.keyboard.press("Tab");
  }
  expect(steps).toBe(tabbable);

  // Keyboard focus must also stay visible inside a top-bar menu, not only on its toggle: open
  // the app menu from the keyboard and arrow onto a row (the panel focuses row 0 on open;
  // ArrowDown moves past it),
  // then read that row's own outline (AppMenu.css's `.mi:focus-visible` forced-colors rule).
  await page.locator('[data-region="app-menu"]').focus();
  await page.keyboard.press("Enter");
  await page.locator(".app-menu-panel").waitFor();
  await page.keyboard.press("ArrowDown");
  const menuRowStyle = await page.evaluate(() => {
    const el = document.activeElement as HTMLElement | null;
    return el?.matches(".app-menu-panel .mi") ? getComputedStyle(el).outlineStyle : null;
  });
  expect(menuRowStyle).not.toBe(null);
  expect(menuRowStyle).not.toBe("none");

  await page.close();
});

test("forced colors: focusing the selected Chat destination adds a mark the border alone does not give", async () => {
  // The selected tab's outline must follow focus. Drawn always, whether or not the tab held
  // focus, it would leave a keyboard user tabbing onto it seeing nothing change. The border stays
  // put either way; only the outline proves that focus itself is visible.
  server = await startServer({});
  const page = await openApp({ forcedColors: "active", colorScheme: "dark" });

  const tab = page.locator('.rail-nav-button[aria-current="page"]');
  const unfocused = await tab.evaluate((el) => getComputedStyle(el).outlineStyle);
  await tab.focus();
  const focused = await tab.evaluate((el) => getComputedStyle(el).outlineStyle);
  expect(focused).not.toBe(unfocused);
  expect(focused).not.toBe("none");

  await page.close();
});

test("forced colors: focusing the selected Settings tab adds a mark the border alone does not give", async () => {
  server = await startServer({});
  const page = await openApp({ forcedColors: "active", colorScheme: "dark" });
  await openSettings(page);

  const tab = page.locator(".settings-tab-on");
  const unfocused = await tab.evaluate((el) => getComputedStyle(el).outlineStyle);
  await tab.focus();
  const focused = await tab.evaluate((el) => getComputedStyle(el).outlineStyle);
  expect(focused).not.toBe(unfocused);
  expect(focused).not.toBe("none");

  await page.close();
});

test("forced colors: focusing the effort in effect adds a mark the border alone does not give", async () => {
  server = await startServer({ fixture: "tests/fixtures/claude/subagent-explore.jsonl" });
  const page = await openApp({ forcedColors: "active", colorScheme: "dark" });

  // The chip renders only for a session, and stays disabled while a turn runs (ModelChip.tsx):
  // send a prompt and wait for the reply and the turn's end before opening it.
  await send(page, "Which file in _templates/ is the daily note template?");
  await page.locator(".reply .md").first().waitFor({ timeout: 30_000 });
  await page.locator(".composer-submit:not([disabled])").waitFor({ timeout: 30_000 });

  await page.locator('.model-chip[aria-haspopup="menu"]').click();
  await page.locator(".chip-menu").waitFor();

  const current = page.locator(".chip-effort.on");
  const unhighlighted = await current.evaluate((el) => getComputedStyle(el).outlineStyle);

  // Focus the effort in effect directly, without walking every model row first: Radix gives a
  // focused item the same `data-highlighted` state a keyboard ArrowDown walk would land it in.
  await current.focus();
  expect(await current.getAttribute("data-highlighted")).not.toBeNull();

  const highlighted = await current.evaluate((el) => getComputedStyle(el).outlineStyle);
  expect(highlighted).not.toBe(unhighlighted);
  expect(highlighted).not.toBe("none");

  await page.keyboard.press("Escape");
  await page.close();
});

test("a large font scale never overflows the top bar, at 1440 or at 800", async () => {
  server = await startServer({ dirty: true });
  const page = await openApp();
  // A git chip in the bar is what the M-band toggle overlap (below) needs room around; assert it
  // actually renders before trusting the width checks that follow.
  await waitForText(page, ".git-chip", "dirty");
  expect(await page.locator(".git-chip").isVisible()).toBe(true);

  await openAppearance(page);
  // Read the select's own option values rather than guessing a label: src/viki_agent/app/settings.py's spec is the
  // source of truth for what "font scale 1.5" means as a select value.
  const scaleValues = await page
    .locator("#appearance-font_scale")
    .evaluate((el: HTMLSelectElement) => Array.from(el.options).map((option) => option.value));
  const target = scaleValues.find((value) => value === "1.5");
  if (target === undefined) throw new Error(`no 1.5 option among ${scaleValues.join(", ")}`);
  await page.locator("#appearance-font_scale").selectOption(target);
  await page.getByRole("button", { name: "Apply" }).click();
  await waitForFontScale(page, "1.5");
  await closeSettings(page);

  // The scale variable alone proves only that the setting was written; `appearance.css` keys the
  // root's `zoom` off it, and that is what makes the topbar's own layout actually shrink below.
  expect(await page.evaluate(() => getComputedStyle(document.documentElement).zoom)).toBe("1.5");

  const fitsWidth = () =>
    page.locator(".topbar").evaluate((el) => el.scrollWidth <= el.clientWidth);

  expect(await fitsWidth()).toBe(true);

  await page.setViewportSize({ width: 800, height: 900 });
  expect(await fitsWidth()).toBe(true);

  await page.close();
});

test("at the M band, the sidebar Vault navigation stays clear of the right rail toggle", async () => {
  // Keep a dirty git chip in the top bar while testing navigation at each width and scale.
  server = await startServer({ dirty: true });
  const page = await openApp();
  await waitForText(page, ".git-chip", "dirty");
  expect(await page.locator(".git-chip").isVisible()).toBe(true);

  async function assertVaultNavigationFree(width: number): Promise<void> {
    await page.setViewportSize({ width, height: 900 });
    const toggle = await elementBox(page, '[data-region="right-rail-toggle"]');
    const rail = await sidebarNavigation(page);
    const navigation = rail.getByRole("button", { name: "Vault", exact: true });
    expect(await navigation.isVisible()).toBe(true);
    expect(await navigation.isEnabled()).toBe(true);
    const vaultBox = await navigation.boundingBox();
    if (!vaultBox) throw new Error("Vault navigation is not rendered");
    const railBox = await rail.boundingBox();
    if (!railBox) throw new Error("Sidebar navigation is not rendered");
    expect(vaultBox.x).toBeGreaterThanOrEqual(railBox.x);
    expect(vaultBox.x + vaultBox.width).toBeLessThanOrEqual(railBox.x + railBox.width);
    expect(vaultBox.y).toBeGreaterThanOrEqual(railBox.y);
    expect(vaultBox.y + vaultBox.height).toBeLessThanOrEqual(railBox.y + railBox.height);
    expect(boxesIntersect(toggle, vaultBox)).toBe(false);

    // The rendered destination's right edge must receive the pointer inside the rail.
    const hitsNavigation = await navigation.evaluate(
      (element, { x, y }) => element.contains(document.elementFromPoint(x, y)),
      { x: vaultBox.x + vaultBox.width - 1, y: vaultBox.y + vaultBox.height / 2 },
    );
    expect(hitsNavigation).toBe(true);
    const drawer = page.getByRole("dialog", { name: "Sessions and agents", exact: true });
    if (await drawer.isVisible()) {
      await page.keyboard.press("Escape");
      await drawer.waitFor({ state: "detached" });
    }
  }

  for (const width of [800, 816, 832]) {
    await assertVaultNavigationFree(width);
  }

  for (const scale of ["1.3", "1.5"]) {
    await openAppearance(page);
    await page.locator("#appearance-font_scale").selectOption(scale);
    await page.getByRole("button", { name: "Apply" }).click();
    await waitForFontScale(page, scale);
    await closeSettings(page);

    await assertVaultNavigationFree(900);
    await assertVaultNavigationFree(1000);
  }

  await page.close();
});

describe("with no backend", () => {
  const PORT = 5198;
  let vite: ViteDevServer | undefined;

  beforeAll(async () => {
    vite = await createServer({
      server: { host: "127.0.0.1", port: PORT, strictPort: true },
      logLevel: "silent",
    });
    await vite.listen();
  });

  afterAll(async () => {
    await vite?.close();
  });

  test("the page's own language and theme are set before any settings document loads", async () => {
    if (!browser) throw new Error("Edge did not launch");
    const page = await browser.newPage({ viewport: VIEWPORT, locale: "ja-JP" });
    await page.goto(`http://127.0.0.1:${PORT}/`);
    await page.waitForSelector('[data-region="shell"]', { timeout: 10_000 });
    // `index.html` sets `lang` before React ever mounts; with no backend, `appearanceFromSettings`
    // never sees a document, so its `dark` fallback is what stays on screen.
    expect(await page.evaluate(() => document.documentElement.lang)).toBe("en");
    await waitForTheme(page, "dark");
    // The `t()` catalog is English only, so even before any settings document loads (this page
    // never loads one) and with a `ja-JP` browser, the app menu toggle's accessible label
    // (`shell.ts`'s `shell.menu.label`) renders in English.
    expect(await page.locator('[data-region="app-menu"]').getAttribute("aria-label")).toBe(
      "App menu",
    );
    await page.close();
  });
});
