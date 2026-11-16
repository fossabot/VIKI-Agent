/**
 * Screenshots of the finished app for a person to look at: the conversation itself, driven by
 * the real server against the fake CLI, at the design's 1440×900, at 1200×900 where the right rail
 * becomes a drawer, and at 400×900 and at 720×450 on a 2× screen (both rails are drawers there),
 * which is what a 1440×900 window reports when Edge is zoomed to 200%. Run from frontend/:
 *
 *   npx vitest run --config vitest.shots.config.ts e2e/app.shots.ts
 *
 * screenshots.shots.ts already covers the rails, the session list and the Ctrl+K palette against
 * mocked REST with the socket down — everything that needs no conversation. It cannot show one:
 * with no socket there are no turns. This is the other half, and it needs the real backend for
 * the same reason the end-to-end tests do.
 *
 * PNGs land in OUT. Never part of scripts/check.py.
 */
import type { Browser, Page } from "playwright-core";
import { afterAll, beforeAll, test } from "vitest";
import { appUrl, launchEdge, send } from "./harness";
import { PORT, type Server, startServer } from "./server";

const OUT = "C:\\tmp\\viki-shots";

let browser: Browser | undefined;

beforeAll(async () => {
  browser = await launchEdge();
}, 60_000);

afterAll(async () => {
  await browser?.close();
});

interface OpenOptions {
  height?: number;
  /** 2 turns 720×450 into the CSS geometry of a 1440×900 window at 200% zoom. */
  deviceScaleFactor?: number;
}

async function open(width: number, options: OpenOptions = {}): Promise<Page> {
  if (!browser) throw new Error("Edge did not launch");
  const { height = 900, deviceScaleFactor } = options;
  const page = await browser.newPage({
    viewport: { width, height },
    ...(deviceScaleFactor === undefined ? {} : { deviceScaleFactor }),
  });
  await page.goto(appUrl(PORT));
  await page.locator('[data-region="shell"]').waitFor({ timeout: 15_000 });
  await page.evaluate(() => document.fonts.ready);
  return page;
}

async function shot(page: Page, name: string): Promise<void> {
  await page.screenshot({ path: `${OUT}\\${name}.png` });
}

let bundleChecked = false;

/**
 * The bundle the server is serving must be the one that draws today's layout, not whatever
 * `frontend/dist` a previous build left behind — `scripts/e2e_server.py` serves `dist`
 * (`__main__.STATIC_DIR`), never `src`, and nothing rebuilds it before a direct run of this file.
 * A stale bundle would still screenshot, just the wrong layout, with nothing here to say so.
 * Mirrors e2e/scenarios/chat/approval.test.ts's own `assertServedBundleIsCurrent`; checked once, since every fixture
 * here is served from the same on-disk `dist`.
 */
async function assertServedBundleIsCurrent(): Promise<void> {
  const origin = `http://127.0.0.1:${PORT}`;
  const index = await (await fetch(`${origin}/`)).text();
  const src = /src="([^"]+\.js)"/.exec(index)?.[1];
  const bundle = src ? await (await fetch(`${origin}${src}`)).text() : "";
  // A class name literal survives minification whole (plain CSS, no class renaming): the
  // narrow-layout drawer this file's 400px shots depend on.
  if (!bundle.includes("rail-drawer-backdrop"))
    throw new Error(
      `the served bundle (${src ?? "missing"}) predates the narrow layout — ` +
        "run `npm --prefix frontend run build` before this file, or run the full check",
    );
}

/** One server for one fixture; every shot inside runs against it. */
async function withServer(
  fixture: string,
  body: () => Promise<void>,
  options: { dirty?: boolean } = {},
): Promise<void> {
  const server: Server = await startServer({ fixture, ...options });
  try {
    if (!bundleChecked) {
      bundleChecked = true;
      await assertServedBundleIsCurrent();
    }
    await body();
  } finally {
    await server.stop();
  }
}

test("a conversation with tool rows and a subagent, at both widths", async () => {
  await withServer("tests/fixtures/claude/subagent-explore.jsonl", async () => {
    for (const width of [1440, 1200]) {
      const page = await open(width);
      await send(page, "Which file in _templates/ is the daily note template?");
      await page.locator(".reply .md").first().waitFor({ timeout: 30_000 });
      await page.waitForFunction(
        () => document.querySelector(".composer-stop") === null,
        undefined,
        {
          timeout: 30_000,
        },
      );
      await shot(page, `app-conversation-${width}`);
      await page.close();
    }
  });
}, 240_000);

test("a permission card in the conversation", async () => {
  await withServer("tests/fixtures/claude/webfetch-denied.jsonl", async () => {
    const page = await open(1440);
    await send(page, "Read example.com and tell me its title.");
    await page.locator(".card").waitFor({ timeout: 30_000 });
    await shot(page, "app-permission-card-1440");
    await page.close();
  });
}, 180_000);

