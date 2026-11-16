/**
 * Hands-free conversation in real Edge against the real server.
 *
 * Three stand-ins, all synthetic: Edge's fake capture plays a WAV this file writes (a 440 Hz tone,
 * never a voice or a real microphone), `scripts/e2e_server.py --voice` runs
 * `tests/fake_voice_worker.py` (`partial-<n>` every 16,000 bytes, `final-<bytes>` at the end), and
 * `addInitScript` swaps `speechSynthesis` for a recorder, as `e2e/scenarios/voice/read-aloud.test.ts` does. The
 * fake worker sends its final only at the end of a capture, so here a mic press ends the capture;
 * the segment-final trigger is covered by `useDictation.endOnFinal.unit.test.tsx` only.
 *
 * Every string asserted is the `en` catalog row (this harness's Edge asks for English).
 *
 * Run from frontend/ after a fresh build (binds 8765; check it is free first):
 *   npx vitest run -c vitest.e2e.config.ts e2e/scenarios/voice/hands-free.test.ts
 */
// The harness owns `e2e/node.d.ts`, which declares only what `server.ts` needs; these modules are
// this file's own business, so they are suppressed here (e2e/scenarios/voice/voice.test.ts does the same).
// @ts-expect-error see above
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
// @ts-expect-error see above
import { tmpdir } from "node:os";
// @ts-expect-error see above
import { join } from "node:path";
import type { Browser, Page } from "playwright-core";
import { afterAll, beforeAll, describe, expect, test } from "vitest";
import { appUrl, launchEdgeWithFakeMic, waitForCount, waitForText } from "../../harness";
import { PORT, type Server, startServer } from "../../server";

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

/** Plain JS on purpose: the page runs this text, so no compiler helper can leak into it. */
const FAKE_ENGINE = `
(() => {
  const voice = (name, lang, uri, localService, isDefault) => ({
    name, lang, voiceURI: uri, localService, default: isDefault,
  });
  const voices = [
    voice("Fake Zira", "en-US", "fake-zira", true, true),
    voice("Fake Aria Online (Natural)", "en-US", "fake-aria-online", false, false),
  ];
  const speech = { log: [], cancels: 0, current: null,
    finish() {
      const u = speech.current;
      speech.current = null;
      synth.speaking = false;
      if (u && u.onend) u.onend({});
    },
  };
  class FakeUtterance {
    constructor(text) {
      this.text = text; this.voice = null; this.lang = ""; this.rate = 1;
      this.onstart = null; this.onend = null; this.onerror = null;
    }
  }
  const synth = new EventTarget();
  synth.speaking = false;
  synth.paused = false;
  synth.pending = false;
  synth.getVoices = () => voices;
  synth.speak = (u) => {
    speech.log.push({ text: u.text, voice: u.voice ? u.voice.voiceURI : null });
    speech.current = u;
    synth.speaking = true;
    if (u.onstart) u.onstart({});
  };
  synth.cancel = () => {
    speech.cancels += 1;
    const u = speech.current;
    speech.current = null;
    synth.speaking = false;
    if (u && u.onerror) u.onerror({ error: "interrupted" });
  };
  synth.pause = () => { synth.paused = true; };
  synth.resume = () => { synth.paused = false; };
  window.__speech = speech;
  window.SpeechSynthesisUtterance = FakeUtterance;
  Object.defineProperty(window, "speechSynthesis", { configurable: true, value: synth });
  setTimeout(() => synth.dispatchEvent(new Event("voiceschanged")), 50);
})();
`;

interface Spoken {
  text: string;
  voice: string | null;
}

let browser: Browser | undefined;
let scratch = "";

beforeAll(async () => {
  scratch = mkdtempSync(join(tmpdir(), "viki-e2e-hands-free-"));
  const wav = join(scratch, "tone.wav");
  writeFileSync(wav, toneWav());
  browser = await launchEdgeWithFakeMic(wav);
}, 60_000);

afterAll(async () => {
  await browser?.close();
  if (scratch) rmSync(scratch, { recursive: true, force: true });
});

async function open(): Promise<Page> {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.addInitScript(FAKE_ENGINE);
  await page.goto(appUrl(PORT));
  await page.locator('[data-region="shell"]').waitFor({ timeout: 15_000 });
  await page.locator(".voice-mic-ready").waitFor({ timeout: 15_000 });
  return page;
}

const spokenOf = (page: Page): Promise<Spoken[]> =>
  page.evaluate(() => (window as unknown as { __speech: { log: Spoken[] } }).__speech.log);

const toggle = (page: Page) => page.getByRole("button", { name: "Hands-free", exact: true });
const strip = ".hands-free-strip";

/** A pressed mic: a capture is open. */
function waitForOpenMic(page: Page): Promise<unknown> {
  return page.waitForFunction(
    () => document.querySelector(".voice-mic")?.getAttribute("aria-pressed") === "true",
    undefined,
    { timeout: 15_000 },
  );
}

