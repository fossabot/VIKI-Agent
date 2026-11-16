/**
 * e2e: vault text is data, never markup. Every surface fed from the vault (session rows, the
 * agents rail, the "Awaiting decision" rows, routines, the git chip and the Ctrl+K palette) is
 * rendered in real Edge with hostile strings in every field, and the page must show them as
 * text: no script runs, no anchor is built from them, and nothing is fetched off 127.0.0.1.
 *
 * The strings are the shapes a compromised or careless vault could carry — a note title
 * pasted from a web page, a retro row quoting an HTML snippet, a branch name someone typed.
 * The REST routes are answered by playwright (page.route), as in the layout and screenshot
 * runs; no backend and no vault are involved.
 */
import type { Browser, Page } from "playwright-core";
import { createServer, type ViteDevServer } from "vite";
import { afterAll, beforeAll, expect, test } from "vitest";
import type { Rails, SearchHit, SessionRow } from "../../../src/protocol";
import { capabilitiesFixture } from "../../../src/testShell";
import { launchEdge } from "../../harness";
import { navigateSidebar } from "../../sidebar-navigation";

const PORT = 5197;
const ORIGIN = `http://127.0.0.1:${PORT}`;
/** Any request to this host means an injected tag was parsed as markup. */
const EVIL = "https://evil.example";

let server: ViteDevServer | undefined;
let browser: Browser | undefined;

const IMG = `<img src="${EVIL}/p.png" onerror="window.__pwned=1">`;
const SCRIPT = `<script>window.__pwned=1</script>`;
const SVG = `"><svg onload="window.__pwned=1"></svg>`;
const JS_URL = "javascript:window.__pwned=1";
/** The header's shared logo is an admitted image with a versioned same-origin URL. */
const BRAND_MARK = /^\/brand\/mark\.png\?v=[0-9a-f]{8}$/;

const now = new Date("2026-09-12T12:00:00.000Z").toISOString();

const sessions: SessionRow[] = [
  {
    session_id: "s1",
    title: IMG,
    first_prompt: SCRIPT,
    entrypoint: "viki-agent",
    modified: now,
    cli_version: SVG,
    other: false,
  },
  {
    session_id: "s2",
    title: "",
    first_prompt: JS_URL,
    entrypoint: "claude-vscode",
    modified: now,
    cli_version: "2.1.259",
    other: false,
  },
];

const rails: Rails = {
  agents: [
    { name: IMG, description: SCRIPT, tools: [SVG], model: JS_URL, last_run: now },
    { name: "librarian", description: SVG, tools: [], model: "sonnet", last_run: "" },
  ],
  gates: [
    { file: SCRIPT, id: SVG, item: IMG, label: JS_URL, owner_applies: false },
    { file: "2026-09-10.md", id: "H2", item: SVG, label: "owner applies", owner_applies: true },
  ],
  routines: [{ when: SVG, name: IMG, planned: true }],
  counts: { knowledge: 1, sources: 2, inbox: 3 },
  git: {
    state: "known",
    ref_state: "branch",
    branch: IMG,
    head: SCRIPT,
    committed_at: now,
    dirty: 1,
  },
};

/** `/api/v2/routines` with a markup-shaped name in every row: the shipped rail and view render
 * this list, not the planned rows above. */
const ROUTINE_NAMES = [IMG, SCRIPT, SVG];
const routines = {
  routines: ROUTINE_NAMES.map((name, index) => ({
    routine_id: `00000000-0000-4000-8000-00000000000${index}`,
    name,
    state: index === 2 ? "draft" : "enabled",
    current_revision: 1,
    state_revision: 1,
    schedule: { kind: "daily", timezone: "UTC" },
    next_run: index === 2 ? null : new Date(Date.now() + (index + 1) * 3_600_000).toISOString(),
    last_run: null,
    active_run: null,
  })),
  scheduler: { state: "running", attended: true, paused: false },
};

