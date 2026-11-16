import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { useEffect } from "react";
import { afterEach, expect, test, vi } from "vitest";
import {
  type KeySpec,
  type SettingsPatchResult,
  type SettingsView,
  ShellApiError,
} from "../../clients/app/shellApi";
import type { SettingsPage } from "../../commands";
import { settingsFixture } from "../../testShell";
import { DictationContext } from "../../voice/dictationContext";
import type { DictationApi, VoiceStatus } from "../../voice/dictationTypes";
import { DocumentPage } from "./DocumentPage";
import { sectionsOf } from "./pages";

vi.mock("../Avatar/AvatarEditor", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../Avatar/AvatarEditor")>();
  return { ...actual, AvatarEditor: vi.fn(actual.AvatarEditor) };
});

afterEach(cleanup);

const FIXED_UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
const SET_RE = /^set$/i;
const RESET_RE = /^reset$/i;

/** `settingsFixture` builds from a table that predates the two avatar keys; a test that needs
 * them extends `keys`, `effective` and `sources` itself, the way the "future.knob" test below
 * extends the fixture for a key the backend added. */
function withAvatarKeys(settings: SettingsView): SettingsView {
  const spec: KeySpec = {
    scope: "app",
    applies: "now",
    kind: "string",
    allowed: null,
    default: null,
    personal: true,
  };
  return {
    ...settings,
    keys: {
      ...settings.keys,
      "identity.user_avatar_id": spec,
      "identity.assistant_avatar_id": spec,
    },
    effective: {
      ...settings.effective,
      "identity.user_avatar_id": null,
      "identity.assistant_avatar_id": null,
    },
    sources: {
      ...settings.sources,
      "identity.user_avatar_id": "default",
      "identity.assistant_avatar_id": "default",
    },
  };
}

function patchResult(view: SettingsView): SettingsPatchResult {
  return { ...view, command_id: "11111111-1111-4111-8111-111111111111", replayed: false };
}

function renderPage(
  page: SettingsPage,
  options: {
    settings?: SettingsView;
    patch?: (changes: Record<string, unknown>) => Promise<SettingsPatchResult>;
    readOnly?: boolean;
  } = {},
): {
  patch: (changes: Record<string, unknown>) => Promise<SettingsPatchResult>;
  onConflict: ReturnType<typeof vi.fn>;
} {
  const settings = options.settings ?? settingsFixture();
  const patch = options.patch ?? vi.fn(async () => patchResult(settings));
  const onConflict = vi.fn();
  render(
    <DocumentPage
      page={page}
      settings={settings}
      headingId="heading"
      readOnlyNoticeId={options.readOnly === true ? "notice" : null}
      patch={patch}
      onConflict={onConflict}
    />,
  );
  return { patch, onConflict };
}

function rejectWith(error: ShellApiError): () => Promise<SettingsPatchResult> {
  return () => Promise.reject(error);
}

test("a change saves and a refusal stays on its row", async () => {
  const calls: Record<string, unknown>[] = [];
  const settings = settingsFixture();
  renderPage("privacy", {
    settings,
    patch: async (changes) => {
      calls.push(changes);
      return patchResult(settings);
    },
  });
  await act(async () => {
    fireEvent.click(screen.getByLabelText("Hide instruction previews"));
  });
  expect(calls).toEqual([{ "privacy.hide_previews": true }]);
  expect(screen.getByRole("status").textContent).toBe("Saved");
  cleanup();

  // A 400 lands on the row that carries the key, as the backend's own per-key code.
  renderPage("general", {
    patch: rejectWith(
      new ShellApiError(400, "invalid_settings", {
        fields: { "identity.display_name": "too_long" },
      }),
    ),
  });
  const field = screen.getByLabelText("Display name");
  await act(async () => {
    fireEvent.change(field, { target: { value: "x".repeat(10) } });
    fireEvent.blur(field);
  });
  const row = document.querySelector('[data-setting-key="identity.display_name"]');
  expect(row?.querySelector(".settings-error")?.textContent).toBe("Outside the allowed length.");
  expect(row?.querySelector(".settings-error")?.getAttribute("role")).toBe("alert");
});

