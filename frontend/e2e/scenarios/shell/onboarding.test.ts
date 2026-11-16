/**
 * The first launch, end to end: a scratch profile with no vault at all, which is what
 * `startServer({ noVault: true })` serves — the harness registers nothing and selects nothing, so
 * `capabilities.workspace.chat_only` is true and the app enters the onboarding view by itself.
 *
 * Real backend, real bundle, real Edge. What is asserted here is what only the whole stack can
 * answer: that Create really writes a vault under the scratch profile, that the select really
 * reaches the store (the banner changes its sentence), and that a draft typed before any of it
 * is still in the box after a reload.
 *
 * Every string asserted here is an English catalog row: the UI is English only.
 *
 * Run from frontend/ (binds 8765; check it is free first):
 *   npx vitest run -c vitest.e2e.config.ts e2e/scenarios/shell/onboarding.test.ts
 */
// The harness owns `e2e/node.d.ts`, which declares only what `server.ts` needs; these three
// modules are this file's own business, so they are suppressed here rather than added to it
// (src/testing/css.ts does the same for the same reason).
// @ts-expect-error see above
import { existsSync, readdirSync } from "node:fs";
// @ts-expect-error see above
import { homedir, tmpdir } from "node:os";
// @ts-expect-error see above
import { join } from "node:path";
import type { Browser, Page } from "playwright-core";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, TEST_TOKEN, waitForText } from "../../harness";
import { PORT, type Server, startServer } from "../../server";

/**
 * `scripts/e2e_server.py`'s own SCRATCH root, mirrored here: one fixed folder, wiped on every
 * start. Windows keeps the temp-dir root; POSIX mirrors `Path.home() / ".cache" / "viki-e2e"`
 * (the fixed POSIX deny set covers `/tmp` and `/private`, so a scratch root under either is
 * refused).
 */
const SCRATCH =
  process.platform === "win32" ? join(tmpdir(), "viki-e2e") : join(homedir(), ".cache", "viki-e2e");
const SCRATCH_HOME = join(SCRATCH, "home");

/** The default team's seven Bot seats (`components/Bots/team.ts`'s `TEAM`, fixed-id ones only),
 * hard-coded: this suite stays a black box over the real backend and never imports frontend
 * source. */
const TEAM_PROFILE_IDS = [
  "6a0d02f9-f8dc-4034-941d-03cedde6c839", // Chief
  "e7b63f13-9eb1-4b0d-8df6-cc08d3be33a1", // Ops
  "b0c7ec1e-85a0-4c20-97f3-e79509370f20", // Research
  "61a1448b-613a-4880-92f7-ec0dd6604bf9", // Writer
  "3f1c9a6e-5b2d-4e8f-9a07-c41d2b6e8f53", // Engineer
  "b9f8d2d5-0670-4212-a744-f4180c3a36fe", // Desk
  "9b0e32bf-2d57-46d8-86ce-799a81ecb248", // Checker
];

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

async function openApp(): Promise<Page> {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto(appUrl());
  await page.waitForSelector('[data-region="shell"]');
  return page;
}

/**
 * Reload and wait past the two round trips the onboarding decision needs.
 *
 * `[data-region="shell"]` paints on mount, before any read lands (Shell.tsx). Entering
 * onboarding needs `capabilities`, `workspaces` AND `settings` together (App.tsx), and
 * `createShellStore.start()` only requests settings and workspaces after capabilities resolves
 * (state/shell.ts) — two sequential round trips after that first paint. A bare
 * `.count()` right after the shell selector does not wait for either of them (playwright-core
 * ships no retrying `expect`; see harness.ts), so it can read "0" a few milliseconds before a
 * regression would flip the view to onboarding. Arm the wait for both responses before the
 * reload so nothing races them, then give the page's own `.then()` handlers (the state commit
 * and React's render) one frame to run: the network response event and the page's own
 * `fetch().then()` are two independent observers of the same bytes, not guaranteed to land in
 * the same tick.
 */
