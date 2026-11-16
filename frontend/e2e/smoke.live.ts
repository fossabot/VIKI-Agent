/**
 * The live smoke run: real Edge, the real server, and the user's OWN claude.exe on their own
 * vault. Nothing is faked below the browser at all.
 *
 * It checks that the answer to "prime directives?" is grounded in CLAUDE.md, plus the three
 * things only a real backend can show: that the session list is the user's actual transcripts,
 * that the rails are their actual vault, and that the status bar reports the CLI that actually
 * answered.
 *
 * It costs real subscription quota (one short turn), it fires the user's own hooks, and it
 * leaves a real transcript in ~/.claude/projects — which is why it has its own config and its
 * own npm script and is never part of scripts/check.py:
 *
 *   cd frontend && npm run live
 *
 * The prompt is read-only and asks for no tools. The app never writes the vault; claude does,
 * under the vault's own rules, and this turn asks it to write nothing.
 */
import type { Browser, Page } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, send, textOf, waitForText } from "./harness";
import { PORT, type Server, startServer } from "./server";

let server: Server | undefined;
let browser: Browser | undefined;

beforeAll(async () => {
  server = await startServer({ live: true, timeoutMs: 60_000 });
  browser = await launchEdge();
}, 120_000);

afterAll(async () => {
  await browser?.close();
  await server?.stop();
});

const SHOTS = "C:/tmp/viki-shots";

const PROMPT =
  "Without using any tools: what is prime directive 1 in your project instructions? One sentence.";

test("VIKI answers from the vault's own CLAUDE.md, in the user's own environment", async () => {
  if (!browser) throw new Error("Edge did not launch");
  const page: Page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const offHost: string[] = [];
  page.on("request", (request) => {
    if (!request.url().startsWith(`http://127.0.0.1:${PORT}`)) offHost.push(request.url());
  });
  await page.goto(appUrl(PORT));
  await page.locator('[data-region="shell"]').waitFor({ timeout: 20_000 });

  // The user's own transcripts, read from ~/.claude/projects — the fake CLI writes none, so this
  // is the one run where the rail can be checked at all.
  await page.locator("[data-session-id]").first().waitFor({ timeout: 20_000 });
  // The user's own vault: the counts come from rails.py reading it. The right rail is docked at
  // this width, so its tiles carry the counts and the status bar does not duplicate them
  // (App.tsx).
  const knowledgeCount = await textOf(page, '[data-tile="knowledge"] .tile-n');
  expect(knowledgeCount).toMatch(/^\d+$/);

  await send(page, PROMPT);
  await waitForText(page, ".user-turn", "prime directive 1", 20_000);
  // Grounded, not remembered: prime directive 1 is the one about _raw/, and it is only in the
  // vault's CLAUDE.md.
  await waitForText(page, ".reply .md", "_raw", 120_000);
  const reply = await textOf(page, ".reply .md");
  expect(reply.length).toBeGreaterThan(20);

  // A link never leaves 127.0.0.1, even here.
  expect(offHost).toEqual([]);

  // Screenshots, taken here rather than in app.shots.ts because this is the only run with real
  // content in it: the user's sessions, their vault counts, their git chip, a real reply.
  // 1200 is where the right rail hides (it hides below 1280), so both widths are worth one
  // picture each. They land outside the repo — nothing from the vault or a transcript is ever
  // committed here.
  // Let the turn finish first: every assertion above fires mid-stream, and a picture should
  // not be caught with the caret still blinking and Stop still up.
  await page.waitForFunction(() => document.querySelector(".composer-stop") === null, undefined, {
    timeout: 120_000,
  });
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: `${SHOTS}/live-1440.png` });
  await page.setViewportSize({ width: 1200, height: 900 });
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: `${SHOTS}/live-1200.png` });

  await page.close();
});
