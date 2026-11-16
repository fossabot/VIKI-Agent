/**
 * The depth bound and the worker parse, in a real browser.
 *
 * `markdownTree.unit.test.ts` and `markdownParser.unit.test.ts` already prove the depth bound and
 * the worker lifecycle against fakes; this proves the two are wired together the way the app ships
 * them — a real Worker, spawned from the real `Markdown.tsx`, parsing text that streamed in over
 * the real WebSocket. `tests/fixtures/claude/markdown-depth.jsonl` (synthetic; hostile-reply.jsonl's
 * frame shapes) streams three replies: one already past the depth bound in a single delta, one
 * built past it 1,800 deltas at a time (`fake.repeat`, so the accumulated text — not any one
 * delta — is what crosses the bound), and one ordinary reply, so the same page also shows the
 * worker parse still renders real structure when there is nothing to bound.
 */
import type { Browser, Page } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, send } from "../../harness";
import { PORT, type Server, startServer } from "../../server";

const LINE1 = `${"- ".repeat(1000)}a`;
const LINE2 = `${"> ".repeat(1800)}a`;

let server: Server | undefined;
let browser: Browser | undefined;

beforeAll(async () => {
  server = await startServer({ fixture: "tests/fixtures/claude/markdown-depth.jsonl" });
  browser = await launchEdge();
}, 60_000);

afterAll(async () => {
  await browser?.close();
  await server?.stop();
});

test("markdown past the depth bound renders plain, in a real Edge window, without crashing", async () => {
  if (!browser) throw new Error("Edge did not launch");
  const page: Page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  let crashed = false;
  page.on("crash", () => {
    crashed = true;
  });
  // The synchronous fallback would pass every check below on its own: only a worker spawned from
  // the parse's own chunk shows the parse left the UI thread.
  const workerUrls: string[] = [];
  page.on("worker", (worker) => {
    workerUrls.push(worker.url());
  });
  await page.goto(appUrl(PORT));
  await page.locator('[data-region="shell"]').waitFor({ timeout: 15_000 });

  // Turn 1: one delta already past the bound. The wait keys on the settled reason so it is
  // never true before the depth-exceeded parse result has landed.
  await send(page, "depth 1");
  await page.waitForFunction(
    ([want]: [string]) =>
      (document.querySelector('.reply .md-plain[data-reason="depth"]')?.textContent ?? "") === want,
    [LINE1] as [string],
    { timeout: 15_000 },
  );
  expect(await page.locator(".reply .md ul").count()).toBe(0);
  expect(crashed).toBe(false);

  // Turn 2: built past the bound 1,800 deltas at a time; only the accumulated text crosses it.
  await send(page, "depth 2");
  await page.waitForFunction(
    ([want]: [string]) =>
      (document.querySelectorAll('.reply .md-plain[data-reason="depth"]')[1]?.textContent ?? "") ===
      want,
    [LINE2] as [string],
    { timeout: 15_000 },
  );
  expect(crashed).toBe(false);

  // Turn 3: an ordinary reply. Structure, not text: the worker parse still renders a real tree.
  await send(page, "depth 3");
  await page.waitForFunction(
    () => {
      const strongs = [...document.querySelectorAll(".reply .md strong")];
      const third = document.querySelectorAll(".reply .md")[2];
      return (
        strongs.some((node) => node.textContent === "b") && (third?.textContent ?? "").includes("©")
      );
    },
    undefined,
    { timeout: 15_000 },
  );
  expect(crashed).toBe(false);
  expect(workerUrls.some((url) => url.includes("markdown.worker"))).toBe(true);

  // The worker answers in order, so turns 1 and 2 hold their settled answers by now: still plain.
  expect(await page.locator(".reply .md ul").count()).toBe(0);
  const plain = await page.evaluate(() =>
    [...document.querySelectorAll(".reply .md-plain")].map((node) => node.textContent),
  );
  expect(plain).toEqual([LINE1, LINE2]);

  await page.close();
});