test("a read-only refusal shows the read-only sentence on the row", async () => {
  renderPage("general", { patch: rejectWith(new ShellApiError(409, "read_only")) });
  const field = screen.getByLabelText("Display name");
  await act(async () => {
    fireEvent.change(field, { target: { value: "Synth User" } });
    fireEvent.blur(field);
  });
  expect(screen.getByRole("alert").textContent).toBe(
    "Settings are read-only; see the recovery guidance.",
  );
});

test("a stale revision is handed to the review panel with the changes that caused it", async () => {
  const error = new ShellApiError(409, "stale_revision", {
    current: settingsFixture({ "identity.display_name": "Someone else" }),
    currentRevision: 9,
  });
  const { onConflict } = renderPage("general", { patch: rejectWith(error) });
  const field = screen.getByLabelText("Display name");
  await act(async () => {
    fireEvent.change(field, { target: { value: "Synth User" } });
    fireEvent.blur(field);
  });
  expect(onConflict).toHaveBeenCalledWith(error, { "identity.display_name": "Synth User" });
  // Nothing is written on the row itself: the panel owns this refusal.
  expect(document.querySelector(".settings-error")).toBeNull();
});

test("a document too large is a page error carrying the byte count the backend reported", async () => {
  renderPage("general", {
    patch: rejectWith(
      new ShellApiError(413, "settings_too_large", { extra: { current_bytes: 70000 } }),
    ),
  });
  const field = screen.getByLabelText("Display name");
  await act(async () => {
    fireEvent.change(field, { target: { value: "Synth User" } });
    fireEvent.blur(field);
  });
  const page = document.querySelector(".settings-page");
  expect(page?.firstElementChild?.nextElementSibling?.className).toBe("settings-error");
  expect(screen.getByRole("alert").textContent).toBe(
    "The settings document is too large (70000 bytes).",
  );
});

test("identity names: an empty display name is saved, an empty assistant name inherits", async () => {
  const calls: Record<string, unknown>[] = [];
  const settings = settingsFixture({
    "identity.display_name": "Synth User",
    "identity.assistant_name": "VIKI",
  });
  renderPage("general", {
    settings,
    patch: async (changes) => {
      calls.push(changes);
      return patchResult(settings);
    },
  });
  const display = screen.getByLabelText("Display name");
  await act(async () => {
    fireEvent.change(display, { target: { value: "" } });
    fireEvent.blur(display);
  });
  const assistant = screen.getByLabelText("Assistant name");
  await act(async () => {
    fireEvent.change(assistant, { target: { value: "" } });
    fireEvent.blur(assistant);
  });
  expect(calls).toEqual([{ "identity.display_name": "" }, { "identity.assistant_name": null }]);
});

test("a key the backend added and no page claims is rendered on General under Other", () => {
  const settings = settingsFixture();
  const extended: SettingsView = {
    ...settings,
    keys: {
      ...settings.keys,
      "future.knob": {
        scope: "app",
        applies: "next_launch",
        kind: "bool",
        allowed: null,
        default: false,
        personal: false,
      },
    },
    effective: { ...settings.effective, "future.knob": false },
    sources: { ...settings.sources, "future.knob": "default" },
  };
  renderPage("general", { settings: extended });
  // Other names its own card; General's identity and onboarding cards carry their own h3s too.
  expect(screen.getByRole("heading", { level: 3, name: "Other" }).textContent).toBe("Other");
  const row = document.querySelector('[data-setting-key="future.knob"]');
  expect(row).not.toBeNull();
  // No catalog row exists for it, so the key itself is the label rather than a catalog key.
  expect(row?.querySelector("label")?.textContent).toBe("future.knob");
  cleanup();
  renderPage("privacy", { settings: extended });
  expect(screen.queryByRole("heading", { level: 3 })).toBeNull();
});

test("the first-run setup row patches the key to null and is refused while read-only", async () => {
  const calls: Record<string, unknown>[] = [];
  const settings = settingsFixture();
  renderPage("general", {
    settings,
    patch: async (changes) => {
      calls.push(changes);
      return patchResult(settings);
    },
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Show first-run setup on next launch" }));
  });
  expect(calls).toEqual([{ "onboarding.completed": null }]);
  cleanup();

  renderPage("general", { settings, readOnly: true });
  const button = screen.getByRole("button", { name: "Show first-run setup on next launch" });
  expect(button.getAttribute("aria-disabled")).toBe("true");
  expect(button.getAttribute("aria-describedby")).toBe("notice");
  await act(async () => {
    fireEvent.click(button);
  });
  expect(calls).toHaveLength(1);
  // The key is never a control of its own: "Set up later" and this button are its only writers.
  expect(document.querySelector('[data-setting-key="onboarding.completed"] input')).toBeNull();
});

