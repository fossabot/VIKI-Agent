import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { KeySpec } from "../../clients/app/shellApi";
import { settingsFixture } from "../../testShell";
import type { ReadAloud } from "../../voice/readAloud";
import { DocumentPage } from "./DocumentPage";
import { ReadAloudVoicesField } from "./ReadAloudVoicesField";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function voice(name: string, lang: string, localService = true): SpeechSynthesisVoice {
  return { name, voiceURI: name, lang, localService, default: false } as SpeechSynthesisVoice;
}

const JA = voice("Fake Haruka", "ja-JP");
const ZIRA = voice("Fake Zira", "en-US");
const DAVID = voice("Fake David", "en-US");
const ONLINE = voice("Fake Aria Online (Natural)", "en-US", false);

function fakeController(
  list: readonly SpeechSynthesisVoice[],
  options: {
    supported?: boolean;
    loadVoices?: () => Promise<readonly SpeechSynthesisVoice[]>;
  } = {},
): ReadAloud {
  return {
    supported: options.supported ?? true,
    voices: () => list,
    loadVoices: options.loadVoices ?? (async () => list),
    subscribe: () => () => {},
  } as unknown as ReadAloud;
}

async function renderField(
  controller: ReadAloud,
  props: { value?: unknown; readOnlyNoticeId?: string | null } = {},
): Promise<ReturnType<typeof vi.fn>> {
  const onChange = vi.fn();
  render(
    <ReadAloudVoicesField
      value={props.value ?? "{}"}
      labelId="label"
      readOnlyNoticeId={props.readOnlyNoticeId ?? null}
      onChange={onChange}
      controller={controller}
    />,
  );
  await act(async () => {});
  return onChange;
}

function stubPlatform(platform: string): void {
  vi.stubGlobal("navigator", { platform });
}

test("rows for the languages with a local voice, and no online voice in any option", async () => {
  await renderField(fakeController([JA, ZIRA, DAVID, ONLINE]));
  expect(screen.getAllByRole("combobox")).toHaveLength(2);
  const english = screen.getByLabelText("English");
  const names = within(english)
    .getAllByRole("option")
    .map((option) => option.textContent);
  expect(names).toEqual(["Automatic (Fake Zira)", "Fake Zira", "Fake David"]);
  expect(document.body.textContent).not.toContain("Online");
  expect(screen.getByLabelText("Japanese")).toBeTruthy();
});

test("a pick sends the sorted JSON and Automatic on the last entry sends null", async () => {
  const onChange = await renderField(fakeController([JA, ZIRA, DAVID]), {
    value: '{"ja":"Fake Haruka"}',
  });
  fireEvent.change(screen.getByLabelText("English"), { target: { value: "Fake David" } });
  expect(onChange).toHaveBeenLastCalledWith('{"en":"Fake David","ja":"Fake Haruka"}');
  fireEvent.change(screen.getByLabelText("Japanese"), { target: { value: "" } });
  expect(onChange).toHaveBeenLastCalledWith(null);
});

test("a stored voice that is not installed shows as not installed", async () => {
  await renderField(fakeController([ZIRA]), { value: '{"en":"Gone Voice"}' });
  const select = screen.getByLabelText("English") as HTMLSelectElement;
  expect(select.value).toBe("Gone Voice");
  expect(within(select).getByText("Gone Voice (not installed)")).toBeTruthy();
});

test("a stored online voice is no option, so it shows as not installed", async () => {
  await renderField(fakeController([ZIRA, ONLINE]), {
    value: '{"en":"Fake Aria Online (Natural)"}',
  });
  const select = screen.getByLabelText("English");
  const labels = within(select)
    .getAllByRole("option")
    .map((option) => option.textContent);
  expect(labels).toContain("Fake Aria Online (Natural) (not installed)");
  expect(labels).not.toContain("Fake Aria Online (Natural)");
});

