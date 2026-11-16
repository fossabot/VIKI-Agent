/**
 * Read aloud in real Edge against the real backend, with a fake speech engine.
 *
 * `addInitScript` swaps `speechSynthesis` and `SpeechSynthesisUtterance` for a recorder before the
 * app loads, so nothing real speaks. The page's controller, the reply's controls, the first-use
 * notice and the Settings voice field run for real; what only the whole stack answers is that the
 * saved speed and voice reach `speak`, that the online voice is neither listed nor spoken with,
 * and that leaving the reply (Settings, New session) cancels the playback.
 *
 * Every string asserted is the `en` catalog row (this harness's Edge asks for English).
 *
 * Run from frontend/ (binds 8765; check it is free first):
 *   npx vitest run -c vitest.e2e.config.ts e2e/scenarios/voice/read-aloud.test.ts
 */
import type { Browser, Page } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, send, waitForCount, waitForText } from "../../harness";
import { type Server, startServer } from "../../server";
import { newSidebarSession } from "../../sidebar-navigation";

interface Logged {
  text: string;
  voice: string | null;
  lang: string;
  rate: number;
}

interface Counters {
  cancels: number;
  pauses: number;
  resumes: number;
}

/** Plain JS on purpose: the page runs this text, so no compiler helper can leak into it. */
const FAKE_ENGINE = `
(() => {
  const voice = (name, lang, uri, localService, isDefault) => ({
    name, lang, voiceURI: uri, localService, default: isDefault,
  });
  const voices = [
    voice("Fake Haruka", "ja-JP", "fake-haruka", true, false),
    voice("Fake Zira", "en-US", "fake-zira", true, true),
    voice("Fake David", "en-US", "fake-david", true, false),
    voice("Fake Aria Online (Natural)", "en-US", "fake-aria-online", false, false),
  ];
  const speech = { log: [], cancels: 0, pauses: 0, resumes: 0, current: null,
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
    speech.log.push({ text: u.text, voice: u.voice ? u.voice.voiceURI : null, lang: u.lang, rate: u.rate });
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
  synth.pause = () => { speech.pauses += 1; synth.paused = true; };
  synth.resume = () => { speech.resumes += 1; synth.paused = false; };
  window.__speech = speech;
  window.SpeechSynthesisUtterance = FakeUtterance;
  Object.defineProperty(window, "speechSynthesis", { configurable: true, value: synth });
  setTimeout(() => synth.dispatchEvent(new Event("voiceschanged")), 50);
})();
`;

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

const logOf = (page: Page): Promise<Logged[]> =>
  page.evaluate(() => (window as unknown as { __speech: { log: Logged[] } }).__speech.log);

const countersOf = (page: Page): Promise<Counters> =>
  page.evaluate(() => {
    const { cancels, pauses, resumes } = (window as unknown as { __speech: Counters }).__speech;
    return { cancels, pauses, resumes };
  });

const readButton = (page: Page) => page.getByRole("button", { name: "Read this reply aloud" });
const pauseButton = (page: Page) => page.getByRole("button", { name: "Pause reading aloud" });
const resumeButton = (page: Page) => page.getByRole("button", { name: "Resume reading aloud" });
const stopButton = (page: Page) => page.getByRole("button", { name: "Stop reading aloud" });

async function openSettingsChat(page: Page): Promise<void> {
  await page.keyboard.press("Control+Comma");
  await page.waitForSelector('[data-region="settings"]');
  await page.locator("#settings-tab-chat").click();
}