test("the page says it is busy while a save is in flight, and stops saying so when it settles", async () => {
  const settings = settingsFixture();
  let release = (): void => {};
  renderPage("privacy", {
    settings,
    patch: () =>
      new Promise<SettingsPatchResult>((resolve) => {
        release = () => resolve(patchResult(settings));
      }),
  });
  const region = document.querySelector(".settings-page") as HTMLElement;
  expect(region.hasAttribute("aria-busy")).toBe(false);
  await act(async () => {
    fireEvent.click(screen.getByLabelText("Hide instruction previews"));
  });
  expect(region.getAttribute("aria-busy")).toBe("true");
  await act(async () => {
    release();
  });
  expect(region.hasAttribute("aria-busy")).toBe(false);
});

test("two saves from one gesture go one after the other, at the revision each read back", async () => {
  // A reset button's mousedown blurs the text field, so both handlers run before the first
  // request answers. The fake mirrors `store.patch` (state/shell.ts): it reads the revision at
  // call time and only publishes the next one when its promise settles. Serialized, the second
  // request reads 4; concurrent, both read 3 and the real server refuses the second as
  // `stale_revision`, losing one of the user's two actions behind a dialog they did not cause.
  const sent: [number, Record<string, unknown>][] = [];
  let committed = settingsFixture({ "identity.display_name": "Synth User" });
  renderPage("general", {
    settings: committed,
    patch: (changes) => {
      const expected = committed.revision;
      sent.push([expected, changes]);
      return Promise.resolve().then(() => {
        committed = { ...committed, revision: expected + 1 };
        return patchResult(committed);
      });
    },
  });
  const field = screen.getByLabelText("Display name");
  await act(async () => {
    fireEvent.change(field, { target: { value: "Half typed" } });
    fireEvent.blur(field);
    fireEvent.click(
      document.querySelector(
        '[data-setting-key="identity.display_name"] .settings-reset',
      ) as HTMLButtonElement,
    );
  });
  expect(sent).toEqual([
    [3, { "identity.display_name": "Half typed" }],
    [4, { "identity.display_name": null }],
  ]);
});

test("the blur that follows an Enter save does not send the same patch twice", async () => {
  const sent: Record<string, unknown>[] = [];
  renderPage("general", {
    patch: async (changes) => {
      sent.push(changes);
      throw new ShellApiError(409, "stale_revision", {
        current: settingsFixture({}, { revision: 9 }),
        currentRevision: 9,
      });
    },
  });
  const field = screen.getByLabelText("Display name");
  await act(async () => {
    fireEvent.change(field, { target: { value: "Synth User" } });
    fireEvent.keyDown(field, { key: "Enter" });
    fireEvent.blur(field);
  });
  // One gesture, one value, one request — otherwise the refused patch is reported twice and the
  // review panel reopens on top of itself.
  expect(sent).toEqual([{ "identity.display_name": "Synth User" }]);
});

test("the two identity avatar rows render an AvatarField, not a text input", () => {
  const settings = withAvatarKeys(settingsFixture());
  renderPage("general", { settings });
  const userRow = document.querySelector('[data-setting-key="identity.user_avatar_id"]');
  const assistantRow = document.querySelector('[data-setting-key="identity.assistant_avatar_id"]');
  expect(userRow?.querySelector("input")).toBeNull();
  expect(assistantRow?.querySelector("input")).toBeNull();
  expect(within(userRow as HTMLElement).getByRole("button", { name: SET_RE })).not.toBeNull();
  expect(within(assistantRow as HTMLElement).getByRole("button", { name: SET_RE })).not.toBeNull();
  for (const row of [userRow, assistantRow]) {
    expect(row?.querySelector(".avatar-field-preview")?.textContent).toBe("(not set)");
    expect(row?.querySelector(".avatar-field-preview [role='img']")).toBeNull();
  }
});

