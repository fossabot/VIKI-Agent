/**
 * The five views (chat, settings, data, onboarding and recovery), rendered for a person to look
 * at. Real backend, real bundle, real Edge; never part of scripts/check.py.
 *
 *   npx vitest run --config vitest.shots.config.ts e2e/shell.shots.ts
 *
 * Four sizes each: the design's 1440×900, 900×900 (the M band, right rail a drawer), 400×900 (the
 * narrow band, both rails drawers and the tabs hidden) and 720×450 at deviceScaleFactor 2, which
 * is the CSS geometry a 1440×900 window reports at 200% zoom.
 *
 * PNGs land in `frontend/test-results/p1/` as `p1-<view>-<width>[@2x].png`.
 *
 * Two servers, because two of the five views only exist on a launch that cannot reach them
 * otherwise: `--no-vault` is what makes onboarding the first thing the app shows, and
 * `--damaged-store` is what makes the recovery shell the only thing it can show.
 */
// The harness owns `e2e/node.d.ts`, which declares only what `server.ts` needs; this module is
// this file's own business (src/testing/css.ts does the same for the same reason).
// @ts-expect-error see above
import { mkdirSync } from "node:fs";
// @ts-expect-error see above
import { homedir, tmpdir } from "node:os";
// @ts-expect-error see above
import { join } from "node:path";
import type { Browser, Locator, Page } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, TEST_TOKEN } from "./harness";
import { ORIGIN, PORT, type Server, startServer } from "./server";

const OUT = "test-results/p1";

interface Size {
  width: number;
  height: number;
  deviceScaleFactor?: number;
  suffix: string;
}

const SIZES: readonly Size[] = [
  { width: 1440, height: 900, suffix: "1440" },
  { width: 900, height: 900, suffix: "900" },
  { width: 400, height: 900, suffix: "400" },
  { width: 720, height: 450, deviceScaleFactor: 2, suffix: "720@2x" },
];

const MATRIX_OUT = "test-results/settings-matrix";
const MATRIX_DETAILS_OUT = "test-results/settings-matrix-details";
const MATRIX_SIZES = [
  { width: 1440, height: 1000, suffix: "1440x1000" },
  { width: 400, height: 700, suffix: "400x700" },
] as const;
const MATRIX_VIEWS = ["general", "workspaces", "persona", "providers"] as const;
const MATRIX_THEMES = ["light", "dark"] as const;
const MATRIX_TRUST_SENTENCES = [
  "Chats can start here, and a tick adds this folder to new chats.",
  "Trust covers VIKI Agent's own actions here: Code view saves, commits, Integrate, Push, worktree creation, archiving and removal, and the looks and saved checks your Decision settings allow, Bot code copies included.",
  "Chats that start here load its CLAUDE.md and skills, run its hooks and connect its MCP servers without asking.",
  "Trust is not a sandbox: Claude Code's permissions decide what a chat reads, edits or runs.",
] as const;
const SCRATCH =
  process.platform === "win32" ? join(tmpdir(), "viki-e2e") : join(homedir(), ".cache", "viki-e2e");
const CODE = join(SCRATCH, "code");

let browser: Browser | undefined;

beforeAll(async () => {
  mkdirSync(OUT, { recursive: true });
  browser = await launchEdge();
}, 60_000);

afterAll(async () => {
  await browser?.close();
});

/**
 * `awaitPath`, when given, is registered as a `waitForResponse` before `page.goto` fires (the
 * only way to not miss a response that lands faster than any wait issued after `goto` returns).
 */
async function open(size: Size, awaitPath?: string): Promise<Page> {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({
    viewport: { width: size.width, height: size.height },
    ...(size.deviceScaleFactor === undefined ? {} : { deviceScaleFactor: size.deviceScaleFactor }),
  });
  const response = awaitPath
    ? page.waitForResponse((res) => res.url().includes(awaitPath))
    : undefined;
  await page.goto(appUrl(PORT));
  await response;
  await page.locator('[data-region="shell"]').waitFor({ timeout: 15_000 });
  await page.evaluate(() => document.fonts.ready);
  return page;
}

