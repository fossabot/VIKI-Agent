/**
 * e2e: the user's answer is recorded before it is delivered, exactly once.
 *
 * `e2e/scenarios/chat/cards.test.ts` proves a click reaches the CLI as the right frame. This
 * file proves the rest, which only the real server can show: the choice exists in the approvals
 * ledger VIKI Agent keeps, with its own delivery state, two rapid clicks produce one record and
 * one `control_response` — not one record and two, nor two records — and a second page's ledger
 * lookup for the same still-open request answers 404 while it is pending and 200 with the same
 * choice ID once the first page answers it.
 *
 * The recorded-allow test also pins answered-card replay: a reload restores the user and reply
 * text, keeps the answered card closed in the server's `session.snapshot` recovery while the
 * tool is still running, then carries the reply to completion.
 * This replay guarantee is separate from the ledger lookup above — deleting
 * `observe`/`findApproval` entirely would leave the reload assertions green.
 *
 * Every assertion reads the server's own side. `GET /api/v2/approvals` is the authenticated REST
 * route the browser answers through (Bearer, never a query token); `/api/e2e/control-responses`
 * is the recorder in the byte path to claude's stdin, which is what turns "the turn carried on"
 * into "the user's actual choice arrived, once".
 *
 * `slow-tool-permission.jsonl` holds its Bash tool open for 8 seconds after the answer, which is
 * what makes the reload window (and the second page's post-answer lookup) reachable at all: the
 * card is already gone (`request.answered`), the turn is still running, and the page must come
 * back saying nothing about a second answer.
 */
import type { Browser, Page } from "playwright-core";
import { afterAll, beforeAll, describe, expect, test } from "vitest";
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
import { newSidebarSession } from "../../sidebar-navigation";

beforeAll(() => {
  recordControlResponses(true);
});

afterAll(() => {
  recordControlResponses(false);
});

interface ApprovalReceiptBody {
  choice_id: string;
  session_id: string;
  state: string;
  reason: string;
  tool_use_id: string;
}

/** The POST's own response, which the service sends only after the attempt has finalized. Set up
 * before the click; awaiting it is what makes a later ledger read a read of a settled row. */
function postedApproval(page: Page) {
  return page.waitForResponse(
    (response) =>
      response.request().method() === "POST" && response.url().endsWith("/api/v2/approvals"),
    { timeout: 15_000 },
  );
}

/**
 * The bundle the server is serving must be one that answers through the ledger.
 *
 * `scripts/e2e_server.py` serves `frontend/dist` (`__main__.STATIC_DIR`), never `src`. A dist
 * built before durable approvals answers cards over the socket instead: no POST ever leaves the
 * page, the card still closes on the server's `request.answered`, and every assertion here fails
 * as a 15-second timeout that names nothing. `scripts/check.py` runs `build` before `e2e`, so
 * the full check is safe; running this file alone is not. One line beats an hour.
 */
async function assertServedBundleIsCurrent(): Promise<void> {
  const origin = `http://127.0.0.1:${PORT}`;
  const index = await (await fetch(`${origin}/`)).text();
  const src = /src="([^"]+\.js)"/.exec(index)?.[1];
  const bundle = src ? await (await fetch(`${origin}${src}`)).text() : "";
  // A route literal survives minification whole, as "api/v2/commands" does in a bundle that
  // sends durable commands.
  if (!bundle.includes("/api/v2/approvals"))
    throw new Error(
      `the served bundle (${src ?? "missing"}) predates durable approvals — ` +
        "run `npm --prefix frontend run build` before this file, or run the full check",
    );
}

async function approvals(page: Page): Promise<ApprovalReceiptBody[]> {
  const response = await page.request.get(`http://127.0.0.1:${PORT}/api/v2/approvals`, {
    headers: { authorization: `Bearer ${TEST_TOKEN}` },
  });
  expect(response.status()).toBe(200);
  return (await response.json()).approvals as ApprovalReceiptBody[];
}

async function open(browser: Browser): Promise<Page> {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto(appUrl(PORT));
  await page.locator('[data-region="shell"]').waitFor({ timeout: 15_000 });
  return page;
}

