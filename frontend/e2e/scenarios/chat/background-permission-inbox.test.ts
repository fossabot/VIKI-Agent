/**
 * e2e: the decision inbox answering a card from a session that is not the one on screen — the
 * one shape `e2e/scenarios/chat/approval.test.ts` cannot reach, since its card-answering scenarios drive a single
 * selected session at a time.
 *
 * Three live sessions each reach the same write-permission card. `collectDecisions`
 * (state/decisions.ts) lists every tracked session's open card in the inbox, in object-then-arrival
 * order — the order their sessions were first seen — so the first row is always the session
 * created first. Since each new session is selected the moment it is made, the one created first
 * is never the one left on screen once a third exists; denying its row and then checking the
 * still-selected session's own card is what proves the click reached a background session, not
 * the one already visible.
 */
import type { Browser, Page } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import {
  appUrl,
  controlResponses,
  launchEdge,
  recordControlResponses,
  send,
  TEST_TOKEN,
  waitForCount,
  waitForText,
} from "../../harness";
import { PORT, type Server, startServer } from "../../server";
import { sidebarNavigation } from "../../sidebar-navigation";

const FIXTURE = "tests/fixtures/claude/write-permission.jsonl";
const MESSAGE = "Create a new note in the knowledge base.";

let server: Server | undefined;
let browser: Browser | undefined;

beforeAll(async () => {
  recordControlResponses(true);
  server = await startServer({ fixture: FIXTURE });
  browser = await launchEdge();
}, 60_000);

afterAll(async () => {
  recordControlResponses(false);
  await browser?.close();
  await server?.stop();
});

interface ApprovalReceiptBody {
  choice_id: string;
  session_id: string;
  state: string;
}

/** The POST's own response, set up before the click so a later ledger read is a read of a
 * settled row (e2e/scenarios/chat/approval.test.ts's own reasoning applies here unchanged). */
function postedApproval(page: Page) {
  return page.waitForResponse(
    (response) =>
      response.request().method() === "POST" && response.url().endsWith("/api/v2/approvals"),
    { timeout: 15_000 },
  );
}

async function approvals(page: Page): Promise<ApprovalReceiptBody[]> {
  const response = await page.request.get(`http://127.0.0.1:${PORT}/api/v2/approvals`, {
    headers: { authorization: `Bearer ${TEST_TOKEN}` },
  });
  expect(response.status()).toBe(200);
  return (await response.json()).approvals as ApprovalReceiptBody[];
}

/** A fresh page whose settings document has loaded: the inbox's own copy is English whatever
 * the `ja-JP` context says, since the UI is English only.
 * The wait is for the `GET /api/v2/settings` response, not for the page's language. */
async function open(): Promise<Page> {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, locale: "ja-JP" });
  const settings = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v2/settings" &&
      response.request().method() === "GET",
  );
  await page.goto(appUrl(PORT));
  await page.locator('[data-region="shell"]').waitFor({ timeout: 15_000 });
  await settings;
  return page;
}

/** Opens the VIKI panel drawer (the undocked right rail), confirms Needs you's own badge shows
 * `count`, then opens Approvals from its "Open Approvals" button. */
async function openInbox(page: Page, count: string): Promise<void> {
  await page.getByRole("button", { name: "VIKI panel" }).click();
  await page.getByRole("dialog", { name: "VIKI panel" }).waitFor();
  await waitForText(page, ".rail-section:has(.need-open) .rail-section-count", count, 15_000);
  // A second Open Approvals sits beside Needs you's own button, so a name-based lookup is
  // ambiguous in strict mode.
  await page.locator("button.need-open").click();
  await page.locator('[role="dialog"][aria-label="Approvals"]').waitFor({ timeout: 15_000 });
}