/** One view at all four sizes. `prepare` drives the page from the shell to what is wanted.
 * `awaitPath` is passed through to `open` (see its comment). */
async function capture(
  view: string,
  prepare: (page: Page) => Promise<void>,
  awaitPath?: string,
): Promise<void> {
  for (const size of SIZES) {
    const page = await open(size, awaitPath);
    await prepare(page);
    await page.screenshot({ path: `${OUT}/p1-${view}-${size.suffix}.png` });
    await page.close();
  }
}

/** Below 800 CSS px the tabs are hidden and the app menu is the only way into the views. */
async function openMenuRow(page: Page, row: RegExp): Promise<void> {
  await page.locator('[data-region="app-menu"]').click();
  await page.getByRole("menuitem", { name: row }).click();
}

test("chat, settings and data on an ordinary launch", async () => {
  const server: Server = await startServer({});
  try {
    // The chat, with the chat-only banner (the scratch vault is registered but the launch still
    // runs where it started). The status bar has no readiness element to poll, so wait
    // for the capabilities response itself (registered in `open`, before `page.goto`), then one
    // animation frame so the render it triggers has painted before the screenshot.
    await capture(
      "chat",
      async (page) => {
        await page.evaluate(
          () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())),
        );
      },
      "/api/v2/capabilities",
    );

    await capture("settings", async (page) => {
      // Only the Settings row starts with "Settings"; its own Ctrl+, shortcut suffix may follow.
      await openMenuRow(page, /^Settings/);
      await page.locator('[data-region="settings"]').waitFor({ timeout: 15_000 });
    });

    await capture("settings-workspaces", async (page) => {
      await openMenuRow(page, /Switch workspace/);
      await page.locator('[data-region="settings"]').waitFor({ timeout: 15_000 });
      await page
        .getByRole("button", { name: /Policy/ })
        .first()
        .click();
      await page.waitForTimeout(500);
    });

    await capture("data", async (page) => {
      await openMenuRow(page, /Export diagnostics/);
      await page.locator(".data-view").waitFor({ timeout: 15_000 });
      // Both the export and the import panel have a Preview button — all four data panels are
      // mounted together (DataView's own comment says so) — so an unscoped query throws a
      // strict-mode violation instead of clicking. Scope to the export region the way
      // e2e/scenarios/shell/settings.test.ts already does for the identical click.
      await page
        .getByRole("region", { name: /Export/ })
        .getByRole("button", { name: /Preview/ })
        .click();
      await page.locator("#export-destination").waitFor({ timeout: 15_000 });
    });
  } finally {
    await server.stop();
  }
}, 300_000);

test("onboarding on a launch with no vault", async () => {
  const server: Server = await startServer({ noVault: true });
  try {
    await capture("onboarding", async (page) => {
      await page.locator(".onboarding-card").first().waitFor({ timeout: 15_000 });
    });
    await capture("onboarding-create", async (page) => {
      await page.locator("#onboarding-card-create").waitFor({ timeout: 15_000 });
      await page.locator("#onboarding-card-create").click();
      await page.locator("#create-name").waitFor({ timeout: 15_000 });
    });
  } finally {
    await server.stop();
  }
}, 300_000);

test("recovery on a launch whose store cannot be opened", async () => {
  const server: Server = await startServer({ damagedStore: true });
  try {
    await capture("recovery", async (page) => {
      await page.locator(".recovery-view").waitFor({ timeout: 15_000 });
      await page.waitForTimeout(500);
    });
  } finally {
    await server.stop();
  }
}, 300_000);

/** Settings is opened through its menu row, including at the narrow viewport. */
async function openMatrixSettings(page: Page): Promise<void> {
  await page.locator('[data-region="app-menu"]').click();
  await page.getByRole("menuitem", { name: /^Settings/ }).click();
  await page.locator('[data-region="settings"]').waitFor();
  await page.locator(".settings-page-title").waitFor();
}