test("Set on an avatar row saves the id the editor hands back", async () => {
  const { AvatarEditor } = await import("../Avatar/AvatarEditor");
  vi.mocked(AvatarEditor).mockImplementationOnce(({ onSaved }) => {
    useEffect(() => onSaved(FIXED_UUID), [onSaved]);
    return <div data-testid="stub-editor" />;
  });
  const calls: Record<string, unknown>[] = [];
  const settings = withAvatarKeys(settingsFixture());
  renderPage("general", {
    settings,
    patch: async (changes) => {
      calls.push(changes);
      return patchResult(settings);
    },
  });
  const row = document.querySelector(
    '[data-setting-key="identity.assistant_avatar_id"]',
  ) as HTMLElement;
  await act(async () => {
    fireEvent.click(within(row).getByRole("button", { name: SET_RE }));
  });
  expect(calls).toEqual([{ "identity.assistant_avatar_id": FIXED_UUID }]);
});

test("Reset on an avatar row that already has an id sends null (the built-in returns)", async () => {
  const calls: Record<string, unknown>[] = [];
  const settings = withAvatarKeys(settingsFixture());
  const withAssistantAvatar: SettingsView = {
    ...settings,
    effective: { ...settings.effective, "identity.assistant_avatar_id": FIXED_UUID },
  };
  renderPage("general", {
    settings: withAssistantAvatar,
    patch: async (changes) => {
      calls.push(changes);
      return patchResult(withAssistantAvatar);
    },
  });
  const row = document.querySelector(
    '[data-setting-key="identity.assistant_avatar_id"]',
  ) as HTMLElement;
  await act(async () => {
    fireEvent.click(within(row).getByRole("button", { name: RESET_RE }));
  });
  expect(calls).toEqual([{ "identity.assistant_avatar_id": null }]);
});

test("avatar_missing renders on the avatar row the backend names", async () => {
  const { AvatarEditor } = await import("../Avatar/AvatarEditor");
  vi.mocked(AvatarEditor).mockImplementationOnce(({ onSaved }) => {
    useEffect(() => onSaved(FIXED_UUID), [onSaved]);
    return <div data-testid="stub-editor" />;
  });
  const settings = withAvatarKeys(settingsFixture());
  renderPage("general", {
    settings,
    patch: rejectWith(
      new ShellApiError(400, "invalid_settings", {
        fields: { "identity.user_avatar_id": "avatar_missing" },
      }),
    ),
  });
  const row = document.querySelector('[data-setting-key="identity.user_avatar_id"]') as HTMLElement;
  await act(async () => {
    fireEvent.click(within(row).getByRole("button", { name: SET_RE }));
  });
  expect(within(row).getByRole("alert").textContent).not.toBe("");
});

test("a read-only document keeps both avatar rows' buttons focusable, naming the notice", () => {
  const settings = withAvatarKeys(settingsFixture());
  renderPage("general", { settings, readOnly: true });
  const userRow = document.querySelector(
    '[data-setting-key="identity.user_avatar_id"]',
  ) as HTMLElement;
  const assistantRow = document.querySelector(
    '[data-setting-key="identity.assistant_avatar_id"]',
  ) as HTMLElement;
  for (const row of [userRow, assistantRow]) {
    const button = within(row).getByRole("button", { name: SET_RE }) as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    expect(button.getAttribute("aria-disabled")).toBe("true");
    expect(button.getAttribute("aria-describedby")).toContain("notice");
  }
});

test("each avatar row shows one visible label, and its buttons name that row", () => {
  const settings = withAvatarKeys(settingsFixture());
  renderPage("general", { settings });
  const userRow = document.querySelector(
    '[data-setting-key="identity.user_avatar_id"]',
  ) as HTMLElement;
  // `.settings-row-label` shows the label once; `AvatarField` renders no label span of its own
  // when the row already gives it one.
  expect(within(userRow).getAllByText("Your avatar")).toHaveLength(1);
  const labelId = userRow.querySelector(".settings-row-label span")?.id;
  expect(labelId).toBeTruthy();
  const button = within(userRow).getByRole("button", { name: SET_RE });
  expect(button.getAttribute("aria-describedby")).toBe(labelId);
  expect(within(userRow).getByText("Excluded from exports by default.")).not.toBeNull();
});

const VOICE_STATUS: VoiceStatus = {
  state: "ready",
  running: false,
  model: "small-q8_0",
  backend: "cpu",
  device: null,
  languages: ["auto", "en"],
  language: "auto",
  language_unavailable: false,
  models: ["small-q8_0"],
  backends: ["cpu"],
  fallback: { backend: "cuda", code: "backend_missing", detail: null },
  cpu_model: "small-q8_0",
  slow_on_cpu: false,
};

