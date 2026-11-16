import { type Browser, chromium, type Locator, type Page } from "playwright-core";

/**
 * The fixed token end-to-end runs use. The server accepts it only when started with
 * VIKI_TEST_TOKEN set to the same value (the test-token hook); normal launches mint a
 * random per-launch token instead.
 */
export const TEST_TOKEN = "e2e-test-token-not-a-secret";

/**
 * The installed Chromium browser through playwright-core: no bundled browsers are downloaded.
 * Tries the `msedge` channel first, then falls back to `chrome` for hosts with no Edge (Linux,
 * some macOS images).
 */
export async function launchEdge(args: string[] = []): Promise<Browser> {
  try {
    return await chromium.launch({ channel: "msedge", headless: true, args });
  } catch (edgeError) {
    try {
      return await chromium.launch({ channel: "chrome", headless: true, args });
    } catch (chromeError) {
      throw new Error(
        `neither the msedge nor the chrome channel could be launched: ${String(edgeError)}; ${String(chromeError)}`,
      );
    }
  }
}

/**
 * `launchEdge` with Edge's fake capture: the microphone is `wav` (16 kHz mono s16le, synthetic
 * audio only), played once, and the permission prompt is answered for the page. Only the
 * dictation end-to-end file uses it; no real microphone is ever opened.
 */
export function launchEdgeWithFakeMic(wav: string): Promise<Browser> {
  return launchEdge([
    "--use-fake-device-for-media-stream",
    "--use-fake-ui-for-media-stream",
    `--use-file-for-fake-audio-capture=${wav}%noloop`,
  ]);
}

export function appUrl(port = 8765, token = TEST_TOKEN): string {
  return `http://127.0.0.1:${port}/#token=${token}`;
}

/**
 * Wait until some element matching `selector` contains `needle`.
 *
 * playwright-core ships the driver, not playwright-test's auto-retrying `expect`, so waiting is
 * explicit here. Anything that arrives over the socket (a streamed reply, a card, a status line)
 * needs this rather than a bare textContent read, which would race the stream.
 */
export function waitForText(
  page: Page,
  selector: string,
  needle: string,
  timeout = 15_000,
): Promise<unknown> {
  return page.waitForFunction(
    ([sel, want]: [string, string]) =>
      [...document.querySelectorAll(sel)].some((node) => (node.textContent ?? "").includes(want)),
    [selector, needle] as [string, string],
    { timeout },
  );
}

/** Wait until the page holds exactly `count` of `selector`. */
export function waitForCount(
  page: Page,
  selector: string,
  count: number,
  timeout = 15_000,
): Promise<unknown> {
  return page.waitForFunction(
    ([sel, want]: [string, number]) => document.querySelectorAll(sel).length === want,
    [selector, count] as [string, number],
    { timeout },
  );
}

/** The text of the first match, "" when there is none. */
export async function textOf(page: Page | Locator, selector: string): Promise<string> {
  const found = page.locator(selector).first();
  if ((await found.count()) === 0) return "";
  return (await found.textContent()) ?? "";
}

/**
 * Type into the focused composer and press Enter, the way the user sends.
 *
 * Waits for Send to be enabled first. An Enter pressed too early does not send and
 * leaves the text in the box, which made a run's second turn time out now and then. That Enter
 * is silent while an earlier send is still waiting, and the waiting note shows instead.
 * Otherwise Composer shows why: a reply is still running (the Stop pill stands in for Send), or
 * the caller's own reason. On timeout the error presses Enter once, so that notice is on
 * screen, then names what blocked it.
 */
