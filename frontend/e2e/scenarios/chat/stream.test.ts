/**
 * e2e: a message typed in real Edge reaches a real claude process and comes back on screen.
 *
 * Everything below the browser is the shipping stack — the launcher's own assembly (minus its
 * window), the real WebSocket, the real transport, the real mapper — with one substitution at
 * the very bottom: the CLI is tests/fake_claude.py replaying a recorded script. Nothing here
 * stubs a route or hands the page a fixture, which is the whole point: the unit and component
 * suites already prove each piece against a double, and this proves they are wired together.
 *
 * The vault, the home directory and the data directory are all scratch (see e2e_server.py
 * --scratch), so the session list starts empty and no transcript of the user's is read.
 */
import type { Browser, Page } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, send, textOf, waitForCount, waitForText } from "../../harness";
import { PORT, type Server, startServer } from "../../server";

let server: Server | undefined;
let browser: Browser | undefined;

beforeAll(async () => {
  server = await startServer({ fixture: "tests/fixtures/claude/text-two-turns.jsonl" });
  browser = await launchEdge();
}, 60_000);

afterAll(async () => {
  await browser?.close();
  await server?.stop();
});

async function open(): Promise<{ page: Page; offHost: string[] }> {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const offHost: string[] = [];
  page.on("request", (request) => {
    if (!request.url().startsWith(`http://127.0.0.1:${PORT}`)) offHost.push(request.url());
  });
  await page.goto(appUrl(PORT));
  await page.locator('[data-region="shell"]').waitFor({ timeout: 15_000 });
  return { page, offHost };
}

test("a typed message opens a session, streams a reply, and takes a second turn", async () => {
  const { page, offHost } = await open();

  // Nothing selected and nothing live: the shell is the empty state.
  await page.locator(".empty").waitFor({ timeout: 10_000 });

  await send(page, "prime directive 1?");
  await waitForText(page, ".user-turn", "prime directive 1?");
  await waitForText(page, ".reply", "Never edit or overwrite anything in", 20_000);

  // Wait for structure, not just text: the fixture's backticks land as a <code> element once the
  // markdown renderer (not innerHTML) has run.
  await page.waitForFunction(
    () => (document.querySelector(".reply code")?.textContent ?? "") === "_raw/",
    undefined,
    { timeout: 20_000 },
  );

  // It came through the markdown renderer rather than innerHTML: the fixture's backticks are a
  // <code> element, and the text is the fixture's, not a truncation of it.
  expect(await textOf(page, ".reply code")).toBe("_raw/");
  expect(await textOf(page, ".reply .md")).toContain("never alter the owner's original words");

  // The turn ended: Send is back (not Stop).
  await waitForCount(page, ".composer-stop", 0);

  await send(page, "reply with B");
  await waitForCount(page, ".user-turn", 2);
  // `.reply` carries VIKI's name and face too, so the reply text itself is `.md`.
  await page.waitForFunction(
    () => (document.querySelectorAll(".reply .md")[1]?.textContent ?? "") === "B",
    undefined,
    { timeout: 20_000 },
  );

  // The header's mode chip shows only once a session reports its mode (0352a1f1 dropped the
  // live chip; the fixture's init reports "auto"). Not the rail: its rows are transcripts on
  // disk, and the fake CLI writes none — that seam is the catalog's, and tests/test_catalog.py
  // owns it.
  expect(await textOf(page, ".conversation-header .mode-chip")).toContain("Auto");
  expect(offHost).toEqual([]);
  await page.close();
});