async function selectMatrixPage(page: Page, view: (typeof MATRIX_VIEWS)[number]): Promise<void> {
  const settings = page.locator('[data-region="settings"]');
  const tab = settings.locator(`[data-page="${view}"]`);
  await tab.click();
  const selectedTab = settings.locator(`[data-page="${view}"][aria-selected="true"]`);
  await selectedTab.waitFor();
  expect(await selectedTab.getAttribute("aria-selected")).toBe("true");
  const headings = {
    general: "General",
    workspaces: "Workspaces and Vault",
    persona: "Instructions and Persona",
    providers: "Providers",
  } as const;
  await settings.getByRole("heading", { name: headings[view], level: 2 }).waitFor();
  if (view === "workspaces") {
    const row = settings.locator(".workspace-row", { hasText: CODE });
    await row.getByRole("button", { name: /More actions for/ }).click();
    await page.getByRole("menuitem", { name: "Details and trust" }).click();
    await settings.getByRole("heading", { name: "Files the Code editor won't save" }).waitFor();
    for (const sentence of [
      "Chats can start here, and a tick adds this folder to new chats.",
      "Trust covers VIKI Agent's own actions here: Code view saves",
      "Chats that start here load its CLAUDE.md and skills",
      "Trust is not a sandbox: Claude Code's permissions decide",
    ])
      await page.getByText(sentence, { exact: false }).waitFor();
  }
}

async function frameMatrixTargets(anchor: Locator, targets: readonly Locator[]): Promise<void> {
  await anchor.scrollIntoViewIfNeeded();
  for (const target of targets) {
    const fits = await target.evaluate((element) => {
      const target = element.getBoundingClientRect();
      const contained = (left: number, top: number, right: number, bottom: number) =>
        target.left >= left &&
        target.top >= top &&
        target.right <= right &&
        target.bottom <= bottom;
      if (!contained(0, 0, window.innerWidth, window.innerHeight)) return false;
      for (let parent = element.parentElement; parent !== null; parent = parent.parentElement) {
        const style = getComputedStyle(parent);
        const clipX = ["auto", "clip", "hidden", "scroll"].includes(style.overflowX);
        const clipY = ["auto", "clip", "hidden", "scroll"].includes(style.overflowY);
        if (clipX || clipY) {
          const rect = parent.getBoundingClientRect();
          const left = rect.left + parent.clientLeft;
          const top = rect.top + parent.clientTop;
          const right = left + parent.clientWidth;
          const bottom = top + parent.clientHeight;
          if (
            (clipX && (target.left < left || target.right > right)) ||
            (clipY && (target.top < top || target.bottom > bottom))
          )
            return false;
        }
      }
      return target.width > 0 && target.height > 0;
    });
    expect(fits).toBe(true);
  }
}

interface LauncherSpot {
  x: number;
  y: number;
}

function matrixLauncherSpots(
  size: (typeof MATRIX_SIZES)[number],
  view: string,
  detail = "",
): LauncherSpot[] {
  const cornerRight = size.width - 108;
  const cornerBottom = size.height - 108;
  if (view === "workspaces")
    return size.width === 1440
      ? [
          { x: 220, y: 90 },
          { x: 8, y: 8 },
          { x: cornerRight, y: 8 },
        ]
      : [
          { x: cornerRight, y: 8 },
          { x: 8, y: 8 },
          { x: 8, y: cornerBottom },
        ];
  if (view === "providers")
    return [
      ...(detail === "git" ? [{ x: 150, y: Math.round(cornerBottom / 2) }] : []),
      { x: cornerRight, y: 8 },
      { x: 8, y: 8 },
      { x: 8, y: cornerBottom },
      { x: cornerRight, y: cornerBottom },
    ];
  return [
    { x: cornerRight, y: 8 },
    { x: 8, y: 8 },
    { x: 8, y: cornerBottom },
  ];
}

async function launcherClears(targets: readonly Locator[]): Promise<boolean> {
  for (const target of targets) {
    const clear = await target.evaluate((element) => {
      const launcher = document.querySelector<HTMLElement>(".dloop-launcher");
      if (launcher === null) throw new Error("Decision launcher is missing");
      const content = element.getBoundingClientRect();
      const button = launcher.getBoundingClientRect();
      return (
        content.right <= button.left ||
        content.left >= button.right ||
        content.bottom <= button.top ||
        content.top >= button.bottom
      );
    });
    if (!clear) return false;
  }
  return true;
}

