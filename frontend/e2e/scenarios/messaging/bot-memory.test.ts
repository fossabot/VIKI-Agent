/**
 * Real bundle and scratch-only Bot harness; saved chat history stays provider-owned. The Bot's
 * memory files are synthetic route fixtures: the scratch vault is only read, never written.
 */
import type { Browser, Page } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import { blankProfileDocument } from "../../../src/clients/profiles/profileApi";
import { appUrl, launchEdge, TEST_TOKEN } from "../../harness";
import { ORIGIN, type Server, startServer } from "../../server";
import { navigateSidebar } from "../../sidebar-navigation";

let server: Server | undefined;
let browser: Browser | undefined;
const INSTRUCTIONS = "You are the synthetic Bot.";
const INDEX = "MEMORY.md";
const TOPIC = "topics/decisions.md";
const INDEX_TEXT = "# MEMORY.md\n\nSynthetic index linking to the decisions topic.\n";
const TOPIC_TEXT = "# Project decisions\n\nUse small reversible steps for project changes.\n";
const DISPLAY_ABSENT = {
  state: "absent",
  frontmatter_state: "absent",
  heading_state: "absent",
  display_title: null,
  title_source: null,
  tags: [],
  type: null,
  status: null,
};

beforeAll(async () => {
  server = await startServer({ bots: true, fixture: "tests/fixtures/claude/text-two-turns.jsonl" });
  browser = await launchEdge();
}, 60_000);
afterAll(async () => {
  await browser?.close();
  await server?.stop();
});

async function privateJson(path: string, payload?: object): Promise<Record<string, unknown>> {
  const response = await fetch(`${ORIGIN}${path}`, {
    method: payload === undefined ? "GET" : "POST",
    headers: {
      authorization: `Bearer ${TEST_TOKEN}`,
      ...(payload === undefined ? {} : { origin: ORIGIN, "content-type": "application/json" }),
    },
    ...(payload === undefined ? {} : { body: JSON.stringify(payload) }),
  });
  if (!response.ok) throw new Error(`Synthetic Bot request answered ${response.status}.`);
  return (await response.json()) as Record<string, unknown>;
}

/** Answer the Bot folder reads with a topic file newer than its index; the index must still lead. */
async function mockMemory(page: Page, botId: Promise<string>): Promise<void> {
  const files = async () => {
    const prefix = `bot-memory/${await botId}`;
    return new Map([
      [`${prefix}/${INDEX}`, { title: INDEX, text: INDEX_TEXT, modified: 1 }],
      [`${prefix}/${TOPIC}`, { title: "Project decisions", text: TOPIC_TEXT, modified: 2 }],
    ]);
  };
  await page.route("**/api/v2/vaults/*/entries*", async (route) => {
    const entries = [...(await files())].map(([ref, file]) => ({
      ref,
      title: file.title,
      bytes: file.text.length,
      modified: file.modified,
    }));
    await route.fulfill({
      json: { entries, partial: false, partial_reasons: [], omitted: 0, read_bytes: 0 },
    });
  });
  await page.route("**/api/v2/vaults/*/metadata", async (route) => {
    const { refs } = route.request().postDataJSON() as { refs: string[] };
    await route.fulfill({
      json: {
        items: refs.map((ref) => ({
          ref,
          state: "absent",
          reason: null,
          read_bytes: 32,
          display_metadata: DISPLAY_ABSENT,
        })),
        partial: false,
        partial_reasons: [],
        omitted: 0,
        read_bytes: 64,
      },
    });
  });
  await page.route("**/api/v2/vaults/*/notes/**", async (route) => {
    const url = new URL(route.request().url());
    const ref = decodeURIComponent(url.pathname.split("/notes/")[1] ?? "");
    const file = (await files()).get(ref);
    if (!file) return route.continue();
    return route.fulfill({
      json: {
        note: {
          ref,
          title: file.title,
          text: file.text,
          source: file.text,
          byte_size: new TextEncoder().encode(file.text).length,
          sha256: "a".repeat(64),
          frontmatter: {},
          frontmatter_error: null,
          links: [],
          display_metadata: {
            ...DISPLAY_ABSENT,
            state: "ready",
            heading_state: "ready",
            display_title: file.title,
            title_source: "h1",
          },
        },
        resolved_links: [],
      },
    });
  });
}

