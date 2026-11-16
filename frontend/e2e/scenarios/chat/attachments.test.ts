/**
 * e2e: the attachment picker, drop, paste, an upload refusal, many thumbnails, a Markdown
 * text-insert offer and a reload, all against the real backend and a real (tiny) image decode —
 * no fixture reply is needed, since nothing here sends a turn.
 */
import type { Browser, JSHandle, Page } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, waitForText } from "../../harness";
import { PORT, type Server, startServer } from "../../server";

/**
 * The real Node `Buffer` global, reached without the ambient `Buffer` type this project omits
 * (see `e2e/node.d.ts`'s own docstring): Playwright's `setInputFiles` serialises its `buffer`
 * field by calling real `Buffer` methods, so a plain `Uint8Array` — whose inherited `toString()`
 * ignores an encoding argument — will not do. The cast to `never` only affects the type this file
 * sees; the value handed to Playwright at runtime is a genuine `Buffer`.
 */
interface NodeBufferLike {
  toString(encoding: string): string;
}
interface NodeBufferStatic {
  from(data: string, encoding: string): NodeBufferLike;
}
const nodeBuffer = (globalThis as unknown as { Buffer: NodeBufferStatic }).Buffer;
function fileBuffer(base64: string, encoding: "base64" | "utf-8" = "base64"): never {
  return nodeBuffer.from(base64, encoding) as never;
}

/** A tiny valid 1x1 PNG (68 bytes), decodes cleanly through the real image checker. */
const PNG_BASE64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=";

const READY = "Ready";
const DECODE_FAILED_COPY = "This is not a PNG, JPEG or WebP still image of up to 40 megapixels.";

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

/**
 * A fresh page on a fresh "new chat" editor, held open until the image limits are known and the
 * settings document has loaded — so the first attach in a test never races either the capability
 * fetch (into a "checking limits" refusal) or the settings load. The strip's copy is English
 * whatever this `ja-JP` context says: the UI is English only. The wait is for the
 * `GET /api/v2/settings` response, not for the page's language. */
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

/** Builds a `File` inside the page (Playwright can only feed real bytes to an `<input>` through
 * `setInputFiles`; drop and paste need a live `DataTransfer` built the same way) and wraps it in
 * a `DataTransfer`, optionally beside a `text/plain` entry. */
function fileDataTransfer(
  page: Page,
  name: string,
  base64: string,
  withText = false,
): Promise<JSHandle<DataTransfer>> {
  return page.evaluateHandle(
    ({ name, base64, withText }) => {
      const binary = atob(base64);
      const bytes = new Uint8Array(binary.length);
      for (let index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index);
      const file = new File([bytes], name, { type: "image/png" });
      const transfer = new DataTransfer();
      transfer.items.add(file);
      if (withText) transfer.setData("text/plain", "some ordinary pasted text");
      return transfer;
    },
    { name, base64, withText },
  );
}

/** Dispatches a real `paste` `ClipboardEvent` on the message box and reports `defaultPrevented`. */
function pasteInto(page: Page, clipboardData: JSHandle<DataTransfer>): Promise<boolean> {
  return page.evaluate((data) => {
    const target = document.querySelector("textarea.composer-box");
    if (!target) throw new Error("no composer textarea");
    const event = new ClipboardEvent("paste", {
      clipboardData: data,
      bubbles: true,
      cancelable: true,
    });
    target.dispatchEvent(event);
    return event.defaultPrevented;
  }, clipboardData);
}

test("the picker gives a Ready chip", async () => {
  const page = await openComposer();
  await page.locator("input.attachment-picker-input").setInputFiles({
    name: "picked.png",
    mimeType: "image/png",
    buffer: fileBuffer(PNG_BASE64),
  });
  await waitForText(page, ".attachment-item-status", READY);
  await page.close();
});

test("a drop on the composer gives a chip", async () => {
  const page = await openComposer();
  const dataTransfer = await fileDataTransfer(page, "dropped.png", PNG_BASE64);
  await page.dispatchEvent('[data-region="composer"]', "drop", { dataTransfer });
  await waitForText(page, ".attachment-item-status", READY);
  await page.close();
});

test("a paste of only a file gives a chip and is prevented", async () => {
  const page = await openComposer();
  const clipboardData = await fileDataTransfer(page, "pasted.png", PNG_BASE64);
  expect(await pasteInto(page, clipboardData)).toBe(true);
  await waitForText(page, ".attachment-item-status", READY);
  await page.close();
});

test("a paste of text and a file leaves the text to the browser: no chip, not prevented", async () => {
  const page = await openComposer();
  const clipboardData = await fileDataTransfer(page, "pasted-with-text.png", PNG_BASE64, true);
  expect(await pasteInto(page, clipboardData)).toBe(false);
  expect(await page.locator(".attachment-item").count()).toBe(0);
  await page.close();
});

test("a spoofed .png shows the decode_failed copy and no Retry", async () => {
  const page = await openComposer();
  await page.locator("input.attachment-picker-input").setInputFiles({
    name: "spoof.png",
    mimeType: "image/png",
    buffer: fileBuffer("this is plain text, not a png", "utf-8"),
  });
  await waitForText(page, ".attachment-item-error", DECODE_FAILED_COPY);
  expect(await page.getByRole("button", { name: "Retry" }).count()).toBe(0);
  await page.close();
});

test("ten images give ten loaded thumbnails", async () => {
  const page = await openComposer();
  const files = Array.from({ length: 10 }, (_, index) => ({
    name: `image-${index}.png`,
    mimeType: "image/png",
    buffer: fileBuffer(PNG_BASE64),
  }));
  await page.locator("input.attachment-picker-input").setInputFiles(files);
  await page.waitForFunction(
    () => {
      const thumbs = [...document.querySelectorAll<HTMLImageElement>(".attachment-thumb")];
      return thumbs.length === 10 && thumbs.every((img) => img.complete && img.naturalWidth > 0);
    },
    undefined,
    { timeout: 30_000 },
  );
  await page.close();
});

test("a .md via the picker offers Insert into message, which inserts its text", async () => {
  const page = await openComposer();
  const noteText = "# a note\n\nsome body text for the insert.";
  await page.locator("input.attachment-picker-input").setInputFiles({
    name: "note.md",
    mimeType: "text/markdown",
    buffer: fileBuffer(noteText, "utf-8"),
  });
  await page.getByRole("button", { name: "Insert into message", exact: true }).click();
  await page.waitForFunction(
    (want) => document.querySelector<HTMLTextAreaElement>("textarea.composer-box")?.value === want,
    noteText,
  );
  await page.close();
});

test("a Ready chip survives a reload", async () => {
  const page = await openComposer();
  const saved = page.waitForResponse(
    (response) =>
      response.request().method() === "PUT" && response.url().includes("/api/v2/drafts/"),
  );
  await page.locator("input.attachment-picker-input").setInputFiles({
    name: "kept.png",
    mimeType: "image/png",
    buffer: fileBuffer(PNG_BASE64),
  });
  await waitForText(page, ".attachment-item-status", READY);
  const response = await saved;
  const draft = (await response.json()) as { key: string; attachments?: readonly unknown[] };
  expect(draft.attachments).toHaveLength(1);

  const reloaded = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v2/settings" &&
      response.request().method() === "GET",
  );
  await page.reload();
  await reloaded; // the settings document
  await page.getByText("Drafts and send history", { exact: true }).click();
  await page.getByLabel(`Restore text from draft ${draft.key}`, { exact: true }).click();
  await waitForText(page, ".attachment-item-status", READY);
  await page.close();
});