async function dragLauncherTo(page: Page, spot: LauncherSpot): Promise<void> {
  const launcher = page.locator(".dloop-launcher");
  const box = await launcher.boundingBox();
  if (box === null) throw new Error("Decision launcher has no viewport bounds");
  const startX = box.x + box.width / 2;
  const startY = box.y + box.height / 2;
  await page.mouse.move(startX, startY);
  await page.mouse.down();
  await page.mouse.move(startX + spot.x - box.x, startY + spot.y - box.y, { steps: 6 });
  await page.mouse.up();
  await page.waitForFunction(
    () => document.querySelector<HTMLElement>(".dloop-launcher")?.dataset.dragging === "false",
  );
}

async function keepLauncherClear(
  page: Page,
  targets: readonly Locator[],
  spots: readonly LauncherSpot[],
): Promise<void> {
  await page.locator(".dloop-launcher").waitFor();
  if (await launcherClears(targets)) return;
  for (const spot of spots) {
    await dragLauncherTo(page, spot);
    if (await launcherClears(targets)) return;
  }
  throw new Error("Decision launcher overlaps required screenshot content");
}

async function captureMatrixDetail(
  page: Page,
  theme: (typeof MATRIX_THEMES)[number],
  name: string,
  anchor: Locator,
  targets: readonly Locator[],
  launcherTargets: readonly Locator[] = targets,
  launcherSpots: readonly LauncherSpot[] = [],
): Promise<void> {
  await frameMatrixTargets(anchor, targets);
  if (launcherSpots.length > 0)
    await keepLauncherClear(page, [...targets, ...launcherTargets], launcherSpots);
  expect(await page.evaluate(() => document.documentElement.dataset.theme)).toBe(theme);
  await page.screenshot({ path: `${MATRIX_DETAILS_OUT}/settings-${name}-400x700-${theme}.png` });
}

async function prepareMatrixCodeFolder(page: Page): Promise<void> {
  const picker = await page.request.get(`${ORIGIN}/api/v2/folders/pick`, {
    headers: { authorization: `Bearer ${TEST_TOKEN}` },
  });
  const { available } = (await picker.json()) as { available: boolean };
  await openMatrixSettings(page);
  await page.getByRole("tab", { name: "Workspaces and Vault" }).click();
  const addFolders = page.locator(".ws-add");
  if (available) await addFolders.getByRole("button", { name: "Type a path instead" }).click();
  await addFolders.getByLabel("Code folder path").fill(CODE);
  await addFolders.getByRole("button", { name: "Add", exact: true }).click();
  await page.locator(".workspace-row .chip", { hasText: /^Not trusted$/ }).waitFor();
  await page.getByRole("button", { name: "Trust…" }).first().click();
  await page.getByRole("button", { name: "Trust this folder" }).click();
  const acknowledgements = page.locator(".policy-acknowledgements input[type=checkbox]");
  for (const box of await acknowledgements.all()) await box.check();
  await page.locator(".policy-acknowledgements").getByRole("button", { name: "Apply" }).click();
  const row = page.locator(".workspace-row", { hasText: CODE });
  await row.locator(".chip", { hasText: /^Trusted$/ }).waitFor();
  await page.getByRole("heading", { name: "Files the Code editor won't save" }).waitFor();
  for (const sentence of [
    "Chats can start here, and a tick adds this folder to new chats.",
    "Trust covers VIKI Agent's own actions here: Code view saves",
    "Chats that start here load its CLAUDE.md and skills",
    "Trust is not a sandbox: Claude Code's permissions decide",
  ])
    await page.getByText(sentence, { exact: false }).waitFor();
}

