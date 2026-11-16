/**
 * `PackVoicesField`: the per-language select (Kokoro/Qwen3/System, the default marked and
 * selected), its hints (c) Kokoro-doesn't-speak-this-language and (d) the system-voice fallback,
 * and each row's Preview button. Preview tests use the real `createReadAloud` controller (a null
 * `synth`, a fake `AudioOut` that never calls `onEnded`) so `begin`/`isCurrent`/`playAudio`/
 * `stopIf`/`statusOf` behave exactly as in production; only `api.speech` is faked.
 */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { PREVIEW_SAMPLES } from "../../voice/packVoices";
import { type AudioOut, createReadAloud, type ReadAloud } from "../../voice/readAloud";
import { type VoiceboxApi, VoiceboxError } from "../../voice/voiceboxApi";
import {
  PACK_LANGUAGES,
  type SpeechEngine,
  type VoicePackApi,
  type VoicePackServices,
  type VoicePackStatus,
} from "../../voice/voicePackApi";
import { PackVoicesField } from "./PackVoicesField";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function flush(): Promise<void> {
  for (let turn = 0; turn < 10; turn += 1) await Promise.resolve();
}

function status(
  overrides: Partial<VoicePackStatus> & Pick<VoicePackStatus, "state">,
): VoicePackStatus {
  return {
    reason: null,
    error: null,
    progress: null,
    downloadBytes: 0,
    diskBytes: 0,
    needBytes: 0,
    freeBytes: null,
    gpu: null,
    kokoro: false,
    speed: { kokoro: null, qwen: null },
    fallbacks: [],
    ...overrides,
  };
}

/** Every `PACK_LANGUAGES` entry set to "system" except the ones named, so a test can make exactly
 * one row's pick natural (and so its Preview button unambiguous). */
function systemExcept(keep: string[]): Record<string, string> {
  const map: Record<string, string> = {};
  for (const lang of PACK_LANGUAGES) if (!keep.includes(lang)) map[lang] = "system";
  return map;
}

function fakeServices(st: VoicePackStatus): VoicePackServices {
  const api: VoicePackApi = {
    status: vi.fn(async () => st),
    act: vi.fn(),
    openSpeechSettings: vi.fn(async () => {}),
  };
  return { api, openUrl: vi.fn() };
}

function voice(name: string, lang: string, localService = true): SpeechSynthesisVoice {
  return { name, voiceURI: name, lang, localService, default: false } as SpeechSynthesisVoice;
}

function fakeVoiceController(list: readonly SpeechSynthesisVoice[]): ReadAloud {
  return {
    supported: true,
    voices: () => list,
    loadVoices: async () => list,
    subscribe: () => () => {},
  } as unknown as ReadAloud;
}

/** An `AudioOut` that decodes and "starts" at once but never calls `onEnded`, so a playing preview
 * stays "speaking" until something stops it — exactly what the Preview tests need to observe. */
function fakeAudio(): AudioOut {
  return {
    decode: async () => ({}) as AudioBuffer,
    start: () => () => {},
    resume: async () => {},
    suspend: async () => {},
  };
}

function realController(): ReadAloud {
  return createReadAloud({
    synth: null,
    makeUtterance: () => ({}) as SpeechSynthesisUtterance,
    audio: fakeAudio(),
    target: window,
    devices: null,
    setTimeout: (cb, ms) => window.setTimeout(cb, ms),
    clearTimeout: (id) => window.clearTimeout(id),
  });
}

function stubPlatform(platform: string): void {
  vi.stubGlobal("navigator", { platform });
}

interface RenderProps {
  value?: unknown;
  systemVoices?: unknown;
  readOnlyNoticeId?: string | null;
  engine?: SpeechEngine;
  controller?: ReadAloud;
  api?: VoiceboxApi;
  services?: VoicePackServices | null;
}

async function renderField(props: RenderProps = {}): Promise<ReturnType<typeof vi.fn>> {
  const onChange = vi.fn();
  // exactOptionalPropertyTypes: an omitted key, not an explicit `undefined`, is how "no override"
  // is spelled for `api`/`services` (same reasoning as VoiceboxField.unit.test.tsx).
  const apiProp = props.api !== undefined ? { api: props.api } : {};
  const servicesProp = props.services !== undefined ? { services: props.services } : {};
  render(
    <PackVoicesField
      value={props.value ?? "{}"}
      systemVoices={props.systemVoices ?? "{}"}
      labelId="label"
      readOnlyNoticeId={props.readOnlyNoticeId ?? null}
      engine={props.engine ?? "pack"}
      onChange={onChange}
      controller={props.controller ?? fakeVoiceController([])}
      {...apiProp}
      {...servicesProp}
    />,
  );
  await act(async () => {});
  return onChange;
}

