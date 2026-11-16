/**
 * e2e: a real avatar built from a synthetic orientation-6 JPEG, against the real backend — the
 * whole vertical slice (upload with the avatar purpose, the crop transform, an authenticated
 * variant read, and the top bar's own identity) proven end to end, no fixture reply is needed
 * since nothing here sends a turn. The editor's own 64 px sample, read before Save, must show
 * the colour the saved variant shows, with and without a Rotate right.
 */
import type { Browser, Page } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, waitForText } from "../../harness";
import { PORT, type Server, startServer } from "../../server";

/**
 * The real Node `Buffer` global, reached without the ambient `Buffer` type this project omits
 * (`e2e/scenarios/chat/attachments.test.ts` carries the same note): Playwright's `setInputFiles` serialises its
 * `buffer` field by calling real `Buffer` methods, which a plain `Uint8Array` cannot stand in for.
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

/**
 * A synthetic 64x64 JPEG (a few hundred bytes) whose STORED pixels are the canonical quadrant
 * image rotated 90 degrees, tagged with EXIF Orientation 6 ("rotate 90 clockwise to display
 * upright"). A decoder that honours the tag — `createImageBitmap(file, { imageOrientation:
 * "from-image" })`, what the editor itself calls — therefore shows four solid quadrants:
 * top-left red, top-right green, bottom-left blue, bottom-right yellow. Made once offline with
 * Pillow and embedded here as a base64 constant; no other fixture file.
 */
const ORIENTED_QUADRANTS_JPEG_BASE64 =
  "/9j/4AAQSkZJRgABAQAAAQABAAD/4QAiRXhpZgAATU0AKgAAAAgAAQESAAMAAAABAAYAAAAAAAD/2wBDAAIBAQEBAQIBAQECAgICAgQDAgICAgUEBAMEBgUGBgYFBgYGBwkIBgcJBwYGCAsICQoKCgoKBggLDAsKDAkKCgr/2wBDAQICAgICAgUDAwUKBwYHCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgr/wAARCABAAEADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD1iiiiv8rz/FMKKKKAP0wooor+Tz/oACiiigD8z6KKK/rA/wCf8KKKKAP0wooor+Tz/oACiiigD+aeiiiv9VD/AGoCiiigD5Dooor/AKqD/mvCiiigD68ooor/AJVz/pQCiiigD5Dooor/AKqD/mvCiiigD//Z";

/**
 * The crop overlay's zoom keys ("+"/"-") shrink or grow the crop's edge by a step the code
 * derives from the CURRENT edge (`Math.floor(edge / 10)`, at least 1), so the reachable edges
 * from a fresh 64x64 crop (edge 64) are not every integer, and no purely-one-directional run of
 * zoom-in presses lands on 32 exactly (64, 58, 53, 48, 44, 40, 36, 33, 30, ... skips it). This
 * sequence — worked out offline against `crop.ts`'s own `zoomCrop` arithmetic and confirmed there
 * — does: six zoom-in, two zoom-out, three zoom-in, ending at edge 32 (position 19, 19).
 */
const ZOOM_TO_EDGE_32 = ["+", "+", "+", "+", "+", "+", "-", "-", "+", "+", "+"];

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

async function openApp(): Promise<Page> {
  if (!browser) throw new Error("missing browser");
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto(appUrl(PORT));
  await page.waitForSelector('[data-region="shell"]');
  return page;
}

/** Ctrl+, is the shortcut the app menu's own row documents. */
async function openSettings(page: Page): Promise<void> {
  await page.keyboard.press("Control+Comma");
  await page.waitForSelector('[data-region="settings"]');
}

interface Colour {
  red: number;
  green: number;
  blue: number;
}

/** The quadrant colours, as the editor shows the JPEG before any rotation. */
const RED: Colour = { red: 255, green: 0, blue: 0 };
const BLUE: Colour = { red: 0, green: 0, blue: 255 };

function expectColour(actual: Colour, expected: Colour): void {
  expect(Math.abs(actual.red - expected.red)).toBeLessThanOrEqual(40);
  expect(Math.abs(actual.green - expected.green)).toBeLessThanOrEqual(40);
  expect(Math.abs(actual.blue - expected.blue)).toBeLessThanOrEqual(40);
}

/**
 * Sets the avatar in `settingKey`'s row to the top-left 32 px quadrant of the editor's grid,
 * after one Rotate right when `rotateRight`, and returns the centre pixel of the editor's own
 * 64 px sample canvas (read before Save) and of the saved 64 px variant the server built.
 */