/** The mic is not pressed and not finishing or cancelling. */
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

/** Click the toggle; the first time, confirm the read-aloud notice that comes before the mic. */
async function enterHandsFree(page: Page): Promise<void> {
  await toggle(page).click();
  await page.getByRole("button", { name: "Start hands-free", exact: true }).click();
  await waitForText(page, strip, "Listening");
  expect(await toggle(page).getAttribute("aria-pressed")).toBe("true");
}

/** End each utterance the controller speaks until the loop listens again. */
async function finishSpeaking(page: Page): Promise<void> {
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    const listening = await page.evaluate(
      (selector) => (document.querySelector(selector)?.textContent ?? "").includes("Listening"),
      strip,
    );
    if (listening) return;
    await page.evaluate(() => {
      const speech = (window as unknown as { __speech: { current: unknown; finish(): void } })
        .__speech;
      if (speech.current) speech.finish();
    });
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("hands-free never listened again after the reply was spoken");
}

describe("a spoken reply", () => {
  let server: Server | undefined;
  beforeAll(async () => {
    server = await startServer({
      fixture: "tests/fixtures/claude/text-two-turns.jsonl",
      voice: true,
    });
  }, 60_000);
  afterAll(async () => {
    await server?.stop();
  });

  test("listens, sends the final itself, speaks the reply, listens again and leaves on Esc", async () => {
    const page = await open();
    try {
      await enterHandsFree(page);
      await waitForOpenMic(page);
      await waitForText(page, ".voice-provisional", "partial-");

      // A mic press ends the capture; its final sends itself, with no Enter and no Send click.
      await page.locator(".voice-mic").click();
      await waitForText(page, ".user-turn", "final-", 20_000);
      await waitForText(page, ".reply", "Never edit or overwrite anything in", 20_000);

      // The reply is spoken with the local voice, and no capture is open while it speaks.
      await waitForText(page, strip, "Speaking", 15_000);
      expect(await page.locator(".voice-mic").getAttribute("aria-pressed")).toBe("false");
      const spoken = await spokenOf(page);
      expect(spoken.length).toBeGreaterThan(0);
      expect(spoken.map((entry) => entry.text).join(" ")).toContain("Never edit");
      for (const entry of spoken) expect(entry.voice).toBe("fake-zira");

      // The end of the speech re-arms the mic: a new capture records.
      await finishSpeaking(page);
      await waitForOpenMic(page);
      await waitForText(page, ".voice-provisional", "partial-");
      expect(await page.locator(".user-turn").count()).toBe(1);

      // Esc leaves the mode and cancels the capture: no final lands and nothing more is sent.
      await page.keyboard.press("Escape");
      await waitForIdleMic(page);
      await waitForCount(page, strip, 0);
      expect(await toggle(page).getAttribute("aria-pressed")).toBe("false");
      await new Promise((resolve) => setTimeout(resolve, 1_500));
      expect(await page.getByLabel("Message").inputValue()).toBe("");
      expect(await page.locator(".user-turn").count()).toBe(1);
    } finally {
      await page.close();
    }
  });
});

describe("a permission card", () => {
  let server: Server | undefined;
  beforeAll(async () => {
    server = await startServer({
      fixture: "tests/fixtures/claude/write-permission.jsonl",
      voice: true,
    });
  }, 60_000);
  afterAll(async () => {
    await server?.stop();
  });

  test("pauses the loop: it waits, says so once and opens no capture", async () => {
    const page = await open();
    try {
      await enterHandsFree(page);
      await waitForOpenMic(page);
      await waitForText(page, ".voice-provisional", "partial-");
      await page.locator(".voice-mic").click();
      await waitForText(page, ".user-turn", "final-", 20_000);

      await page.locator(".card").first().waitFor({ timeout: 15_000 });
      await waitForText(page, strip, "Waiting for you", 15_000);
      await expect
        .poll(async () => (await spokenOf(page)).map((entry) => entry.text))
        .toContain("Approval needed on screen");

      // No capture opens while the card waits, and voice answers nothing.
      await new Promise((resolve) => setTimeout(resolve, 1_500));
      expect(await page.locator(".voice-mic").getAttribute("aria-pressed")).toBe("false");
      expect(await page.locator(".card").count()).toBeGreaterThan(0);
      const approvals = (await spokenOf(page)).filter(
        (entry) => entry.text === "Approval needed on screen",
      );
      expect(approvals).toHaveLength(1);

      // The first Esc leaves the mode and leaves the turn (and its card) running.
      await page.keyboard.press("Escape");
      await waitForCount(page, strip, 0);
      expect(await toggle(page).getAttribute("aria-pressed")).toBe("false");
      expect(await page.locator(".card").count()).toBeGreaterThan(0);
      expect(await page.locator(".composer-stop").count()).toBe(1);
    } finally {
      await page.close();
    }
  });
});