// ---- Structure: engine gate, rows, optgroups, defaults -----------------------------------------

test("engine other than pack shows only the one-line description", async () => {
  await renderField({ engine: "system" });
  expect(
    screen.getByText("Select Natural voices under Read replies aloud with to use this setting."),
  ).toBeTruthy();
  expect(screen.queryByRole("combobox")).toBeNull();
});

test("a Kokoro-capable language gets both optgroups; a non-Kokoro language gets only Qwen3", async () => {
  await renderField({});
  const english = screen.getByLabelText("English") as HTMLSelectElement;
  const groups = Array.from(english.querySelectorAll("optgroup")).map((g) => g.label);
  expect(groups).toEqual([
    "Kokoro — fast (not downloaded)",
    "Qwen3 — more expressive, needs a strong GPU",
  ]);
  const japanese = screen.getByLabelText("Japanese") as HTMLSelectElement;
  expect(japanese.querySelectorAll("optgroup")).toHaveLength(1);
  expect(japanese.querySelector("option[value='system']")).toBeTruthy();
});

test("once Kokoro is installed its optgroup label drops the not-downloaded note", async () => {
  await renderField({ services: fakeServices(status({ state: "ready", kokoro: true })) });
  const english = screen.getByLabelText("English") as HTMLSelectElement;
  expect(english.querySelector("optgroup")?.label).toBe("Kokoro — fast");
});

test("marks today's default option and selects it with no stored pick", async () => {
  await renderField({});
  const english = screen.getByLabelText("English") as HTMLSelectElement;
  expect(english.value).toBe("Ryan"); // Kokoro not installed: Qwen3's default wins
  const labels = within(english)
    .getAllByRole("option")
    .map((option) => option.textContent);
  expect(labels).toContain("Ryan — male, English (default)");
  expect(labels).toContain("Heart — female, American");
});

// ---- Picking a voice ----------------------------------------------------------------------------

test("picking a non-default voice stores it", async () => {
  const onChange = await renderField({});
  fireEvent.change(screen.getByLabelText("English"), { target: { value: "af_heart" } });
  expect(onChange).toHaveBeenLastCalledWith('{"en":"af_heart"}');
});

test("picking back to today's default clears the stored entry", async () => {
  const onChange = await renderField({ value: '{"en":"af_heart"}' });
  fireEvent.change(screen.getByLabelText("English"), { target: { value: "Ryan" } });
  expect(onChange).toHaveBeenLastCalledWith(null);
});

test("read-only keeps the select focusable, names the notice and drops a change", async () => {
  const onChange = await renderField({ readOnlyNoticeId: "notice" });
  const english = screen.getByLabelText("English");
  expect(english.getAttribute("aria-disabled")).toBe("true");
  expect(english.getAttribute("aria-describedby")).toBe("notice");
  fireEvent.change(english, { target: { value: "af_heart" } });
  expect(onChange).not.toHaveBeenCalled();
});

// ---- Hints (c) and (d) --------------------------------------------------------------------------