async function reloadAfterShellReads(page: Page): Promise<void> {
  const settled = Promise.all([
    page.waitForResponse((response) => response.url().includes("/api/v2/settings")),
    page.waitForResponse((response) => response.url().includes("/api/v2/workspaces")),
  ]);
  await page.reload();
  await page.waitForSelector('[data-region="shell"]');
  await settled;
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
}

/**
 * The durable fact the onboarding decision is actually made from (App.tsx), read the
 * way harness.ts's own test-only routes are: a real GET with the test's Bearer token, not a DOM
 * proxy for it. A dropped "Set up later" PATCH — the failure the "does not come back" tests below
 * are named for — shows up here even on a run where the DOM race above is won by chance.
 */
async function onboardingCompleted(): Promise<boolean> {
  const response = await fetch(`http://127.0.0.1:${PORT}/api/v2/settings`, {
    headers: { Authorization: `Bearer ${TEST_TOKEN}` },
  });
  const body = (await response.json()) as { effective: Record<string, unknown> };
  return body.effective["onboarding.completed"] === true;
}

/** A private GET, read the way harness.ts's own test-only routes are: a real request with the
 * test's Bearer token, not a DOM proxy for it (mirrors bot-seats.test.ts's own `privateJson`). */
async function privateJson(path: string): Promise<Record<string, unknown>> {
  const response = await fetch(`http://127.0.0.1:${PORT}${path}`, {
    headers: { Authorization: `Bearer ${TEST_TOKEN}` },
  });
  if (!response.ok) throw new Error(`GET ${path} answered ${response.status}.`);
  return (await response.json()) as Record<string, unknown>;
}

test("create on a clean profile writes the vault, adds the team's Bots, selects it, and keeps the draft", async () => {
  server = await startServer({ noVault: true });
  const page = await openApp();

  // The first launch has no earlier page. Set up later reaches the chat where the draft is
  // typed: a draft written before the vault exists has to survive the reload.
  await page.waitForSelector(".onboarding-card");
  const back = page.getByRole("button", { name: "Back", exact: true });
  expect(await back.isVisible()).toBe(true);
  expect(await back.isDisabled()).toBe(true);
  await page.getByText("No earlier page in this window.", { exact: true }).waitFor();
  await page.locator("#onboarding-card-later").click();
  await page.waitForSelector('[data-region="conversation"]');
  await page.getByLabel("Message").fill("A draft to send later");
  // The saved state, not merely the box's text: leaving the chat unmounts the composer, so
  // the save has to have landed before the view changes.
  await waitForText(page, ".draft-status", "Draft saved");

  // Back into the view the way the user would: the chat-only banner's own Create button.
  await page.locator(".chat-only-banner").getByRole("button", { name: "Create a vault" }).click();
  await page.locator("#create-name").fill("End to end");
  // The suggestion fills the parent and the folder from the name; without waiting for it the
  // create would post an empty parent and land somewhere the assertion below cannot look.
  await page.waitForFunction(() => {
    const field = document.querySelector<HTMLInputElement>("#create-parent");
    return field !== null && field.value !== "";
  });
  const parent = await page.locator("#create-parent").inputValue();
  // Both fields must be read before the submit: the result panel replaces the form.
  const folder = await page.locator("#create-folder").inputValue();
  expect(parent.toLowerCase()).toContain(SCRATCH_HOME.toLowerCase());

  // Nothing creates the suggested parent first: the scratch home has never held a vault, so
  // `%USERPROFILE%\VIKI\Vaults` does not exist when Create is pressed. That the default create
  // succeeds anyway is the point — `vault_storage._destination` treats an explicit parent that
  // spells the app's own suggestion exactly as an omitted one.
  expect(existsSync(parent)).toBe(false);
  await page.getByRole("button", { name: "Create" }).click();
  await page.waitForSelector('[data-region="create-vault-result"]');

  // The vault is on disk, with the welcome note the bootstrap writes.
  const created = join(parent, folder);
  expect(existsSync(created)).toBe(true);
  expect(readdirSync(created)).toContain("Welcome.md");

  // fillStarterTeam (seatBots.ts) fires alongside the select, once Create resolves: one seat at
  // a time, sequentially, so this allows up to 15s for every seat to land before asserting.
  await expect
    .poll(async () => ((await privateJson("/api/v2/bots")).bots as unknown[]).length, {
      timeout: 15_000,
    })
    .toBe(TEAM_PROFILE_IDS.length);
  const teamBots = (await privateJson("/api/v2/bots")).bots as Array<{
    profile_id: string;
    workspace_id: string;
    chat: { profile_revision: number | null; started: boolean };
    routine_ids: string[];
  }>;
  const workspaces = (await privateJson("/api/v2/workspaces")).workspaces as Array<{
    workspace_id: string;
  }>;
  expect(workspaces).toHaveLength(1);
  const workspaceId = workspaces[0]?.workspace_id;
  expect(teamBots.map((bot) => bot.profile_id).sort()).toEqual([...TEAM_PROFILE_IDS].sort());
  for (const bot of teamBots) {
    expect(bot.workspace_id).toBe(workspaceId);
    expect(bot.chat.profile_revision).toBeNull();
    expect(bot.chat.started).toBe(false);
    expect(bot.routine_ids).toEqual([]);
  }

  // The select reached the store: the banner stops offering to create one and says the
  // registered vault becomes active at the next launch.
  await waitForText(page, ".onboarding-form", "next launch");
  await page.reload();
  await page.waitForSelector('[data-region="shell"]');
  await waitForText(page, ".chat-only-banner", "becomes active on the next launch");
  // The draft outlived the launch that created a vault. It is not back in the box — a reload
  // opens a new editor key, and a saved draft comes back only by an explicit choice — but it
  // is still saved, and the recovery disclosure is where the user gets it back.
  await page.locator(".draft-recovery summary").click();
  await page.locator(".draft-recovery-list li").first().waitFor({ timeout: 15_000 });
  expect(await page.locator(".draft-recovery-list li").count()).toBeGreaterThan(0);

  await page.close();
});