function fakeDictation(): DictationApi {
  return {
    status: vi.fn(async () => VOICE_STATUS),
    clearFallback: vi.fn(async () => ({ ...VOICE_STATUS, fallback: null })),
    socket: vi.fn(),
    startCapture: vi.fn(),
    microphones: vi.fn(async () => []),
    workletUrl: "/w.js",
  };
}

function withVoiceKeys(): SettingsView {
  const settings = settingsFixture();
  const spec = (kind: KeySpec["kind"], allowed: KeySpec["allowed"], def: unknown): KeySpec => ({
    scope: "app",
    applies: "now",
    kind,
    allowed,
    default: def,
    personal: false,
  });
  const keys = {
    "voice.dictation_language": spec("enum", ["auto", "en", "ja"], "auto"),
    "voice.model": spec("enum", ["auto", "large-v3-turbo-q8_0", "small-q8_0"], "auto"),
    "voice.backend": spec("enum", ["auto", "cpu", "cuda"], "auto"),
    "voice.read_aloud_rate": spec("number", [0.8, 1, 1.2], 1),
  };
  return {
    ...settings,
    keys: { ...settings.keys, ...keys },
    effective: {
      ...settings.effective,
      "voice.dictation_language": "auto",
      "voice.model": "auto",
      "voice.backend": "auto",
      "voice.read_aloud_rate": 1,
    },
    sources: {
      ...settings.sources,
      "voice.dictation_language": "default",
      "voice.model": "default",
      "voice.backend": "default",
      "voice.read_aloud_rate": "default",
    },
  };
}

function renderWithDictation(api: DictationApi | null, settings: SettingsView): void {
  const patch = vi.fn(async () => patchResult(settings));
  render(
    <DictationContext.Provider value={api}>
      <DocumentPage
        page="chat"
        settings={settings}
        headingId="heading"
        readOnlyNoticeId={null}
        patch={patch}
        onConflict={vi.fn()}
      />
    </DictationContext.Provider>,
  );
}

const optionValues = (key: string): string[] => {
  const select = document.querySelector(`[data-setting-key="${key}"] select`);
  return [...(select as HTMLSelectElement).options].map((option) => option.value);
};

test("the model select lists installed models only and a save refreshes the status", async () => {
  const api = fakeDictation();
  renderWithDictation(api, withVoiceKeys());
  await act(async () => {});
  expect(optionValues("voice.model")).toEqual(["auto", "small-q8_0"]);
  expect(optionValues("voice.backend")).toEqual(["auto", "cpu"]);
  expect(optionValues("voice.dictation_language")).toEqual(["auto", "en"]);
  // A read-aloud row keeps the key's own list.
  expect(optionValues("voice.read_aloud_rate")).toEqual(["0.8", "1", "1.2"]);
  expect(api.status).toHaveBeenCalledTimes(1);

  const select = document.querySelector('[data-setting-key="voice.model"] select');
  await act(async () => {
    fireEvent.change(select as HTMLSelectElement, { target: { value: "small-q8_0" } });
  });
  expect(api.status).toHaveBeenCalledTimes(2);

  // Saving a read-aloud key does not read the dictation status again.
  const rate = document.querySelector('[data-setting-key="voice.read_aloud_rate"] select');
  await act(async () => {
    fireEvent.change(rate as HTMLSelectElement, { target: { value: "1.2" } });
  });
  expect(api.status).toHaveBeenCalledTimes(2);
});

test("the panel renders and Try GPU again calls clearFallback", async () => {
  const api = fakeDictation();
  renderWithDictation(api, withVoiceKeys());
  await act(async () => {});
  // Scoped to the panel itself: it carries its own "Dictation" heading, separate from the card's
  // "Voice input" title, so a page-wide text query would not be ambiguous either way.
  const panel = document.querySelector('[data-setting-key="voice.status"]') as HTMLElement;
  expect(within(panel).getByText("Dictation")).not.toBeNull();
  await act(async () => {
    fireEvent.click(within(panel).getByRole("button", { name: "Try GPU again" }));
  });
  expect(api.clearFallback).toHaveBeenCalledTimes(1);
});