test("a question card", async () => {
  await withServer("tests/fixtures/claude/ask-user-question.jsonl", async () => {
    const page = await open(1440);
    await send(page, "Tea or coffee?");
    await page.locator(".card").waitFor({ timeout: 30_000 });
    await shot(page, "app-question-card-1440");
    await page.close();
  });
}, 180_000);

/** Wait until the turn is over — the Stop pill is the only thing on screen that says it runs. */
async function settled(page: Page): Promise<void> {
  await page.waitForFunction(() => document.querySelector(".composer-stop") === null, undefined, {
    timeout: 30_000,
  });
}

test("the narrow and zoomed conversation, and the composer while a turn runs", async () => {
  // The narrow layout's evidence: the same transcript the 1440 shot above draws, in the window
  // the user gets at 400 CSS pixels and at 200% zoom.
  //
  // The running shot needs its own fixture: the real server finishes subagent-explore.jsonl in
  // well under a second, so `.composer-stop` is already gone before this assertion could ever
  // see it. interrupt-mid-stream.jsonl holds the turn open until Stop is pressed instead.
  await withServer("tests/fixtures/claude/interrupt-mid-stream.jsonl", async () => {
    const narrow = await open(400);
    await send(narrow, "count for me");
    await narrow.locator(".composer-stop").waitFor({ timeout: 30_000 });
    await shot(narrow, "app-running-400");
    await narrow.getByLabel("Stop").click();
    await settled(narrow);
    await narrow.close();
  });

  // The at-rest shots need the skill-reading reply subagent-explore.jsonl gives; a stopped turn
  // ends cancelled, with no `.reply .md` for these to wait on.
  await withServer("tests/fixtures/claude/subagent-explore.jsonl", async () => {
    const narrow = await open(400);
    await send(narrow, "Which file in _templates/ is the daily note template?");
    await narrow.locator(".reply .md").first().waitFor({ timeout: 30_000 });
    await settled(narrow);
    await shot(narrow, "app-conversation-400");
    await narrow.close();

    const zoomed = await open(720, { height: 450, deviceScaleFactor: 2 });
    await send(zoomed, "Which file in _templates/ is the daily note template?");
    await zoomed.locator(".reply .md").first().waitFor({ timeout: 30_000 });
    await settled(zoomed);
    await shot(zoomed, "app-conversation-720x2");
    await zoomed.close();
  });
}, 300_000);

test("a permission card in the narrow window", async () => {
  await withServer("tests/fixtures/claude/webfetch-denied.jsonl", async () => {
    const page = await open(400);
    await send(page, "Read example.com and tell me its title.");
    await page.locator(".card").waitFor({ timeout: 30_000 });
    await shot(page, "app-permission-card-400");
    await page.close();
  });
}, 180_000);

test("the Write permission card and its question", async () => {
  await withServer("tests/fixtures/claude/write-permission.jsonl", async () => {
    const page = await open(1440);
    await send(page, "Create a new note in the knowledge base.");
    await page.locator(".card").waitFor({ timeout: 30_000 });
    await shot(page, "app-permission-write-1440");
    await page.close();
  });
}, 180_000);

test("the plan card, with its chip and note", async () => {
  await withServer("tests/fixtures/claude/plan-mode-exit.jsonl", async () => {
    const page = await open(1440);
    await send(
      page,
      "Make a two-step plan for listing the files in _templates/ (do not list them yet), then call ExitPlanMode with the plan.",
    );
    await page.locator(".card").waitFor({ timeout: 60_000 });
    await shot(page, "app-plan-card-1440");
    await page.close();
  });
}, 240_000);

test("the card after a stop that had to kill the process, at both widths", async () => {
  // The fixture never acknowledges the interrupt, so the stop ladder runs to its last rung and
  // the driver kills the tree; the card is MSG_STOP_KILLED (forced_stop). The wait is long
  // because the rung's own deadline is the production control_timeout_s.
  await withServer("tests/fixtures/claude/stop-unacknowledged.jsonl", async () => {
    for (const width of [1440, 400]) {
      const page = await open(width);
      await send(page, "Write a long report. Tell me when it's done.");
      const stop = page.getByLabel("Stop");
      await stop.waitFor({ timeout: 30_000 });
      await stop.click();
      await page.locator(".error-card").waitFor({ timeout: 30_000 });
      await shot(page, `app-error-forced-stop-${width}`);
      await page.close();
    }
  });
}, 300_000);

test("the card after the process ended on its own", async () => {
  await withServer("tests/fixtures/claude/exit-mid-turn.jsonl", async () => {
    const page = await open(1440);
    await send(page, "Tidy up the notes in my inbox.");
    await page.locator(".error-card").waitFor({ timeout: 30_000 });
    await shot(page, "app-error-crashed-1440");
    await page.close();
  });
}, 180_000);
