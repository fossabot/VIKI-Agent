/**
 * The narrow and zoomed layouts, for a person to look at: the shell at each tested size,
 * both rail drawers open, the palette at 400, and the top bar at 503 with a branch name long
 * enough to need its ellipsis. Real Edge against the Vite dev server with every REST route
 * answered from the synthetic data below (playwright's page.route; no backend, no vault, the
 * socket down) — the same harness as screenshots.shots.ts, plus a deviceScaleFactor so 720x450
 * at 2x can stand in for a 1440x900 window at 200% zoom. Run from frontend/:
 *
 *   npx vitest run --config vitest.shots.config.ts e2e/layout.shots.ts
 *
 * PNGs land in OUT (Playwright creates the folder). Nothing here binds 8765.
 */
import type { Browser, Page } from "playwright-core";
import { createServer, type ViteDevServer } from "vite";
import { afterAll, beforeAll, test } from "vitest";
import type { Rails, SearchResult, SessionRow, SessionsResult } from "../src/protocol";
import { launchEdge } from "./harness";

const PORT = 5196;
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

/** The listing as an observation (rows plus state, reason and omitted count): a complete read
 * of seven synthetic rows. Typed, so a change to the REST shape breaks this file at compile time
 * rather than mid-run. */
const sessionsResult: SessionsResult = {
  sessions: [
    row("s1", "Weekly review draft", at(0, 18, 42)),
    row("s2", "Search palette taste round", at(0, 17, 20)),
    row("s3", "Reminder to buy milk", at(0, 9, 5)),
    row("s4", "Retro gate H1 decision", at(1, 21, 10)),
    row("s5", "Vault backup plan", at(1, 8, 30)),
    row("s6", "Paper: attention maps as evidence", at(5, 14, 0)),
    row("s7", "scratch", at(6, 10, 0), { entrypoint: "sdk-py", other: true }),
  ],
  state: "ready",
  reason: "",
  omitted: 0,
};

const railsFor = (branch: string): Rails => ({
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
  ],
  routines: [
    { when: "07:30", name: "/daily · morning brief", planned: true },
    { when: "22:00", name: "/inbox sweep", planned: true },
  ],
  counts: { knowledge: 116, sources: 202, inbox: 1 },
  git: {
    state: "known",
    ref_state: "branch",
    branch,
    head: "c8868e6",
    committed_at: at(0, 19, 16),
    dirty: 3,
  },
});

/** 42 characters: what the chip's ellipsis is for. */
const LONG_BRANCH = "feature/narrow-layout-and-rail-drawers-e2e";

const searchFor = (q: string): SearchResult => {
  const hits = q.includes("milk")
    ? [
        {
          session_id: "s3",
          title: "Reminder to buy milk",
          role: "title" as const,
          snippet: "",
          at: "",
        },
        {
          session_id: "s2",
          title: "Search palette taste round",
          role: "user" as const,
          snippet: "Put the milk example in the test",
          at: at(0, 17, 20),
        },
      ]
    : [];
  // `total` is the sessions the listing managed to list, not the hits found in them
  // (state/catalog.ts): with a partial fixture the note would otherwise read "7 of 2 searched".
  return {
    hits,
    state: "ready",
    reason: "",
    scanned: sessionsResult.sessions.length,
    total: sessionsResult.sessions.length,
    request_id: "",
  };
};

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

interface OpenOptions {
  height?: number;
  deviceScaleFactor?: number;
  branch?: string;
}