test("without a provider the voice rows list every allowed value and no panel shows", async () => {
  renderWithDictation(null, withVoiceKeys());
  await act(async () => {});
  expect(optionValues("voice.model")).toEqual(["auto", "large-v3-turbo-q8_0", "small-q8_0"]);
  expect(document.querySelector('[data-setting-key="voice.status"]')).toBeNull();
});

test("renders the speech engine row", () => {
  const settings = settingsFixture();
  const withEngine: SettingsView = {
    ...settings,
    keys: {
      ...settings.keys,
      "voice.speech_engine": {
        scope: "app",
        applies: "now",
        kind: "string",
        allowed: null,
        default: "system",
        personal: false,
      },
    },
    effective: { ...settings.effective, "voice.speech_engine": "system" },
    sources: { ...settings.sources, "voice.speech_engine": "default" },
  };
  renderPage("chat", { settings: withEngine });
  const row = document.querySelector('[data-setting-key="voice.speech_engine"]');
  expect(row).not.toBeNull();
  expect(row?.querySelector('[role="radiogroup"]')).not.toBeNull();
});

test("renders the natural voice row", () => {
  const settings = settingsFixture();
  const withPackVoices: SettingsView = {
    ...settings,
    keys: {
      ...settings.keys,
      "voice.speech_engine": {
        scope: "app",
        applies: "now",
        kind: "string",
        allowed: null,
        default: "system",
        personal: false,
      },
      "voice.pack_voices": {
        scope: "app",
        applies: "now",
        kind: "string",
        allowed: null,
        default: "{}",
        personal: false,
      },
    },
    effective: {
      ...settings.effective,
      "voice.speech_engine": "pack",
      "voice.pack_voices": "{}",
    },
    sources: {
      ...settings.sources,
      "voice.speech_engine": "default",
      "voice.pack_voices": "default",
    },
  };
  renderPage("chat", { settings: withPackVoices });
  const row = document.querySelector('[data-setting-key="voice.pack_voices"]');
  expect(row).not.toBeNull();
  const selects = row?.querySelectorAll("select");
  expect(selects?.length).toBe(10);
});

test("General groups its rows into cards titled Names and avatars, Code view and Setup", () => {
  const settings = settingsFixture();
  const withCodeView: SettingsView = {
    ...settings,
    keys: {
      ...settings.keys,
      "workbench.code_font_size": {
        scope: "app",
        applies: "now",
        kind: "number",
        allowed: [11, 12, 13],
        default: 13,
        personal: false,
      },
    },
    effective: { ...settings.effective, "workbench.code_font_size": 13 },
    sources: { ...settings.sources, "workbench.code_font_size": "default" },
  };
  renderPage("general", { settings: withCodeView });
  const titles = screen.getAllByRole("heading", { level: 3 }).map((heading) => heading.textContent);
  expect(titles).toEqual(["Names and avatars", "Code view", "Setup"]);
});

test("every button the page renders carries a btn variant class, or is a settings-pill", () => {
  renderPage("general");
  const buttons = [...document.querySelectorAll(".settings-page button")];
  expect(
    buttons.length > 0 &&
      buttons.every((button) => {
        const classes = [...button.classList];
        return (
          classes.includes("settings-pill") ||
          (classes.includes("btn") &&
            ["go", "alt", "txt"].some((variant) => classes.includes(variant)))
        );
      }),
  ).toBe(true);
});

test("sectionsOf starts a new titled run at each start key, leaving the rest in the run before it", () => {
  expect(
    sectionsOf([
      "chat.default_mode",
      "chat.default_model",
      "voice.read_aloud_rate",
      "voice.read_aloud_voices",
      "voice.voicebox_profile",
      "voice.voicebox_url",
      "voice.dictation_language",
      "voice.model",
    ]),
  ).toEqual([
    { title: "New chats", keys: ["chat.default_mode", "chat.default_model"] },
    { title: "Read aloud", keys: ["voice.read_aloud_rate", "voice.read_aloud_voices"] },
    { title: "Voicebox", keys: ["voice.voicebox_profile", "voice.voicebox_url"] },
    { title: "Voice input", keys: ["voice.dictation_language", "voice.model"] },
  ]);
});

test("sectionsOf with no start among its keys (Privacy) is one untitled run", () => {
  expect(sectionsOf(["privacy.hide_previews", "privacy.diagnostics_include_paths"])).toEqual([
    { title: null, keys: ["privacy.hide_previews", "privacy.diagnostics_include_paths"] },
  ]);
});
