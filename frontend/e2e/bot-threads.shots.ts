/**
 * Bot thread groups, their independently visible states, and the delete confirmation. Synthetic
 * scratch vault and scripted Claude fixture only; this is a visual run, not part of scripts/check.py.
 *
 *   npx vitest run --config vitest.shots.config.ts e2e/bot-threads.shots.ts
 *
 * Run from frontend/ after a fresh build with port 8765 free. PNGs land in
 * `test-results/bot-threads/` at 1440×1000 and 400×700 in light and dark themes.
 */
// @ts-expect-error the frontend harness omits Node's ambient types
import { Buffer } from "node:buffer";
// @ts-expect-error the shots harness declares only what server.ts needs
import { mkdirSync } from "node:fs";
import type { Browser, Locator, Page } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge } from "./harness";
import { type Server, startServer } from "./server";
import { navigateSidebar } from "./sidebar-navigation";

const OUT = "test-results/bot-threads";
const SIZES = [
  { width: 1440, height: 1000 },
  { width: 400, height: 700 },
] as const;
const THEMES = ["light", "dark"] as const;

let browser: Browser | undefined;
let server: Server | undefined;

beforeAll(async () => {
  mkdirSync(OUT, { recursive: true });
  browser = await launchEdge();
  server = await startServer({
    bots: true,
    team: true,
    fixture: "tests/fixtures/claude/ask-user-question.jsonl",
  });
}, 120_000);

afterAll(async () => {
  await browser?.close();
  await server?.stop();
});

async function ready(page: Page): Promise<void> {
  await page.locator('[data-region="shell"]').waitFor({ timeout: 15_000 });
  await page.evaluate(() => document.fonts.ready);
}

async function createGroup(page: Page, members: readonly string[]): Promise<void> {
  const firstMember = members[0];
  if (!firstMember) throw new Error("A thread group needs at least one Bot");
  const expectedCount = (await page.locator(".bot-thread-workspace-row").count()) + 1;
  await page.getByRole("button", { name: "New Bot thread", exact: true }).click();
  const participants = page.getByRole("complementary", { name: "Participants", exact: true });
  await participants.getByRole("checkbox", { name: firstMember, exact: true }).waitFor();
  for (const name of ["Chief", "Checker", "Research"]) {
    const checkbox = participants.getByRole("checkbox", { name, exact: true });
    if (members.includes(name) !== (await checkbox.isChecked())) await checkbox.click();
  }
  await page.getByRole("button", { name: "Create thread", exact: true }).click();
  await page.waitForFunction(
    (count) => document.querySelectorAll(".bot-thread-workspace-row").length === count,
    expectedCount,
  );
}

async function capture(page: Page, name: string, prepare?: () => Promise<void>): Promise<void> {
  for (const size of SIZES) {
    await page.setViewportSize(size);
    for (const theme of THEMES) {
      await page.emulateMedia({ colorScheme: theme });
      await page.waitForFunction(
        (value) => document.documentElement.dataset.theme === value,
        theme,
      );
      await page.evaluate(() => document.fonts.ready);
      await prepare?.();
      await page.screenshot({ path: `${OUT}/bot-threads-${name}-${size.width}-${theme}.png` });
    }
  }
}

async function assertFullyShown(locator: Locator, label: string): Promise<void> {
  await locator.waitFor({ state: "visible" });
  const geometry = await locator.evaluate((node) => {
    const rect = node.getBoundingClientRect();
    const viewport = {
      left: 0,
      top: 0,
      right: window.innerWidth,
      bottom: window.innerHeight,
    };
    const clip = { left: -Infinity, top: -Infinity, right: Infinity, bottom: Infinity };
    for (let ancestor = node.parentElement; ancestor; ancestor = ancestor.parentElement) {
      const style = getComputedStyle(ancestor);
      const bounds = ancestor.getBoundingClientRect();
      const left = bounds.left + ancestor.clientLeft;
      const top = bounds.top + ancestor.clientTop;
      if (["auto", "scroll", "hidden", "clip"].includes(style.overflowX)) {
        clip.left = Math.max(clip.left, left);
        clip.right = Math.min(clip.right, left + ancestor.clientWidth);
      }
      if (["auto", "scroll", "hidden", "clip"].includes(style.overflowY)) {
        clip.top = Math.max(clip.top, top);
        clip.bottom = Math.min(clip.bottom, top + ancestor.clientHeight);
      }
    }
    const inside = (bounds: typeof viewport) =>
      rect.width > 0 &&
      rect.height > 0 &&
      rect.left >= bounds.left - 0.5 &&
      rect.top >= bounds.top - 0.5 &&
      rect.right <= bounds.right + 0.5 &&
      rect.bottom <= bounds.bottom + 0.5;
    return {
      rect: { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom },
      viewport,
      clip,
      insideViewport: inside(viewport),
      insideAncestors: inside(clip),
    };
  });
  expect(geometry.insideViewport, `${label} outside viewport: ${JSON.stringify(geometry)}`).toBe(
    true,
  );
  expect(geometry.insideAncestors, `${label} clipped: ${JSON.stringify(geometry)}`).toBe(true);
}

