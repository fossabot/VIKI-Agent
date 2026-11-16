/**
 * Bot memory: the selected Bot's card, the memory viewer, Bot activity in a task thread, and the
 * read-only Vault note. The Bot and Vault are the e2e server's synthetic scratch Start with VIKI
 * vault. Its memory files are synthetic route fixtures added to the vault's real entries, so
 * nothing here reads a real vault or ~/.claude and the Bot's folder is never written.
 *
 *   npx vitest run --config vitest.shots.config.ts e2e/bot-memory.shots.ts
 *
 * Run from frontend/ after a fresh build with port 8765 free. Never part of scripts/check.py.
 * PNGs land in `test-results/bot-memory/` at 1440×1000 and 400×700 in light and dark themes.
 */
// @ts-expect-error the shots harness declares only the Node types needed by server.ts
import { mkdirSync } from "node:fs";
import type { Browser, Locator, Page } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, TEST_TOKEN } from "./harness";
import { ORIGIN, type Server, startServer } from "./server";
import { navigateSidebar } from "./sidebar-navigation";

const OUT = "test-results/bot-memory";
const SIZES = [
  { width: 1440, height: 1000 },
  { width: 400, height: 700 },
] as const;
const THEMES = ["light", "dark"] as const;
const STATES = ["card", "modal", "thread", "vault"] as const;
type State = (typeof STATES)[number];

const NOTES = [
  {
    file: "MEMORY.md",
    title: "Memory index",
    text: "# Memory index\n\nSynthetic index. Project decisions are kept in topics/decisions.md.\n",
  },
  {
    file: "topics/decisions.md",
    title: "Project decisions",
    text: "# Project decisions\n\nUse small reversible steps for project changes.\n",
  },
] as const;
const [INDEX, TOPIC] = NOTES;
const HASH = "a".repeat(64);

let browser: Browser | undefined;
let server: Server | undefined;
let botId = "";

beforeAll(async () => {
  mkdirSync(OUT, { recursive: true });
  server = await startServer({
    bots: true,
    team: true,
    fixture: "tests/fixtures/claude/text-two-turns.jsonl",
  });
  browser = await launchEdge();
}, 120_000);

afterAll(async () => {
  await browser?.close();
  await server?.stop();
});

/**
 * The Chief Bot's real id, from the synthetic server; the shots never show it as the only label.
 * The Bot row exists only once the Bots page has opened the Chief seat, so call this after that.
 */
async function chiefBotId(): Promise<string> {
  let id = "";
  await expect
    .poll(
      async () => {
        const response = await fetch(`${ORIGIN}/api/v2/bots`, {
          headers: { authorization: `Bearer ${TEST_TOKEN}`, origin: ORIGIN },
        });
        if (!response.ok) return "";
        const body = (await response.json()) as {
          bots?: { bot_id: string; profile?: { identity?: { name?: string } } }[];
        };
        id = body.bots?.find((bot) => bot.profile?.identity?.name === "Chief")?.bot_id ?? "";
        return id;
      },
      { timeout: 15_000 },
    )
    .not.toBe("");
  return id;
}

const ref = (note: (typeof NOTES)[number]) => `bot-memory/${botId}/${note.file}`;
const source = () => `Source bot-memory/${botId}/`;

function displayMetadata(title: string) {
  return {
    state: "ready",
    frontmatter_state: "absent",
    heading_state: "ready",
    display_title: title,
    title_source: "h1",
    tags: [],
    type: null,
    status: null,
  };
}