async function backToChat(page: Page): Promise<void> {
  if (await page.locator('[data-region="conversation"]').isVisible()) return;
  const settings = page.locator('[data-region="settings"]');
  const selected = await settings
    .locator('[role="tab"][aria-selected="true"]')
    .getAttribute("data-page");
  expect(["general", "chat"]).toContain(selected);
  const back = settings.getByRole("button", { name: "Back", exact: true });
  expect(await back.isVisible()).toBe(true);
  expect(await back.isEnabled()).toBe(true);
  await back.focus();
  await page.keyboard.press("Escape");
  if (selected === "chat") {
    await settings.locator('[data-page="general"][aria-selected="true"]').waitFor();
    expect(await settings.isVisible()).toBe(true);
    expect(await back.isVisible()).toBe(true);
    expect(await back.isEnabled()).toBe(true);
    await back.focus();
    await page.keyboard.press("Escape");
  }
  await page.waitForSelector('[data-region="conversation"]');
}

test("read aloud speaks with the saved voice and speed, never an online voice, and stops on leaving", async () => {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.addInitScript(FAKE_ENGINE);
  await page.goto(appUrl());
  await page.locator('[data-region="shell"]').waitFor({ timeout: 15_000 });

  await send(page, "prime directive 1?");
  await waitForText(page, ".reply", "Prime directive 1", 20_000);
  await waitForCount(page, ".composer-stop", 0);

  // Settings → Chat: speed and the English voice, saved, and no online voice offered.
  await openSettingsChat(page);
  const rate = page.locator('[data-setting-key="voice.read_aloud_rate"]');
  await rate.locator("select").selectOption("1.5");
  await waitForText(page, '[data-setting-key="voice.read_aloud_rate"]', "Saved");
  const voices = page.locator('[data-setting-key="voice.read_aloud_voices"]');
  await voices.getByLabel("English", { exact: true }).selectOption("fake-david");
  await waitForText(page, '[data-setting-key="voice.read_aloud_voices"]', "Saved");
  const options = await voices.locator("option").allTextContents();
  expect(options).toContain("Fake David");
  expect(options.filter((name) => /Online|Aria/.test(name))).toEqual([]);
  await backToChat(page);

  // First Read: the notice, then the confirmed Read speaks with the saved voice and speed.
  await readButton(page).click();
  await page.locator(".read-aloud-note").waitFor();
  await page.getByRole("button", { name: "Read aloud", exact: true }).click();
  await expect.poll(async () => (await logOf(page)).length).toBeGreaterThan(0);
  const first = (await logOf(page))[0];
  expect(first?.voice).toBe("fake-david");
  expect(first?.rate).toBe(1.5);
  expect(first?.lang).toBe("en-US");
  expect(first?.text).toContain("Prime directive 1");

  // Pause, Resume and Stop each move their counter and swap the buttons.
  await pauseButton(page).click();
  await resumeButton(page).waitFor();
  expect((await countersOf(page)).pauses).toBe(1);
  await resumeButton(page).click();
  await pauseButton(page).waitFor();
  expect((await countersOf(page)).resumes).toBe(1);
  const beforeStop = (await countersOf(page)).cancels;
  await stopButton(page).click();
  await readButton(page).waitFor();
  expect((await countersOf(page)).cancels).toBe(beforeStop + 1);

  // Read again: no notice this time. Leaving for Settings cancels the playback.
  await readButton(page).click();
  await stopButton(page).waitFor();
  expect(await page.locator(".read-aloud-note").count()).toBe(0);
  const beforeSettings = (await countersOf(page)).cancels;
  await openSettingsChat(page);
  await expect.poll(async () => (await countersOf(page)).cancels).toBeGreaterThan(beforeSettings);
  await backToChat(page);

  // Read once more, then New session cancels it.
  await readButton(page).click();
  await stopButton(page).waitFor();
  const beforeNew = (await countersOf(page)).cancels;
  await newSidebarSession(page);
  await expect.poll(async () => (await countersOf(page)).cancels).toBeGreaterThan(beforeNew);

  const log = await logOf(page);
  expect(log.length).toBeGreaterThan(0);
  for (const entry of log) {
    expect(entry.voice).not.toBeNull();
    expect(entry.voice).not.toBe("fake-aria-online");
  }
  await page.close();
});