async function assertSelectedList(page: Page, selectedRow: Locator): Promise<void> {
  const rows = page.locator(".bot-thread-workspace-row");
  expect(await rows.count()).toBe(3);
  await assertFullyShown(
    page.getByRole("button", { name: "New Bot thread", exact: true }),
    "New Bot thread",
  );
  for (let index = 0; index < 3; index++) {
    await assertFullyShown(rows.nth(index), `group ${index + 1}`);
    await assertFullyShown(
      rows.nth(index).locator(".bot-thread-row-title"),
      `group ${index + 1} title`,
    );
  }
  await assertFullyShown(selectedRow, "selected group");
  const faces = selectedRow.locator(".bot-thread-list-face");
  expect(await faces.count()).toBeGreaterThan(0);
  for (let index = 0; index < (await faces.count()); index++) {
    await assertFullyShown(faces.nth(index), `selected face ${index + 1}`);
  }
  await assertFullyShown(
    selectedRow.locator(".bot-thread-phase").filter({ hasText: "Working" }),
    "selected Working phase",
  );
  await assertFullyShown(selectedRow.locator(".bot-thread-row-time"), "selected activity time");
  await assertFullyShown(selectedRow.locator(".bot-thread-row-needs"), "selected Needs You");
  const horizontalOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
  );
  expect(horizontalOverflow, "document has horizontal overflow").toBe(false);
}

async function assertDeleteDialog(
  dialog: Locator,
  selectedTitle: string,
  hasDraft: boolean,
): Promise<void> {
  await assertFullyShown(dialog, "delete confirmation");
  const body = dialog.locator("p").first();
  await assertFullyShown(body, "delete title and consequence");
  const text = await body.textContent();
  expect(text).toContain(`Delete “${selectedTitle}”?`);
  expect(text).toContain("Only the thread group is deleted.");
  expect(text).toContain("The Bots' chats and their Claude Code transcripts stay.");
  expect(text?.includes("Unsent text and files in this thread are discarded.")).toBe(hasDraft);
  await assertFullyShown(
    dialog.getByRole("button", { name: "Delete thread group", exact: true }),
    "delete action",
  );
  await assertFullyShown(
    dialog.getByRole("button", { name: "Cancel", exact: true }),
    "cancel action",
  );
}

test("selected list states and thread-group delete confirmation", async () => {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: SIZES[0], colorScheme: "light" });
  try {
    await page.goto(appUrl());
    await ready(page);
    await navigateSidebar(page, "Bot threads");
    await page.getByRole("navigation", { name: "Threads", exact: true }).waitFor();

    // Create every group before starting the scripted turn, which keeps New available.
    await createGroup(page, ["Chief"]);
    await createGroup(page, ["Chief", "Research"]);
    await createGroup(page, ["Chief", "Checker"]);
    const rows = page.locator(".bot-thread-workspace-row");
    await expect(await rows.count()).toBe(3);

    await page
      .getByRole("textbox", { name: "Message the thread", exact: true })
      .fill("Show a scripted question for this visual example.");
    await page.getByRole("button", { name: "Send to team", exact: true }).click();
    await page.locator(".bot-thread-questions .card").waitFor({ timeout: 30_000 });
    const selectedRow = rows.filter({ has: page.locator('[aria-current="page"]') });
    await assertSelectedList(page, selectedRow);
    await capture(page, "selected-list", async () => {
      await assertSelectedList(page, selectedRow);
    });

    // Thread deletion is unavailable while any group runs. Capture the active row first, then
    // stop its scripted question turn before opening another group's confirmation.
    await page.locator(".bot-thread-remove-blocked").waitFor({ state: "visible" });
    const stop = page
      .locator(".bot-thread-composer")
      .getByRole("button", { name: "Stop", exact: true });
    await stop.click();
    await stop.waitFor({ state: "hidden" });
    await page
      .locator(".bot-thread-phase")
      .filter({ hasText: "Working" })
      .waitFor({ state: "hidden" });
    await page.locator(".bot-thread-remove-blocked").waitFor({ state: "hidden" });
    const deleteAction = page.getByRole("button", { name: "Delete thread group…", exact: true });
    expect(await deleteAction.getAttribute("aria-disabled")).not.toBe("true");

    // Use an idle group for both draft states.
    await rows.nth(1).getByRole("button").click();
    const selectedTitle =
      (await page.locator(".bot-thread-toolbar h2").textContent())?.trim() ?? "";
    expect(selectedTitle).not.toBe("");
    await page.setViewportSize(SIZES[1]);
    await page.getByRole("button", { name: "Delete thread group…", exact: true }).click();
    const dialog = page.getByRole("alertdialog");
    await assertDeleteDialog(dialog, selectedTitle, false);
    await capture(page, "delete-empty-draft", async () => {
      await dialog.scrollIntoViewIfNeeded();
      await assertDeleteDialog(dialog, selectedTitle, false);
    });
    await dialog.getByRole("button", { name: "Cancel", exact: true }).click();

    await page
      .getByRole("textbox", { name: "Message the thread", exact: true })
      .fill("Keep this unsent draft with its attachment.");
    await page.locator('input[type="file"]').setInputFiles({
      name: "synthetic-reference.png",
      mimeType: "image/png",
      buffer: Buffer.from(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=",
        "base64",
      ),
    });
    await page.locator(".attachment-item").waitFor();
    await page
      .locator(".attachment-item-status")
      .filter({ hasText: "Ready" })
      .waitFor({ state: "visible" });
    await page.setViewportSize(SIZES[1]);
    await page.getByRole("button", { name: "Delete thread group…", exact: true }).click();
    const draftDialog = page.getByRole("alertdialog");
    await assertDeleteDialog(draftDialog, selectedTitle, true);
    await capture(page, "delete-unsent-draft", async () => {
      await draftDialog.scrollIntoViewIfNeeded();
      await assertDeleteDialog(draftDialog, selectedTitle, true);
    });
  } finally {
    await page.close();
  }
});
