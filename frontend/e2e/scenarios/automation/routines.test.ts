/**
 * Routines end to end, against the real server with the fake CLI: a profile made over REST, then
 * everything else through the Routines tab in real Edge. The user creates a manual routine, sees
 * the rehearsal's first turn, finds the saved routine in the right rail with none of the planned
 * rows `/api/rails` still carries, and pauses all.
 *
 * What this file does NOT drive, and why: the test run and the enabling. Under this harness a
 * profile launch cannot succeed, so a test run ends `blocked` / `profile_blocked` and the routine
 * never becomes Tested. Two facts, both outside this file:
 *   - the fake CLI answers `--version` with 2.1.259 (`tests/fake_claude.py` FAKE_VERSION), but
 *     `profile_capabilities.baseline` proves its capability rows only for FIXTURE_VERSION 2.1.281,
 *     so `runtime.effort` and `runtime.permission_mode` are `unverified` and the launch is not
 *     `launchable` (`POST /api/v2/profiles/{id}/start-preview` answers 422 with exactly those two
 *     blockers, whatever the profile's mode is, since effort is always the profile's own);
 *   - `scripts/e2e_server.py` registers the scratch vault with `register_existing`, so its policy
 *     is `existing_readonly`, and enabling needs an ordinary workspace (`unattended_policy`, which
 *     the rehearsal below already lists).
 * Both are the harness's to change; nothing here works around them.
 *
 * Nothing here is live: the CLI is the replay fake and the vault, home and data folder are the
 * scratch ones.
 *
 * Run from frontend/ (binds 8765; check it is free first):
 *   npm run e2e -- e2e/scenarios/automation/routines.test.ts
 */
import type { Browser, Page } from "playwright-core";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import type { ProfileDocumentV1 } from "../../../src/clients/profiles/profileApi";
import { appUrl, launchEdge, TEST_TOKEN } from "../../harness";
import { ORIGIN, type Server, startServer } from "../../server";
import { navigateSidebar } from "../../sidebar-navigation";

const NAME = "Morning check";
const INSTRUCTIONS = "You are the e2e profile.";
const PROMPT = "Say hello once.";
/** The three rows `/api/rails` always carries, which the shipped UI must not show. */
const PLANNED = ["/daily · morning brief", "curator · nightly", "/weekly + /retro"];

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

const PROFILE: ProfileDocumentV1 = {
  format_version: 1,
  identity: {
    name: "E2E profile",
    purpose: "",
    avatar_asset_id: null,
    accent: "blue",
    shape: "rounded",
  },
  runtime: { provider: "claude", model: null, effort: "high", permission_mode: "default" },
  instructions: INSTRUCTIONS,
  workspace: { allowed_workspace_ids: [] },
  context: { items: [] },
  native_assets: [],
  tools: { required: [], denied: [], mcp_configuration: null },
  memory: { mode: "provider_inherited", sources: [] },
  limits: {
    timeout_seconds: null,
    max_root_tasks: null,
    token_budget: null,
    currency_budget: null,
  },
  provenance: { kind: "user", derived_from: null, native_source: null },
};

/** `profileApi.create`'s request, sent from node: a non-GET carries the page's own Origin. */
async function createProfile(): Promise<void> {
  const response = await fetch(`${ORIGIN}/api/v2/profiles`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${TEST_TOKEN}`,
      Origin: ORIGIN,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      profile_id: crypto.randomUUID(),
      command_id: crypto.randomUUID(),
      document: PROFILE,
    }),
  });
  if (response.status !== 200 && response.status !== 201) {
    throw new Error(`creating the profile answered ${response.status}: ${await response.text()}`);
  }
}

async function openRoutines(): Promise<Page> {
  if (!browser) throw new Error("Edge did not launch");
  // A ja-JP context, yet English copy: the UI is English only.
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, locale: "ja-JP" });
  await page.goto(appUrl());
  await page.waitForSelector('[data-region="shell"]');
  await navigateSidebar(page, "Routines");
  await page.getByRole("heading", { level: 1, name: "Routines" }).waitFor();
  return page;
}

test("a manual routine is created with its rehearsal, listed in the rail with no planned row, and paused with the rest", async () => {
  server = await startServer({ fixture: "tests/fixtures/claude/text-two-turns.jsonl" });
  await createProfile();
  const page = await openRoutines();

  // Create: a manual routine on the profile and the active workspace.
  await page.getByRole("button", { name: "New routine" }).click();
  await page.getByLabel("Name", { exact: true }).fill(NAME);
  await page.getByLabel("Prompt", { exact: true }).fill(PROMPT);
  await page.getByLabel("Manual only").check();

  // The rehearsal shows the exact first turn before anything is saved: the profile's
  // instructions, a blank line, then the prompt.
  const turn = page.locator(".routine-editor .routine-preview-turn");
  await turn.waitFor();
  expect(await turn.textContent()).toBe(`${INSTRUCTIONS}\n\n${PROMPT}`);
  await page.getByRole("button", { name: "Save", exact: true }).click();

  // Saved: a draft, in its detail.
  const detail = page.locator(".routine-detail");
  await detail.getByRole("heading", { level: 2, name: NAME }).waitFor();
  expect(await detail.locator("> p .chip").textContent()).toBe("Draft");

  // The right rail belongs to the chat view. It lists the routine, and only it: the planned rows
  // /api/rails still serves never render.
  await navigateSidebar(page, "Chat");
  const rail = page.locator('[data-region="right-rail"] .rail-section--routines');
  const rows = rail.locator(".routines-rail-row");
  await rows.first().waitFor();
  expect(await rows.count()).toBe(1);
  expect(await rows.first().locator(".routines-rail-name").textContent()).toBe(NAME);
  expect(await rail.locator(".rail-section-count").count()).toBe(0);
  const shell = (await page.locator('[data-region="shell"]').textContent()) ?? "";
  for (const planned of PLANNED) expect(shell).not.toContain(planned);
  expect(shell).not.toContain("planned");

  // A rail row opens that routine in the view; there, pause all shows the notice and the rail's
  // count follows.
  await rows.first().click();
  await page.locator(".routine-detail").getByRole("heading", { level: 2, name: NAME }).waitFor();
  await page.getByRole("button", { name: "Pause all" }).click();
  await page.getByText("All routines are paused.").waitFor();
  await page.getByRole("button", { name: "Resume" }).waitFor();
  await navigateSidebar(page, "Chat");
  await rail.locator(".rail-section-count", { hasText: "Paused" }).waitFor();
  await page.close();
});