test.each([
  ["Win32", "Windows Settings"],
  ["MacIntel", "System Settings"],
  ["Linux x86_64", "speech-dispatcher"],
])("the languages without a voice and the steps for %s", async (platform, steps) => {
  stubPlatform(platform);
  await renderField(fakeController([ZIRA]));
  const missing = screen.getByText(/^No installed voice: /);
  expect(missing.textContent).toContain("Japanese");
  expect(missing.textContent).not.toContain("English");
  expect(screen.getByText(new RegExp(steps))).toBeTruthy();
});

test("no local voice at all gives the no-voices sentence and the steps", async () => {
  stubPlatform("Win32");
  await renderField(fakeController([ONLINE]));
  expect(screen.queryByRole("combobox")).toBeNull();
  expect(screen.getByText(/^No voice for reading aloud/)).toBeTruthy();
  expect(screen.getByText(/Windows Settings/)).toBeTruthy();
});

test("unsupported says so and loads nothing", async () => {
  const loadVoices = vi.fn(async () => []);
  await renderField(fakeController([], { supported: false, loadVoices }));
  expect(screen.getByText("This browser cannot read aloud.")).toBeTruthy();
  expect(loadVoices).not.toHaveBeenCalled();
});

test("the loading line stays until the voices resolve", async () => {
  let resolve: (list: readonly SpeechSynthesisVoice[]) => void = () => {};
  const loadVoices = () =>
    new Promise<readonly SpeechSynthesisVoice[]>((done) => {
      resolve = done;
    });
  await renderField(fakeController([ZIRA], { loadVoices }));
  expect(screen.getByText("Looking for installed voices…")).toBeTruthy();
  expect(screen.queryByRole("combobox")).toBeNull();
  await act(async () => resolve([ZIRA]));
  expect(screen.getByLabelText("English")).toBeTruthy();
});

test("read-only keeps the selects focusable, names the notice and sends nothing", async () => {
  const onChange = await renderField(fakeController([ZIRA, DAVID]), {
    readOnlyNoticeId: "notice",
  });
  const select = screen.getByLabelText("English");
  expect(select.getAttribute("aria-disabled")).toBe("true");
  expect(select.getAttribute("aria-describedby")).toBe("notice");
  fireEvent.change(select, { target: { value: "Fake David" } });
  expect(onChange).not.toHaveBeenCalled();
});

test("DocumentPage's Chat page has the speed select and the voices row", async () => {
  const base = settingsFixture();
  const spec = (allowed: readonly unknown[] | null, fallback: unknown, kind: string): KeySpec =>
    ({
      scope: "app",
      applies: "now",
      kind,
      allowed,
      default: fallback,
      personal: false,
    }) as KeySpec;
  const settings = {
    ...base,
    keys: {
      ...base.keys,
      "voice.read_aloud_rate": spec([0.75, 1, 1.25, 1.5, 2], 1, "number"),
      "voice.read_aloud_voices": spec(null, "{}", "string"),
    },
    effective: { ...base.effective, "voice.read_aloud_rate": 1, "voice.read_aloud_voices": "{}" },
    sources: {
      ...base.sources,
      "voice.read_aloud_rate": "default",
      "voice.read_aloud_voices": "default",
    },
  } as typeof base;
  render(
    <DocumentPage
      page="chat"
      settings={settings}
      headingId="heading"
      readOnlyNoticeId={null}
      patch={vi.fn()}
      onConflict={vi.fn()}
    />,
  );
  const rate = screen.getByLabelText("Read-aloud speed");
  expect(within(rate).getByText("1.5×")).toBeTruthy();
  const row = document.querySelector('[data-setting-key="voice.read_aloud_voices"]');
  expect(row?.textContent).toContain("Read-aloud voices");
  expect(row?.textContent).toContain("the browser's online voices are not used");
  // jsdom has no speechSynthesis, so the shared controller is unsupported.
  expect(row?.textContent).toContain("This browser cannot read aloud.");
});