test("Deny in the inbox denies a background session's card, once, and it stays closed", async () => {
  const page = await open();

  // The page's own first session, then two more from "New session", make three live sessions.
  // Each is selected the instant it is created, so by the time the third reaches its card, the
  // first two are held in the background: tracked, but off screen.
  await send(page, MESSAGE);
  await waitForText(page, ".card .chip", "Waiting for approval", 15_000);

  const newSession = page.locator("button", { hasText: "New session" });
  await newSession.click();
  await send(page, MESSAGE);
  await waitForText(page, ".card .chip", "Waiting for approval", 15_000);

  await newSession.click();
  await send(page, MESSAGE);
  await waitForText(page, ".card .chip", "Waiting for approval", 15_000);

  // The top bar may wrap to keep the name and controls visible at the narrowest window. The resize
  // switches the layout to its narrow mode asynchronously (a `matchMedia` change event), so the
  // rail toggles are not there yet on the very next tick; wait for both before measuring, or the
  // bar this reads is missing its toggles, not the narrow layout this window already shows. The git chip
  // is hidden at 400px (below the sub-band's threshold), so there is nothing of its to wait for.
  await page.setViewportSize({ width: 400, height: 900 });
  await page.locator('[data-region="left-rail-toggle"]').waitFor({ timeout: 15_000 });
  await page.locator('[data-region="right-rail-toggle"]').waitFor({ timeout: 15_000 });
  const bar = await page.evaluate(() => {
    const el = document.querySelector<HTMLElement>(".topbar");
    const wordmark = document.querySelector<HTMLElement>(".wordmark");
    if (!el || !wordmark) return null;
    const bounds = (element: Element) => {
      const rect = element.getBoundingClientRect();
      return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom };
    };
    const textRects = (element: Element) => {
      const range = document.createRange();
      range.selectNodeContents(element);
      return [...range.getClientRects()].map((rect) => ({
        left: rect.left,
        right: rect.right,
        top: rect.top,
        bottom: rect.bottom,
      }));
    };
    const rect = el.getBoundingClientRect();
    return {
      width: innerWidth,
      windowHeight: innerHeight,
      height: rect.height,
      bounds: bounds(el),
      scroll: el.scrollWidth,
      client: el.clientWidth,
      right: rect.right,
      paddingRight: Number.parseFloat(getComputedStyle(el).paddingRight),
      wordmark: {
        bounds: bounds(wordmark),
        scroll: wordmark.scrollWidth,
        client: wordmark.clientWidth,
        text: textRects(wordmark),
      },
      controls: [...el.querySelectorAll<HTMLElement>("button, select")]
        .filter((control) => control.getClientRects().length > 0)
        .map((control) => {
          const box = bounds(control);
          const host = control.closest(".topbar-right, .topbar");
          if (!host) throw new Error("Top bar control host is missing");
          return {
            label: control.getAttribute("aria-label") ?? control.textContent,
            bounds: box,
            host: bounds(host),
            text: textRects(control),
            hit: control.contains(
              document.elementFromPoint(box.right - 1, (box.top + box.bottom) / 2),
            ),
          };
        }),
      shellLeft: document.querySelector<HTMLElement>(".shell")?.scrollLeft,
      bodyLeft: document.body.scrollLeft,
      frameLeft: document.querySelector<HTMLElement>(".shell-body")?.scrollLeft,
    };
  });
  expect(bar).not.toBeNull();
  if (!bar) throw new Error("Top bar geometry is missing");
  expect(bar.height).toBeGreaterThanOrEqual(49);
  expect(bar?.scroll).toBeLessThanOrEqual((bar?.client ?? 0) + 1);
  expect(bar.shellLeft).toBe(0);
  expect(bar.bodyLeft).toBe(0);
  expect(bar.frameLeft).toBe(0);
  expect(bar.wordmark.scroll).toBeLessThanOrEqual(bar.wordmark.client + 1);
  expect(bar.wordmark.text.length).toBeGreaterThan(0);
  expect(bar.controls.length).toBeGreaterThan(0);
  for (const item of [
    { ...bar.wordmark, label: "Assistant name", host: bar.bounds },
    ...bar.controls,
  ]) {
    for (const box of [item.bounds, ...item.text]) {
      for (const host of [
        item.bounds,
        item.host,
        bar.bounds,
        { left: 0, right: bar.width, top: 0, bottom: bar.windowHeight },
      ]) {
        expect(box.left, item.label ?? "control").toBeGreaterThanOrEqual(host.left - 1);
        expect(box.right, item.label ?? "control").toBeLessThanOrEqual(host.right + 1);
        expect(box.top, item.label ?? "control").toBeGreaterThanOrEqual(host.top - 1);
        expect(box.bottom, item.label ?? "control").toBeLessThanOrEqual(host.bottom + 1);
      }
    }
  }
  for (const [index, control] of bar.controls.entries()) {
    expect(control.hit, control.label ?? "control").toBe(true);
    for (const other of bar.controls.slice(index + 1)) {
      expect(
        control.bounds.left < other.bounds.right &&
          control.bounds.right > other.bounds.left &&
          control.bounds.top < other.bounds.bottom &&
          control.bounds.bottom > other.bounds.top,
      ).toBe(false);
    }
  }
  // The moved creation action must still fit and receive the pointer inside the narrow rail.
  const navigation = await sidebarNavigation(page);
  const sessionButton = navigation.getByRole("button", { name: "New session", exact: true });
  const sessionBox = await sessionButton.boundingBox();
  const railBox = await navigation.boundingBox();
  if (!sessionBox || !railBox) throw new Error("Sidebar creation geometry is missing");
  expect(sessionBox.x).toBeGreaterThanOrEqual(railBox.x);
  expect(sessionBox.x + sessionBox.width).toBeLessThanOrEqual(railBox.x + railBox.width);
  expect(
    await sessionButton.evaluate((el) => {
      const box = el.getBoundingClientRect();
      return el.contains(document.elementFromPoint(box.right - 1, box.top + box.height / 2));
    }),
  ).toBe(true);
  await page.keyboard.press("Escape");
  await page.getByRole("dialog", { name: "Sessions and agents" }).waitFor({ state: "detached" });
  await page.setViewportSize({ width: 1440, height: 900 });
  expect((await page.locator(".topbar").boundingBox())?.height).toBe(49);

  // The foreground card — the third, still-selected session — before the inbox touches anything.
  await waitForCount(page, ".card-column .card", 1, 15_000);

  // The badge counts every open card this window tracks, not only the one on screen; Approvals
  // opens from the VIKI panel drawer, which exists only below 1280px — and closes that drawer
  // the moment it opens (showDecisions), so the badge is read again once the rail docks, below.
  await page.setViewportSize({ width: 400, height: 900 });
  await openInbox(page, "3");
  await waitForCount(page, ".decision-inbox-row", 3, 15_000);

  const firstRow = page.locator(".decision-inbox-row").nth(0);
  const accepted = postedApproval(page);
  await firstRow.locator(".btn.alt", { hasText: "Deny" }).click();
  const receipt = await (await accepted).json();
  await waitForCount(page, ".decision-inbox-row", 2, 15_000);

  // The foreground session's own card is untouched: the denial reached a background one.
  expect(await page.locator(".card-column .card").count()).toBe(1);

  // Needs you's own badge is gone from the DOM with the drawer (above): widen past the 1280px
  // breakpoint so the rail docks and the badge mounts there, then narrow back so the VIKI panel
  // drawer this test still expects below (the post-reload openInbox) exists again.
  await page.setViewportSize({ width: 1440, height: 900 });
  await waitForText(page, ".rail-section:has(.need-open) .rail-section-count", "2", 15_000);
  await page.setViewportSize({ width: 400, height: 900 });

  const recorded = await approvals(page);
  expect(recorded).toHaveLength(1);
  expect(recorded[0]?.choice_id).toBe(receipt.choice_id);
  expect(recorded[0]?.state).toBe("delivered_locally");

  // Hard rule 6: a deny, and nothing else, reached claude — once, with no rule smuggled back.
  expect(await controlResponses(PORT)).toEqual([
    { behavior: "deny", message: "The owner denied this request." },
  ]);

  // A reload must not bring the answered card back, and must not answer it again.
  const reloaded = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v2/settings" &&
      response.request().method() === "GET",
  );
  await page.reload();
  await page.locator('[data-region="shell"]').waitFor({ timeout: 15_000 });
  await reloaded; // the settings document
  await openInbox(page, "2");
  await waitForCount(page, ".decision-inbox-row", 2, 15_000);
  expect(await approvals(page)).toHaveLength(1);

  await page.close();
}, 120_000);
