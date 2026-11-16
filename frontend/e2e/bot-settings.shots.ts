/** Bot profile settings shots from the synthetic scratch server and real app bundle.
 *
 * Run from frontend/ with the installed `vitest.shots.config.ts`, a fresh build, and port 8765
 * free. No provider request is made. PNGs land in `test-results/bot-settings/`.
 */
// @ts-expect-error this shots module owns its output directory
import { mkdirSync } from "node:fs";
import type { Browser, Page } from "playwright-core";
import { afterAll, beforeAll, describe, expect, test } from "vitest";
import type { SettingsView } from "../src/clients/app/shellApi";
import { blankProfileDocument } from "../src/clients/profiles/profileApi";
import { appUrl, launchEdge, TEST_TOKEN } from "./harness";
import { ORIGIN, type Server, startServer } from "./server";
import { navigateSidebar } from "./sidebar-navigation";

const OUT = "test-results/bot-settings";
const STATES = [
  "overview-look",
  "gallery-open",
  "gallery-scrolled",
  "colour-row",
  "model-permissions",
  "cannot-apply",
  "try-chat",
] as const;
const SIZES = [
  { width: 400, height: 700 },
  { width: 1440, height: 1000 },
] as const;
const THEMES = ["light", "dark"] as const;
const SCALES = [1.5, 1] as const;
type State = (typeof STATES)[number];
type Theme = (typeof THEMES)[number];

let browser: Browser | undefined;
const written = new Set<string>();

beforeAll(async () => {
  mkdirSync(OUT, { recursive: true });
  assertManifest();
  browser = await launchEdge();
}, 60_000);

afterAll(async () => {
  await browser?.close();
});

function matrix(): Map<string, [number, number]> {
  const result = new Map<string, [number, number]>();
  for (const state of STATES)
    for (const size of SIZES)
      for (const theme of THEMES)
        for (const scale of [1, 1.5]) {
          const name = filename(state, size.width, size.height, theme, scale);
          if (result.has(name)) throw new Error(`Duplicate Bot settings matrix key: ${name}`);
          result.set(name, [size.width, size.height]);
        }
  return result;
}

function filename(
  state: State,
  width: number,
  height: number,
  theme: (typeof THEMES)[number],
  scale: number,
): string {
  return `bot-settings__${state}__${width}x${height}__${theme}__f${scale === 1 ? "1p0" : "1p5"}.png`;
}

function assertManifest(): void {
  const manifest = matrix();
  expect(STATES).toHaveLength(7);
  expect(SIZES).toHaveLength(2);
  expect(THEMES).toHaveLength(2);
  expect(SCALES).toHaveLength(2);
  expect(manifest.size).toBe(56);
  for (const [name, [width, height]] of manifest) {
    expect(name).toMatch(
      new RegExp(
        `^bot-settings__(${STATES.join("|")})__${width}x${height}__(light|dark)__f(1p0|1p5)\\.png$`,
      ),
    );
  }
}