const hits: SearchHit[] = [
  { session_id: "s1", title: IMG, role: "title", snippet: "", at: "" },
  { session_id: "s2", title: SCRIPT, role: "user", snippet: SVG, at: now },
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

async function open(): Promise<{ page: Page; offOrigin: string[] }> {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const offOrigin: string[] = [];
  page.on("request", (request) => {
    if (!request.url().startsWith(ORIGIN)) offOrigin.push(request.url());
  });
  await page.route("**/api/**", (route) => {
    const url = new URL(route.request().url());
    let body: unknown = null;
    if (url.pathname === "/api/sessions")
      body = { sessions, state: "ready", reason: "", omitted: 0 };
    else if (url.pathname === "/api/rails") body = rails;
    else if (url.pathname === "/api/v2/capabilities")
      body = capabilitiesFixture({ workspace: { shared_scratch: false } });
    else if (url.pathname === "/api/v2/routines") body = routines;
    else if (url.pathname === "/api/search")
      body = { hits, state: "ready", reason: "", scanned: 0, total: 0, request_id: "" };
    if (body === null) return route.fulfill({ status: 404 });
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });
  await page.goto(`${ORIGIN}/#token=hostile`);
  await page.locator('[data-region="shell"]').waitFor({ timeout: 10_000 });
  await page.locator('[data-session-id="s1"]').waitFor({ timeout: 10_000 });
  await page.locator(".tile").first().waitFor({ timeout: 10_000 });
  return { page, offOrigin };
}

test("hostile vault text renders as text: no script, no anchor, no off-origin request", async () => {
  const { page, offOrigin } = await open();

  // The palette shows the same strings through a third route.
  await page.keyboard.press("Control+k");
  await page.locator('[role="dialog"] input').waitFor({ timeout: 5_000 });
  await page.keyboard.type("x");
  await page.locator(".hit").first().waitFor({ timeout: 5_000 });

  expect(await page.evaluate(() => (window as { __pwned?: number }).__pwned)).toBeUndefined();
  // Nothing injected became an element: the payloads are text nodes, so no tag exists for them.
  // The page's images are same-origin: the two static brand marks (TopBar.tsx).
  const imgSrcs = await page
    .locator("img")
    .evaluateAll((nodes) =>
      nodes.map((node) => (node as HTMLImageElement).getAttribute("src") ?? ""),
    );
  expect(imgSrcs.filter((src) => !BRAND_MARK.test(src))).toEqual([]);
  expect(await page.locator("svg[onload]").count()).toBe(0);
  expect(await page.locator("script:not([type='module']):not([src])").count()).toBe(0);
  const hrefs = await page
    .locator("a[href]")
    .evaluateAll((nodes) =>
      nodes.map((node) => (node as HTMLAnchorElement).getAttribute("href") ?? ""),
    );
  expect(hrefs.filter((href) => href.toLowerCase().startsWith("javascript:"))).toEqual([]);
  expect(offOrigin).toEqual([]);

  // ... and the user still sees what the vault says, verbatim.
  const text = (await page.locator('[data-region="shell"]').textContent()) ?? "";
  expect(text).toContain(IMG);
  expect(text).toContain(SCRIPT);
  await page.close();
});

test("routine names render as text in the rail and in the Routines view", async () => {
  const { page, offOrigin } = await open();

  const railNames = page.locator(".rail-section--routines .routines-rail-name");
  await railNames.first().waitFor({ timeout: 10_000 });
  expect((await railNames.allTextContents()).sort()).toEqual([...ROUTINE_NAMES].sort());
  // A name is a text node: no element sits inside it, so the payloads built nothing.
  expect(await page.locator(".rail-section--routines .routines-rail-name *").count()).toBe(0);

  await navigateSidebar(page, "Routines");
  const rowNames = page.locator(".routines-row-name");
  await rowNames.first().waitFor({ timeout: 10_000 });
  expect((await rowNames.allTextContents()).sort()).toEqual([...ROUTINE_NAMES].sort());
  expect(await page.locator(".routines-row-name *").count()).toBe(0);

  expect(await page.evaluate(() => (window as { __pwned?: number }).__pwned)).toBeUndefined();
  const imgSrcs = await page
    .locator("img")
    .evaluateAll((nodes) =>
      nodes.map((node) => (node as HTMLImageElement).getAttribute("src") ?? ""),
    );
  expect(imgSrcs.filter((src) => !BRAND_MARK.test(src))).toEqual([]);
  expect(await page.locator("svg[onload]").count()).toBe(0);
  expect(await page.locator("script:not([type='module']):not([src])").count()).toBe(0);
  expect(offOrigin).toEqual([]);
  await page.close();
});
