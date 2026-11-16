/**
 * e2e: the decision surface round-trips through the real server.
 *
 * A card's wording (the chip, the question, an option's label, a tool's name) is decided by the
 * mapper from a real `can_use_tool` control_request, not invented in the browser — and the
 * user's click has to become a real answer that the CLI's own pending request is actually
 * waiting on, or nothing after it should ever arrive. That click is a POST to the durable
 * approval ledger (`e2e/scenarios/chat/approval.test.ts` covers the record itself), which the
 * server delivers through the same seam as the two socket answer messages (kept as a
 * compatibility transport), so what reaches claude does not depend on the transport. A
 * component test with a stubbed `onAnswer`/`onRespond` cannot show either fact: it proves the
 * card calls the prop, not that the prop's frame is what unblocks the process. Two fixtures, two
 * servers (each recorded from a single real turn that stops at exactly one card): a question
 * card answered by picking an option, and a tool permission card denied — judged first the way
 * the user would judge it, by what the app does next: the card disappears and the turn moves on
 * and finishes.
 *
 * WHAT "proven" MEANS HERE, because it takes two different things. `tests/replay.py` matches a
 * `control_response` on its `request_id` alone (`Replayer.feed`) — it never looks at the body
 * the click produced. So the turn carrying on proves that *a* response for the exact pending
 * request reached the CLI (nothing else in the script would unblock it, and the turn would hang
 * and the test would time out otherwise), and nothing about *what it said*: that half would stay
 * green with allow and deny inverted, or with the answers map keyed by the chip's `header`
 * instead of the question text — the bug cfe7c01 fixed. The body is therefore read back
 * from the server, over `/api/e2e/control-responses`, a route `scripts/e2e_server.py` adds to
 * its own app object and a real launch has no way to serve. What that route returns is the
 * frame claude actually received, so clicking Allow once instead of Deny, or Coffee instead of Tea,
 * fails these tests rather than passing them.
 */
import type { Browser, Page } from "playwright-core";
import { afterAll, beforeAll, describe, expect, test } from "vitest";
import {
  appUrl,
  controlResponses,
  launchEdge,
  recordControlResponses,
  send,
  textOf,
  waitForCount,
  waitForText,
} from "../../harness";
import { PORT, type Server, startServer } from "../../server";

// Before either describe's own beforeAll, which is where the servers start: the recorder is
// chosen once, at server start-up. Off again at the end, because the e2e files share a worker.
beforeAll(() => {
  recordControlResponses(true);
});

afterAll(() => {
  recordControlResponses(false);
});

async function open(browser: Browser): Promise<Page> {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto(appUrl(PORT));
  await page.locator('[data-region="shell"]').waitFor({ timeout: 15_000 });
  return page;
}

describe("a question card (AskUserQuestion)", () => {
  let server: Server | undefined;
  let browser: Browser | undefined;

  beforeAll(async () => {
    server = await startServer({ fixture: "tests/fixtures/claude/ask-user-question.jsonl" });
    browser = await launchEdge();
  }, 60_000);

  afterAll(async () => {
    await browser?.close();
    await server?.stop();
  });

  test("shows the model's own question and options, and picking one unblocks the CLI's pending request so the turn finishes", async () => {
    if (!browser) throw new Error("Edge did not launch");
    const page = await open(browser);

    await send(page, "tea or coffee?");
    // Cards.tsx: the chip is the question's own `header`, not a fixed label — this is the
    // fixture's AskUserQuestion input, worded nowhere in the frontend.
    await waitForText(page, ".card .chip", "Beverage", 15_000);
    expect(await textOf(page, ".card-question")).toBe("Do you prefer Tea or Coffee?");
    await waitForText(page, ".card-option", "Tea");
    await waitForText(page, ".card-option", "Coffee");

    // A pick only selects (Cards.tsx's `selectOption`); the card's own Submit sends the answer.
    await page.locator(".card-option", { hasText: "Tea" }).first().click();
    await page.locator(".card .btn.go", { hasText: "Submit" }).click();

    // The card disabling itself proves only that React re-rendered; the fixture's script will
    // not send its closing line ("You chose Tea.") unless a question.answer for this exact
    // pending request actually reached the CLI — so this line is the wire proof that the click
    // produced a real frame, not a local UI change.
    await waitForCount(page, ".card", 0, 15_000);
    await waitForText(page, ".reply .md", "You chose Tea.", 15_000);
    await waitForCount(page, ".composer-stop", 0);

    // ...and this is what that frame said. One answer, an allow, and an answers map keyed by the
    // QUESTION — not by "Beverage", the chip's `header`, which is the shape cfe7c01 had to fix
    // and which the fixture's own recorded continuation would replay word for word regardless.
    // Clicking "Coffee" above changes the value here; nothing else in the run would notice.
    const sent = await controlResponses(PORT);
    expect(sent.map((answer) => answer.behavior)).toEqual(["allow"]);
    expect(sent[0]?.updatedInput?.answers).toEqual({ "Do you prefer Tea or Coffee?": "Tea" });
    // Hard rule 6: an answer never carries permission rules back.
    expect(sent[0]?.updatedPermissions).toBeUndefined();
    await page.close();
  });
});

describe("a permission card, denied (WebFetch)", () => {
  let server: Server | undefined;
  let browser: Browser | undefined;

  beforeAll(async () => {
    server = await startServer({ fixture: "tests/fixtures/claude/webfetch-denied.jsonl" });
    browser = await launchEdge();
  }, 60_000);

  afterAll(async () => {
    await browser?.close();
    await server?.stop();
  });

  test("shows the tool's own question, and responding to it unblocks the CLI so the user sees the refusal", async () => {
    if (!browser) throw new Error("Edge did not launch");
    const page = await open(browser);

    await send(page, "go fetch that page for me");
    // Cards.tsx: for a "tool" card the chip is fixed ("Waiting for approval"); the question is
    // mapper.tool_question(tool) — WebFetch's own sentence, not a generic one.
    await waitForText(page, ".card .chip", "Waiting for approval", 15_000);
    expect(await textOf(page, ".card-question")).toBe("May I read this web page?");
    expect(await textOf(page, ".card-tool-name")).toBe("WebFetch");

    await page.locator(".card .btn.alt", { hasText: "Deny" }).click();

    // ErrorCard/NoticeRow have nothing to say about a plain tool denial (that path is only for
    // an auto-mode refusal the CLI itself decides on); what the user actually sees for a denied
    // *card* is the tool's own row turning up marked failed — read from Blocks.tsx's
    // ToolBlockRows, which is what a click through `.tool-row--error` actually renders, not a
    // guess at a message that does not exist. The fixture's own recorded continuation (a
    // tool_result with is_error true, "The owner declined this fetch.") only plays once a
    // permission.respond for this exact pending request reached the CLI, so this is the wire
    // proof that the click produced a real frame.
    await waitForCount(page, ".card", 0, 15_000);
    await waitForText(page, ".tool-row--error", "WebFetch", 15_000);
    await waitForCount(page, ".composer-stop", 0, 15_000);

    // ...and this is the frame itself, the whole body, which is the only place deny and allow are
    // distinguishable in this run: that continuation is an is_error tool_result either way. The
    // deny is hard rule 6's, the message is the one the user's refusal carries back, and there
    // is nothing else in the object — no updatedPermissions, so a denial cannot smuggle a rule.
    const sent = await controlResponses(PORT);
    expect(sent).toEqual([{ behavior: "deny", message: "The owner denied this request." }]);
    await page.close();
  });
});
