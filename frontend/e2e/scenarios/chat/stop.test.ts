/**
 * e2e: Stop is a real control frame, not a local UI reset, and the session survives it.
 *
 * A unit test can stub the driver and prove the component calls `onStop`; it cannot prove that
 * clicking the pill actually reaches the CLI's stdin, that the CLI's own acknowledgement is what
 * ends the turn (rather than the browser just pretending it did), or that the same session keeps
 * working afterwards. Those three facts only exist once a real WebSocket, a real ClaudeTransport
 * and a real (scripted) child process are all in the loop together — which is what this harness
 * is for. `interrupt-mid-stream.jsonl`'s own script is built for exactly this: it streams a long
 * partial reply and then blocks, waiting for the interrupt control frame before it ever sends a
 * result, so a green run here means the click produced that frame and nothing else did.
 *
 * One thing "Stopped" alone does not distinguish: driver.py's stop is layered, and the last
 * layer (an unacknowledged interrupt) kills the process tree outright and reports the very same
 * "cancelled" status plus a `.error-card` (MSG_STOP_KILLED). So the cancelled line by itself is
 * consistent with a broken interrupt that only ever falls through to the kill. What actually
 * discriminates a real acknowledged interrupt from that fallback is the absence of the kill's own
 * error card, and — more convincingly — that the *same* CLI process goes on to take a second
 * turn: a killed process cannot do that, so that second turn is where the real proof lands.
 */
import type { Browser, Page } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, send, waitForCount, waitForText } from "../../harness";
import { PORT, type Server, startServer } from "../../server";

let server: Server | undefined;
let browser: Browser | undefined;

beforeAll(async () => {
  server = await startServer({ fixture: "tests/fixtures/claude/interrupt-mid-stream.jsonl" });
  browser = await launchEdge();
}, 60_000);

afterAll(async () => {
  await browser?.close();
  await server?.stop();
});

test("Stop cancels a running turn, and a second turn on the same session still works", async () => {
  if (!browser) throw new Error("Edge did not launch");
  const page: Page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto(appUrl(PORT));
  await page.locator('[data-region="shell"]').waitFor({ timeout: 15_000 });

  await send(page, "count for me");
  await waitForText(page, ".user-turn", "count for me");
  // The fixture counts 1..85 and then stalls: waiting for "85" is waiting for every delta before
  // the stall to have actually arrived, not just the first one.
  await waitForText(page, ".reply .md", "85", 15_000);

  const stop = page.getByLabel("Stop");
  await stop.waitFor({ timeout: 5_000 });
  await stop.click();

  // The turn only ends "cancelled" once the CLI's own result frame says so, which its script
  // withholds until it has actually seen our interrupt control frame.
  await waitForText(page, ".reply-cancelled", "Stopped", 15_000);
  await waitForCount(page, ".composer-stop", 0);

  // Rule out the layered stop's last-resort kill: that path also ends with "cancelled", but it
  // additionally reports a crash (`.error-card`, MSG_STOP_KILLED). No such card means the CLI
  // actually acknowledged the interrupt rather than having to be killed.
  expect(await page.locator(".card, .error-card").count()).toBe(0);

  // The real proof the click reached the *process* rather than just the composer's local state:
  // the stop killed a turn, not the session, so the same CLI process takes a second message and
  // finishes it normally. A killed process could not do this.
  await send(page, "go again");
  await waitForCount(page, ".user-turn", 2);
  await page.waitForFunction(
    () => (document.querySelectorAll(".reply .md")[1]?.textContent ?? "") === "done",
    undefined,
    { timeout: 15_000 },
  );
  await waitForCount(page, ".composer-stop", 0);

  await page.close();
});
