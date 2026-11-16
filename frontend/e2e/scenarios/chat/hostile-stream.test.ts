/**
 * e2e: vault text is data; so is model output. `e2e/scenarios/vaults/hostileContent.test.ts`
 * already proves this for the REST surfaces (session rows, rails, search) by having playwright
 * answer those routes directly — no backend involved. The reply text a turn streams back never
 * goes through a REST route or through `page.route` at all: it arrives over the WebSocket,
 * through the real mapper and the real reducer, and is rendered by `Markdown.tsx`'s
 * `ReactMarkdown`, a completely different component from anything
 * `e2e/scenarios/vaults/hostileContent.test.ts` exercises. That renderer's whole safety case
 * rests on one fact stated in its own docstring — without `rehype-raw`, raw HTML in the source
 * becomes text, never an element — and the only way to know that claim actually holds in a real
 * browser, against the real streaming path, is to stream it hostile text and look at the live
 * DOM.
 * `tests/fixtures/claude/hostile-reply.jsonl` is a synthetic reply carrying a `<script>` tag, an
 * `<img onerror>`, a markdown link to a `javascript:` URL and an attribute-breakout `<svg
 * onload>`, each its own delta the way a real answer streams in pieces — and the `<script>` tag
 * itself is split mid-token ("<scr" then "ipt>...</script>") across two of those deltas. That
 * split is the point, not incidental realism: it is the one case `markdown.accept.test.tsx`
 * cannot cover (a component test is handed the whole accumulated string at once), and the one a
 * renderer or reducer that parsed each delta in isolation — instead of the string the reducer
 * actually accumulates — would fail: "<scr" alone contains no tag for anything to misparse, so
 * only re-parsing the joined text catches it.
 *
 * The fixture also carries one ORDINARY https link, and this test clicks it, because "links go
 * to the default browser" is the half of the same rule that only a real browser can falsify.
 * `markdown.accept.test.tsx` pins `event.defaultPrevented`, which is the event object's own
 * property and says nothing about what a browser then does; drop the `preventDefault` and jsdom
 * still sees no navigation, while the real Edge `--app` window leaves the app for good — inside
 * the extension-less, cookie-less profile whose lifetime is tied to the Job that keeps the app
 * alive. Clicking it here is the only place that failure is visible, and the only e2e in the
 * suite where a regression sends the user somewhere other than 127.0.0.1.
 */
import type { Browser, Page } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, send, waitForOpenedUrl } from "../../harness";
import { PORT, type Server, startServer } from "../../server";

const IMG = '<img src="https://evil.example/p.png" onerror="window.__pwned=1">';
const SCRIPT = "<script>window.__pwned=1</script>";
const SVG = '"><svg onload="window.__pwned=1"></svg>';
const JS_LINK = "[open this](javascript:window.__pwned=1)";
const LINK = "https://example.com/changelog";
/** The header's shared logo is an admitted image with a versioned same-origin URL. */
const BRAND_MARK = /^\/brand\/mark\.png\?v=[0-9a-f]{8}$/;

let server: Server | undefined;
let browser: Browser | undefined;

beforeAll(async () => {
  server = await startServer({ fixture: "tests/fixtures/claude/hostile-reply.jsonl" });
  browser = await launchEdge();
}, 60_000);

afterAll(async () => {
  await browser?.close();
  await server?.stop();
});

test("a hostile streamed reply renders as text, and its one safe link opens away from this window", async () => {
  if (!browser) throw new Error("Edge did not launch");
  const page: Page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const offHost: string[] = [];
  page.on("request", (request) => {
    if (!request.url().startsWith(`http://127.0.0.1:${PORT}`)) offHost.push(request.url());
  });
  await page.goto(appUrl(PORT));
  await page.locator('[data-region="shell"]').waitFor({ timeout: 15_000 });

  await send(page, "summarise that page");
  await page.waitForFunction(
    () => (document.querySelector(".reply .md")?.textContent ?? "").includes("everything the page"),
    undefined,
    { timeout: 15_000 },
  );

  expect(await page.evaluate(() => (window as { __pwned?: number }).__pwned)).toBeUndefined();
  // The page's images are same-origin: the portrait endpoint and the shared static brand
  // mark (TopBar.tsx); a real off-origin <img> tag here would mean the hostile markup got
  // parsed as an element instead of text.
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
  expect(offHost).toEqual([]);

  // ... and the user still sees exactly what the model sent, verbatim.
  const text = (await page.locator(".reply .md").first().textContent()) ?? "";
  expect(text).toContain(IMG);
  expect(text).toContain(SCRIPT);
  expect(text).toContain(SVG);
  expect(text).toContain(JS_LINK.slice(JS_LINK.indexOf("(") + 1, -1)); // the javascript: URL itself

  // The ordinary link, which IS an anchor (Markdown.tsx only builds one for http/https/mailto).
  const link = page.locator(`.reply .md a[href="${LINK}"]`);
  await link.waitFor({ timeout: 10_000 });
  await link.click();

  // Where it goes is the server's business — open.url → winproc.open_external, which this run
  // replaces with a recorder so a test never launches a browser on the machine it runs on.
  await waitForOpenedUrl(LINK, PORT);
  // And where it does NOT go is this window. Nothing in the CSP stops a top-level navigation
  // (no navigate-to, and form-action only covers forms), so without Markdown.tsx's
  // preventDefault the app is simply replaced by example.com. The order is deliberate: each
  // line below is another round-trip to the browser, and offHost — the one assertion that still
  // fires when the navigation FAILS, because the attempt is a request either way — goes last,
  // after the page has had those round-trips to commit anything it started.
  expect(page.url().startsWith(`http://127.0.0.1:${PORT}/`)).toBe(true);
  expect(await page.locator('[data-region="shell"]').count()).toBe(1);
  expect(await page.locator(`.reply .md a[href="${LINK}"]`).count()).toBe(1);
  expect(offHost).toEqual([]);
  await page.close();
});
