/**
 * VIKI's voice conversation in its launcher panel, as App wires it: a second hands-free loop
 * bound to VIKI's chat, started or ended by Alt+T, ended first by the panel's Escape, and the
 * rule that the open panel owns the window's voice. The dictation api is the real
 * `createDictationApi`, so its per-window socket order holds, over scripted sockets the test
 * answers for the server, with a scripted status and no microphone; the read-aloud settings are
 * a ready controller with one local English voice.
 */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, GENERATION, sessionRow } from "../../accept/m3b/fakes";
import type { SettingsView } from "../../clients/app/shellApi";
import { createFakeDecisionLoopApi } from "../../clients/decisionLoop/fake";
import type { ProfileApi } from "../../clients/profiles/profileApi";
import type { SessionRow } from "../../protocol";
import { fakeDurable } from "../../testDurable";
import { type FakeShellApi, fakeShellApi, settingsFixture } from "../../testShell";
import { createDictationApi } from "../../voice/dictationApi";
import { DictationContext } from "../../voice/dictationContext";
import type { DictationWebSocket } from "../../voice/dictationSocket";
import type { CaptureOptions, DictationApi, VoiceStatus } from "../../voice/dictationTypes";
import { createReadAloud } from "../../voice/readAloud";
import { withSpokenAnswerRequest } from "../../voice/spokenAnswer";
import * as ReadAloudModule from "../../voice/useReadAloudSettings";

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  sessionStorage.clear();
  localStorage.clear();
  vi.restoreAllMocks();
});

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const pending = () => new Promise<never>(() => {});
const profiles: ProfileApi = {
  list: async () => [],
  get: pending,
  create: pending,
  revise: pending,
  archive: pending,
  duplicate: pending,
  compare: pending,
};
const READY: VoiceStatus = {
  state: "ready",
  running: false,
  model: "small-q8_0",
  backend: "cpu",
  device: null,
  languages: ["auto"],
  language: "auto",
  language_unavailable: false,
  models: ["small-q8_0"],
  backends: ["cpu"],
  fallback: null,
  cpu_model: "small-q8_0",
  slow_on_cpu: false,
};

async function settled() {
  await act(async () => {
    for (let index = 0; index < 20; index++) await Promise.resolve();
  });
}

/** One `/ws/voice` socket as the api creates it; the test opens it, speaks for the server and
 * closes it. */