describe("a recorded allow (Bash)", () => {
  let server: Server | undefined;
  let browser: Browser | undefined;

  beforeAll(async () => {
    server = await startServer({ fixture: "tests/fixtures/claude/slow-tool-permission.jsonl" });
    await assertServedBundleIsCurrent();
    browser = await launchEdge();
  }, 60_000);

  afterAll(async () => {
    await browser?.close();
    await server?.stop();
  });

  test("a recorded allow survives reload with one record and one control_response", async () => {
    if (!browser) throw new Error("Edge did not launch");
    const page = await open(browser);
    const posts: string[] = [];
    page.on("request", (request) => {
      if (request.method() === "POST" && request.url().endsWith("/api/v2/approvals"))
        posts.push(request.postData() ?? "");
    });

    await send(page, "count the notes");
    await waitForText(page, ".card .chip", "Waiting for approval", 15_000);
    await waitForText(page, ".card-tool-name", "Bash", 15_000);
    // The POST answers only after the attempt finalizes, while `request.answered` (which closes
    // the card) precedes that write — so the receipt, not the card, is what the ledger read waits
    // for. Without this the row can still read `delivering`.
    const accepted = postedApproval(page);
    await page.locator(".card .btn", { hasText: "Allow once" }).first().click();
    const receipt = await (await accepted).json();
    expect(receipt.state).toBe("delivered_locally");
    // The card still closes on the server's own request.answered.
    await waitForCount(page, ".card", 0, 15_000);

    const recorded = await approvals(page);
    expect(recorded).toHaveLength(1);
    expect(recorded[0]?.choice_id).toBe(receipt.choice_id);
    expect(recorded[0]?.state).toBe("delivered_locally");
    expect(recorded[0]?.reason).toBe("");
    // The answer itself is only behind /choice, never in the metadata page above.
    expect(JSON.stringify(recorded[0])).not.toContain("allow");
    const detail = await page.request.get(
      `http://127.0.0.1:${PORT}/api/v2/approvals/${recorded[0]?.choice_id}/choice`,
      { headers: { authorization: `Bearer ${TEST_TOKEN}` } },
    );
    const record = await detail.json();
    expect(record.choice).toEqual({ kind: "permission", allow: true });
    // The displayed projection is the server's capture of the card, not anything the page sent.
    expect(record.request.data.tool).toBe("Bash");
    expect(record.request.data.tool_use_id).toBe(recorded[0]?.tool_use_id);
    expect(posts).toHaveLength(1);

    // The tool is still running. A reload must neither reopen the card nor answer again.
    await page.reload();
    await page.locator('[data-region="shell"]').waitFor({ timeout: 15_000 });
    await waitForText(page, ".reply .md", "Counting them now.", 20_000);
    await waitForText(page, ".user-turn", "count the notes", 20_000);
    expect(await page.locator(".composer-stop").count()).toBe(1);
    expect(await page.locator(".card").count()).toBe(0);
    expect(await approvals(page)).toHaveLength(1);
    expect(posts).toHaveLength(1);

    // And exactly one answer ever reached claude, carrying no permission rules with it.
    const sent = await controlResponses(PORT);
    expect(sent).toHaveLength(1);
    expect(sent[0]?.behavior).toBe("allow");
    expect(sent[0]?.updatedPermissions).toBeUndefined();

    // The tool finishes and the reloaded page carries on with it.
    await waitForText(page, ".reply .md", "132 notes.", 30_000);
    await waitForCount(page, ".card", 0);
    await page.close();
  }, 90_000);

  test("a second page's ledger lookup sees the request pending, then recorded", async () => {
    if (!browser) throw new Error("Edge did not launch");
    const page1 = await open(browser);
    // A reopened window can restore the preceding test's selection; this case needs a fresh turn.
    await newSidebarSession(page1);
    await waitForCount(page1, ".reply", 0, 15_000);
    await page1
      .locator('textarea[aria-label="Message"][placeholder^="Talk to "]')
      .waitFor({ timeout: 15_000 });
    await send(page1, "count the notes");
    await waitForText(page1, ".card .chip", "Waiting for approval", 15_000);

    // A second page can never SELECT this session — the rail is built from on-disk transcripts
    // the fake CLI never writes (e2e/scenarios/chat/reload.test.ts) — but `vault.sessions` still
    // names it live, so `viki.ts` opens it in the background and App.tsx's card-observing effect
    // looks its card up exactly as page1's own reload would, with no row ever clicked.
    const page2 = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const firstLookup = page2.waitForResponse(
      (response) => response.request().method() === "GET" && response.url().includes("/approval?"),
      { timeout: 15_000 },
    );
    await page2.goto(appUrl(PORT));
    const pending = await firstLookup;
    expect(pending.status()).toBe(404);
    const lookupUrl = pending.url();
    expect(lookupUrl).toContain("/approval?");
    expect(lookupUrl).toContain("runtime_id=");
    expect(lookupUrl).toContain("request_id=");
    const recordedSessionId = /\/sessions\/([^/]+)\/approval/.exec(lookupUrl)?.[1];

    const accepted = postedApproval(page1);
    await page1.locator(".card .btn", { hasText: "Allow once" }).first().click();
    const receipt = await (await accepted).json();
    await waitForCount(page1, ".card", 0, 15_000);

    // The ledger outlives the previous test's session (one server per describe), so read this
    // session's rows, not the whole scope.
    const recorded = (await approvals(page1)).filter((r) => r.session_id === recordedSessionId);
    expect(recorded).toHaveLength(1);
    expect(recorded[0]?.choice_id).toBe(receipt.choice_id);

    // page2 checked once and never re-polls on its own (ensure() in state/approvals.ts only
    // looks a request up the first time it is seen), so this re-issues exactly the query its
    // background effect already made, from page2's own request context.
    const after = await page2.request.get(lookupUrl, {
      headers: { authorization: `Bearer ${TEST_TOKEN}` },
    });
    expect(after.status()).toBe(200);
    expect((await after.json()).choice_id).toBe(receipt.choice_id);

    await page1.close();
    await page2.close();
  }, 90_000);
});

