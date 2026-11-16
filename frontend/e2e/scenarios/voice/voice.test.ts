/**
 * Dictation in real Edge against the real server.
 *
 * The browser side is the shipping code: the mic button, the AudioWorklet under the exact CSP, the
 * `/ws/voice` socket and the draft machine. Two things are stand-ins, both synthetic: Edge's fake
 * capture plays a WAV this file writes (a 440 Hz tone between silences, never a real voice or a
 * real microphone), and `scripts/e2e_server.py --voice` runs `tests/fake_voice_worker.py` in the
 * place of the speech engine. That worker ignores what the audio says: it answers `partial-<n>`
 * every 16,000 bytes and `final-<bytes>` at the end, so the draft proves the audio flowed through
 * the worklet and the socket, and the final's number is the audio's byte count.
 *
 * Run from frontend/ after a fresh build (binds 8765; check it is free first):
 *   npx vitest run -c vitest.e2e.config.ts e2e/scenarios/voice/voice.test.ts
 */
// The harness owns `e2e/node.d.ts`, which declares only what `server.ts` needs; these modules are
// this file's own business, so they are suppressed here (e2e/scenarios/shell/settings.test.ts does the same).
// @ts-expect-error see above
import { randomUUID } from "node:crypto";
// @ts-expect-error see above
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
// @ts-expect-error see above
import { tmpdir } from "node:os";
// @ts-expect-error see above
import { join } from "node:path";
import type { Browser, Page } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdgeWithFakeMic, TEST_TOKEN, waitForText } from "../../harness";
import { ORIGIN, PORT, type Server, startServer } from "../../server";

const RATE = 16_000;

/** 16 kHz mono s16le: 0.5 s of zeros, 2 s of a 440 Hz tone at -20 dBFS, 0.5 s of zeros. */
function toneWav(): Uint8Array {
  const samples = new Int16Array(RATE * 3);
  for (let i = RATE / 2; i < RATE * 2.5; i++) {
    samples[i] = Math.round(3277 * Math.sin((2 * Math.PI * 440 * i) / RATE));
  }
  const bytes = new Uint8Array(44 + samples.byteLength);
  const view = new DataView(bytes.buffer);
  const tag = (offset: number, text: string): void => {
    for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
  };
  tag(0, "RIFF");
  view.setUint32(4, 36 + samples.byteLength, true);
  tag(8, "WAVE");
  tag(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, RATE, true);
  view.setUint32(28, RATE * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  tag(36, "data");
  view.setUint32(40, samples.byteLength, true);
  samples.forEach((sample, i) => {
    view.setInt16(44 + 2 * i, sample, true);
  });
  return bytes;
}

let server: Server | undefined;
let browser: Browser | undefined;
let scratch = "";
const violations: string[] = [];

beforeAll(async () => {
  scratch = mkdtempSync(join(tmpdir(), "viki-e2e-voice-"));
  const wav = join(scratch, "tone.wav");
  writeFileSync(wav, toneWav());
  server = await startServer({
    fixture: "tests/fixtures/claude/text-two-turns.jsonl",
    voice: true,
  });
  browser = await launchEdgeWithFakeMic(wav);
}, 60_000);

afterAll(async () => {
  await browser?.close();
  await server?.stop();
  if (scratch) rmSync(scratch, { recursive: true, force: true });
});

async function open(size = { width: 1440, height: 900 }): Promise<Page> {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: size });
  // Every document records its CSP violations before any script of the app runs.
  await page.exposeFunction("reportCsp", (what: string) => {
    violations.push(what);
  });
  await page.addInitScript(() => {
    document.addEventListener("securitypolicyviolation", (event) => {
      (window as unknown as { reportCsp(what: string): void }).reportCsp(
        `${event.violatedDirective} ${event.blockedURI}`,
      );
    });
  });
  await page.goto(appUrl(PORT));
  await page.locator('[data-region="shell"]').waitFor({ timeout: 15_000 });
  // The status fetch answered "ready": an unready mic has another class and hides when narrow.
  await page.locator(".voice-mic-ready").waitFor({ timeout: 15_000 });
  return page;
}

/** The draft once it matches `pattern`; on timeout, the error names what the box held. */
async function waitForDraft(page: Page, pattern: RegExp, timeout = 15_000): Promise<string> {
  const box = page.getByLabel("Message");
  const deadline = Date.now() + timeout;
  let value = "";
  while (Date.now() < deadline) {
    value = await box.inputValue();
    if (pattern.test(value)) return value;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`the draft stayed ${JSON.stringify(value)}, never matching ${pattern}`);
}

/** The mic is not pressed and not finishing or cancelling: the next press starts a capture. */
function waitForIdleMic(page: Page): Promise<unknown> {
  return page.waitForFunction(
    () => {
      const mic = document.querySelector(".voice-mic");
      return mic?.getAttribute("aria-pressed") === "false" && !mic.hasAttribute("aria-disabled");
    },
    undefined,
    { timeout: 15_000 },
  );
}

async function count(page: Page, selector: string): Promise<number> {
  return page.locator(selector).count();
}

test("dictation fills the draft and waits for Send", async () => {
  const page = await open();
  try {
    const box = page.getByLabel("Message");
    await box.fill("meeting notes");

    // A quick click is toggle mode: it starts, and the next click finishes.
    await page.locator(".voice-mic").click();
    await waitForText(page, ".voice-provisional", "partial-");
    await page.locator(".voice-mic").click();

    // The final lands after the typed text, with the one space Latin words need.
    await waitForDraft(page, /^meeting notes final-\d+$/);
    await waitForIdleMic(page);

    // Nothing was sent: the dictation path only edits the draft.
    expect(await count(page, ".user-turn")).toBe(0);
    expect(await count(page, ".reply")).toBe(0);

    await page.locator(".composer-submit:not([disabled])").waitFor({ timeout: 15_000 });
    await box.press("Enter");
    await waitForText(page, ".user-turn", "meeting notes final-");
    await waitForText(page, ".reply", "Never edit or overwrite anything in", 20_000);
  } finally {
    await page.close();
  }
});