/** Serve the Bot's two synthetic files; every other request reaches the real scratch vault. */
async function mockMemory(page: Page): Promise<void> {
  const byRef = new Map(NOTES.map((note) => [ref(note), note]));
  await page.route("**/api/v2/vaults/*/entries*", async (route) => {
    if (new URL(route.request().url()).searchParams.has("q")) return route.fallback();
    try {
      const response = await route.fetch();
      const body = (await response.json()) as { entries: unknown[] };
      body.entries.push(
        ...NOTES.map((note, index) => ({
          ref: ref(note),
          title: note.title,
          bytes: new TextEncoder().encode(note.text).length,
          modified: 1_800_000_000 - index,
        })),
      );
      return await route.fulfill({ response, json: body });
    } catch (error) {
      // The shot may close its page while this request is in flight; only that is ignored.
      if (page.isClosed()) return;
      throw error;
    }
  });
  await page.route("**/api/v2/vaults/*/metadata", async (route) => {
    const { refs } = route.request().postDataJSON() as { refs: string[] };
    if (!refs.every((item) => byRef.has(item))) return route.fallback();
    return route.fulfill({
      json: {
        items: refs.map((item) => ({
          ref: item,
          state: "ready",
          reason: null,
          read_bytes: 32,
          display_metadata: displayMetadata(byRef.get(item)?.title ?? ""),
        })),
        partial: false,
        partial_reasons: [],
        omitted: 0,
        read_bytes: refs.length * 32,
      },
    });
  });
  await page.route("**/api/v2/vaults/*/notes/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const note = byRef.get(decodeURIComponent(path.split("/notes/")[1] ?? ""));
    if (!note || route.request().method() !== "GET") return route.fallback();
    return route.fulfill({
      json: {
        note: {
          ref: ref(note),
          title: note.title,
          text: note.text,
          source: note.text,
          byte_size: new TextEncoder().encode(note.text).length,
          sha256: HASH,
          frontmatter: {},
          frontmatter_error: null,
          links: [],
          display_metadata: displayMetadata(note.title),
        },
        resolved_links: [],
        partial: false,
        partial_reasons: [],
        omitted: 0,
      },
    });
  });
  await page.route("**/api/v2/vaults/*/backlinks/bot-memory/**", (route) =>
    route.fulfill({ json: { backlinks: [], partial: false, partial_reasons: [], omitted: 0 } }),
  );
  await page.route("**/api/v2/vaults/*/history/bot-memory/**", (route) =>
    route.fulfill({ json: { state: "ready", commits: [] } }),
  );
}

/** Open the app on Bots and select Chief, which is a seat until its conversation is opened. */
async function goToChief(page: Page): Promise<Locator> {
  await page.goto(appUrl());
  await page.locator('[data-region="shell"]').waitFor({ timeout: 15_000 });
  await navigateSidebar(page, "Bots");
  await page.locator(".bots-view").waitFor();
  await page.locator(".bots-view > .loading").waitFor({ state: "hidden", timeout: 15_000 });
  await page
    .getByRole("group", { name: "Bot roster", exact: true })
    .getByRole("button", { name: "Chief", exact: true })
    .click();
  const selected = page.getByRole("region", { name: "Selected Bot", exact: true });
  await selected.getByRole("heading", { name: "Chief", level: 2 }).waitFor();
  return selected;
}

/** Select Chief and wait until the selected item is a Bot: only a Bot has "New task thread". */
async function selectChief(page: Page): Promise<Locator> {
  const selected = await goToChief(page);
  const newThread = selected.getByRole("button", { name: "New task thread", exact: true });
  await expect.poll(() => newThread.isEnabled(), { timeout: 15_000 }).toBe(true);
  return selected;
}

/**
 * Open the Chief seat's conversation once on a throwaway page, which creates the Bot (nothing is
 * sent), then cache its id.
 */
async function ensureChiefId(size: (typeof SIZES)[number]): Promise<void> {
  if (botId) return;
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: { width: size.width, height: size.height } });
  try {
    const selected = await goToChief(page);
    const conversation = selected.getByRole("button", { name: "Open conversation", exact: true });
    await expect.poll(() => conversation.isEnabled(), { timeout: 15_000 }).toBe(true);
    await conversation.click();
    await page.getByRole("region", { name: "Bot activity", exact: true }).waitFor();
    botId = await chiefBotId();
  } finally {
    await page.unrouteAll({ behavior: "ignoreErrors" });
    await page.close();
  }
}

async function open(page: Page): Promise<Locator> {
  await mockMemory(page);
  return selectChief(page);
}

/** The card lists both files with their exact refs and readable titles. */
async function expectCard(selected: Locator): Promise<Locator> {
  const card = selected.locator(".bot-memory-card");
  await card.getByRole("heading", { name: "Bot memory", exact: true }).waitFor();
  await card.getByText(source(), { exact: true }).waitFor();
  const list = card.getByRole("list", { name: "All files", exact: true });
  for (const note of NOTES) {
    const row = list.getByRole("listitem").filter({ hasText: ref(note) });
    await row.getByText(note.title, { exact: true }).waitFor();
    await row.getByText(ref(note), { exact: true }).waitFor();
  }
  await card.getByRole("button", { name: "Ask Chief to change…", exact: true }).waitFor();
  return card;
}

/** The viewer shows the folder root MEMORY.md first, then the topic file, with exact refs. */
async function expectViewerFiles(dialog: Locator): Promise<void> {
  await dialog.getByText(source(), { exact: true }).waitFor();
  const files = dialog.getByRole("navigation", { name: "All files", exact: true });
  const buttons = files.getByRole("button");
  await files.getByText(ref(INDEX), { exact: true }).waitFor();
  await files.getByText(ref(TOPIC), { exact: true }).waitFor();
  await expect.poll(() => buttons.count()).toBe(2);
  expect(await buttons.first().textContent()).toContain(ref(INDEX));
}

