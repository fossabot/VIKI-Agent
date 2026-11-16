/**
 * Screenshots for a person to look at: the shell with the rails, the session list and the Ctrl+K
 * palette, rendered in real Edge against the Vite dev server with every REST route
 * answered from the synthetic data below (playwright's page.route; no backend, no vault, and
 * the socket stays down, as in the layout e2e). Run from frontend/:
 *
 *   npx vitest run --config vitest.shots.config.ts
 *
 * PNGs land in OUT (Playwright creates the folder).
 */
import type { Browser, Page } from "playwright-core";
import { createServer, type ViteDevServer } from "vite";
import { afterAll, beforeAll, expect, test } from "vitest";
import type { Rails, SearchHit, SessionRow } from "../src/protocol";
import { launchEdge } from "./harness";

const PORT = 5198;
const ORIGIN = `http://127.0.0.1:${PORT}`;
const OUT = "C:\\tmp\\viki-shots";

let server: ViteDevServer | undefined;
let browser: Browser | undefined;

const DAY = 24 * 60 * 60 * 1000;
const now = Date.now();
const at = (daysAgo: number, hour: number, minute: number): string => {
  const d = new Date(now - daysAgo * DAY);
  d.setHours(hour, minute, 0, 0);
  return d.toISOString();
};

const row = (
  id: string,
  title: string,
  modified: string,
  extra: Partial<SessionRow> = {},
): SessionRow => ({
  session_id: id,
  title,
  first_prompt: title,
  entrypoint: "viki-agent",
  modified,
  cli_version: "2.1.259",
  other: false,
  ...extra,
});

const sessions: SessionRow[] = [
  row("s1", "Weekly review draft", at(0, 18, 42)),
  row("s2", "Search palette taste round", at(0, 17, 20)),
  row("s3", "Reminder to buy milk", at(0, 9, 5)),
  row("s4", "Retro gate H1 decision", at(1, 21, 10)),
  row("s5", "Vault backup plan", at(1, 8, 30)),
  row("s6", "Paper: attention maps as evidence", at(5, 14, 0)),
  row("s7", "scratch", at(6, 10, 0), { entrypoint: "sdk-py", other: true }),
];

const rails: Rails = {
  agents: [
    {
      name: "researcher",
      description: "Web research and ingestion",
      tools: ["WebFetch", "WebSearch", "Write"],
      model: "opus",
      last_run: at(0, 9, 12),
    },
    {
      name: "librarian",
      description: "Files inbox items",
      tools: ["Read", "Write", "Edit"],
      model: "sonnet",
      last_run: at(1, 22, 40),
    },
    {
      name: "curator",
      description: "Nightly reconciliation",
      tools: ["Read", "Write", "Edit"],
      model: "opus",
      last_run: "",
    },
    {
      name: "connector",
      description: "Link discovery",
      tools: ["Read", "Edit"],
      model: "sonnet",
      last_run: at(7, 23, 5),
    },
  ],
  gates: [
    {
      file: "2026-09-10.md",
      id: "H1",
      item: "/recall skill",
      label: "medium",
      owner_applies: false,
    },
    {
      file: "2026-09-10.md",
      id: "H4",
      item: "CLAUDE.md containment line",
      label: "owner applies",
      owner_applies: true,
    },
    {
      file: "2026-09-03.md",
      id: "M2",
      item: "weekly review template",
      label: "low",
      owner_applies: false,
    },
  ],
  routines: [
    { when: "07:30", name: "/daily · morning brief", planned: true },
    { when: "22:00", name: "/inbox sweep", planned: true },
  ],
  counts: { knowledge: 116, sources: 202, inbox: 1 },
  git: {
    state: "known",
    ref_state: "branch",
    branch: "main",
    head: "c8868e6",
    committed_at: at(0, 19, 16),
    dirty: 3,
  },
};

const hits: SearchHit[] = [
  { session_id: "s3", title: "Reminder to buy milk", role: "title", snippet: "", at: "" },
  {
    session_id: "s2",
    title: "Search palette taste round",
    role: "user",
    snippet: "Put the milk example in the test",
    at: at(0, 17, 20),
  },
  {
    session_id: "s5",
    title: "Vault backup plan",
    role: "assistant",
    snippet: "Not milk: we were talking about backups. Every night at 22:00…",
    at: at(1, 8, 30),
  },
];

beforeAll(async () => {
  server = await createServer({
    server: { host: "127.0.0.1", port: PORT, strictPort: true },
    logLevel: "silent",
  });
  await server.listen();
  browser = await launchEdge();
});

afterAll(async () => {
  await browser?.close();
  await server?.close();
});

async function open(width: number, height = 900): Promise<Page> {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: { width, height } });
  await page.route("**/api/**", (route) => {
    const url = new URL(route.request().url());
    let body: unknown = null;
    if (url.pathname === "/api/rails") body = rails;
    else if (url.pathname === "/api/sessions")
      body = { sessions, state: "ready", reason: "", omitted: 0 };
    else if (url.pathname === "/api/search") {
      const q = url.searchParams.get("q") ?? "";
      body = {
        hits: q.includes("milk") ? hits : [],
        state: "ready",
        reason: "",
        scanned: 0,
        total: 0,
        request_id: "",
      };
    }
    if (body === null) return route.fulfill({ status: 404 });
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });
  await page.goto(`${ORIGIN}/#token=screenshots`);
  await page.locator('[data-region="shell"]').waitFor({ timeout: 10_000 });
  if (width >= 1280) {
    // the right rail (and its tiles) hides below 1280 px
    await page.locator(".tile").first().waitFor({ timeout: 10_000 });
  }
  await page.locator('[data-session-id="s1"]').waitFor({ timeout: 10_000 });
  await page.evaluate(() => document.fonts.ready);
  return page;
}

async function shot(page: Page, name: string): Promise<void> {
  await page.screenshot({ path: `${OUT}\\${name}.png` });
}

test("the shell, a selected session, the palette, and the 1200px layout", async () => {
  const page = await open(1440);
  await shot(page, "m3b-1-shell-1440");

  await page.locator('[data-session-id="s2"]').click();
  await expect(page.locator(".sess-selected").count()).resolves.toBe(1);
  await shot(page, "m3b-2-selected-1440");

  await page.keyboard.press("Control+k");
  await page.locator('[role="dialog"] input').waitFor({ timeout: 5_000 });
  await page.keyboard.type("milk");
  await page.locator(".hit").first().waitFor({ timeout: 5_000 });
  await page.keyboard.press("ArrowDown");
  await shot(page, "m3b-3-palette-1440");
  await page.keyboard.press("Escape");

  const narrow = await open(1200);
  await shot(narrow, "m3b-4-shell-1200");
});