test("required settings matrix", async () => {
  mkdirSync(MATRIX_OUT, { recursive: true });
  mkdirSync(MATRIX_DETAILS_OUT, { recursive: true });
  const server: Server = await startServer({
    bots: true,
    team: true,
    fixture: "tests/fixtures/claude/text-two-turns.jsonl",
  });
  try {
    mkdirSync(CODE, { recursive: true });
    if (!browser) throw new Error("Edge did not launch");
    const setup = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    await setup.goto(appUrl(PORT));
    await setup.locator('[data-region="shell"]').waitFor();
    await prepareMatrixCodeFolder(setup);
    await setup.close();

    for (const view of MATRIX_VIEWS) {
      for (const size of MATRIX_SIZES) {
        for (const theme of MATRIX_THEMES) {
          const page = await browser.newPage({
            viewport: { width: size.width, height: size.height },
          });
          await page.goto(appUrl(PORT));
          await page.locator('[data-region="shell"]').waitFor();
          await openMatrixSettings(page);
          await page.getByRole("tab", { name: "Appearance" }).click();
          await page.locator("#appearance-theme").selectOption(theme);
          await page.waitForFunction(
            (wanted) => document.documentElement.dataset.theme === wanted,
            theme,
          );
          const savedTheme = page.waitForResponse(
            (response) =>
              new URL(response.url()).pathname === "/api/v2/settings" &&
              response.request().method() === "PATCH",
          );
          await page.getByRole("button", { name: "Apply", exact: true }).click();
          expect((await savedTheme).status()).toBe(200);
          await selectMatrixPage(page, view);
          await page.waitForFunction(
            (wanted) => document.documentElement.dataset.theme === wanted,
            theme,
          );
          expect(await page.evaluate(() => document.documentElement.dataset.theme)).toBe(theme);

          let captureTargets: Locator[];
          let captureAnchor: Locator;
          let launcherTargets: Locator[] = [];
          if (view === "general") {
            await page.getByRole("heading", { name: "Names and avatars" }).waitFor();
            expect(await page.getByText("(not set)", { exact: true }).count()).toBe(2);
            const previews = page.locator(".avatar-field-preview");
            expect(await previews.count()).toBe(2);
            captureTargets = [
              ...(await previews.all()),
              ...(await page.locator(".avatar-field-empty").all()),
            ];
            captureAnchor = previews.nth(1);
          } else if (view === "workspaces") {
            await page.getByRole("heading", { name: "Files the Code editor won't save" }).waitFor();
            const trustTargets = MATRIX_TRUST_SENTENCES.map((sentence) =>
              page.getByText(sentence, { exact: true }),
            );
            captureTargets = trustTargets;
            captureAnchor = page.getByText(MATRIX_TRUST_SENTENCES[3], { exact: true });
          } else if (view === "persona") {
            await page.getByRole("heading", { name: "Instruction files" }).waitFor();
            await page.getByText("AGENTS.md", { exact: true }).waitFor();
            await page.getByText("CLAUDE.md", { exact: true }).waitFor();
            expect(await page.locator(".instruction-files-row").count()).toBe(2);
            const assistantName = page.getByLabel("Assistant name in this vault");
            await assistantName.waitFor();
            captureAnchor = assistantName;
            captureTargets = [assistantName];
            if (size.width === 1440) {
              captureTargets.push(
                page.getByText("AGENTS.md", { exact: true }),
                page.locator(".instruction-files-row").nth(0).locator(".settings-pill"),
                page.getByText("CLAUDE.md", { exact: true }),
                page.locator(".instruction-files-row").nth(1).locator(".settings-pill"),
              );
            }
          } else {
            await page.getByRole("heading", { name: "Claude Code" }).waitFor();
            await page.getByRole("heading", { name: "Git" }).waitFor();
            await page.locator(".settings-facts").nth(1).waitFor();
            const modelsHeading = page.getByRole("heading", { name: "Chat models" });
            await modelsHeading.waitFor();
            await page.getByText("Loading model connections…").waitFor({ state: "detached" });
            const modelsCard = page.locator(".settings-page > .settings-card").filter({
              has: modelsHeading,
            });
            const modelHelp = modelsCard.locator(".settings-row-desc").first();
            const currentDefault = modelsCard.locator(".llm-current");
            const nativeChoice = modelsCard.locator(".llm-choice").first();
            const nativeHeading = nativeChoice.getByRole("heading", {
              name: "Claude Code sign-in",
            });
            const defaultLabel = nativeChoice.getByText("Default for new chats", { exact: true });
            captureAnchor = modelsHeading;
            captureTargets = [modelsHeading, modelHelp, currentDefault];
            launcherTargets = [...captureTargets, nativeHeading, defaultLabel];
            if (size.width === 400) captureTargets.push(nativeHeading, defaultLabel);
            if (size.width === 1440) {
              const providerCards = page.locator(".settings-page > .settings-card");
              captureTargets.push(
                providerCards.nth(0).getByRole("heading", { name: "Claude Code" }),
                providerCards.nth(0).getByText("Ready", { exact: true }),
                providerCards.nth(1).getByRole("heading", { name: "Git" }),
                providerCards.nth(1).getByText("Not found", { exact: true }),
              );
              launcherTargets.push(...captureTargets.slice(3));
            }
          }

          await page.evaluate(() => document.fonts.ready);
          await page.evaluate(
            () =>
              new Promise<void>((resolve) =>
                requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
              ),
          );
          const selected = page.locator(`[data-page="${view}"][aria-selected="true"]`);
          expect(await selected.count()).toBe(1);
          const heading = page.locator(".settings-page-title");
          expect(await heading.isVisible()).toBe(true);
          await frameMatrixTargets(captureAnchor, captureTargets);
          if (view === "workspaces" || view === "providers")
            await keepLauncherClear(
              page,
              view === "providers" ? launcherTargets : captureTargets,
              matrixLauncherSpots(size, view),
            );
          expect(await page.evaluate(() => document.documentElement.dataset.theme)).toBe(theme);
          await page.screenshot({
            path: `${MATRIX_OUT}/settings-${view}-${size.suffix}-${theme}.png`,
          });

          if (size.width === 400 && view === "workspaces") {
            const refusalHeading = page.getByRole("heading", {
              name: "Files the Code editor won't save",
            });
            await captureMatrixDetail(
              page,
              theme,
              "workspaces-refusal",
              refusalHeading,
              [refusalHeading],
              captureTargets,
              matrixLauncherSpots(size, "workspaces", "refusal"),
            );
          } else if (size.width === 400 && view === "persona") {
            const agentsRow = page.locator(".instruction-files-row").nth(0);
            const claudeRow = page.locator(".instruction-files-row").nth(1);
            const agentsName = page.getByText("AGENTS.md", { exact: true });
            const agentsState = agentsRow.locator(".settings-pill");
            const claudeName = page.getByText("CLAUDE.md", { exact: true });
            const claudeState = claudeRow.locator(".settings-pill");
            await captureMatrixDetail(page, theme, "persona-agents", agentsState, [
              agentsName,
              agentsState,
            ]);
            await captureMatrixDetail(page, theme, "persona-claude", claudeState, [
              claudeName,
              claudeState,
            ]);
          } else if (size.width === 400 && view === "providers") {
            const providerCards = page.locator(".settings-page > .settings-card");
            const claudeCard = providerCards.nth(0);
            const gitCard = providerCards.nth(1);
            const claudeHeading = claudeCard.getByRole("heading", { name: "Claude Code" });
            const claudeReady = claudeCard.getByText("Ready", { exact: true });
            const gitHeading = gitCard.getByRole("heading", { name: "Git" });
            const gitMissing = gitCard.getByText("Not found", { exact: true });
            await captureMatrixDetail(
              page,
              theme,
              "providers-claude",
              claudeHeading,
              [claudeHeading, claudeReady],
              launcherTargets,
              matrixLauncherSpots(size, "providers", "claude"),
            );
            await captureMatrixDetail(
              page,
              theme,
              "providers-git",
              gitHeading,
              [gitHeading, gitMissing],
              launcherTargets,
              matrixLauncherSpots(size, "providers", "git"),
            );
          }
          await page.close();
        }
      }
    }
  } finally {
    await server.stop();
  }
}, 300_000);