test("hold to talk records while the button is down", async () => {
  const page = await open();
  try {
    await page.locator(".voice-mic").hover();
    await page.mouse.down();
    await waitForText(page, ".voice-provisional", "partial-");
    await page.mouse.up();

    await waitForDraft(page, /^final-\d+$/);
    await waitForIdleMic(page);
    expect(await count(page, ".user-turn")).toBe(0);
  } finally {
    await page.close();
  }
});

test("Esc cancels dictation, keeps the typed text and idles the mic", async () => {
  const page = await open();
  try {
    const box = page.getByLabel("Message");
    await box.fill("keep this");
    await page.locator(".voice-mic").click();
    await waitForText(page, ".voice-provisional", "partial-");

    await box.press("Escape");
    await waitForIdleMic(page);

    expect(await box.inputValue()).toBe("keep this");
    expect(await count(page, ".voice-strip")).toBe(0);
    expect(await count(page, ".user-turn")).toBe(0);
    // Send is back for the typed text.
    await page.locator(".composer-submit:not([disabled])").waitFor({ timeout: 15_000 });
  } finally {
    await page.close();
  }
});

test("Enter while recording sends nothing and shows the notice", async () => {
  const page = await open();
  try {
    const box = page.getByLabel("Message");
    await box.fill("do not send yet");
    await page.locator(".voice-mic").click();
    await waitForText(page, ".voice-provisional", "partial-");

    await box.press("Enter");
    await waitForText(page, ".composer-blocked", "Esc");
    expect(await count(page, ".user-turn")).toBe(0);
    expect(await box.inputValue()).toBe("do not send yet");

    await box.press("Escape");
    await waitForIdleMic(page);
    expect(await box.inputValue()).toBe("do not send yet");
  } finally {
    await page.close();
  }
});

test("at 400 px the ready mic and Send lie inside the pill", async () => {
  const page = await open({ width: 400, height: 900 });
  try {
    await page.getByLabel("Message").fill("x");
    await page.locator(".composer-submit:not([disabled])").waitFor({ timeout: 15_000 });
    const overflowing = await page
      .locator(".composer")
      .evaluate((el) => el.scrollWidth > el.clientWidth + 1);
    expect(overflowing).toBe(false);
    const pill = await page.locator('[data-region="composer"]').boundingBox();
    if (!pill) throw new Error("the composer is not rendered");
    for (const selector of [".voice-mic", ".composer-submit"]) {
      expect(await page.locator(selector).isVisible()).toBe(true);
      const inner = await page.locator(selector).first().boundingBox();
      if (!inner) throw new Error(`${selector} is not rendered`);
      expect(inner.x).toBeGreaterThanOrEqual(pill.x);
      expect(inner.x + inner.width).toBeLessThanOrEqual(pill.x + pill.width);
      expect(inner.y).toBeGreaterThanOrEqual(pill.y);
      expect(inner.y + inner.height).toBeLessThanOrEqual(pill.y + pill.height);
    }
  } finally {
    await page.close();
  }
});

/** `GET /api/v2/voice` with the harness token. */
async function voiceStatus(): Promise<{ fallback: { code: string } | null }> {
  const response = await fetch(`${ORIGIN}/api/v2/voice`, {
    headers: { Authorization: `Bearer ${TEST_TOKEN}` },
  });
  expect(response.status).toBe(200);
  const body = (await response.json()) as { voice: { fallback: { code: string } | null } };
  return body.voice;
}

// Windows only: the fake machine has the cuda model installed but no cuda backend files, and the
// other platforms have no GPU pair. This case runs last of the dictation cases: it leaves
// `voice.backend` at cuda for the rest of the file.
test.skipIf(process.platform !== "win32")(
  "a GPU fallback shows its code and Try GPU again clears it",
  async () => {
    // The page opens first: with cuda chosen, a status read says `backend_missing` and the mic
    // refuses to start, so only a page whose status read said "ready" reaches the server's fallback.
    const page = await open();
    const headers = {
      Authorization: `Bearer ${TEST_TOKEN}`,
      Origin: ORIGIN,
      "Content-Type": "application/json",
    };
    const current = await fetch(`${ORIGIN}/api/v2/settings`, { headers });
    const { revision } = (await current.json()) as { revision: number };
    const patched = await fetch(`${ORIGIN}/api/v2/settings`, {
      method: "PATCH",
      headers,
      body: JSON.stringify({
        command_id: randomUUID(),
        expected_revision: revision,
        changes: { "voice.backend": "cuda" },
      }),
    });
    expect(patched.status).toBe(200);

    try {
      await page.locator(".voice-mic").click();
      await waitForText(page, ".voice-provisional", "partial-");
      await waitForText(page, ".voice-strip", "backend_missing");
      expect((await voiceStatus()).fallback?.code).toBe("backend_missing");

      await page.getByRole("button", { name: "Try GPU again" }).click();
      const deadline = Date.now() + 15_000;
      while ((await voiceStatus()).fallback !== null && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 100));
      }
      expect((await voiceStatus()).fallback).toBeNull();

      // The capture that was running when the fallback showed still ends with its final.
      await page.locator(".voice-mic").click();
      await waitForDraft(page, /^final-\d+$/);
      await waitForIdleMic(page);
    } finally {
      await page.close();
    }
  },
);

test("no page of this file recorded a CSP violation", () => {
  expect(violations).toEqual([]);
});
