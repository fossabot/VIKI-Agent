/** Real bundle and scratch-only Bot harness; saved chat history stays provider-owned. */
import type { Browser, Locator, Page } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import { blankProfileDocument } from "../../../src/clients/profiles/profileApi";
import { SEAT_STARTERS } from "../../../src/components/Profiles/seatStarters";
import { appUrl, launchEdge, TEST_TOKEN } from "../../harness";
import { ORIGIN, type Server, startServer } from "../../server";
import { navigateSidebar } from "../../sidebar-navigation";

let server: Server | undefined;
let browser: Browser | undefined;
const INSTRUCTIONS = "You are the synthetic Bot.";
/** First line of the memory rule that follows a Bot's instructions in its system prompt, before
 * the Bot rules; both texts are pinned in tests/messaging/test_bot_memory_rule.py. */
const MEMORY_RULE_HEAD = "Memory rule from VIKI Agent (skip it if this chat has no auto memory):";

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
async function createBot(
  name: string,
  instructions = INSTRUCTIONS,
): Promise<{ page: Page; botId: string; sessionId: string; profileId: string }> {
  if (!browser) throw new Error("Edge did not launch");
  const profileId = crypto.randomUUID();
  const document = blankProfileDocument();
  document.identity.name = name;
  document.instructions = instructions;
  await privateJson("/api/v2/profiles", {
    profile_id: profileId,
    command_id: crypto.randomUUID(),
    document,
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  await page.goto(appUrl());
  await page.locator('[data-region="shell"]').waitFor();
  await navigateSidebar(page, "Bots");
  await page.getByRole("button", { name: "New Bot", exact: true }).click();
  const create = page.getByRole("region", { name: "New Bot", exact: true });
  await create.getByRole("combobox", { name: "Profile", exact: true }).selectOption(profileId);
  await create.getByRole("button", { name: "Create Bot", exact: true }).click();
  await page
    .getByRole("region", { name: "Selected Bot", exact: true })
    .getByRole("heading", { name, exact: true })
    .waitFor();
  const listing = await privateJson("/api/v2/bots");
  const bot = (
    listing.bots as Array<{ bot_id: string; profile_id: string; chat_session_id: string }>
  ).find((row) => row.profile_id === profileId);
  if (!bot) throw new Error("The created synthetic Bot was not listed.");
  return { page, botId: bot.bot_id, sessionId: bot.chat_session_id, profileId };
}

/** A fresh Bot chat's composer opens empty: its instructions and the rules go to Claude
 * Code as the system prompt (ADR-084). Types `text` there as a long draft of the user's own. */
async function longDraft(page: Page, textarea: Locator, text: string): Promise<string> {
  await page.locator(".empty-brand-face").waitFor();
  await expect.poll(() => textarea.inputValue()).toBe("");
  await textarea.fill(text);
  expect(await textarea.inputValue()).toBe(text);
  return text;
}

async function assertOpeningFits(page: Page): Promise<void> {
  const geometry = await page.evaluate(async () => {
    await document.fonts.ready;
    await new Promise<void>((resolve) =>
      requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
    );
    const scroll = document.querySelector<HTMLElement>(".conversation-scroll");
    if (!scroll) throw new Error("Conversation scroll clip is missing");
    const clip = scroll.getBoundingClientRect();
    const empty = scroll.querySelector<HTMLElement>(".empty");
    const column = empty?.closest<HTMLElement>(".message-column");
    const brand = empty?.querySelector<HTMLElement>(".empty-brand");
    if (!empty || !column || !brand) throw new Error("Bot opening layout is missing");
    const emptyRect = empty.getBoundingClientRect();
    const columnRect = column.getBoundingClientRect();
    const emptyStyle = getComputedStyle(empty);
    const brandStyle = getComputedStyle(brand);
    const selectors = [
      ".empty-title",
      ".empty-sub",
      ".empty-brand-face",
      ".empty-wordmark",
      ".empty-acronym",
    ];
    const elements = selectors.map((selector) => {
      const element = scroll.querySelector<HTMLElement>(selector);
      if (!element) throw new Error(`${selector} is missing from the conversation`);
      const rect = element.getBoundingClientRect();
      return {
        selector,
        left: rect.left,
        right: rect.right,
        top: rect.top,
        bottom: rect.bottom,
        width: rect.width,
        height: rect.height,
        text: element.textContent?.trim() ?? "",
      };
    });
    const launcher =
      innerWidth === 400 ? document.querySelector<HTMLElement>(".dloop-launcher") : null;
    if (innerWidth === 400 && (!launcher || getComputedStyle(launcher).position !== "fixed")) {
      throw new Error("Fixed VIKI launcher is missing at mobile width");
    }
    const launcherRect = launcher?.getBoundingClientRect();
    return {
      clip: { left: clip.left, right: clip.right, top: clip.top, bottom: clip.bottom },
      viewport: { width: innerWidth, height: innerHeight },
      layout: {
        emptyClass: empty.className,
        emptyTop: emptyRect.top,
        emptyBottom: emptyRect.bottom,
        marginTop: emptyStyle.marginTop,
        paddingTop: emptyStyle.paddingTop,
        paddingBottom: emptyStyle.paddingBottom,
        gap: emptyStyle.gap,
        brandMarginTop: brandStyle.marginTop,
        brandGap: brandStyle.gap,
        columnTop: columnRect.top,
        columnBottom: columnRect.bottom,
        columnJustify: getComputedStyle(column).justifyContent,
        scrollTop: scroll.scrollTop,
        scrollHeight: scroll.scrollHeight,
        clientHeight: scroll.clientHeight,
      },
      elements,
      launcherPaint: launcherRect
        ? {
            left: launcherRect.left - 8,
            right: launcherRect.right + 8,
            top: launcherRect.top - 8,
            bottom: launcherRect.bottom + 8,
          }
        : null,
    };
  });
  const context = JSON.stringify({
    clip: geometry.clip,
    viewport: geometry.viewport,
    ...geometry.layout,
  });
  for (const rect of geometry.elements) {
    const message = `${rect.selector}: ${context}`;
    expect(rect.width, message).toBeGreaterThan(0);
    expect(rect.height, message).toBeGreaterThan(0);
    expect(rect.left, message).toBeGreaterThanOrEqual(Math.max(geometry.clip.left, 0) - 1);
    expect(rect.right, message).toBeLessThanOrEqual(
      Math.min(geometry.clip.right, geometry.viewport.width) + 1,
    );
    expect(rect.top, message).toBeGreaterThanOrEqual(Math.max(geometry.clip.top, 0) - 1);
    expect(rect.bottom, message).toBeLessThanOrEqual(
      Math.min(geometry.clip.bottom, geometry.viewport.height) + 1,
    );
  }
  const face = geometry.elements.find((rect) => rect.selector === ".empty-brand-face");
  if (!face) throw new Error("Bot face geometry is missing");
  expect(Math.abs(face.width - face.height)).toBeLessThanOrEqual(1);
  expect(face.width).toBeGreaterThanOrEqual(96);
  expect(face.width).toBeLessThanOrEqual(160);
  if (geometry.viewport.width === 400) expect(Math.abs(face.width - 96)).toBeLessThanOrEqual(1);
  for (const selector of [".empty-wordmark", ".empty-acronym"]) {
    const rect = geometry.elements.find((element) => element.selector === selector);
    expect(rect?.text).toBeTruthy();
    if (geometry.viewport.width === 400) {
      if (!rect || !geometry.launcherPaint) throw new Error("Mobile opening geometry is missing");
      const paint = geometry.launcherPaint;
      const intersects =
        rect.left < paint.right &&
        rect.right > paint.left &&
        rect.top < paint.bottom &&
        rect.bottom > paint.top;
      expect(
        intersects,
        `${selector} overlaps launcher: ${JSON.stringify({ rect, paint })}; ${context}`,
      ).toBe(false);
    }
  }
}

test("full Chief draft keeps the fresh Bot opening visible at desktop and mobile sizes", async () => {
  const instructions = SEAT_STARTERS.find((starter) => starter.name === "Chief")?.instructions;
  if (!instructions) throw new Error("Chief starter instructions are missing");
  const { page } = await createBot("Chief", instructions);
  try {
    await page.getByRole("button", { name: "Open conversation", exact: true }).click();
    const textarea = page.getByRole("textbox", { name: "Message", exact: true });
    const draft = await longDraft(page, textarea, instructions);
    await assertOpeningFits(page);

    await page.setViewportSize({ width: 400, height: 700 });
    expect(await textarea.inputValue()).toBe(draft);
    await assertOpeningFits(page);
    await textarea.evaluate((element: HTMLTextAreaElement) => {
      element.focus();
      element.setSelectionRange(element.value.length, element.value.length);
    });
    const edited = `${draft}\nMobile edit.`;
    await page.keyboard.insertText("\nMobile edit.");
    expect(await textarea.inputValue()).toBe(edited);
    const overflow = await textarea.evaluate((element) => ({
      scrollHeight: element.scrollHeight,
      clientHeight: element.clientHeight,
      overflowY: getComputedStyle(element).overflowY,
    }));
    expect(overflow.overflowY).toBe("auto");
    expect(overflow.scrollHeight).toBeGreaterThan(overflow.clientHeight + 1);
    await assertOpeningFits(page);
  } finally {
    await page.close();
  }
});

test("full Chief draft task thread opening fits beside the unsent main draft", async () => {
  const instructions = SEAT_STARTERS.find((starter) => starter.name === "Chief")?.instructions;
  if (!instructions) throw new Error("Chief starter instructions are missing");
  const { page, sessionId } = await createBot("Chief", instructions);
  try {
    await page.getByRole("button", { name: "Open conversation", exact: true }).click();
    const mainTextarea = page.getByRole("textbox", { name: "Message", exact: true });
    const draft = await longDraft(page, mainTextarea, instructions);
    await mainTextarea.evaluate((element: HTMLTextAreaElement) => {
      element.focus();
      element.setSelectionRange(element.value.length, element.value.length);
    });
    await page.keyboard.insertText("\nMain unsent edit.");
    expect(await mainTextarea.inputValue()).toBe(`${draft}\nMain unsent edit.`);
    expect((await privateJson(`/api/v2/profile-sessions/${sessionId}`)).started).toBe(false);

    await page.getByRole("button", { name: "Chats & routines", exact: true }).click();
    await page.getByRole("button", { name: "New task thread", exact: true }).click();
    await page
      .getByRole("navigation", { name: "Bot chat", exact: true })
      .getByText("Task thread", { exact: true })
      .waitFor();
    const review = page.getByText(/Profile instructions · Chief · revision/);
    const drafts = page.locator(".composer-slot > .local-drafts");
    await review.waitFor({ state: "visible" });
    await drafts.getByText("Other drafts in this window", { exact: true }).waitFor();
    await expect
      .poll(() =>
        drafts
          .locator("button")
          .filter({ hasText: new RegExp(`^Draft ${sessionId.slice(0, 8)}`) })
          .count(),
      )
      .toBe(1);

    const textarea = page.getByRole("textbox", { name: "Message", exact: true });
    await longDraft(page, textarea, draft);
    await assertOpeningFits(page);
    await page.setViewportSize({ width: 400, height: 700 });
    expect(await textarea.inputValue()).toBe(draft);
    await assertOpeningFits(page);
    await textarea.evaluate((element: HTMLTextAreaElement) => {
      element.focus();
      element.setSelectionRange(element.value.length, element.value.length);
    });
    const edited = `${draft}\nMobile edit.`;
    await page.keyboard.insertText("\nMobile edit.");
    expect(await textarea.inputValue()).toBe(edited);
    const overflow = await textarea.evaluate((element) => ({
      scrollHeight: element.scrollHeight,
      clientHeight: element.clientHeight,
      overflowY: getComputedStyle(element).overflowY,
    }));
    expect(overflow.overflowY).toBe("auto");
    expect(overflow.scrollHeight).toBeGreaterThan(overflow.clientHeight + 1);
    await assertOpeningFits(page);
    expect(await review.isVisible()).toBe(true);
    expect(await drafts.getByText("Other drafts in this window", { exact: true }).isVisible()).toBe(
      true,
    );
  } finally {
    await page.close();
  }
});

test("reopens the same unstarted prepared conversation after page reload", async () => {
  const { page, botId, sessionId } = await createBot("Prepared Bot");
  try {
    await page.getByRole("button", { name: "Open conversation", exact: true }).click();
    await page.getByRole("region", { name: "Bot activity", exact: true }).waitFor();
    const face = page.locator(".empty-brand-face");
    await face.waitFor({ state: "visible" });
    expect((await page.locator(".empty-wordmark").innerText()).replace(/\s/g, "")).toBe(
      "VIKIAGENT",
    );
    expect(
      await page
        .locator(".empty-brand")
        .evaluate((element) => getComputedStyle(element, "::before").display),
    ).toBe("none");
    const faceBox = await face.boundingBox();
    if (!faceBox) throw new Error("Bot face has no bounding box");
    expect(faceBox.width).toBeGreaterThanOrEqual(96);
    expect(faceBox.width).toBeLessThanOrEqual(160);
    expect(Math.abs(faceBox.width - faceBox.height)).toBeLessThanOrEqual(1);
    const avatarBox = await face.locator(".avatar").boundingBox();
    if (!avatarBox) throw new Error("Bot avatar has no bounding box");
    expect(avatarBox.x).toBeGreaterThanOrEqual(faceBox.x);
    expect(avatarBox.y).toBeGreaterThanOrEqual(faceBox.y);
    expect(avatarBox.x + avatarBox.width).toBeLessThanOrEqual(faceBox.x + faceBox.width + 1);
    expect(avatarBox.y + avatarBox.height).toBeLessThanOrEqual(faceBox.y + faceBox.height + 1);
    const preparedTile = page.locator(".rail-team").getByRole("button", {
      name: "Prepared Bot",
      exact: true,
    });
    await preparedTile.waitFor();
    expect(await preparedTile.getAttribute("aria-current")).toBe("true");
    expect(
      await page
        .locator('[data-region="right-rail"]')
        .getByText("Prepared Bot", { exact: true })
        .count(),
    ).toBe(1);
    expect(await page.evaluate(() => sessionStorage.getItem("viki.selected"))).toBe(sessionId);
    const before = await privateJson(`/api/v2/profile-sessions/${sessionId}`);
    expect(before.started).toBe(false);
    await page.setViewportSize({ width: 400, height: 700 });
    const narrowFace = await face.boundingBox();
    if (!narrowFace) throw new Error("Bot face has no bounding box at narrow width");
    expect(Math.abs(narrowFace.width - 96)).toBeLessThanOrEqual(1);
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.reload();
    await page.getByRole("region", { name: "Bot activity", exact: true }).waitFor();
    expect(await page.evaluate(() => sessionStorage.getItem("viki.selected"))).toBe(sessionId);
    expect(await page.locator('[data-region="conversation-header"]').textContent()).toContain(
      "Prepared Bot",
    );
    const after = await privateJson(`/api/v2/profile-sessions/${sessionId}`);
    expect(after.session_id).toBe(sessionId);
    expect(after.started).toBe(false);
    await page.getByRole("button", { name: "Chats & routines", exact: true }).click();
    expect(await page.evaluate(() => sessionStorage.getItem("viki.bot"))).toBe(botId);
    await page
      .getByRole("region", { name: "Selected Bot", exact: true })
      .getByRole("heading", { name: "Prepared Bot", exact: true })
      .waitFor();
  } finally {
    await page.close();
  }
});

test("task threads reopen independently while the Bot keeps its main chat", async () => {
  const name = `Thread${"Bot".repeat(24)}`;
  const { page, botId, sessionId } = await createBot(name);
  try {
    await page.getByRole("button", { name: "New task thread", exact: true }).click();
    await page
      .getByRole("navigation", { name: "Bot chat", exact: true })
      .getByText("Task thread", { exact: true })
      .waitFor();
    await page.getByRole("region", { name: "Bot activity", exact: true }).waitFor();
    const threadId = await page.evaluate(() => sessionStorage.getItem("viki.selected"));
    expect(threadId).not.toBe(sessionId);
    expect((await privateJson(`/api/v2/profile-sessions/${threadId}`)).started).toBe(false);
    expect(
      ((await privateJson(`/api/v2/bots/${botId}`)).bot as { chat_session_id: string })
        .chat_session_id,
    ).toBe(sessionId);
    await page.reload();
    await page
      .getByRole("navigation", { name: "Bot chat", exact: true })
      .getByText("Task thread", { exact: true })
      .waitFor();
    await page.getByRole("region", { name: "Bot activity", exact: true }).waitFor();
    expect(await page.evaluate(() => sessionStorage.getItem("viki.selected"))).toBe(threadId);
    await page.getByRole("button", { name: "Chats & routines", exact: true }).click();
    const threads = page.getByRole("region", { name: "Task threads", exact: true });
    await threads.getByRole("button", { name: `Open thread ${name}`, exact: true }).waitFor();
    await page.getByRole("button", { name: "Open conversation", exact: true }).click();
    await page
      .getByRole("navigation", { name: "Bot chat", exact: true })
      .getByText("Main Bot chat", { exact: true })
      .waitFor()
      .catch(async (error: unknown) => {
        const state = await page.evaluate(() => ({
          selected: sessionStorage.getItem("viki.selected"),
          alerts: [...document.querySelectorAll('[role="alert"]')].map(
            (element) => element.textContent,
          ),
          header: document.querySelector('[data-region="conversation-header"]')?.textContent,
        }));
        throw new Error(`Main Bot chat did not open: ${JSON.stringify(state)}`, { cause: error });
      });
    expect(await page.evaluate(() => sessionStorage.getItem("viki.selected"))).toBe(sessionId);
    expect((await privateJson(`/api/v2/profile-sessions/${sessionId}`)).started).toBe(false);
    await page.setViewportSize({ width: 400, height: 800 });
    const bar = page.getByRole("navigation", { name: "Bot chat", exact: true });
    expect(await bar.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
    expect(
      await bar.evaluate(
        (element) => element.getBoundingClientRect().right <= window.innerWidth + 1,
      ),
    ).toBe(true);
  } finally {
    await page.close();
  }
});

test("an owned manual routine uses locked setup, rehearsal and Test-before-Enable controls", async () => {
  const { page, profileId } = await createBot("Routine Bot");
  try {
    await page.getByRole("button", { name: "New routine", exact: true }).click();
    const editor = page.getByRole("region", { name: "New routine", exact: true });
    expect(await editor.getByRole("combobox", { name: "Profile", exact: true }).isDisabled()).toBe(
      true,
    );
    expect(await editor.getByRole("combobox", { name: "Profile", exact: true }).inputValue()).toBe(
      profileId,
    );
    expect(
      await editor.getByRole("combobox", { name: "Workspace", exact: true }).isDisabled(),
    ).toBe(true);
    await editor.getByLabel("Name", { exact: true }).fill("Bot owned check");
    await editor.getByLabel("Prompt", { exact: true }).fill("Say hello once.");
    await editor.getByLabel("Manual only", { exact: true }).check();
    const rehearsal = editor.locator(".routine-preview-turn");
    await rehearsal.first().waitFor();
    // A run's system prompt is the Bot's instructions, then the memory rule and the Bot rules;
    // its first message is the prompt alone.
    await expect.poll(() => rehearsal.count()).toBe(2);
    const [system, first] = await rehearsal.allTextContents();
    const opening = `${INSTRUCTIONS}\n\n${MEMORY_RULE_HEAD}\n`;
    expect(system?.slice(0, opening.length)).toBe(opening);
    expect(first).toBe("Say hello once.");
    await editor.getByRole("button", { name: "Save", exact: true }).click();
    const detail = page.locator(".bots-routines .routine-detail");
    await detail.getByRole("heading", { name: "Bot owned check", exact: true }).waitFor();
    expect(await detail.getByRole("button", { name: "Enable", exact: true }).count()).toBe(0);
    await detail.getByRole("button", { name: "Test run", exact: true }).click();
    await detail.getByRole("button", { name: "Enable", exact: true }).waitFor();
    await detail.getByRole("button", { name: "Enable", exact: true }).click();
    await detail.getByRole("button", { name: "Pause", exact: true }).waitFor();
    expect(await detail.getByRole("button", { name: "Run now", exact: true }).isVisible()).toBe(
      true,
    );
    expect(await detail.getByRole("heading", { name: "History", exact: true }).isVisible()).toBe(
      true,
    );
    await detail.getByRole("button", { name: "Pause", exact: true }).click();
    await detail.getByRole("button", { name: "Enable", exact: true }).waitFor();
    expect(
      await page
        .getByText("Scheduled routines need an awake host and an open VIKI Agent window.", {
          exact: true,
        })
        .isVisible(),
    ).toBe(true);
  } finally {
    await page.close();
  }
});
