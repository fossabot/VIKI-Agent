import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { VoiceMenu, type VoiceMenuProps } from "./VoiceMenu";
import type { VoiceboxApi, VoiceboxStatus } from "./voiceboxApi";
import { VoiceboxContext } from "./voiceboxApi";
import { VOICEBOX_RELEASES_URL, type VoicePackStatus } from "./voicePackApi";

afterEach(cleanup);

const URL = "http://127.0.0.1:17493";

const READY: VoiceboxStatus = {
  state: "ready",
  gpu: true,
  profiles: [
    { id: "nova", name: "Nova", language: "en" },
    { id: "sora", name: "Sora", language: "ja" },
  ],
};

/** A `VoicePackStatus` fixture: every field defaulted, override just what a test cares about. */
function pack(overrides: Partial<VoicePackStatus>): VoicePackStatus {
  return {
    state: "not_installed",
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

function fakeApi(status: VoiceboxApi["status"]): VoiceboxApi {
  return { status, speech: vi.fn() };
}

function renderMenu(overrides: Partial<VoiceMenuProps> = {}) {
  const patch = vi.fn().mockResolvedValue(undefined);
  const openSettings = vi.fn();
  const onStatus = vi.fn();
  render(
    <VoiceMenu
      engine="voicebox"
      pack={null}
      profile={null}
      speakWhileWriting={false}
      url={URL}
      patch={patch}
      openSettings={openSettings}
      onStatus={onStatus}
      {...overrides}
    />,
  );
  return { patch, openSettings, onStatus };
}

function openMenu(): void {
  fireEvent.click(screen.getByRole("button", { name: "Voice options" }));
}

/** Radix's dismissal (Escape, outside click) resolves a tick after the DOM event itself — see
 * OrganizedSessionMenu.unit.test.tsx's own Escape test for the same flush. */
async function flush(): Promise<void> {
  await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
}

test("runs one status call per open, aborted when the menu closes", async () => {
  const signals: Array<AbortSignal | undefined> = [];
  const status = vi.fn((signal?: AbortSignal) => {
    signals.push(signal);
    return new Promise<VoiceboxStatus>(() => {});
  });
  renderMenu({ api: fakeApi(status) });

  openMenu();
  expect(status).toHaveBeenCalledTimes(1);
  expect(signals[0]?.aborted).toBe(false);

  // Radix's own dismissable layer listens on `document`, not `window` (OrganizedSessionMenu's
  // own Escape test dispatches the same way for the same reason).
  fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
  await flush();
  expect(signals[0]?.aborted).toBe(true);

  openMenu();
  expect(status).toHaveBeenCalledTimes(2);
  expect(signals[1]?.aborted).toBe(false);
});

test("lists Voicebox voices as radio rows", async () => {
  renderMenu({ profile: "sora", api: fakeApi(vi.fn(() => Promise.resolve(READY))) });
  openMenu();

  // Named, not the generic role: the generic query is already satisfied by System voice (plus,
  // for a profile not yet confirmed, the fallback row) before the check resolves, so it must not
  // be what this test waits on.
  await screen.findByRole("menuitemradio", { name: "Nova (English)" });
  const rows = screen.getAllByRole("menuitemradio");
  expect(rows.map((row) => row.textContent)).toEqual([
    "System voice",
    "Nova (English)",
    "Sora (Japanese)",
  ]);
  expect(
    screen.getByRole("menuitemradio", { name: "Sora (Japanese)" }).getAttribute("aria-checked"),
  ).toBe("true");
  expect(
    screen.getByRole("menuitemradio", { name: "System voice" }).getAttribute("aria-checked"),
  ).toBe("false");
});

test("a stored profile the check doesn't list stays selected under a fallback row", async () => {
  renderMenu({ profile: "ghost", api: fakeApi(vi.fn(() => Promise.resolve(READY))) });
  openMenu();

  const chosen = await screen.findByRole("menuitemradio", { name: "Chosen Voicebox voice" });
  expect(chosen.getAttribute("aria-checked")).toBe("true");
});

test("shows the unexpected-answer line", async () => {
  renderMenu({
    api: fakeApi(
      vi.fn(() =>
        Promise.resolve<VoiceboxStatus>({ state: "unexpected", gpu: null, profiles: [] }),
      ),
    ),
  });
  openMenu();
  await screen.findByText(`Something other than Voicebox answered at ${URL}.`);
});

test("shows the no_settings line", async () => {
  renderMenu({
    api: fakeApi(
      vi.fn(() =>
        Promise.resolve<VoiceboxStatus>({ state: "no_settings", gpu: null, profiles: [] }),
      ),
    ),
  });
  openMenu();
  await screen.findByText("Voicebox settings aren't available here.");
});

test("shows the ready-with-no-voices line", async () => {
  renderMenu({
    api: fakeApi(
      vi.fn(() => Promise.resolve<VoiceboxStatus>({ state: "ready", gpu: true, profiles: [] })),
    ),
  });
  openMenu();
  await screen.findByText("Voicebox has no voices yet.");
});

test("shows the thrown-check line", async () => {
  renderMenu({ api: fakeApi(vi.fn(() => Promise.reject(new Error("boom")))) });
  openMenu();
  await screen.findByText("Couldn't check Voicebox.");
});

test("choosing a voice patches its id", async () => {
  const { patch } = renderMenu({
    profile: null,
    api: fakeApi(vi.fn(() => Promise.resolve(READY))),
  });
  openMenu();

  fireEvent.click(await screen.findByRole("menuitemradio", { name: "Nova (English)" }));
  expect(patch).toHaveBeenCalledExactlyOnceWith({
    "voice.speech_engine": "voicebox",
    "voice.voicebox_profile": "nova",
  });
});

test("choosing System voice patches the system engine", async () => {
  const { patch } = renderMenu({
    profile: "nova",
    api: fakeApi(vi.fn(() => Promise.resolve(READY))),
  });
  openMenu();
  await screen.findByRole("menuitemradio", { name: "Nova (English)" });

  fireEvent.click(screen.getByRole("menuitemradio", { name: "System voice" }));
  expect(patch).toHaveBeenCalledExactlyOnceWith({ "voice.speech_engine": "system" });
});

test("shows the unreachable line, and Check Voicebox re-checks with the menu kept open", async () => {
  const status = vi
    .fn<VoiceboxApi["status"]>()
    .mockResolvedValueOnce({ state: "unreachable", gpu: null, profiles: [] })
    .mockResolvedValueOnce(READY);
  renderMenu({ api: fakeApi(status), url: URL });
  openMenu();

  await screen.findByText(`Voicebox isn't running at ${URL}.`);
  fireEvent.click(screen.getByRole("menuitem", { name: "Check Voicebox" }));

  expect(status).toHaveBeenCalledTimes(2);
  expect(screen.getByRole("menu")).toBeTruthy();
  await screen.findByRole("menuitemradio", { name: "Nova (English)" });
});

test("the checkbox is disabled without a chosen voice", () => {
  // Disabled-ness comes from `engine`/`profile` alone, so this needs no Voicebox check to settle.
  renderMenu({ profile: null, api: fakeApi(vi.fn(() => new Promise<VoiceboxStatus>(() => {}))) });
  openMenu();

  const row = screen.getByRole("menuitemcheckbox", { name: "Speak while VIKI writes" });
  expect(row.getAttribute("aria-disabled")).toBe("true");
});

test("the checkbox patches true", () => {
  const { patch } = renderMenu({
    profile: "nova",
    speakWhileWriting: false,
    api: fakeApi(vi.fn(() => new Promise<VoiceboxStatus>(() => {}))),
  });
  openMenu();

  fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Speak while VIKI writes" }));
  expect(patch).toHaveBeenCalledExactlyOnceWith({ "voice.speak_while_writing": true });
});

test("the checkbox patches false", () => {
  const { patch } = renderMenu({
    profile: "nova",
    speakWhileWriting: true,
    api: fakeApi(vi.fn(() => new Promise<VoiceboxStatus>(() => {}))),
  });
  openMenu();

  fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Speak while VIKI writes" }));
  expect(patch).toHaveBeenCalledExactlyOnceWith({ "voice.speak_while_writing": false });
});

test("the checkbox shows on/off state: class, aria-checked, and the check indicator", () => {
  renderMenu({
    profile: "nova",
    speakWhileWriting: true,
    api: fakeApi(vi.fn(() => new Promise<VoiceboxStatus>(() => {}))),
  });
  openMenu();
  const onRow = screen.getByRole("menuitemcheckbox", { name: "Speak while VIKI writes" });
  expect(onRow.classList.contains("on")).toBe(true);
  expect(onRow.getAttribute("aria-checked")).toBe("true");
  expect(onRow.querySelector(".voice-menu-check")).not.toBeNull();
  cleanup();

  renderMenu({
    profile: "nova",
    speakWhileWriting: false,
    api: fakeApi(vi.fn(() => new Promise<VoiceboxStatus>(() => {}))),
  });
  openMenu();
  const offRow = screen.getByRole("menuitemcheckbox", { name: "Speak while VIKI writes" });
  expect(offRow.classList.contains("on")).toBe(false);
  expect(offRow.getAttribute("aria-checked")).toBe("false");
  expect(offRow.querySelector(".voice-menu-check")).toBeNull();
});

test("Voice settings… calls openSettings", () => {
  const { openSettings } = renderMenu({
    api: fakeApi(vi.fn(() => new Promise<VoiceboxStatus>(() => {}))),
  });
  openMenu();

  fireEvent.click(screen.getByRole("menuitem", { name: "Voice settings…" }));
  expect(openSettings).toHaveBeenCalledTimes(1);
});

test("onStatus receives the resolved status", async () => {
  const { onStatus } = renderMenu({ api: fakeApi(vi.fn(() => Promise.resolve(READY))) });
  openMenu();
  await screen.findByRole("menuitemradio", { name: "Nova (English)" });

  expect(onStatus).toHaveBeenCalledExactlyOnceWith(READY);
});

test("no api makes no call, even with a context provider, when api is explicitly null", () => {
  const status = vi.fn(() => Promise.resolve(READY));
  render(
    <VoiceboxContext.Provider value={fakeApi(status)}>
      <VoiceMenu
        engine="voicebox"
        pack={null}
        profile={null}
        speakWhileWriting={false}
        url={URL}
        patch={vi.fn()}
        openSettings={vi.fn()}
        api={null}
      />
    </VoiceboxContext.Provider>,
  );
  openMenu();
  expect(status).not.toHaveBeenCalled();
});

test("an omitted api falls back to VoiceboxContext", () => {
  const status = vi.fn(() => Promise.resolve(READY));
  render(
    <VoiceboxContext.Provider value={fakeApi(status)}>
      <VoiceMenu
        engine="voicebox"
        pack={null}
        profile={null}
        speakWhileWriting={false}
        url={URL}
        patch={vi.fn()}
        openSettings={vi.fn()}
      />
    </VoiceboxContext.Provider>,
  );
  openMenu();
  expect(status).toHaveBeenCalledTimes(1);
});

test("makes no status call on open unless the engine is voicebox", async () => {
  const status = vi.fn(() => Promise.resolve(READY));
  renderMenu({ engine: "system", api: fakeApi(status) });
  openMenu();
  await flush();
  expect(status).not.toHaveBeenCalled();
  cleanup();

  renderMenu({ engine: "pack", api: fakeApi(status) });
  openMenu();
  await flush();
  expect(status).not.toHaveBeenCalled();
});

test("System voice is the only row, and the sole choice, for the system engine", () => {
  renderMenu({ engine: "system" });
  openMenu();
  expect(screen.getAllByRole("menuitemradio").map((row) => row.textContent)).toEqual([
    "System voice",
  ]);
  expect(
    screen.getByRole("menuitemradio", { name: "System voice" }).getAttribute("aria-checked"),
  ).toBe("true");
});

test("Natural voice offers itself once the pack is installed, and patches the pack engine", () => {
  const { patch } = renderMenu({ engine: "system", pack: pack({ state: "ready" }) });
  openMenu();

  fireEvent.click(screen.getByRole("menuitemradio", { name: "Natural voice" }));
  expect(patch).toHaveBeenCalledExactlyOnceWith({ "voice.speech_engine": "pack" });
});

test("Natural voice is checked once the pack engine is chosen", () => {
  renderMenu({ engine: "pack", pack: pack({ state: "ready" }) });
  openMenu();
  expect(
    screen.getByRole("menuitemradio", { name: "Natural voice" }).getAttribute("aria-checked"),
  ).toBe("true");
});

test("Natural voice stays off the menu before the pack is installed", () => {
  renderMenu({ engine: "system", pack: pack({ state: "not_installed" }) });
  openMenu();
  expect(screen.queryByRole("menuitemradio", { name: "Natural voice" })).toBeNull();
});

test("Natural voices… offers Settings before the pack is installed, and calls openSettings", () => {
  const { openSettings } = renderMenu({ pack: null });
  openMenu();

  fireEvent.click(screen.getByRole("menuitem", { name: "Natural voices…" }));
  expect(openSettings).toHaveBeenCalledTimes(1);
});

test("Natural voices… drops off the menu once the pack is installed", () => {
  renderMenu({ pack: pack({ state: "stopped" }) });
  openMenu();
  expect(screen.queryByRole("menuitem", { name: "Natural voices…" })).toBeNull();
});

test("Natural voices… also stays off the menu when the platform flatly can't run it", () => {
  renderMenu({ pack: pack({ state: "unsupported", reason: "platform" }) });
  openMenu();
  expect(screen.queryByRole("menuitem", { name: "Natural voices…" })).toBeNull();
});

test("Natural voices… still shows for an unsupported pack with another reason", () => {
  renderMenu({ pack: pack({ state: "unsupported", reason: "no_nvidia" }) });
  openMenu();
  expect(screen.queryByRole("menuitem", { name: "Natural voices…" })).not.toBeNull();
});

test("Get Voicebox opens the releases page once the own app is unreachable", async () => {
  const openUrl = vi.fn();
  renderMenu({
    openUrl,
    api: fakeApi(
      vi.fn(() =>
        Promise.resolve<VoiceboxStatus>({ state: "unreachable", gpu: null, profiles: [] }),
      ),
    ),
  });
  openMenu();

  fireEvent.click(await screen.findByRole("menuitem", { name: "Get Voicebox" }));
  expect(openUrl).toHaveBeenCalledExactlyOnceWith(VOICEBOX_RELEASES_URL);
});

test("Get Voicebox is hidden without a VoicePackContext to open it with", async () => {
  renderMenu({
    api: fakeApi(
      vi.fn(() =>
        Promise.resolve<VoiceboxStatus>({ state: "unreachable", gpu: null, profiles: [] }),
      ),
    ),
  });
  openMenu();

  await screen.findByText(`Voicebox isn't running at ${URL}.`);
  expect(screen.queryByRole("menuitem", { name: "Get Voicebox" })).toBeNull();
});

test("Get Voicebox stays hidden for an answer that isn't Voicebox", async () => {
  renderMenu({
    openUrl: vi.fn(),
    api: fakeApi(
      vi.fn(() =>
        Promise.resolve<VoiceboxStatus>({ state: "unexpected", gpu: null, profiles: [] }),
      ),
    ),
  });
  openMenu();

  await screen.findByText(`Something other than Voicebox answered at ${URL}.`);
  expect(screen.queryByRole("menuitem", { name: "Get Voicebox" })).toBeNull();
});

test("the checkbox is clickable for an installed pack with no profile at all", () => {
  const { patch } = renderMenu({ engine: "pack", pack: pack({ state: "ready" }), profile: null });
  openMenu();
  fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Speak while VIKI writes" }));
  expect(patch).toHaveBeenCalledExactlyOnceWith({ "voice.speak_while_writing": true });
});

test("the checkbox stays disabled for the pack engine before it is installed", () => {
  renderMenu({ engine: "pack", pack: pack({ state: "not_installed" }) });
  openMenu();
  const row = screen.getByRole("menuitemcheckbox", { name: "Speak while VIKI writes" });
  expect(row.getAttribute("aria-disabled")).toBe("true");
});