async function open(width: number, options: OpenOptions = {}): Promise<Page> {
  if (!browser) throw new Error("Edge did not launch");
  const { height = 900, deviceScaleFactor, branch = "main" } = options;
  const rails = railsFor(branch);
  const page = await browser.newPage({
    viewport: { width, height },
    ...(deviceScaleFactor === undefined ? {} : { deviceScaleFactor }),
  });
  await page.route("**/api/**", (route) => {
    const url = new URL(route.request().url());
    let body: unknown = null;
    if (url.pathname === "/api/rails") body = rails;
    else if (url.pathname === "/api/sessions") body = sessionsResult;
    else if (url.pathname === "/api/search") body = searchFor(url.searchParams.get("q") ?? "");
    // The durable draft store, answered as an empty healthy one would answer. Without these two
    // the app's own /api/v2/drafts calls 404, the composer shows
    // "Can't save the draft. The text stays in this window." with a retry button, and every
    // measurement and every screenshot here is of a broken backend rather than of the layout.
    else if (url.pathname === "/api/v2/drafts") body = { drafts: [], next_after: null };
    else {
      const key = /^\/api\/v2\/drafts\/(.+)$/.exec(url.pathname)?.[1];
      if (key) body = { key, revision: 0, text: "", saved_at: null };
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
  // Wait for something REST has filled in that exists at every width. A session row would be the
  // obvious choice and is the wrong one: below 800 the left rail is not in the DOM at all, so
  // waiting for one times out before a single narrow picture is taken. The git chip is in the
  // top bar, which is in the DOM at every width; inside the sub-band the chip is hidden, so the
  // wait is for it being attached, not visible.
  await page.locator(".git-chip").waitFor({ timeout: 10_000, state: "attached" });
  await page.evaluate(() => document.fonts.ready);
  return page;
}

async function shot(page: Page, name: string): Promise<void> {
  await page.screenshot({ path: `${OUT}\\${name}.png` });
}

/** Open a rail drawer by the accessible name of its top-bar toggle, and wait for the panel. */
async function openDrawer(page: Page, label: string): Promise<void> {
  await page.getByRole("button", { name: label }).click();
  await page.getByRole("dialog", { name: label }).waitFor({ timeout: 5_000 });
  // The slide-in is 160ms; give it a frame past that so no shot catches the panel mid-travel.
  await page.locator(".rail-drawer").evaluate(
    (el) =>
      new Promise((resolve) => {
        el.addEventListener("animationend", resolve, { once: true });
        setTimeout(resolve, 400);
      }),
  );
}

test("the shell at every tested size", async () => {
  for (const [width, height] of [
    [1440, 900],
    [1200, 900],
    [1024, 768],
    [800, 600],
    [400, 900],
  ]) {
    const page = await open(width as number, { height: height as number });
    await shot(page, `e2-shell-${width}`);
    await page.close();
  }
});

test("the VIKI panel as a drawer, at each width where the right rail cannot dock", async () => {
  for (const [width, height] of [
    [1200, 900],
    [1024, 768],
    [800, 600],
    [400, 900],
  ]) {
    const page = await open(width as number, { height: height as number });
    await openDrawer(page, "VIKI panel");
    await shot(page, `e2-right-drawer-${width}`);
    await page.close();
  }
});

test("the sessions drawer and the palette at 400", async () => {
  const page = await open(400);
  await openDrawer(page, "Sessions and agents");
  await shot(page, "e2-left-drawer-400");
  await page.keyboard.press("Escape");
  await page.getByRole("dialog", { name: "Sessions and agents" }).waitFor({ state: "detached" });

  await page.keyboard.press("Control+k");
  await page.locator('[role="dialog"] input').waitFor({ timeout: 5_000 });
  await page.keyboard.type("milk");
  await page.locator(".hit").first().waitFor({ timeout: 5_000 });
  await shot(page, "e2-palette-400");
  await page.close();
});

test("720x450 at device scale 2, which is 1440x900 zoomed to 200%", async () => {
  const page = await open(720, { height: 450, deviceScaleFactor: 2 });
  await shot(page, "e2-shell-720x2");
  await openDrawer(page, "Sessions and agents");
  await shot(page, "e2-left-drawer-720x2");
  await page.close();
});

test("the top bar at 503 with a branch name longer than the chip", async () => {
  const page = await open(503, { branch: LONG_BRANCH });
  await shot(page, "e2-topbar-long-branch-503");
  await page.close();
});