async function createBot(name: string): Promise<{
  page: Page;
  botId: string;
  sends: () => number;
  vaultWrites: () => number;
}> {
  if (!browser) throw new Error("Edge did not launch");
  const profileId = crypto.randomUUID();
  const document = blankProfileDocument();
  document.identity.name = name;
  document.instructions = INSTRUCTIONS;
  await privateJson("/api/v2/profiles", {
    profile_id: profileId,
    command_id: crypto.randomUUID(),
    document,
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  let sends = 0;
  let vaultWrites = 0;
  // The app sends a chat turn as a WebSocket `turn.send` frame, not an HTTP command.
  page.on("websocket", (socket) => {
    socket.on("framesent", ({ payload }) => {
      const frame = JSON.parse(String(payload)) as { type?: string };
      if (frame.type === "turn.send") sends += 1;
    });
  });
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (request.method() === "POST" && path === "/api/v2/commands") sends += 1;
    if (
      request.method() !== "GET" &&
      path.startsWith("/api/v2/vaults/") &&
      !path.endsWith("/metadata")
    )
      vaultWrites += 1;
  });
  let known: (id: string) => void = () => {};
  await mockMemory(
    page,
    new Promise<string>((resolve) => {
      known = resolve;
    }),
  );
  await page.goto(appUrl());
  await page.locator('[data-region="shell"]').waitFor();
  await navigateSidebar(page, "Bots");
  await page.getByRole("button", { name: "New Bot", exact: true }).click();
  const create = page.getByRole("region", { name: "New Bot", exact: true });
  await create.getByRole("combobox", { name: "Profile", exact: true }).selectOption(profileId);
  await create.getByRole("button", { name: "Create Bot", exact: true }).click();
  await selectedBot(page).getByRole("heading", { name, exact: true }).waitFor();
  const listing = await privateJson("/api/v2/bots");
  const bot = (listing.bots as Array<{ bot_id: string; profile_id: string }>).find(
    (row) => row.profile_id === profileId,
  );
  if (!bot) throw new Error("The created synthetic Bot was not listed.");
  known(bot.bot_id);
  return { page, botId: bot.bot_id, sends: () => sends, vaultWrites: () => vaultWrites };
}

function selectedBot(page: Page) {
  return page.getByRole("region", { name: "Selected Bot", exact: true });
}

test("the Bot memory card lists index and topic; View memory shows exact refs, index first", async () => {
  const { page, botId, sends, vaultWrites } = await createBot("Memory viewer");
  try {
    const prefix = `bot-memory/${botId}`;
    const card = selectedBot(page).locator(".bot-memory-card");
    await card.getByRole("heading", { name: "Bot memory", exact: true }).waitFor();
    await card.getByText(`Source ${prefix}/`, { exact: true }).waitFor();
    await card.getByText(`${prefix}/${INDEX}`, { exact: true }).waitFor();
    await card.getByText(`${prefix}/${TOPIC}`, { exact: true }).waitFor();
    expect(await page.locator(".bot-threads details").count()).toBe(0);

    await card.getByRole("button", { name: "View memory", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Bot memory", exact: true });
    const files = dialog.getByRole("navigation", { name: "All files", exact: true });
    await files.waitFor();
    expect(await files.locator("li button code").allTextContents()).toEqual([
      `${prefix}/${INDEX}`,
      `${prefix}/${TOPIC}`,
    ]);
    await expect
      .poll(() => dialog.locator(".bot-memory-selected-ref").textContent())
      .toBe(`${prefix}/${INDEX}`);
    await dialog.getByRole("heading", { name: INDEX, exact: true, level: 3 }).waitFor();
    await dialog.getByRole("heading", { name: INDEX, exact: true, level: 1 }).waitFor();
    expect(await dialog.getByText(`# ${INDEX}`, { exact: true }).count()).toBe(0);
    await files.getByRole("button", { name: /decisions/ }).click();
    await expect
      .poll(() => dialog.locator(".bot-memory-selected-ref").textContent())
      .toBe(`${prefix}/${TOPIC}`);
    await dialog
      .getByRole("heading", { name: "Project decisions", exact: true, level: 3 })
      .waitFor();
    await dialog
      .getByRole("heading", { name: "Project decisions", exact: true, level: 1 })
      .waitFor();
    expect(await dialog.getByText("# Project decisions", { exact: true }).count()).toBe(0);
    expect(sends()).toBe(0);
    expect(vaultWrites()).toBe(0);
  } finally {
    await page.close();
  }
});

test.each(["Ask to review", "Ask to forget all…"])(
  "%s opens an unsent task-thread draft scoped to the selected Bot",
  async (label) => {
    const { page, botId, sends, vaultWrites } = await createBot(`Memory ${label}`);
    try {
      const ref = `bot-memory/${botId}`;
      const card = selectedBot(page).locator(".bot-memory-card");
      await card.getByText(`Source ${ref}/`, { exact: true }).waitFor();
      await card.getByRole("button", { name: label, exact: true }).click();
      const draft = page.getByRole("textbox", { name: "Message", exact: true });
      await expect.poll(() => draft.inputValue()).toContain(`folder at ${ref}/`);
      if (label === "Ask to forget all…")
        expect(await draft.inputValue()).toContain(
          "confirm the listed files before changing or deleting anything",
        );
      expect(sends()).toBe(0);
      expect(vaultWrites()).toBe(0);
    } finally {
      await page.close();
    }
  },
);

test("Ask {name} to change… opens an unsent main-chat draft for the selected Bot", async () => {
  const { page, botId, sends, vaultWrites } = await createBot("Memory changer");
  try {
    const ref = `bot-memory/${botId}`;
    const card = selectedBot(page).locator(".bot-memory-card");
    await card.getByText(`Source ${ref}/`, { exact: true }).waitFor();
    await card.getByRole("button", { name: "Ask Memory changer to change…", exact: true }).click();
    const draft = page.getByRole("textbox", { name: "Message", exact: true });
    await expect
      .poll(() => draft.inputValue())
      .toContain(`Please change the memory files in ${ref}/ as follows.`);
    expect(sends()).toBe(0);
    expect(vaultWrites()).toBe(0);
  } finally {
    await page.close();
  }
});
