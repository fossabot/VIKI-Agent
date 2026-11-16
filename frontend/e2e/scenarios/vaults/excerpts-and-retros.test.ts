/**
 * Vault excerpts end-to-end, because the feature was dead at runtime while every unit suite was
 * green: a vault excerpt attached in the note inspector must reach the turn and come back as a
 * citation, and go `stale` when the note changes behind the app's back.
 *
 * The scratch vault starts with only CLAUDE.md, so each test writes its own note into it after
 * the server is up (the launch wipes the scratch root). The browser context is `ja-JP`, yet the
 * chrome is English, the UI's only language; the excerpt dialog and the citation summary are
 * fixed English text too, asserted as such.
 *
 * Run from frontend/ (binds 8765; check it is free first):
 *   npm run e2e -- e2e/scenarios/vaults/excerpts-and-retros.test.ts
 */
// The harness owns `e2e/node.d.ts`, which declares only what `server.ts` needs; these three
// modules are this file's own business, so they are suppressed here rather than added to it
// (`e2e/scenarios/shell/onboarding.test.ts` and `e2e/scenarios/vaults/browser.test.ts` do the
// same).
// @ts-expect-error see above
import { mkdirSync, writeFileSync } from "node:fs";
// @ts-expect-error see above
import { homedir, tmpdir } from "node:os";
// @ts-expect-error see above
import { join } from "node:path";
import type { Browser, Page } from "playwright-core";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, send } from "../../harness";
import { type Server, startServer } from "../../server";
import { navigateSidebar } from "../../sidebar-navigation";

/** `scripts/e2e_server.py`'s own SCRATCH root, mirrored as `e2e/scenarios/shell/onboarding.test.ts` does. */
const SCRATCH =
  process.platform === "win32" ? join(tmpdir(), "viki-e2e") : join(homedir(), ".cache", "viki-e2e");

const NOTE = "alpha/plain.md";
const NOTE_TEXT = "# Plain\n\nnothing special here\n";

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

function writeNote(ref: string, text: string): void {
  const path = join(SCRATCH, "vault", ...ref.split("/"));
  mkdirSync(join(path, ".."), { recursive: true });
  writeFileSync(path, text, "utf-8");
}

/** Starts the server, seeds the note, and opens a `ja-JP` page once the settings document has
 * loaded (the UI is English whatever the browser says). */
async function open(): Promise<Page> {
  server = await startServer({ fixture: "tests/fixtures/claude/text-two-turns.jsonl" });
  writeNote(NOTE, NOTE_TEXT);
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, locale: "ja-JP" });
  const settings = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v2/settings" &&
      response.request().method() === "GET",
  );
  await page.goto(appUrl());
  await page.waitForSelector('[data-region="shell"]');
  await settings;
  return page;
}

async function showView(page: Page, view: "Chat" | "Vault"): Promise<void> {
  await navigateSidebar(page, view);
}

/** From the Vault view: open the note and attach its first three lines through the dialog. */
async function attachExcerpt(page: Page): Promise<void> {
  await showView(page, "Vault");
  await page.getByRole("navigation", { name: "Notes" }).waitFor();
  await page.locator(".vault-list .vault-row-label > button", { hasText: "plain" }).click();
  await page.getByRole("button", { name: "Note actions" }).click();
  await page.getByRole("menuitem", { name: "Attach excerpt" }).click();
  const dialog = page.getByRole("dialog", { name: "Attach excerpt" });
  await dialog.getByLabel("Last line").fill("3");
  await dialog.locator("pre").waitFor();
  await dialog.getByRole("button", { name: "Attach" }).click();
  await showView(page, "Chat");
}

test("an attached excerpt shows its chip with a character count and comes back as a citation", async () => {
  const page = await open();
  await attachExcerpt(page);
  const chip = page.locator(".excerpt-chip");
  await chip.waitFor();
  await page.waitForSelector('.excerpt-chip[data-state="fresh"]');
  expect(await chip.locator(".excerpt-chip-label").textContent()).toBe(`${NOTE} (lines 1–3)`);
  expect(await chip.locator(".excerpt-chip-label").isVisible()).toBe(true);
  const meta = chip.locator(".excerpt-chip-meta");
  expect(await meta.isVisible()).toBe(true);
  expect(await meta.textContent()).toMatch(/^[\d,]+ characters · To be sent to Claude$/);
  const budget = page.locator(".excerpt-chips-footer").filter({ hasText: /^App-added content:/ });
  expect(await budget.isVisible()).toBe(true);
  expect(await budget.textContent()).toMatch(
    /^App-added content: [\d,]+ characters in total \(limit: 12,000 characters\)$/,
  );

  const details = chip.locator("details.excerpt-chip-details");
  const modified = details
    .locator(".excerpt-chip-diagnostics > div")
    .filter({ hasText: "Modified" })
    .locator("dd");
  const hash = details.locator("code");
  expect(await details.getAttribute("open")).toBeNull();
  expect(await modified.isVisible()).toBe(false);
  expect(await hash.isVisible()).toBe(false);
  await chip.getByLabel(`Excerpt details for ${NOTE}, lines 1–3`, { exact: true }).click();
  expect(await details.getAttribute("open")).not.toBeNull();
  expect(await modified.isVisible()).toBe(true);
  expect(await modified.textContent()).toMatch(/\S/);
  expect(await modified.textContent()).not.toBe("—");
  expect(await hash.isVisible()).toBe(true);
  expect(await hash.textContent()).toMatch(/^[0-9a-f]{64}$/);

  await send(page, "Tell me about this excerpt");
  const cite = page.locator(".user-turn details.excerpt-cite");
  await cite.waitFor({ timeout: 15_000 });
  expect(await cite.getAttribute("data-ref")).toBe(NOTE);
  expect(await cite.locator("summary").textContent()).toBe(`Excerpt: ${NOTE} (lines 1–3)`);
  await page.close();
});

test("a note edited behind the app's back turns the chip stale and disables Send", async () => {
  const page = await open();
  await attachExcerpt(page);
  await page.waitForSelector('.excerpt-chip[data-state="fresh"]');

  writeNote(NOTE, "# Plain\n\nchanged behind the app's back\n");
  // The chip is checked once per mount; leaving the chat and returning checks it again.
  await showView(page, "Vault");
  await page.getByRole("navigation", { name: "Notes" }).waitFor();
  await showView(page, "Chat");
  await page.waitForSelector('.excerpt-chip[data-state="stale"]');

  await page.getByLabel("Message").fill("This should not send yet");
  expect(await page.locator(".composer-submit").isDisabled()).toBe(true);
  expect(await page.locator(".excerpt-chip-meta").textContent()).toContain("The note has changed");
  expect(await page.locator(".excerpt-chip-meta").isVisible()).toBe(true);
  await page.close();
});
