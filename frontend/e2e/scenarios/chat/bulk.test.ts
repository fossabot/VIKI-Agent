/**
 * e2e: a very large reply survives the whole path — the real WebSocket, the real reducer's
 * repeated string-append, and the real DOM — not just a single oversized frame.
 *
 * `tests/test_transport.py` already pins the transport's own hard limit on one frame (5 MiB);
 * that is a unit fact about `ClaudeTransport`'s framing and does not need a browser. What it
 * cannot show is whether many separate small envelopes streamed one at a time actually add up to
 * the right thing once they have gone through the WebSocket, `sessionReducer`'s `block.text +
 * text` append, and React's own re-render — a place a truncation, an off-by-one on a chunk
 * boundary, or a rendering slowdown could each lose or garble text without any single frame ever
 * being too big. `tests/fixtures/claude/bulk-reply.jsonl` uses `fake.repeat` so the fixture file
 * itself stays a few KB while the CLI still emits a ~2.1 MB reply frame by frame.
 *
 * CHUNK_LEN/COUNT use fewer, larger chunks than the ~1,500 × ~1.4 KB one might first pick: each
 * delta costs a frame, a reducer append and a render, so 100 × 21 KB reaches the same ~2.1 MB
 * total in well under a minute while still proving the same thing (many small frames, not one).
 *
 * The UI thread does not re-parse the reply per delta: the parse runs in a worker under a
 * deadline (`markdownParser.ts`), a streaming owner's newer text replaces its queued parse, and a
 * text over the 256 KiB size cap is never parsed at all. What grows with the reply is the
 * reducer's append and the DOM text itself, which is what this file is here to cover. (Asserting
 * a length has a trap: the renderer trims the reply's trailing whitespace, so the DOM holds one
 * character fewer than the CLI sent.)
 *
 * The test itself gets a generous timeout (see the `test(...)` call below): the length wait alone
 * has been observed to take anywhere from ~20s to ~1 minute run to run on the same machine (a
 * slower one, or one with an on-access scanner touching the repeated frame, would take longer
 * still), and that budget must fit inside whatever this file's own timeout is, not the suite's
 * default.
 */
import type { Browser, Page } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, send, waitForCount } from "../../harness";
import { PORT, type Server, startServer } from "../../server";

const CHUNK_LEN = 21_000;
const COUNT = 100;
const EXPECTED_LENGTH = CHUNK_LEN * COUNT;
const TEST_TIMEOUT_MS = 180_000;

let server: Server | undefined;
let browser: Browser | undefined;

beforeAll(async () => {
  server = await startServer({ fixture: "tests/fixtures/claude/bulk-reply.jsonl" });
  browser = await launchEdge();
}, 60_000);

afterAll(async () => {
  await browser?.close();
  await server?.stop();
});

test(
  "a ~2 MB streamed reply arrives whole, and the page stays responsive after it",
  async () => {
    if (!browser) throw new Error("Edge did not launch");
    const page: Page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    await page.goto(appUrl(PORT));
    await page.locator('[data-region="shell"]').waitFor({ timeout: 15_000 });

    await send(page, "please reply at length");
    await page.locator(".reply .md").first().waitFor({ timeout: 15_000 });
    // Length, not a substring: a truncation or a duplicated chunk both still "contain" any given
    // fragment, so only the exact character count actually proves nothing was lost or repeated.
    // The wait itself gets most of the test's own budget (see TEST_TIMEOUT_MS above) rather than
    // the vitest suite default, since this is the one step whose cost is machine-dependent.
    await page.waitForFunction(
      (want) => (document.querySelector(".reply .md")?.textContent?.length ?? 0) >= want,
      EXPECTED_LENGTH,
      { timeout: 150_000 },
    );
    const length = await page.evaluate(
      () => document.querySelector(".reply .md")?.textContent?.length ?? 0,
    );
    expect(length).toBe(EXPECTED_LENGTH);
    await waitForCount(page, ".composer-stop", 0, 15_000);

    // The huge reply did not wedge the tab: the composer still accepts a second turn.
    await send(page, "still there?");
    await waitForCount(page, ".user-turn", 2, 15_000);
    await page.waitForFunction(
      () => (document.querySelectorAll(".reply .md")[1]?.textContent ?? "") === "still here",
      undefined,
      { timeout: 15_000 },
    );
    await waitForCount(page, ".composer-stop", 0);

    await page.close();
  },
  TEST_TIMEOUT_MS,
);