async function openViewer(opener: Locator, page: Page): Promise<Locator> {
  await opener.click();
  const dialog = page.getByRole("dialog", { name: "Bot memory", exact: true });
  await dialog.waitFor();
  await expectViewerFiles(dialog);
  return dialog;
}

/** The lazy Markdown renderer has run: its h1 exists and no plain-text "# …" line is left. */
async function expectRenderedTopic(body: Locator): Promise<void> {
  await body.getByRole("heading", { name: TOPIC.title, level: 1 }).waitFor();
  expect(await body.getByText(`# ${TOPIC.title}`, { exact: true }).count()).toBe(0);
}

async function selectTopic(dialog: Locator): Promise<void> {
  await dialog
    .getByRole("button")
    .filter({ hasText: ref(TOPIC) })
    .click();
  await dialog
    .locator(".bot-memory-selected-ref")
    .filter({ hasText: ref(TOPIC) })
    .waitFor();
  await dialog.getByRole("heading", { name: TOPIC.title, level: 3 }).waitFor();
  await dialog.getByText("Use small reversible steps for project changes.").waitFor();
  await expectRenderedTopic(dialog);
  const pressed = await dialog
    .getByRole("button")
    .filter({ hasText: ref(TOPIC) })
    .getAttribute("aria-pressed");
  expect(pressed).toBe("true");
}

async function capture(page: Page, state: State, size: (typeof SIZES)[number], theme: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.waitForFunction((value) => document.documentElement.dataset.theme === value, theme);
  await page.screenshot({
    path: `${OUT}/bot-memory-${state}-${size.width}x${size.height}-${theme}.png`,
  });
}

async function shoot(page: Page, state: State, size: (typeof SIZES)[number], theme: string) {
  await ensureChiefId(size);
  const selected = await open(page);
  const card = await expectCard(selected);
  if (state === "card") {
    await card.scrollIntoViewIfNeeded();
    await card.waitFor({ state: "visible" });
    return capture(page, state, size, theme);
  }

  if (state === "modal" || state === "vault") {
    const dialog = await openViewer(
      card.getByRole("button", { name: "View memory", exact: true }),
      page,
    );
    await selectTopic(dialog);
    if (state === "modal") return capture(page, state, size, theme);
    await dialog.getByRole("button", { name: "Open in Vault", exact: true }).click();
    const reader = page.locator(".vault-browser");
    await reader.getByText(ref(TOPIC), { exact: true }).first().waitFor();
    expect(await page.locator(".vault-ref").textContent()).toBe(ref(TOPIC));
    await reader
      .getByText("Bot memory is read-only in Vault. Ask the Bot to change it from its page.", {
        exact: true,
      })
      .waitFor();
    await reader.getByText("Use small reversible steps for project changes.").waitFor();
    await expectRenderedTopic(reader);
    expect(await reader.getByRole("button", { name: "Edit note", exact: true }).count()).toBe(0);
    return capture(page, state, size, theme);
  }

  const newThread = selected.getByRole("button", { name: "New task thread", exact: true });
  await expect.poll(() => newThread.isEnabled(), { timeout: 15_000 }).toBe(true);
  await newThread.click();
  const bar = page.getByRole("navigation", { name: "Bot chat", exact: true });
  await bar.getByText("Task thread", { exact: true }).waitFor();
  await expect.poll(() => bar.locator(".bot-chat-name strong").textContent()).toBe("Chief");
  if (size.width < 1000) {
    await page.getByRole("button", { name: "VIKI panel", exact: true }).click();
    await page
      .getByRole("dialog", { name: "VIKI panel", exact: true })
      .evaluate((element) => Promise.all(element.getAnimations().map((a) => a.finished)));
  }
  const activity = page.getByRole("region", { name: "Bot activity", exact: true });
  await activity.getByText(source(), { exact: true }).waitFor();
  const view = activity.getByRole("button", { name: "View memory", exact: true });
  await view.waitFor();
  // The viewer must open from here with both files, then closes so the shot shows the activity.
  const dialog = await openViewer(view, page);
  await dialog.getByRole("button", { name: "Close", exact: true }).click();
  await dialog.waitFor({ state: "hidden" });
  await view.waitFor({ state: "visible" });
  return capture(page, state, size, theme);
}

for (const state of STATES) {
  for (const size of SIZES) {
    for (const theme of THEMES) {
      test(`${state} ${size.width}x${size.height} ${theme}`, async () => {
        if (!browser) throw new Error("Edge did not launch");
        const page = await browser.newPage({
          viewport: { width: size.width, height: size.height },
          colorScheme: theme,
        });
        try {
          await shoot(page, state, size, theme);
        } finally {
          await page.unrouteAll({ behavior: "ignoreErrors" });
          await page.close();
        }
      }, 60_000);
    }
  }
}