async function saveTopLeftQuadrant(
  page: Page,
  settingKey: string,
  rotateRight: boolean,
): Promise<{ sample: Colour; saved: Colour }> {
  await openSettings(page);

  const row = page.locator(`[data-setting-key="${settingKey}"]`);
  await row.getByRole("button", { name: "Set", exact: true }).click();

  const dialog = page.locator(".avatar-editor");
  await dialog.waitFor();
  await page.locator("#avatar-editor-file").setInputFiles({
    name: "quadrants.jpg",
    mimeType: "image/jpeg",
    buffer: fileBuffer(ORIENTED_QUADRANTS_JPEG_BASE64),
  });

  const overlay = page.locator(".avatar-editor-crop-overlay");
  await overlay.waitFor();
  if (rotateRight) await dialog.getByRole("button", { name: "Rotate right", exact: true }).click();
  await waitForText(page, ".avatar-editor-crop-readout", "0, 0, 64");
  for (const key of ZOOM_TO_EDGE_32) await overlay.press(key);
  await waitForText(page, ".avatar-editor-crop-readout", "19, 19, 32");

  // The zoom kept the crop's centre fixed, so it drifted off (0, 0); walk it back with the
  // large (Shift) step. moveCrop clamps at the edge, so four 10px steps land exactly on 0
  // regardless of the small rounding drift zoomCrop's own math can leave in the position.
  for (let i = 0; i < 4; i++) await overlay.press("Shift+ArrowLeft");
  for (let i = 0; i < 4; i++) await overlay.press("Shift+ArrowUp");
  await waitForText(page, ".avatar-editor-crop-readout", "0, 0, 32");

  // The sample canvases repaint in an effect after the crop changes, so read after a frame.
  const sample = await page.evaluate(async () => {
    await new Promise(requestAnimationFrame);
    const canvas = document.querySelector<HTMLCanvasElement>(
      '.avatar-editor-sample canvas[width="64"]',
    );
    const ctx = canvas?.getContext("2d");
    if (!ctx) throw new Error("no 64 px sample canvas");
    const [red = 0, green = 0, blue = 0] = ctx.getImageData(32, 32, 1, 1).data;
    return { red, green, blue };
  });

  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await dialog.waitFor({ state: "detached", timeout: 20_000 });
  await waitForText(
    page,
    `[data-setting-key="${settingKey}"] .settings-row-saved`,
    "Saved",
    20_000,
  );

  const saved = await page.evaluate(async (key) => {
    const token = sessionStorage.getItem("viki.token");
    if (!token) throw new Error("no token in sessionStorage");
    const auth = { Authorization: `Bearer ${token}` };

    const settingsResponse = await fetch("/api/v2/settings", { headers: auth });
    if (!settingsResponse.ok) throw new Error(`settings GET answered ${settingsResponse.status}`);
    const settingsBody = (await settingsResponse.json()) as {
      effective: Record<string, unknown>;
    };
    const avatarId = settingsBody.effective[key];
    if (typeof avatarId !== "string") throw new Error(`no avatar id in ${key}`);

    const avatarResponse = await fetch(`/api/v2/avatars/${avatarId}/64`, { headers: auth });
    if (!avatarResponse.ok) throw new Error(`avatar GET answered ${avatarResponse.status}`);
    const bitmap = await createImageBitmap(await avatarResponse.blob());
    const canvas = document.createElement("canvas");
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("no 2d context");
    ctx.drawImage(bitmap, 0, 0);
    const [red = 0, green = 0, blue = 0] = ctx.getImageData(
      Math.floor(bitmap.width / 2),
      Math.floor(bitmap.height / 2),
      1,
      1,
    ).data;
    return { red, green, blue };
  }, settingKey);

  return { sample, saved };
}

test("an avatar built from an orientation-6 JPEG reaches VIKI's face as a real crop", async () => {
  const page = await openApp();
  const { sample, saved } = await saveTopLeftQuadrant(page, "identity.assistant_avatar_id", false);

  expectColour(sample, RED);
  expectColour(saved, RED);
  // Settings renders with no right rail at all (App.tsx), so leave Settings — Escape, as
  // e2e/scenarios/shell/appearance.test.ts's closeSettings does — before the right rail's own Face can show the saved avatar.
  await page.keyboard.press("Escape");
  await page.waitForSelector('[data-region="conversation"]');
  await page.waitForFunction(() => {
    const img = document.querySelector<HTMLImageElement>(".face-img");
    return img?.src.startsWith("blob:");
  });
  await page.close();
});

test("after Rotate right, the saved avatar shows what the editor's sample showed", async () => {
  // A clockwise quarter turn brings the bottom-left (blue) quadrant to the top left.
  const page = await openApp();
  const { sample, saved } = await saveTopLeftQuadrant(page, "identity.user_avatar_id", true);

  expectColour(sample, BLUE);
  expectColour(saved, BLUE);
  await page.close();
});