async function postProfile(profileId: string, cannotApply: boolean): Promise<void> {
  const document = blankProfileDocument();
  document.identity.name = "Synthetic Bot";
  document.identity.purpose = "A profile for the Bot settings gallery.";
  document.instructions = "Use concise, clear language.";
  document.runtime.model = "sonnet";
  document.runtime.permission_mode = "plan";
  if (cannotApply) document.tools.required = ["Bash"];
  const response = await fetch(`${ORIGIN}/api/v2/profiles`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${TEST_TOKEN}`,
      origin: ORIGIN,
      "content-type": "application/json",
    },
    body: JSON.stringify({ profile_id: profileId, command_id: crypto.randomUUID(), document }),
  });
  if (!response.ok) throw new Error(`Synthetic profile setup answered ${response.status}.`);
}

async function persistAppearanceSettings(theme: Theme, scale: number): Promise<void> {
  const headers = {
    authorization: `Bearer ${TEST_TOKEN}`,
    origin: ORIGIN,
    "content-type": "application/json",
  };
  const currentResponse = await fetch(`${ORIGIN}/api/v2/settings`, { headers });
  if (!currentResponse.ok)
    throw new Error(`Synthetic settings read answered ${currentResponse.status}.`);
  const current = (await currentResponse.json()) as SettingsView;
  const commandId = crypto.randomUUID();
  const savedResponse = await fetch(`${ORIGIN}/api/v2/settings`, {
    method: "PATCH",
    headers,
    body: JSON.stringify({
      command_id: commandId,
      expected_revision: current.revision,
      changes: {
        "appearance.theme": theme,
        "accessibility.font_scale": scale,
      },
    }),
  });
  if (!savedResponse.ok)
    throw new Error(`Synthetic settings patch answered ${savedResponse.status}.`);
  const saved = (await savedResponse.json()) as SettingsView;
  expect(saved.effective["appearance.theme"]).toBe(theme);
  expect(saved.effective["accessibility.font_scale"]).toBe(scale);
}

async function openSettings(
  width: number,
  height: number,
  theme: (typeof THEMES)[number],
  scale: number,
  cannotApply: boolean,
  diagnosticName: string,
): Promise<Page> {
  if (!browser) throw new Error("Edge did not launch");
  await persistAppearanceSettings(theme, scale);
  const profileId = crypto.randomUUID();
  await postProfile(profileId, cannotApply);
  const page = await browser.newPage({
    viewport: { width, height },
    colorScheme: theme,
  });
  try {
    const settingsRead = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/api/v2/settings" &&
        response.request().method() === "GET",
    );
    await page.goto(appUrl());
    await page.locator('[data-region="shell"]').waitFor({ timeout: 15_000 });
    await settingsRead;
    await page.waitForFunction(
      ([fontScale, wantedTheme]) =>
        document.documentElement.style.getPropertyValue("--font-scale") === fontScale &&
        document.documentElement.dataset.theme === wantedTheme,
      [String(scale), theme],
    );
    await navigateSidebar(page, "Bots");
    await page.getByRole("button", { name: "New Bot", exact: true }).click();
    const create = page.getByRole("region", { name: "New Bot", exact: true });
    await create.getByRole("combobox", { name: "Profile", exact: true }).selectOption(profileId);
    const creation = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/api/v2/bots" &&
        response.request().method() === "POST",
    );
    await create.getByRole("button", { name: "Create Bot", exact: true }).click();
    const created = await creation;
    if (![200, 201].includes(created.status())) {
      const body = (await created.json().catch(() => null)) as {
        error?: { code?: unknown };
      } | null;
      const rawCode = body?.error?.code;
      const code =
        typeof rawCode === "string" && /^[a-z][a-z0-9_.:-]{0,127}$/.test(rawCode)
          ? rawCode
          : "unrecognized";
      throw new Error(`Synthetic Bot creation answered ${created.status()} (${code}).`);
    }
    const selected = page.getByRole("region", { name: "Selected Bot", exact: true });
    await selected.getByRole("heading", { name: "Synthetic Bot", exact: true }).waitFor();
    await selected.getByRole("button", { name: "Edit profile", exact: true }).click();
    await page.getByRole("region", { name: "Look", exact: true }).waitFor();
    const overview = page.getByRole("region", { name: "Profile overview", exact: true });
    await overview.waitFor();
    await overview
      .getByRole("button", { name: "Open Bot", exact: true })
      .and(overview.locator("button:enabled"))
      .waitFor({ state: "visible" });
    await page.evaluate(() => document.fonts.ready);
    return page;
  } catch (error) {
    await captureDiagnostic(page, diagnosticName);
    await page.close().catch(() => undefined);
    throw error;
  }
}

async function captureDiagnostic(page: Page, name: string): Promise<void> {
  try {
    mkdirSync(`${OUT}/diagnostics`, { recursive: true });
    await page.screenshot({ path: `${OUT}/diagnostics/${name}` });
  } catch {
    // Keep the original test failure if diagnostic capture also fails.
  }
}

async function assertAppearance(page: Page, theme: Theme, scale: number): Promise<void> {
  const actual = await page.evaluate(() => ({
    theme: document.documentElement.dataset.theme,
    scale: document.documentElement.style.getPropertyValue("--font-scale"),
    zoom: getComputedStyle(document.documentElement).zoom,
  }));
  expect(actual.theme, JSON.stringify(actual)).toBe(theme);
  expect(actual.scale, JSON.stringify(actual)).toBe(String(scale));
  expect(actual.zoom, JSON.stringify(actual)).toBe(String(scale));
}

async function fitsViewportAndClips(targets: readonly ReturnType<Page["locator"]>[]) {
  const results = await Promise.all(
    targets.map(async (target) => ({
      label: await target.getAttribute("aria-label"),
      geometry: await target.evaluate((element) => {
        const rect = element.getBoundingClientRect();
        const clips = [
          { left: 0, top: 0, right: innerWidth, bottom: innerHeight, name: "viewport" },
        ];
        for (let parent = element.parentElement; parent; parent = parent.parentElement) {
          const style = getComputedStyle(parent);
          const bounds = parent.getBoundingClientRect();
          const scaleX = parent.offsetWidth > 0 ? bounds.width / parent.offsetWidth : 1;
          const scaleY = parent.offsetHeight > 0 ? bounds.height / parent.offsetHeight : 1;
          if (["auto", "clip", "hidden", "scroll"].includes(style.overflowX))
            clips.push({
              left: bounds.left + parent.clientLeft * scaleX,
              top: -Infinity,
              right: bounds.left + (parent.clientLeft + parent.clientWidth) * scaleX,
              bottom: Infinity,
              name: "overflowX",
            });
          if (["auto", "clip", "hidden", "scroll"].includes(style.overflowY))
            clips.push({
              left: -Infinity,
              top: bounds.top + parent.clientTop * scaleY,
              right: Infinity,
              bottom: bounds.top + (parent.clientTop + parent.clientHeight) * scaleY,
              name: "overflowY",
            });
        }
        return {
          rect: {
            left: rect.left,
            top: rect.top,
            right: rect.right,
            bottom: rect.bottom,
            width: rect.width,
            height: rect.height,
          },
          clips,
          fits:
            rect.width > 0 &&
            rect.height > 0 &&
            clips.every(
              (clip) =>
                rect.left >= clip.left - 1 &&
                rect.right <= clip.right + 1 &&
                rect.top >= clip.top - 1 &&
                rect.bottom <= clip.bottom + 1,
            ),
        };
      }),
    })),
  );
  for (const result of results) expect(result.geometry.fits, JSON.stringify(result)).toBe(true);
}

async function expectNoHorizontalOverflow(page: Page): Promise<void> {
  const result = await page.evaluate(() => {
    const elements = [
      document.body,
      ...document.querySelectorAll<HTMLElement>("form,[role=dialog],.bot-icon-dialog-options"),
    ];
    return elements.map((element) => ({
      name:
        element.tagName.toLowerCase() +
        (element.getAttribute("role") ? `[role=${element.getAttribute("role")}]` : ""),
      scrollWidth: element.scrollWidth,
      clientWidth: element.clientWidth,
    }));
  });
  for (const item of result)
    expect(item.scrollWidth, JSON.stringify(item)).toBeLessThanOrEqual(item.clientWidth + 1);
}

async function capture(
  page: Page,
  state: State,
  size: (typeof SIZES)[number],
  theme: (typeof THEMES)[number],
  scale: number,
): Promise<void> {
  const name = filename(state, size.width, size.height, theme, scale);
  if (written.has(name)) throw new Error(`Bot settings screenshot would overwrite ${name}`);
  written.add(name);
  await assertAppearance(page, theme, scale);
  await expectNoHorizontalOverflow(page);
  await assertAppearance(page, theme, scale);
  await page.screenshot({ path: `${OUT}/${name}` });
}

async function expectCaptionContained(
  card: ReturnType<Page["locator"]>,
  expectedText?: string,
): Promise<ReturnType<Page["locator"]>> {
  const caption = card.locator("span");
  const result = await card.evaluate((element) => {
    const span = element.querySelector("span");
    if (!(span instanceof HTMLElement)) return null;
    const cardRect = element.getBoundingClientRect();
    const captionRect = span.getBoundingClientRect();
    const range = document.createRange();
    range.selectNodeContents(span);
    const textRects = [...range.getClientRects()];
    return {
      text: span.textContent?.trim() ?? "",
      boxContained:
        captionRect.left >= cardRect.left &&
        captionRect.right <= cardRect.right &&
        captionRect.top >= cardRect.top &&
        captionRect.bottom <= cardRect.bottom,
      textContained:
        textRects.length > 0 &&
        textRects.every(
          (rect) =>
            rect.left >= cardRect.left &&
            rect.right <= cardRect.right &&
            rect.top >= cardRect.top &&
            rect.bottom <= cardRect.bottom,
        ),
      scrollWidth: span.scrollWidth,
      clientWidth: span.clientWidth,
    };
  });
  expect(result, "Option caption span was missing").not.toBeNull();
  if (expectedText !== undefined) expect(result?.text, JSON.stringify(result)).toBe(expectedText);
  expect(result?.text.length, JSON.stringify(result)).toBeGreaterThan(0);
  expect(result?.boxContained, JSON.stringify(result)).toBe(true);
  expect(result?.textContained, JSON.stringify(result)).toBe(true);
  expect(result?.scrollWidth, JSON.stringify(result)).toBeLessThanOrEqual(result?.clientWidth ?? 0);
  return caption;
}

async function openGallery(page: Page, theme: Theme, scale: number): Promise<void> {
  const opener = page.getByRole("button", { name: /^Choose icon/ });
  await assertAppearance(page, theme, scale);
  await fitsViewportAndClips([opener]);
  await opener.click();
  const dialog = page.getByRole("dialog", { name: "Choose an icon", exact: true });
  await dialog.waitFor();
  const focusedTile = dialog.locator('input[type="radio"]:focus');
  const focusedCard = focusedTile.locator("xpath=ancestor::label[1]");
  expect(await focusedTile.count()).toBe(1);
  const focusedCaption = await expectCaptionContained(focusedCard, "Automatic");
  await assertAppearance(page, theme, scale);
  await expectNoHorizontalOverflow(page);
  await fitsViewportAndClips([
    dialog,
    focusedTile,
    focusedCard,
    focusedCaption,
    dialog.getByRole("button", { name: "Use icon", exact: true }),
    dialog.getByRole("button", { name: "Cancel", exact: true }),
    opener,
  ]);
}

async function prepareState(page: Page, state: State, theme: Theme, scale: number): Promise<void> {
  await assertAppearance(page, theme, scale);
  if (state === "overview-look") {
    await fitsViewportAndClips([
      page.getByRole("region", { name: "Profile overview", exact: true }),
      page.getByRole("region", { name: "Look", exact: true }),
      ...(await page.locator(".bot-look-actions button").all()),
    ]);
    return;
  }
  if (state === "gallery-open" || state === "gallery-scrolled") {
    await openGallery(page, theme, scale);
    if (state === "gallery-open") return;
    const dialog = page.getByRole("dialog", { name: "Choose an icon", exact: true });
    const grid = dialog.locator(".bot-icon-dialog-options");
    const lastTile = dialog.locator('input[type="radio"]').last();
    const lastCard = lastTile.locator("xpath=ancestor::label[1]");
    const lastCaption = lastCard.locator("span");
    const gridOverflowsVertically = await grid.evaluate(
      (element) => element.scrollHeight > element.clientHeight,
    );
    await lastCard.scrollIntoViewIfNeeded();
    await lastCard.click();
    expect(await lastTile.isChecked()).toBe(true);
    if (gridOverflowsVertically)
      expect(await grid.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
    await expectCaptionContained(lastCard);
    await assertAppearance(page, theme, scale);
    await fitsViewportAndClips([
      lastTile,
      lastCard,
      lastCaption,
      dialog.getByRole("button", { name: "Use icon", exact: true }),
      dialog.getByRole("button", { name: "Cancel", exact: true }),
    ]);
    return;
  }
  const target = {
    "colour-row": page.locator(".bot-hue-field"),
    "model-permissions": page.getByRole("group", { name: "Model and permissions", exact: true }),
    "cannot-apply": page.getByRole("region", { name: "Cannot apply in a chat", exact: true }),
    "try-chat": page.getByRole("region", { name: "Try it in a chat", exact: true }),
  }[state];
  await target.waitFor({ state: "visible" });
  const controls =
    state === "colour-row"
      ? await target.locator(".bot-hue-choice").all()
      : state === "model-permissions"
        ? [
            page.getByLabel("Model", { exact: true }),
            page.getByLabel("Effort", { exact: true }),
            page.getByLabel("Permission mode", { exact: true }),
          ]
        : state === "cannot-apply"
          ? [target.getByRole("button", { name: "Clear all", exact: true })]
          : [target.getByRole("button", { name: "Start a chat", exact: true })];
  if (state === "colour-row") expect(controls).toHaveLength(8);
  for (const control of controls) {
    const reached = state === "model-permissions" ? control.locator("..") : control;
    await reached.scrollIntoViewIfNeeded();
    await assertAppearance(page, theme, scale);
    await expectNoHorizontalOverflow(page);
    if (state === "colour-row") {
      const avatar = control.locator(".bot-hue-avatar");
      const caption = control.locator(".bot-hue-choice-name");
      expect(await caption.isVisible()).toBe(true);
      expect((await caption.innerText()).trim().length).toBeGreaterThan(0);
      await fitsViewportAndClips([control, avatar, caption]);
    } else {
      await fitsViewportAndClips([reached, control]);
    }
  }
  if (state === "colour-row") {
    const framing = await target.evaluate((element) => {
      let top = 0;
      let bottom = innerHeight;
      for (let parent = element.parentElement; parent; parent = parent.parentElement) {
        if (!["auto", "clip", "hidden", "scroll"].includes(getComputedStyle(parent).overflowY))
          continue;
        const bounds = parent.getBoundingClientRect();
        const scaleY = parent.offsetHeight > 0 ? bounds.height / parent.offsetHeight : 1;
        top = Math.max(top, bounds.top + parent.clientTop * scaleY);
        bottom = Math.min(bottom, bounds.top + (parent.clientTop + parent.clientHeight) * scaleY);
      }
      return { height: element.getBoundingClientRect().height, availableHeight: bottom - top };
    });
    // A taller palette keeps the last complete choice in view after the individual checks.
    if (framing.height <= framing.availableHeight + 1) {
      await target.scrollIntoViewIfNeeded();
      await assertAppearance(page, theme, scale);
      await expectNoHorizontalOverflow(page);
      await fitsViewportAndClips([
        target,
        ...controls,
        ...(await target.locator(".bot-hue-avatar,.bot-hue-choice-name").all()),
      ]);
    }
  } else if (state === "model-permissions") {
    // Each Field is reachable separately; capture the start of this scrollable section.
    const heading = target.locator("legend");
    await heading.evaluate((element) =>
      element.scrollIntoView({ block: "start", inline: "nearest" }),
    );
    await assertAppearance(page, theme, scale);
    await expectNoHorizontalOverflow(page);
    await fitsViewportAndClips([heading, page.getByLabel("Model", { exact: true }).locator("..")]);
  } else {
    await fitsViewportAndClips(controls);
  }
}

const combinations = SIZES.flatMap((size) =>
  THEMES.flatMap((theme) => SCALES.map((scale) => ({ size, theme, scale }))),
).sort(
  (a, b) =>
    Number(b.size.width === 400 && b.scale === 1.5) -
    Number(a.size.width === 400 && a.scale === 1.5),
);

describe("Bot settings", { concurrent: false }, () => {
  for (const state of STATES)
    describe(state, () => {
      let server: Server | undefined;

      beforeAll(async () => {
        server = await startServer({
          bots: true,
          fixture: "tests/fixtures/claude/text-two-turns.jsonl",
        });
      });

      afterAll(async () => {
        await server?.stop();
      });

      for (const { size, theme, scale } of combinations) {
        const name = filename(state, size.width, size.height, theme, scale);
        test(name, async () => {
          const page = await openSettings(
            size.width,
            size.height,
            theme,
            scale,
            state === "cannot-apply",
            name,
          );
          try {
            await prepareState(page, state, theme, scale);
            await capture(page, state, size, theme, scale);
            if (state === "gallery-open") {
              await page.keyboard.press("Escape");
              const opener = page.getByRole("button", { name: /^Choose icon/ });
              expect(await opener.evaluate((element) => document.activeElement === element)).toBe(
                true,
              );
              expect(await opener.isVisible()).toBe(true);
              await assertAppearance(page, theme, scale);
              await fitsViewportAndClips([opener]);
            }
          } catch (error) {
            await captureDiagnostic(page, name);
            await page.close().catch(() => undefined);
            throw error;
          }
          await page.close();
        });
      }
    });
});
