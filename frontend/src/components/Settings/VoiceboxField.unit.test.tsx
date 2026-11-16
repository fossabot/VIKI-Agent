import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { KeySpec, SettingsView } from "../../clients/app/shellApi";
import { settingsFixture } from "../../testShell";
import {
  type VoiceboxApi,
  VoiceboxContext,
  type VoiceboxProfile,
  type VoiceboxStatus,
} from "../../voice/voiceboxApi";
import {
  VOICEBOX_RELEASES_URL,
  type VoicePackApi,
  VoicePackContext,
} from "../../voice/voicePackApi";
import { DocumentPage } from "./DocumentPage";
import { VoiceboxField, type VoiceboxFieldProps } from "./VoiceboxField";

afterEach(() => {
  cleanup();
});

const URL = "http://127.0.0.1:17493";
const AIKO: VoiceboxProfile = { id: "p1", name: "Aiko", language: "ja" };
const MAX: VoiceboxProfile = { id: "p2", name: "Max", language: "en" };

function ready(profiles: VoiceboxProfile[], gpu: boolean | null = true): VoiceboxStatus {
  return { state: "ready", gpu, profiles };
}

function fakeApi(status: () => Promise<VoiceboxStatus>): VoiceboxApi {
  return { status: vi.fn(status), speech: vi.fn(async () => new ArrayBuffer(0)) };
}

async function renderField(
  props: Partial<Omit<VoiceboxFieldProps, "api">> & { api?: VoiceboxApi | "none" } = {},
): Promise<ReturnType<typeof vi.fn>> {
  const onChange = vi.fn();
  // `exactOptionalPropertyTypes` treats an explicit `api={undefined}` as different from omitting
  // the prop, so "no api" is a spread with the key left out entirely, never `undefined`'s value.
  const apiProp = props.api === "none" ? {} : { api: props.api ?? fakeApi(async () => ready([])) };
  render(
    <VoiceboxField
      value={props.value === undefined ? null : props.value}
      url={props.url ?? URL}
      labelId="label"
      readOnlyNoticeId={props.readOnlyNoticeId ?? null}
      engine={props.engine ?? "voicebox"}
      onChange={onChange}
      {...apiProp}
    />,
  );
  await act(async () => {});
  return onChange;
}

test("no api says Voicebox is unavailable and nothing else renders", async () => {
  await renderField({ api: "none" });
  expect(screen.getByText("Voicebox isn't available in this window.")).toBeTruthy();
  expect(screen.queryByRole("combobox")).toBeNull();
  expect(screen.queryByRole("button")).toBeNull();
});

test("no call on mount while off (value is null)", async () => {
  const status = vi.fn(async () => ready([]));
  await renderField({ value: null, api: fakeApi(status) });
  expect(status).not.toHaveBeenCalled();
  expect(screen.getByRole("button").textContent).toBe("Check Voicebox");
});

test("auto-check while on (value is a string) runs on mount with no click", async () => {
  const status = vi.fn(async () => ready([AIKO]));
  await renderField({ value: "p1", api: fakeApi(status) });
  expect(status).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("button").textContent).toBe("Check again");
});

test("lists Voicebox voices after a check, Off first", async () => {
  await renderField({ value: "p1", api: fakeApi(async () => ready([AIKO, MAX])) });
  const select = screen.getByRole("combobox") as HTMLSelectElement;
  const labels = [...select.options].map((option) => option.textContent);
  expect(labels).toEqual(["Off", "Aiko (Japanese)", "Max (English)"]);
});

test("a pick sends the chosen id, and Off sends null", async () => {
  const onChange = await renderField({ value: "p1", api: fakeApi(async () => ready([AIKO, MAX])) });
  const select = screen.getByRole("combobox");
  fireEvent.change(select, { target: { value: "p2" } });
  expect(onChange).toHaveBeenLastCalledWith("p2");
  fireEvent.change(select, { target: { value: "" } });
  expect(onChange).toHaveBeenLastCalledWith(null);
});