test("open an existing folder lands in the plain chat-only chat, and onboarding does not come back", async () => {
  server = await startServer({ noVault: true });
  const page = await openApp();
  await page.waitForSelector(".onboarding-card");

  await page.locator("#onboarding-card-open").click();
  // `--scratch` still creates its vault folder even with no VIKI_VAULT, so there is something
  // real to register read-only.
  await page.getByLabel("Folder path").fill(join(SCRATCH, "vault"));
  await page.getByRole("button", { name: "Open" }).click();
  await page.waitForSelector('[data-region="open-vault-result"]');
  await waitForText(page, '[data-region="open-vault-result"]', "read-only");

  // A registered workspace means the view is no longer entered automatically; the count below
  // has to be read after both reads the onboarding decision needs (reloadAfterShellReads), not
  // right after the reload's first paint.
  await reloadAfterShellReads(page);
  expect(await page.locator(".onboarding-card").count()).toBe(0);
  await waitForText(page, ".chat-only-banner", "becomes active on the next launch");

  await page.close();
});

test("set up later leaves the plain chat-only banner and does not come back", async () => {
  server = await startServer({ noVault: true });
  const page = await openApp();
  await page.waitForSelector(".onboarding-card");

  await page.locator("#onboarding-card-later").click();
  await page.waitForSelector('[data-region="conversation"]');
  await waitForText(page, ".chat-only-banner", "No vault is connected");

  // The count has to be read after both reads the onboarding decision needs
  // (reloadAfterShellReads), not right after the reload's first paint.
  await reloadAfterShellReads(page);
  expect(await page.locator(".onboarding-card").count()).toBe(0);
  // The DOM count only proves this instant; the failure this test is named for (a dropped
  // "Set up later" PATCH) is a persistence bug, so check the durable fact directly too.
  expect(await onboardingCompleted()).toBe(true);

  await page.close();
});
