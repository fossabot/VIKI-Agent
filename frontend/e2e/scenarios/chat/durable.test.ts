/** Real browser + Python storage/API + synthetic provider. No user data or native model. */
import type { Browser } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, TEST_TOKEN, waitForText } from "../../harness";
import { PORT, type Server, startServer } from "../../server";

let server: Server | undefined;
let browser: Browser | undefined;
beforeAll(async () => {
  server = await startServer({ fixture: "tests/fixtures/claude/text-two-turns.jsonl" });
  browser = await launchEdge();
});
afterAll(async () => {
  await browser?.close();
  await server?.stop();
});

test("a saved unsent draft survives reload and is restored only by an explicit choice", async () => {
  if (!browser) throw new Error("missing browser");
  const page = await browser.newPage();
  await page.goto(appUrl(PORT));
  const saved = page.waitForResponse(
    (response) =>
      response.request().method() === "PUT" && response.url().includes("/api/v2/drafts/"),
  );
  await page.getByLabel("Message", { exact: true }).fill("Unsent note 🥚");
  const response = await saved;
  expect(response.status()).toBe(200);
  const draft = await response.json();
  expect(draft.text).toBe("Unsent note 🥚");
  await waitForText(page, ".draft-status", "Draft saved");
  await page.reload();
  expect(await page.getByLabel("Message", { exact: true }).inputValue()).toBe("");
  await page.getByText("Drafts and send history", { exact: true }).click();
  await page.getByLabel(`Restore text from draft ${draft.key}`, { exact: true }).click();
  await page.waitForFunction(
    () => document.querySelector<HTMLTextAreaElement>("textarea")?.value === "Unsent note 🥚",
  );
  const commands = await page.request.get(`http://127.0.0.1:${PORT}/api/v2/commands`, {
    headers: { authorization: `Bearer ${TEST_TOKEN}` },
  });
  expect((await commands.json()).commands).toHaveLength(0);
  await page.close();
});

test("submission saves its exact revision and survives reload without another POST", async () => {
  if (!browser) throw new Error("missing browser");
  const page = await browser.newPage();
  const posts: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().endsWith("/api/v2/commands"))
      posts.push(request.postData() ?? "");
  });
  await page.goto(appUrl(PORT));
  const accepted = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" && response.url().endsWith("/api/v2/commands"),
  );
  await page.getByLabel("Message", { exact: true }).fill("durable synthetic prompt");
  await page.getByLabel("Message", { exact: true }).press("Enter");
  const response = await accepted;
  expect(response.status()).toBe(202);
  const receipt = await response.json();
  expect(receipt.state).toBe("accepted");
  expect(receipt.draft_revision).toBeGreaterThan(0);
  await page.waitForFunction(
    () => document.querySelector<HTMLTextAreaElement>("textarea")?.value === "",
  );
  const submission = await page.request.get(
    `http://127.0.0.1:${PORT}/api/v2/commands/${receipt.command_id}/submission`,
    { headers: { authorization: `Bearer ${TEST_TOKEN}` } },
  );
  expect((await submission.json()).text).toBe("durable synthetic prompt");
  await page.reload();
  await page.getByText("Drafts and send history", { exact: true }).click();
  await page.getByLabel(`Restore text from send ${receipt.command_id}`, { exact: true }).click();
  await page.waitForFunction(
    () =>
      document.querySelector<HTMLTextAreaElement>("textarea")?.value === "durable synthetic prompt",
  );
  expect(posts).toHaveLength(1);
  await page.close();
});
