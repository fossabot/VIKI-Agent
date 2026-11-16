/** Real bundle and scratch Team-pack vault; opening a seat makes its profile and Bot once. */
import type { Browser } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, TEST_TOKEN } from "../../harness";
import { ORIGIN, type Server, startServer } from "../../server";
import { navigateSidebar } from "../../sidebar-navigation";

let server: Server | undefined;
let browser: Browser | undefined;
const CHIEF_PROFILE_ID = "6a0d02f9-f8dc-4034-941d-03cedde6c839";

beforeAll(async () => {
  server = await startServer({
    bots: true,
    team: true,
    fixture: "tests/fixtures/claude/text-two-turns.jsonl",
  });
  browser = await launchEdge();
}, 60_000);
afterAll(async () => {
  await browser?.close();
  await server?.stop();
});

async function privateJson(path: string): Promise<Record<string, unknown>> {
  const response = await fetch(`${ORIGIN}${path}`, {
    headers: { authorization: `Bearer ${TEST_TOKEN}` },
  });
  if (!response.ok) throw new Error(`GET ${path} answered ${response.status}.`);
  return (await response.json()) as Record<string, unknown>;
}

async function profileIds(): Promise<string[]> {
  const listing = await privateJson("/api/v2/profiles?archived=false");
  return (listing.profiles as Array<{ profile_id: string }>).map((row) => row.profile_id);
}

async function bots(): Promise<Array<{ profile_id: string; chat_session_id: string }>> {
  const listing = await privateJson("/api/v2/bots");
  return listing.bots as Array<{ profile_id: string; chat_session_id: string }>;
}

test("opening the Chief seat makes one profile and one Bot, and a reopen reuses both", async () => {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  try {
    await page.goto(appUrl());
    await page.locator('[data-region="shell"]').waitFor();
    await navigateSidebar(page, "Bots");
    const roster = page.getByRole("group", { name: "Bot roster", exact: true });
    await roster.getByRole("button", { name: "Chief", exact: true }).click();
    const desk = page.getByRole("region", { name: "Selected Bot", exact: true });
    await desk.getByRole("heading", { name: "Chief", exact: true }).waitFor();
    await desk.getByText("Not started", { exact: true }).waitFor();
    expect(await profileIds()).not.toContain(CHIEF_PROFILE_ID);
    const open = desk.getByRole("button", { name: "Open conversation", exact: true });
    await expect.poll(() => open.isEnabled()).toBe(true);
    await open.click();
    await page.getByRole("region", { name: "Bot activity", exact: true }).waitFor();

    const selected = await page.evaluate(() => sessionStorage.getItem("viki.selected"));
    const made = await bots();
    expect(made).toHaveLength(1);
    expect(made[0]?.profile_id).toBe(CHIEF_PROFILE_ID);
    expect(made[0]?.chat_session_id).toBe(selected);
    expect((await profileIds()).filter((id) => id === CHIEF_PROFILE_ID)).toHaveLength(1);
    const session = await privateJson(`/api/v2/profile-sessions/${selected}`);
    expect(session.started).toBe(false);
    expect((await privateJson("/api/v2/routines")).routines).toEqual([]);
    await page.locator(".rail-team").getByRole("button", { name: "Chief", exact: true }).waitFor();

    await navigateSidebar(page, "Bots");
    await roster.getByRole("button", { name: "Chief", exact: true }).click();
    await page.getByRole("button", { name: "Open conversation", exact: true }).click();
    await page.getByRole("region", { name: "Bot activity", exact: true }).waitFor();
    expect(await page.evaluate(() => sessionStorage.getItem("viki.selected"))).toBe(selected);
    expect(await bots()).toHaveLength(1);
    expect((await profileIds()).filter((id) => id === CHIEF_PROFILE_ID)).toHaveLength(1);
  } finally {
    await page.close();
  }
});