test("hint (c): once Kokoro is installed, the languages it doesn't speak say so in one line", async () => {
  await renderField({ services: fakeServices(status({ state: "ready", kokoro: true })) });
  expect(
    screen.getByText("Kokoro doesn't speak Russian, German, Japanese, and Korean here."),
  ).toBeTruthy();
  expect(screen.queryByText(/Kokoro doesn't speak English/)).toBeNull();
});

test("hint (d): a natural pick with an installed system voice gets no fallback note", async () => {
  await renderField({ controller: fakeVoiceController([voice("Fake David", "en-US")]) });
  const english = screen.getByLabelText("English");
  const previewSlot = english.nextElementSibling; // the Preview placeholder <span> (no api)
  expect(previewSlot?.nextElementSibling?.tagName).toBe("LABEL"); // straight to the next row
});

test("hint (d): a natural pick with no system voice installed says so", async () => {
  // Every other language forced to "system" (its own, different hint) so this sentence, which
  // only the natural-valued row contributes to, names just the one language.
  await renderField({ value: JSON.stringify(systemExcept(["en"])) });
  expect(screen.getByText("No system voice to fall back on for English.")).toBeTruthy();
});

test("hint (d): the system-voice pick names the matching voice", async () => {
  await renderField({
    controller: fakeVoiceController([voice("Fake David", "en-US")]),
    value: '{"en":"system"}',
  });
  expect(screen.getByText("Fake David reads English.")).toBeTruthy();
});

test("hint (d): the system-voice pick with none installed offers Add a Windows voice", async () => {
  stubPlatform("Win32");
  const services = fakeServices(status({ state: "ready" }));
  await renderField({ services, value: '{"en":"system"}' });
  expect(
    screen.getByText("No system voice for English is installed, so its replies aren't read aloud."),
  ).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Add a Windows voice…" }));
  expect(services.api.openSpeechSettings).toHaveBeenCalled();
});

test("a system-picked row is excluded from the Kokoro summary and keeps its own hint", async () => {
  await renderField({
    services: fakeServices(status({ state: "ready", kokoro: true })),
    value: '{"de":"system"}',
  });
  expect(screen.getByText("Kokoro doesn't speak Russian, Japanese, and Korean here.")).toBeTruthy();
  expect(screen.queryByText(/Kokoro doesn't speak German/)).toBeNull();
  expect(
    screen.getByText("No system voice for German is installed, so its replies aren't read aloud."),
  ).toBeTruthy();
});

// ---- Preview: when it shows at all --------------------------------------------------------------

test("no api renders no Preview button anywhere", async () => {
  await renderField({});
  expect(screen.queryByRole("button", { name: "Preview" })).toBeNull();
});

test("a system-voice row has no Preview button even with an api", async () => {
  // Every language set to "system" (not just English): the other rows would otherwise mount a
  // real PreviewCell needing the full ReadAloud surface, which this test's minimal fake lacks.
  const api: VoiceboxApi = { status: vi.fn(), speech: vi.fn() };
  await renderField({ api, value: JSON.stringify(systemExcept([])) });
  const english = screen.getByLabelText("English");
  expect(english.nextElementSibling?.tagName).toBe("SPAN");
});

// ---- Preview: the flow itself -------------------------------------------------------------------

test("Preview speaks the fixed sample once and plays it", async () => {
  const controller = realController();
  const speech = vi.fn(async () => new ArrayBuffer(4));
  const api: VoiceboxApi = { status: vi.fn(), speech };
  const services = fakeServices(status({ state: "ready", kokoro: true }));
  await renderField({ controller, api, services, value: JSON.stringify(systemExcept(["en"])) });

  fireEvent.click(screen.getByRole("button", { name: "Preview" }));
  await act(async () => {
    await flush();
  });

  expect(speech).toHaveBeenCalledTimes(1);
  expect(speech).toHaveBeenCalledWith(PREVIEW_SAMPLES.en, "en", expect.any(AbortSignal));
  expect(controller.statusOf("settings:preview:en")).toBe("speaking");
  expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();
});

test("shows the cold waiting line when the pack is stopped", async () => {
  const controller = realController();
  const api: VoiceboxApi = {
    status: vi.fn(),
    speech: vi.fn(() => new Promise<ArrayBuffer>(() => {})),
  };
  const services = fakeServices(status({ state: "stopped", kokoro: true }));
  await renderField({ controller, api, services, value: JSON.stringify(systemExcept(["en"])) });

  fireEvent.click(screen.getByRole("button", { name: "Preview" }));

  expect(
    screen.getByText("Starting natural voices… The first preview can take over a minute."),
  ).toBeTruthy();
});

test("shows the plain waiting line once the pack is already warm", async () => {
  const controller = realController();
  const api: VoiceboxApi = {
    status: vi.fn(),
    speech: vi.fn(() => new Promise<ArrayBuffer>(() => {})),
  };
  const services = fakeServices(status({ state: "ready", kokoro: true }));
  await renderField({ controller, api, services, value: JSON.stringify(systemExcept(["en"])) });

  fireEvent.click(screen.getByRole("button", { name: "Preview" }));

  expect(screen.getByText("Preparing the preview…")).toBeTruthy();
});

test("Stop cancels a pending preview without showing a failure", async () => {
  const controller = realController();
  const api: VoiceboxApi = {
    status: vi.fn(),
    speech: vi.fn(() => new Promise<ArrayBuffer>(() => {})),
  };
  const services = fakeServices(status({ state: "ready", kokoro: true }));
  await renderField({ controller, api, services, value: JSON.stringify(systemExcept(["en"])) });

  fireEvent.click(screen.getByRole("button", { name: "Preview" }));
  expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Stop" }));
  await act(async () => {
    await flush();
  });

  expect(screen.getByRole("button", { name: "Preview" })).toBeTruthy();
  expect(screen.queryByText(/didn't play/)).toBeNull();
  expect(controller.owner()).toBeNull();
});

test("unmount stops this row's own pending preview", async () => {
  const controller = realController();
  const api: VoiceboxApi = {
    status: vi.fn(),
    speech: vi.fn(() => new Promise<ArrayBuffer>(() => {})),
  };
  const services = fakeServices(status({ state: "ready", kokoro: true }));
  const onChange = vi.fn();
  const { unmount } = render(
    <PackVoicesField
      value={JSON.stringify(systemExcept(["en"]))}
      systemVoices="{}"
      labelId="label"
      readOnlyNoticeId={null}
      engine="pack"
      onChange={onChange}
      controller={controller}
      api={api}
      services={services}
    />,
  );
  await act(async () => {});

  fireEvent.click(screen.getByRole("button", { name: "Preview" }));
  expect(controller.owner()).toBe("settings:preview:en");
  unmount();
  expect(controller.owner()).toBeNull();
});

test("a Kokoro pick previews at once with 'Add the fast voice first.' when Kokoro isn't installed", async () => {
  const controller = realController();
  const speech = vi.fn(async () => new ArrayBuffer(4));
  const api: VoiceboxApi = { status: vi.fn(), speech };
  const services = fakeServices(status({ state: "ready", kokoro: false }));
  await renderField({
    controller,
    api,
    services,
    value: JSON.stringify({ ...systemExcept(["en"]), en: "af_heart" }),
  });

  fireEvent.click(screen.getByRole("button", { name: "Preview" }));
  await act(async () => {
    await flush();
  });

  expect(screen.getByText("Add the fast voice first.")).toBeTruthy();
  expect(speech).not.toHaveBeenCalled();
});

test("refuses at once, without calling the api, while the pack isn't installed", async () => {
  const controller = realController();
  const speech = vi.fn(async () => new ArrayBuffer(4));
  const api: VoiceboxApi = { status: vi.fn(), speech };
  const services = fakeServices(status({ state: "not_installed" }));
  await renderField({ controller, api, services, value: JSON.stringify(systemExcept(["en"])) });

  fireEvent.click(screen.getByRole("button", { name: "Preview" }));
  await act(async () => {
    await flush();
  });

  expect(
    screen.getByText("Natural voices aren't installed yet. Install them in Settings › Chat."),
  ).toBeTruthy();
  expect(speech).not.toHaveBeenCalled();
});

test("refuses at once when there is no pack status to read at all", async () => {
  const controller = realController();
  const speech = vi.fn(async () => new ArrayBuffer(4));
  const api: VoiceboxApi = { status: vi.fn(), speech };
  await renderField({ controller, api, value: JSON.stringify(systemExcept(["en"])) });

  fireEvent.click(screen.getByRole("button", { name: "Preview" }));
  await act(async () => {
    await flush();
  });

  expect(
    screen.getByText("Natural voices aren't installed yet. Install them in Settings › Chat."),
  ).toBeTruthy();
  expect(speech).not.toHaveBeenCalled();
});

test("maps a voicebox_* failure through voiceboxProblem's pack wording", async () => {
  const controller = realController();
  const speech = vi.fn(async () => {
    throw new VoiceboxError("voicebox_timeout");
  });
  const api: VoiceboxApi = { status: vi.fn(), speech };
  const services = fakeServices(status({ state: "ready", kokoro: true }));
  await renderField({ controller, api, services, value: JSON.stringify(systemExcept(["en"])) });

  fireEvent.click(screen.getByRole("button", { name: "Preview" }));
  await act(async () => {
    await flush();
  });

  expect(
    screen.getByText("The natural voice didn't answer in time. Try again in a moment."),
  ).toBeTruthy();
});

test("a non-voicebox failure falls back to the generic line", async () => {
  const controller = realController();
  const speech = vi.fn(async () => {
    throw new Error("boom");
  });
  const api: VoiceboxApi = { status: vi.fn(), speech };
  const services = fakeServices(status({ state: "ready", kokoro: true }));
  await renderField({ controller, api, services, value: JSON.stringify(systemExcept(["en"])) });

  fireEvent.click(screen.getByRole("button", { name: "Preview" }));
  await act(async () => {
    await flush();
  });

  expect(screen.getByText("The preview didn't play. Try again.")).toBeTruthy();
});