class VoiceSocketFake implements DictationWebSocket {
  binaryType = "blob";
  sent: (string | ArrayBuffer)[] = [];
  closedWith: number[] = [];
  onopen: ((event: unknown) => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  send(data: string | ArrayBuffer) {
    this.sent.push(data);
  }
  close(code?: number) {
    this.closedWith.push(code ?? 1005);
  }
  open() {
    act(() => this.onopen?.({}));
  }
  commands(): { type: string; capture_id?: string }[] {
    return this.sent.flatMap((data) => (typeof data === "string" ? [JSON.parse(data)] : []));
  }
  captureId(): string {
    return this.commands().find((command) => command.type === "begin")?.capture_id ?? "";
  }
  say(message: Record<string, unknown>) {
    act(() => this.onmessage?.({ data: JSON.stringify(message) }));
  }
}

/** The window's one dictation api: main.tsx's `createDictationApi`, its sockets scripted. */
function voiceService() {
  const sockets: VoiceSocketFake[] = [];
  const startCapture = vi.fn(async (_options: CaptureOptions) => ({
    deviceId: null,
    stop: vi.fn(async () => {}),
  }));
  const api: DictationApi = {
    ...createDictationApi("tok", {
      workletUrl: "/worklet.js",
      startCapture,
      microphones: async () => [],
      location: { protocol: "http:", host: "127.0.0.1:8765" },
      createSocket: () => {
        const socket = new VoiceSocketFake();
        sockets.push(socket);
        return socket;
      },
    }),
    // The two HTTP calls, scripted; `socket` stays the api's own, with its per-window order.
    status: vi.fn(async () => READY),
    report: vi.fn(),
  };
  return { api, sockets, startCapture };
}

function readySettings(effective: Record<string, unknown> = {}): SettingsView {
  const base = settingsFixture({ "onboarding.completed": true });
  return { ...base, effective: { ...base.effective, ...effective } };
}

function mount(
  options: { voice?: DictationApi; shell?: FakeShellApi; sessions?: SessionRow[] } = {},
) {
  // What the window's system voice was asked to say.
  const spoken: string[] = [];
  const controller = createReadAloud({
    synth: {
      paused: false,
      getVoices: () => [
        { name: "Zira", voiceURI: "Zira", lang: "en-US", localService: true, default: false },
      ],
      addEventListener: () => {},
      removeEventListener: () => {},
      speak: (utterance: { text: string }) => spoken.push(utterance.text),
      cancel: () => {},
      pause: () => {},
      resume: () => {},
    } as unknown as SpeechSynthesis,
    makeUtterance: (text) =>
      ({ text, voice: null, lang: "", rate: 1, onend: null, onerror: null }) as never,
    target: new EventTarget(),
    devices: null,
    setTimeout: () => 0,
    clearTimeout: () => {},
  });
  vi.spyOn(ReadAloudModule, "useReadAloudSettings").mockReturnValue({
    controller,
    rate: 1,
    voices: {},
    noticeSeen: true,
    acknowledge: () => {},
  });
  const { viki, socket } = fakeViki();
  const shell = options.shell ?? fakeShellApi({ settings: readySettings() });
  const app = (
    <App
      viki={viki}
      rest={fakeRest({ sessions: options.sessions ?? [] })}
      durable={fakeDurable()}
      shell={shell}
      profileApi={profiles}
      decisionApi={createFakeDecisionLoopApi()}
    />
  );
  render(
    options.voice ? (
      <DictationContext.Provider value={options.voice}>{app}</DictationContext.Provider>
    ) : (
      app
    ),
  );
  act(() => socket.open());
  return { socket, shell, spoken };
}

const panel = () => screen.queryByRole("dialog", { name: "VIKI" });
function openPanel(): HTMLElement {
  const dialog = panel();
  if (!dialog) throw new Error("the panel is closed");
  return dialog;
}
/** VIKI's own Hands-free button, in the open panel. */
const panelToggle = () => panel()?.querySelector<HTMLButtonElement>(".hands-free-toggle") ?? null;
/** The main message box's Hands-free button, outside the panel. */
const mainToggle = () =>
  [...document.querySelectorAll<HTMLButtonElement>(".hands-free-toggle")].find(
    (button) => !button.closest(".dloop-panel"),
  ) ?? null;
const mainStrip = () =>
  [...document.querySelectorAll(".hands-free-strip")].find(
    (strip) => !strip.closest(".dloop-panel"),
  ) ?? null;
const patches = (shell: FakeShellApi) =>
  shell.calls.filter((call) => call.method === "patchSettings").map((call) => call.args[2]);

async function key(code: "KeyT" | "KeyV", target: Element | Window = window) {
  act(() => {
    fireEvent.keyDown(target, { altKey: true, code });
  });
  await settled();
}
async function pressEscape() {
  act(() => {
    fireEvent.keyDown(document.activeElement ?? window, { key: "Escape" });
  });
  await settled();
}
async function click(element: Element | null) {
  if (!element) throw new Error("nothing to click");
  await act(async () => void fireEvent.click(element));
  await settled();
}
/** The server's side of one hands-free capture on `capture`: it starts, hears `text` and ends. */
async function hear(capture: VoiceSocketFake | undefined, text: string) {
  if (!capture) throw new Error("no capture socket");
  capture.open();
  const id = capture.captureId();
  capture.say({
    type: "capturing",
    capture_id: id,
    model: "small-q8_0",
    backend: "cpu",
    device: null,
    language: "auto",
    language_unavailable: false,
    fallback: null,
  });
  capture.say({ type: "final", capture_id: id, segment: 0, text });
  await settled();
  capture.say({ type: "done", capture_id: id });
  await settled();
}
const AT = "2026-10-24T00:00:00.000Z";
/** A finished turn in `sessionId`, delivered live from `seq` on: the user's words, then `reply`. */
function finishedTurn(sessionId: string, seq: number, reply: string): Record<string, unknown>[] {
  const frame = (type: string, offset: number, data: Record<string, unknown>) => ({
    v: 2,
    generation: GENERATION,
    request_id: "",
    type,
    session_id: sessionId,
    seq: seq + offset,
    data,
  });
  return [
    frame("user.message", 0, { text: "Plan the trip budget", command: "", at: AT }),
    frame("text.done", 1, { block_id: "b1", text: reply }),
    frame("turn.done", 2, {
      status: "success",
      usage: { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_creation_tokens: 0 },
      context_percent: null,
      duration_ms: 1,
    }),
  ];
}

test("Alt+T with the panel closed opens it into Message VIKI and starts VIKI's voice conversation: one capture", async () => {
  const voice = voiceService();
  mount({ voice: voice.api });
  await settled();
  const messageBox = screen.getByRole("textbox", { name: "Message" });
  messageBox.focus();
  expect(panel()).toBeNull();

  await key("KeyT", messageBox);
  const dialog = within(openPanel());
  expect(document.activeElement).toBe(dialog.getByRole("textbox", { name: "Message VIKI" }));
  expect(panelToggle()?.getAttribute("aria-pressed")).toBe("true");
  expect(panelToggle()?.getAttribute("aria-keyshortcuts")).toBe("Alt+T");
  expect(dialog.getByText("Listening")).toBeTruthy();
  expect(voice.startCapture).toHaveBeenCalledTimes(1);
  expect(voice.sockets).toHaveLength(1);
  voice.sockets[0]?.open();
  expect(voice.sockets[0]?.commands().map((command) => command.type)).toEqual(["auth", "begin"]);

  // Alt+T again, from inside the open panel, ends it.
  await key("KeyT", dialog.getByRole("textbox", { name: "Message VIKI" }));
  expect(panel()).toBeTruthy();
  expect(panelToggle()?.getAttribute("aria-pressed")).toBe("false");
  expect(voice.sockets[0]?.commands().at(-1)?.type).toBe("cancel");
  expect(voice.startCapture).toHaveBeenCalledTimes(1);
});

test("main voice on: a launcher click un-presses the main toggle and cancels its capture; after the panel closes the main stays off", async () => {
  const voice = voiceService();
  mount({ voice: voice.api });
  await settled();
  await click(mainToggle());
  expect(mainToggle()?.getAttribute("aria-pressed")).toBe("true");
  expect(voice.startCapture).toHaveBeenCalledTimes(1);
  const main = voice.sockets[0];
  main?.open();
  const mainId = main?.captureId();
  expect(mainId).toMatch(UUID);

  await click(document.querySelector("button.dloop-launcher"));
  const dialog = openPanel();
  expect(mainToggle()?.getAttribute("aria-pressed")).toBe("false");
  expect(main?.commands()).toContainEqual({ type: "cancel", capture_id: mainId });
  expect(mainStrip()).toBeNull();
  // The panel offers the window's mic and its own Hands-free button, off until pressed.
  expect(dialog.querySelector(".voice-mic")).toBeTruthy();
  expect(panelToggle()?.getAttribute("aria-pressed")).toBe("false");
  expect(within(dialog).getByRole("tooltip").textContent).toBe("Start voice conversation (Alt+T)");

  act(() => main?.onclose?.({ code: 1000 }));
  await pressEscape();
  expect(panel()).toBeNull();
  expect(mainToggle()?.getAttribute("aria-pressed")).toBe("false");
  expect(mainStrip()).toBeNull();
  expect(voice.startCapture).toHaveBeenCalledTimes(1);
  expect(voice.sockets).toHaveLength(1);
});

test("opening the panel cancels the main box's plain dictation too", async () => {
  const voice = voiceService();
  mount({ voice: voice.api });
  await settled();
  const mic = [...document.querySelectorAll(".voice-mic")].find(
    (button) => !button.closest(".dloop-panel"),
  );
  await click(mic ?? null);
  expect(voice.startCapture).toHaveBeenCalledTimes(1);
  const main = voice.sockets[0];
  main?.open();
  const mainId = main?.captureId();
  expect(mainToggle()?.getAttribute("aria-pressed")).toBe("false");

  await key("KeyV");
  openPanel();
  expect(main?.commands()).toContainEqual({ type: "cancel", capture_id: mainId });
});

test("Alt+T while the main box listens hands the mic over: the panel's socket connects only after the main one closed", async () => {
  const voice = voiceService();
  mount({ voice: voice.api });
  await settled();
  await click(mainToggle());
  const main = voice.sockets[0];
  main?.open();
  const mainId = main?.captureId();

  await key("KeyT");
  expect(panelToggle()?.getAttribute("aria-pressed")).toBe("true");
  expect(mainToggle()?.getAttribute("aria-pressed")).toBe("false");
  expect(main?.commands()).toContainEqual({ type: "cancel", capture_id: mainId });
  // The panel's capture has started, but its socket waits for the main one to close.
  expect(voice.startCapture).toHaveBeenCalledTimes(2);
  expect(voice.sockets).toHaveLength(1);

  act(() => main?.onclose?.({ code: 1000 }));
  await settled();
  expect(voice.sockets).toHaveLength(2);
  const next = voice.sockets[1];
  next?.open();
  expect(next?.commands().map((command) => command.type)).toEqual(["auth", "begin"]);
  expect(next?.captureId()).not.toBe(mainId);
});

test("with VIKI's voice on, the first Esc ends it and keeps the panel; the next closes the panel", async () => {
  const voice = voiceService();
  mount({ voice: voice.api });
  await settled();
  await key("KeyT");
  expect(panelToggle()?.getAttribute("aria-pressed")).toBe("true");
  const capture = voice.sockets[0];
  capture?.open();
  const id = capture?.captureId();

  await pressEscape();
  expect(panel()).toBeTruthy();
  expect(panelToggle()?.getAttribute("aria-pressed")).toBe("false");
  expect(capture?.commands()).toContainEqual({ type: "cancel", capture_id: id });

  await pressEscape();
  expect(panel()).toBeNull();
});

test("Alt+V closing the panel ends VIKI's voice conversation; reopened, it is off and no mic opens", async () => {
  const voice = voiceService();
  mount({ voice: voice.api });
  await settled();
  await key("KeyT");
  expect(panelToggle()?.getAttribute("aria-pressed")).toBe("true");
  const capture = voice.sockets[0];
  capture?.open();
  const id = capture?.captureId();

  await key("KeyV");
  expect(panel()).toBeNull();
  expect(capture?.commands()).toContainEqual({ type: "cancel", capture_id: id });

  await click(document.querySelector("button.dloop-launcher"));
  openPanel();
  expect(panelToggle()?.getAttribute("aria-pressed")).toBe("false");
  expect(panel()?.querySelector(".hands-free-strip")).toBeNull();
  expect(voice.startCapture).toHaveBeenCalledTimes(1);
});

test("a capture's final in the panel sends VIKI's first message, saves the new chat and waits Thinking", async () => {
  const voice = voiceService();
  const { socket, shell } = mount({ voice: voice.api });
  await settled();
  await key("KeyT");
  await hear(voice.sockets[0], "What is due today?");

  const saved = patches(shell).find((changes) =>
    Object.hasOwn(changes as object, "decision.launcher_session"),
  ) as Record<string, string> | undefined;
  const chatId = saved?.["decision.launcher_session"] ?? "";
  expect(chatId).toMatch(UUID);
  expect(
    socket.sent.some((frame) => frame.type === "session.open" && frame.session_id === chatId),
  ).toBe(true);
  const sent = socket.sent.find((frame) => frame.type === "turn.send");
  expect(sent?.session_id).toBe(chatId);
  expect(String(sent?.text).startsWith("What is due today?")).toBe(true);
  expect(sent?.text).toBe(withSpokenAnswerRequest("What is due today?"));
  expect(panel()?.querySelector(".hands-free-strip")?.textContent).toContain("Thinking");
  expect(panelToggle()?.getAttribute("aria-pressed")).toBe("true");
});

test("VIKI's chat changing underneath (another window's save) ends its voice conversation, and nothing from the other chat is spoken", async () => {
  const other = "4c0f8d36-3c43-4a5e-9a2b-0d1f2e3c4b5a";
  const voice = voiceService();
  const { socket, shell, spoken } = mount({
    voice: voice.api,
    sessions: [sessionRow({ session_id: other, title: "Trip budget" })],
  });
  await settled();
  // This window holds the other chat, its reply finished.
  await click(document.querySelector(`[data-session-id="${other}"]`));
  act(() => {
    for (const frame of finishedTurn(other, 1, "Your trip budget is ready.")) socket.receive(frame);
  });
  await settled();

  // Voice on in VIKI's panel: its first message starts VIKI's chat, and VIKI thinks there.
  await key("KeyT");
  await hear(voice.sockets[0], "What is due today?");
  expect(panel()?.querySelector(".hands-free-strip")?.textContent).toContain("Thinking");

  // Another window makes the other chat VIKI's; this window adopts that document with its next
  // settings write (here the panel menu's End focus).
  const before = shell.data.settings;
  shell.data.settings = {
    ...before,
    revision: before.revision + 1,
    values: { ...before.values, "decision.launcher_session": other },
    effective: { ...before.effective, "decision.launcher_session": other },
  };
  await click(within(openPanel()).getAllByRole("button", { name: "More" })[0] ?? null);
  await click(screen.getByRole("menuitem", { name: "End focus" }));

  expect(spoken).toEqual([]);
  expect(panelToggle()?.getAttribute("aria-pressed")).toBe("false");
  expect(
    within(openPanel()).getByText("Hands-free stopped because you switched chats."),
  ).toBeTruthy();
});

test("New conversation ends a running voice conversation with the switched-chats notice", async () => {
  const savedId = "7d3b1c52-5c1e-4a43-9d55-2f0c4a1f6e10";
  const shell = fakeShellApi({
    settings: readySettings({ "decision.launcher_session": savedId }),
  });
  const voice = voiceService();
  mount({ voice: voice.api, shell });
  await settled();
  await key("KeyT");
  expect(panelToggle()?.getAttribute("aria-pressed")).toBe("true");

  await click(within(openPanel()).getByRole("button", { name: "New conversation" }));
  expect(panelToggle()?.getAttribute("aria-pressed")).toBe("false");
  expect(
    within(openPanel()).getByText("Hands-free stopped because you switched chats."),
  ).toBeTruthy();
});

test("without a DictationContext, VIKI's chat has no Hands-free button and Alt+T only opens the panel", async () => {
  mount();
  await settled();
  await key("KeyT");
  const dialog = openPanel();
  expect(document.activeElement).toBe(
    within(dialog).getByRole("textbox", { name: "Message VIKI" }),
  );
  expect(within(dialog).queryByRole("button", { name: "Hands-free" })).toBeNull();
  await key("KeyT");
  expect(panel()).toBe(dialog);
});
