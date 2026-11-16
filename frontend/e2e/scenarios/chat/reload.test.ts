/**
 * e2e: losing the connection mid-stream loses nothing. `ReconnectBanner`'s own docstring
 * states the guarantee this test exists to check: "a reply that was streaming does not stop
 * arriving because the page lost the socket — the actor keeps its seq log and replays from
 * after_seq on reconnect." That is a claim about three real things staying in step across a real
 * network interruption — the browser's own reconnect backoff (`socket.ts`), the server's
 * `SessionActor` staying alive and logging while nobody is listening, and `session.open{after_seq}`
 * replaying exactly the gap — and no unit test doubles all three at once the way a real dropped
 * WebSocket against the real server does.
 *
 * For that last part to mean anything, the drop has to open an actual gap: something the CLI
 * emits while the socket is down, that only `after_seq` replay (not the reducer's own dedupe of a
 * frame it already applied) can be the reason the page ends up with. `reconnect-mid-stream.jsonl`
 * paces its numbered deltas 150ms apart with `fake.sleep`, which the replay engine runs on the
 * server's real event loop independently of whether any browser is attached — the CLI keeps
 * counting whether or not the page is listening. The socket is closed right after "n03", well
 * before the CLI is done, so several more deltas land purely server-side before the client's own
 * reconnect (`socket.ts`'s default first retry is 500ms) picks the stream back up.
 *
 * This test drops the socket rather than calling `page.reload()`. That is a choice, not a
 * limitation. The left rail is `GET /api/sessions`, which is `Catalog.sessions()` reading
 * `.jsonl` transcripts off disk, files `tests/fake_claude.py` never writes (a gap
 * `e2e/scenarios/chat/stream.test.ts` already names as catalog's own seam), so a session started
 * under the fake CLI has no row to click. That does not leave a reloaded page with nowhere to
 * go: `App.tsx` remembers the selected id in sessionStorage and `viki.ts` re-opens every id
 * `vault.sessions` reports live, so the page comes back to its session without any row being
 * involved — and `e2e/scenarios/chat/approval.test.ts`'s recorded-allow test reloads mid-turn in
 * this harness and asserts the reply carries on to completion afterwards.
 *
 * The real-reload path is therefore covered, where it belongs. What no reload test can reach is
 * this case: a socket that DROPS mid-stream. `SocketClient`'s `dial()` re-sends
 * `session.open{after_seq}` for everything in `this.tracked` on each fresh connection, and
 * because forcing the live socket closed is not a navigation, React's own state — the tracked
 * set, the turns already applied — survives untouched. That is what makes the server's
 * `after_seq` replay, rather than a from-scratch history load, the thing actually under test
 * here; a reload throws exactly that state away and cannot show it. Adding a second real-reload
 * case to this file would re-run `e2e/scenarios/chat/approval.test.ts`'s recorded-allow reload against another
 * fixture and leave the only gap this file covers exactly where it is, so it is deliberately
 * not here.
 */
import type { Browser, Page } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, send, textOf, waitForCount, waitForText } from "../../harness";
import { PORT, type Server, startServer } from "../../server";

let server: Server | undefined;
let browser: Browser | undefined;

beforeAll(async () => {
  server = await startServer({ fixture: "tests/fixtures/claude/reconnect-mid-stream.jsonl" });
  browser = await launchEdge();
}, 60_000);

afterAll(async () => {
  await browser?.close();
  await server?.stop();
});

/** Keeps a handle to the page's own WebSocket instance on `window`, from before the app's first
 * connect, so the test can force it closed the way a dropped network would — without touching
 * any app code, since nothing in the app itself ever exposes the socket. */
async function exposeSocket(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const Native = window.WebSocket;
    class Tapped extends Native {
      constructor(url: string | URL, protocols?: string | string[]) {
        super(url, protocols);
        (window as unknown as { __socket?: WebSocket }).__socket = this;
      }
    }
    window.WebSocket = Tapped as unknown as typeof WebSocket;
  });
}

test("a lost connection mid-stream reconnects, replays what was missed, and Stop still works", async () => {
  if (!browser) throw new Error("Edge did not launch");
  const page: Page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await exposeSocket(page);
  await page.goto(appUrl(PORT));
  await page.locator('[data-region="shell"]').waitFor({ timeout: 15_000 });

  await send(page, "count for me");
  await waitForText(page, ".user-turn", "count for me");
  // Close the socket after only the first few numbers, well before the fixture's own pacing has
  // finished — the deltas still to come are the gap this test is actually about.
  await waitForText(page, ".reply .md", "n03", 15_000);
  const before = await textOf(page, ".reply .md");

  // Simulate the network dropping the socket out from under the page — not a navigation, so
  // React's own state (the tracked session, the turns already applied) survives exactly as it
  // would across a real blip, which is what makes `after_seq` replay (rather than a from-scratch
  // history load) the thing actually being exercised here.
  await page.evaluate(() => (window as unknown as { __socket?: WebSocket }).__socket?.close());

  await waitForText(page, ".banner", "Disconnected", 10_000);
  // The reconnect is the socket's own backoff (500ms by default), not anything this test drives.
  await waitForCount(page, ".banner", 0, 10_000);

  // The fixture keeps counting up to "n10" whether or not anyone is connected; seeing it land
  // is the proof that whatever the CLI emitted during the disconnect actually reached the page —
  // either replayed on reconnect or streamed live afterwards, which `after_seq` makes seamless
  // either way.
  await waitForText(page, ".reply .md", "n10", 20_000);
  const after = await textOf(page, ".reply .md");

  // Grew past the pre-drop snapshot (the gap was not silently dropped) and the pre-drop text is
  // an exact, untouched prefix of it (nothing before the drop was rewritten or reordered), and
  // nothing from the gap window shows up twice (no duplicate replay of an already-applied delta).
  expect(after.length).toBeGreaterThan(before.length);
  expect(after.startsWith(before)).toBe(true);
  expect(after.match(/n05/g)?.length ?? 0).toBe(1);

  // The turn the drop happened in the middle of is still the live one: Stop still reaches it.
  const stop = page.getByLabel("Stop");
  await stop.waitFor({ timeout: 5_000 });
  await stop.click();
  await waitForText(page, ".reply-cancelled", "Stopped", 15_000);
  await waitForCount(page, ".composer-stop", 0);

  // Rule out the layered stop's last-resort kill (see e2e/scenarios/chat/stop.test.ts): that path also ends
  // "cancelled" but additionally reports a crash. No such card means the interrupt was actually
  // acknowledged across the reconnect, not just before it.
  expect(await page.locator(".card, .error-card").count()).toBe(0);

  await page.close();
});