describe("a recorded denial (WebFetch)", () => {
  let server: Server | undefined;
  let browser: Browser | undefined;

  beforeAll(async () => {
    server = await startServer({ fixture: "tests/fixtures/claude/webfetch-denied.jsonl" });
    await assertServedBundleIsCurrent();
    browser = await launchEdge();
  }, 60_000);

  afterAll(async () => {
    await browser?.close();
    await server?.stop();
  });

  test("two rapid Deny clicks record once and deny once", async () => {
    if (!browser) throw new Error("Edge did not launch");
    const page = await open(browser);
    const posts: string[] = [];
    page.on("request", (request) => {
      if (request.method() === "POST" && request.url().endsWith("/api/v2/approvals"))
        posts.push(request.postData() ?? "");
    });

    await send(page, "go fetch that page for me");
    await waitForText(page, ".card .chip", "Waiting for approval", 15_000);
    const accepted = postedApproval(page);
    await page.locator(".card .btn.alt", { hasText: "Deny" }).dblclick();
    const receipt = await (await accepted).json();
    await waitForCount(page, ".card", 0, 15_000);
    await waitForText(page, ".tool-row--error", "WebFetch", 15_000);

    expect(posts).toHaveLength(1);
    const recorded = await approvals(page);
    expect(recorded).toHaveLength(1);
    expect(recorded[0]?.choice_id).toBe(receipt.choice_id);
    expect(recorded[0]?.state).toBe("delivered_locally");
    const detail = await page.request.get(
      `http://127.0.0.1:${PORT}/api/v2/approvals/${recorded[0]?.choice_id}/choice`,
      { headers: { authorization: `Bearer ${TEST_TOKEN}` } },
    );
    expect((await detail.json()).choice).toEqual({ kind: "permission", allow: false });
    // The body claude received: hard rule 6's deny, once, with no rule smuggled back.
    expect(await controlResponses(PORT)).toEqual([
      { behavior: "deny", message: "The owner denied this request." },
    ]);
    await page.close();
  }, 90_000);
});
