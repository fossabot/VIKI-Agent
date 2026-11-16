/**
 * e2e: durability across a lost response, a double Enter, a stalled upload, all
 * checked against what the server and the fake CLI actually received, not only what the DOM
 * shows — a duplicate or a silent auto-send would leave the DOM looking right while the server
 * side told a different story.
 *
 * Each test starts its own server (`startServer` inside the test, stopped in `afterEach`), so
 * `GET /api/v2/commands` start empty and an exact count is never
 * polluted by an earlier test's session.
 */
import type { Browser, Page } from "playwright-core";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, TEST_TOKEN, waitForText } from "../../harness";
import { PORT, type Server, startServer } from "../../server";

/**
 * The real Node `Buffer` global, reached without the ambient `Buffer` type this project omits
 * (see `e2e/node.d.ts`'s own docstring): Playwright's `setInputFiles` serialises its `buffer`
 * field by calling real `Buffer` methods, so a plain `Uint8Array` will not do. The cast to
 * `never` only affects the type this file sees; the value handed to Playwright at runtime is a
 * genuine `Buffer`.
 */
interface NodeBufferLike {
  toString(encoding: string): string;
}
interface NodeBufferStatic {
  from(data: string, encoding: string): NodeBufferLike;
}
const nodeBuffer = (globalThis as unknown as { Buffer: NodeBufferStatic }).Buffer;
function fileBuffer(base64: string): never {
  return nodeBuffer.from(base64, "base64") as never;
}

/** A tiny valid 1x1 PNG (68 bytes); never actually decoded in this file, since the one test that
 * attaches it holds the upload request open and lets it stall. */
const PNG_BASE64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=";

/** The fixture's fixed first reply, unique enough to prove a turn actually completed. */
const FIRST_REPLY = "Prime directive 1";

let browser: Browser | undefined;
let server: Server | undefined;

beforeAll(async () => {
  browser = await launchEdge();
});

afterAll(async () => {
  await browser?.close();
});

afterEach(async () => {
  await server?.stop();
  server = undefined;
});

/** A fresh page on a fresh "new chat" editor, held open until the `GET /api/v2/settings`
 * response arrives and the attachment-capability fetch has
 * resolved: every case here types into the composer, so both races e2e/scenarios/chat/attachments.test.ts
 * guards against apply here too. */
async function openComposer(): Promise<Page> {
  if (!browser) throw new Error("missing browser");
  const page = await browser.newPage({ locale: "ja-JP" });
  const settings = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v2/settings" &&
      response.request().method() === "GET",
  );
  const capabilities = page.waitForResponse((response) =>
    response.url().endsWith("/api/v2/attachment-capabilities"),
  );
  await page.goto(appUrl(PORT));
  await page.locator('[data-region="shell"]').waitFor({ timeout: 15_000 });
  await settings;
  await capabilities;
  return page;
}

/** Reload and wait for the `GET /api/v2/settings` response. */
async function reloadSettled(page: Page): Promise<void> {
  const settings = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v2/settings" &&
      response.request().method() === "GET",
  );
  await page.reload();
  await settings;
}

function commandPosts(page: Page): string[] {
  const posts: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().endsWith("/api/v2/commands"))
      posts.push(request.postData() ?? "");
  });
  return posts;
}

async function commandCount(page: Page): Promise<number> {
  const response = await page.request.get(`http://127.0.0.1:${PORT}/api/v2/commands`, {
    headers: { authorization: `Bearer ${TEST_TOKEN}` },
  });
  return ((await response.json()) as { commands: unknown[] }).commands.length;
}

test("a lost command POST response leaves exactly one command and one turn after a reload", async () => {
  server = await startServer({ fixture: "tests/fixtures/claude/text-two-turns.jsonl" });
  const page = await openComposer();
  const posts = commandPosts(page);
  await page.route("**/api/v2/commands", async (route) => {
    if (route.request().method() !== "POST") {
      await route.continue();
      return;
    }
    // The request really reaches the server — its command really commits — and only the page's
    // own view of the response is lost, which is what a dropped connection looks like.
    const response = await route.fetch();
    expect(response.status()).toBe(202);
    await route.abort();
  });
  const box = page.getByLabel("Message", { exact: true });
  await box.fill("a lost response prompt");
  await page.locator(".composer-submit:not([disabled])").waitFor({ timeout: 15_000 });
  await box.press("Enter");
  await waitForText(page, ".reply .md", FIRST_REPLY);

  await reloadSettled(page);
  await waitForText(page, ".user-turn", "a lost response prompt");
  expect(await page.locator(".user-turn").count()).toBe(1);
  expect(await commandCount(page)).toBe(1);
  expect(posts).toHaveLength(1);
  await page.close();
});

test("a double Enter gives exactly one command POST", async () => {
  server = await startServer({ fixture: "tests/fixtures/claude/text-two-turns.jsonl" });
  const page = await openComposer();
  const posts = commandPosts(page);
  const accepted = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" && response.url().endsWith("/api/v2/commands"),
  );
  const box = page.getByLabel("Message", { exact: true });
  await box.fill("double enter prompt");
  await page.locator(".composer-submit:not([disabled])").waitFor({ timeout: 15_000 });
  // Two presses back to back, with no wait between them: the second must land while the first
  // send is still in flight, which is exactly the race `sending.current` in Composer exists for.
  await Promise.all([box.press("Enter"), box.press("Enter")]);
  const response = await accepted;
  expect(response.status()).toBe(202);
  await waitForText(page, ".reply .md", FIRST_REPLY);

  expect(await page.locator(".user-turn").count()).toBe(1);
  expect(await commandCount(page)).toBe(1);
  expect(posts).toHaveLength(1);
  await page.close();
});

test("a reload while an attachment upload is held shows no Ready chip and no command", async () => {
  server = await startServer({ fixture: "tests/fixtures/claude/text-two-turns.jsonl" });
  const page = await openComposer();
  const box = page.getByLabel("Message", { exact: true });
  const saved = page.waitForResponse(
    (response) =>
      response.request().method() === "PUT" && response.url().includes("/api/v2/drafts/"),
  );
  await box.fill("held upload prompt");
  const draft = (await (await saved).json()) as { key: string };

  let releaseUpload = () => {};
  await page.route("**/api/v2/assets", async (route) => {
    if (route.request().method() !== "POST") {
      await route.continue();
      return;
    }
    // Never forwarded and never resolved: the server never even sees this request, which is
    // what a stalled upload — not merely a lost response — looks like from the page.
    await new Promise<void>((resolve) => {
      releaseUpload = resolve;
    });
    await route.abort().catch(() => {});
  });
  await page.locator("input.attachment-picker-input").setInputFiles({
    name: "held.png",
    mimeType: "image/png",
    buffer: fileBuffer(PNG_BASE64),
  });
  await waitForText(page, ".attachment-item-status", "Uploading");

  await reloadSettled(page);
  expect(await page.locator(".attachment-item").count()).toBe(0);
  expect(await commandCount(page)).toBe(0);

  // The text half of the same draft is still there: a stalled upload never blocked the autosave.
  await page.getByText("Drafts and send history", { exact: true }).click();
  await page.getByLabel(`Restore text from draft ${draft.key}`, { exact: true }).click();
  await page.waitForFunction(
    () => document.querySelector<HTMLTextAreaElement>("textarea")?.value === "held upload prompt",
  );
  // The restore only brought back the text half of the draft: the held upload was never saved
  // into it as an attachment, so no chip appears once the restored draft is showing either.
  expect(await page.locator(".attachment-item").count()).toBe(0);

  releaseUpload();
  await page.close();
});