export async function send(page: Page, text: string): Promise<void> {
  const composer = page.locator(
    ".pane[data-focused] > .composer-slot, main.conversation > .composer-slot",
  );
  const box = composer.getByLabel("Message");
  await box.fill(text);
  try {
    await composer.locator(".composer-submit:not([disabled])").waitFor({ timeout: 15_000 });
  } catch (error) {
    const submit = composer.locator(".composer-submit");
    const disabled =
      (await submit.count()) === 0
        ? "no .composer-submit"
        : await submit.getAttribute("disabled", { timeout: 1_000 });
    const stopPresent = (await composer.locator(".composer-stop").count()) > 0;
    const cardNote = await textOf(composer, ".composer-wrap > .card-note");
    await box.press("Enter", { timeout: 1_000 });
    const draftStatus = await textOf(composer, ".draft-status");
    const refusal = await textOf(composer, ".composer-refusal");
    const blocked = await textOf(composer, ".composer-blocked");
    throw new Error(
      `Send stayed disabled (disabled=${disabled}, composer-stop present=${stopPresent}); ` +
        `draft-status=${JSON.stringify(draftStatus)} composer-refusal=${JSON.stringify(refusal)} ` +
        `composer-blocked=${JSON.stringify(blocked)} card-note=${JSON.stringify(cardNote)}`,
      { cause: error },
    );
  }
  await box.press("Enter");
}

/**
 * What the server did with what the page sent it, read back through the two test-only routes
 * `scripts/e2e_server.py` adds to its own app object (never to `server.create_app`).
 *
 * The page is the wrong place to look for either. A card's answer leaves the browser as a
 * WebSocket frame and the browser never hears what became of it; `open.url` leaves as one too
 * and its whole point is that nothing happens in the page. Everything below reads the server's
 * side of those two, which is what turns "the turn carried on" into "the turn carried on
 * because the user's actual choice arrived".
 */

/** A card answer as claude receives it: what `PermissionResultAllow`/`Deny` serialise to. */
export interface CardAnswer {
  behavior?: string;
  message?: string;
  updatedInput?: { answers?: Record<string, string> };
  /** Permission rules suggested back to the CLI. Hard rule 6: VIKI never sends any. */
  updatedPermissions?: unknown[];
}

/** The wire frame the answer is wrapped in; only the innermost body matters to a test. */
interface RawControlResponse {
  response?: { response?: CardAnswer };
}

/**
 * Ask the NEXT `startServer` to record the control_response frames it writes to claude's stdin.
 *
 * The recorder sits in the byte path between the server and the CLI, so e2e_server.py only
 * installs it when this is set, and only the file that needs the evidence sets it. The e2e files
 * share one vitest worker (`fileParallelism: false`), so whoever turns it on turns it off again.
 */
export function recordControlResponses(on: boolean): void {
  if (on) {
    process.env.VIKI_E2E_RECORD_FRAMES = "1";
  } else {
    delete process.env.VIKI_E2E_RECORD_FRAMES;
  }
}

/**
 * The card answers the server has written to claude so far, oldest first.
 *
 * Needs `recordControlResponses(true)` before the server started, and needs no polling at the
 * call site: the fixtures only play on past a card once the answer has reached the CLI, so any
 * wait for what comes after the card has already waited for this.
 */
export async function controlResponses(port = 8765): Promise<CardAnswer[]> {
  const body = await testRoute<{ frames: RawControlResponse[] }>("control-responses", port);
  return body.frames.map((frame) => frame.response?.response ?? {});
}

/** Every URL an `open.url` handed the opener, oldest first. */
export async function openedUrls(port = 8765): Promise<string[]> {
  return (await testRoute<{ urls: string[] }>("opened", port)).urls;
}

/** Wait until the server has opened `url`; on timeout, say what it opened instead. */
export async function waitForOpenedUrl(url: string, port = 8765, timeout = 10_000): Promise<void> {
  const deadline = Date.now() + timeout;
  let urls: string[] = [];
  while (Date.now() < deadline) {
    urls = await openedUrls(port);
    if (urls.includes(url)) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`the server never opened ${url}; it opened ${JSON.stringify(urls)}`);
}

async function testRoute<T>(name: string, port: number): Promise<T> {
  const response = await fetch(`http://127.0.0.1:${port}/api/e2e/${name}`, {
    headers: { Authorization: `Bearer ${TEST_TOKEN}` },
  });
  if (!response.ok) {
    throw new Error(`GET /api/e2e/${name} answered ${response.status}, not 200`);
  }
  return (await response.json()) as T;
}