test("a stored id not listed shows Chosen voice before a check, then not in Voicebox after", async () => {
  let resolve: (status: VoiceboxStatus) => void = () => {};
  const api = fakeApi(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  await renderField({ value: "gone", api });
  const select = screen.getByRole("combobox") as HTMLSelectElement;
  expect(select.value).toBe("gone");
  expect(screen.getByText("Chosen voice")).toBeTruthy();
  await act(async () => resolve(ready([AIKO])));
  expect(select.value).toBe("gone");
  expect(screen.getByText("Chosen voice (not in Voicebox)")).toBeTruthy();
});

test("read-only keeps the select focusable, names the notice and sends nothing", async () => {
  const onChange = await renderField({
    value: "p1",
    readOnlyNoticeId: "notice",
    api: fakeApi(async () => ready([AIKO])),
  });
  const select = screen.getByRole("combobox");
  expect(select.getAttribute("aria-disabled")).toBe("true");
  expect(select.getAttribute("aria-describedby")).toBe("notice");
  fireEvent.change(select, { target: { value: "" } });
  expect(onChange).not.toHaveBeenCalled();
});

test.each([
  ["no_settings" as const, "Voicebox settings aren't available here."],
  [
    "unreachable" as const,
    `Voicebox isn't answering at ${URL}. Start the Voicebox app, then select Check again.`,
  ],
  [
    "unexpected" as const,
    `Something other than Voicebox answered at ${URL}. Check the Voicebox address.`,
  ],
])("status text for %s", async (state, expected) => {
  await renderField({
    value: "p1",
    api: fakeApi(async () => ({ state, gpu: null, profiles: [] })),
  });
  expect(screen.getByRole("status").textContent).toBe(expected);
});

test("ready with no profiles says so", async () => {
  await renderField({ value: "p1", api: fakeApi(async () => ready([])) });
  expect(screen.getByRole("status").textContent).toBe(
    "Voicebox has no voices yet. Create a voice profile in Voicebox, then select Check again.",
  );
});

test("ready with gpu false warns speech will be slow", async () => {
  await renderField({ value: "p1", api: fakeApi(async () => ready([AIKO], false)) });
  expect(screen.getByRole("status").textContent).toBe(
    "Voicebox is running without a GPU, so speech will be slow.",
  );
});

test("ready with profiles and a GPU just says it is running", async () => {
  await renderField({ value: "p1", api: fakeApi(async () => ready([AIKO], true)) });
  expect(screen.getByRole("status").textContent).toBe("Voicebox is running.");
});

test("a thrown check says it could not check Voicebox", async () => {
  await renderField({
    value: "p1",
    api: fakeApi(() => Promise.reject(new Error("boom"))),
  });
  expect(screen.getByRole("status").textContent).toBe("Couldn't check Voicebox.");
});

test("the Check button re-runs status and flips to Check again", async () => {
  const status = vi.fn(async () => ready([AIKO]));
  await renderField({ value: null, api: fakeApi(status) });
  expect(status).not.toHaveBeenCalled();
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Check Voicebox" }));
  });
  expect(status).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("button").textContent).toBe("Check again");
  expect(screen.getByRole("status").textContent).toBe("Voicebox is running.");
});

test("no Voicebox check unless the engine is voicebox", async () => {
  const status = vi.fn(async () => ready([AIKO]));
  await renderField({ value: "p1", engine: "system", api: fakeApi(status) });
  expect(status).not.toHaveBeenCalled();
  expect(
    screen.getByText(
      "Select My own Voicebox app (advanced) under Read replies aloud with to use this setting.",
    ),
  ).toBeTruthy();
  expect(screen.queryByRole("combobox")).toBeNull();
  expect(screen.queryByRole("button")).toBeNull();
});

test("a later flip to voicebox shows the field but still does not auto-check", async () => {
  const status = vi.fn(async () => ready([AIKO]));
  await renderField({ value: "p1", engine: "pack", api: fakeApi(status) });
  expect(screen.queryByRole("combobox")).toBeNull();
  expect(status).not.toHaveBeenCalled();
});

function fakePackApi(): VoicePackApi {
  return {
    status: vi.fn(() => Promise.reject(new Error("unused"))),
    act: vi.fn(() => Promise.reject(new Error("unused"))),
    openSpeechSettings: vi.fn(async () => {}),
  };
}

test("the unreachable status line gains a Get Voicebox button", async () => {
  const openUrl = vi.fn();
  const onChange = vi.fn();
  render(
    <VoicePackContext.Provider value={{ api: fakePackApi(), openUrl }}>
      <VoiceboxField
        value={null}
        url={URL}
        labelId="label"
        readOnlyNoticeId={null}
        engine="voicebox"
        onChange={onChange}
        api={fakeApi(async () => ({ state: "unreachable", gpu: null, profiles: [] }))}
      />
    </VoicePackContext.Provider>,
  );
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Check Voicebox" }));
  });
  fireEvent.click(screen.getByRole("button", { name: "Get Voicebox" }));
  expect(openUrl).toHaveBeenCalledWith(VOICEBOX_RELEASES_URL);
});

test("no Get Voicebox button without a VoicePackContext, even when unreachable", async () => {
  await renderField({
    value: null,
    api: fakeApi(async () => ({ state: "unreachable", gpu: null, profiles: [] })),
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Check Voicebox" }));
  });
  expect(screen.queryByRole("button", { name: "Get Voicebox" })).toBeNull();
});

function withVoiceboxKeys(settings: SettingsView): SettingsView {
  const spec: KeySpec = {
    scope: "app",
    applies: "now",
    kind: "string",
    allowed: null,
    default: null,
    personal: false,
  };
  return {
    ...settings,
    keys: {
      ...settings.keys,
      "voice.voicebox_profile": spec,
      "voice.voicebox_url": { ...spec, default: URL },
    },
    effective: {
      ...settings.effective,
      "voice.voicebox_profile": "p1",
      "voice.voicebox_url": URL,
    },
    sources: {
      ...settings.sources,
      "voice.voicebox_profile": "default",
      "voice.voicebox_url": "default",
    },
  };
}

test("the row renders on Chat, wired to the Voicebox url setting", async () => {
  const settings = {
    ...withVoiceboxKeys(settingsFixture()),
    effective: {
      ...withVoiceboxKeys(settingsFixture()).effective,
      "voice.speech_engine": "voicebox",
    },
  };
  render(
    <VoiceboxContext.Provider value={fakeApi(async () => ready([AIKO]))}>
      <DocumentPage
        page="chat"
        settings={settings}
        headingId="heading"
        readOnlyNoticeId={null}
        patch={vi.fn()}
        onConflict={vi.fn()}
      />
    </VoiceboxContext.Provider>,
  );
  await act(async () => {});
  const row = document.querySelector('[data-setting-key="voice.voicebox_profile"]');
  expect(row).not.toBeNull();
  expect(row?.querySelector("select")).not.toBeNull();
  expect(row?.querySelector("button")?.textContent).toBe("Check again");
});
