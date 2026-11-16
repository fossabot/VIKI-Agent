/**
 * The Composer's hands-free bridge at the loop's edges: the mode ending by any path
 * other than the Composer's own Esc, a mic press while the loop is not listening, and the Esc it
 * handles itself. The api is a scripted fake: no socket, no microphone.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { t as tr } from "../../i18n/t";
import { emptyView, type Turn } from "../../state/view";
import { DictationContext } from "../../voice/dictationContext";
import type {
  CaptureOptions,
  DictationApi,
  VoiceServerMessage,
  VoiceSocketHandlers,
  VoiceStatus,
} from "../../voice/dictationTypes";
import type { HandsFreeBridge, HandsFreeComposerEvent } from "../../voice/handsFree";
import { createReadAloud } from "../../voice/readAloud";
import { withSpokenAnswerRequest } from "../../voice/spokenAnswer";
import { useHandsFree } from "../../voice/useHandsFree";
import {
  ReadAloudContext,
  type ReadAloudSettings,
  useReadAloudReply,
} from "../Conversation/ReadAloud";
import { Composer } from "./Composer";

beforeEach(() => {
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  localStorage.clear();
});

const READY: VoiceStatus = {
  state: "ready",
  running: false,
  model: "small-q8_0",
  backend: "cpu",
  device: null,
  languages: ["auto", "en", "ja"],
  language: "auto",
  language_unavailable: false,
  models: ["small-q8_0"],
  backends: ["cpu"],
  fallback: null,
  cpu_model: "small-q8_0",
  slow_on_cpu: false,
};

const settled = () =>
  act(async () => {
    for (let i = 0; i < 10; i++) await Promise.resolve();
  });

/** A scripted dictation api: every socket call lands in `log`, and the newest socket's messages are
 * played by hand. */
function dictation() {
  const log: string[] = [];
  const sockets: VoiceSocketHandlers[] = [];
  const api = {
    status: vi.fn(async () => READY),
    clearFallback: vi.fn(async () => READY),
    socket: vi.fn((handlers: VoiceSocketHandlers) => {
      sockets.push(handlers);
      return {
        begin: vi.fn((id: string) => log.push(`begin ${id}`)),
        audio: vi.fn(),
        end: vi.fn((id: string) => log.push(`end ${id}`)),
        cancel: vi.fn((id: string) => log.push(`cancel ${id}`)),
        close: vi.fn(),
      };
    }),
    startCapture: vi.fn(async (options: CaptureOptions) => ({
      deviceId: options.deviceId,
      stop: vi.fn(async () => {}),
    })),
    microphones: vi.fn(async () => []),
    workletUrl: "/worklet.js",
  } satisfies DictationApi;
  const id = () =>
    log
      .filter((line) => line.startsWith("begin "))
      .at(-1)
      ?.slice(6) ?? "";
  const message = (m: VoiceServerMessage) => sockets.at(-1)?.message(m);
  return {
    api,
    log,
    id,
    capturing: () =>
      act(() =>
        message({
          type: "capturing",
          capture_id: id(),
          model: "small-q8_0",
          backend: "cpu",
          device: null,
          language: "auto",
          language_unavailable: false,
          fallback: null,
        }),
      ),
    final: (text: string) => message({ type: "final", capture_id: id(), segment: 0, text }),
    done: () => message({ type: "done", capture_id: id() }),
  };
}

async function mount(listening: boolean) {
  const voice = dictation();
  const onSend = vi.fn(async (_text: string) => true);
  const dispatch = vi.fn((_event: HandsFreeComposerEvent) => undefined);
  const view = (bridge: HandsFreeBridge) => (
    <DictationContext.Provider value={voice.api}>
      <Composer running={false} onSend={onSend} onStop={vi.fn()} handsFree={bridge} />
    </DictationContext.Provider>
  );
  const { container, rerender } = render(view({ active: true, listening, dispatch }));
  await settled();
  /** Re-renders with no settling, so it can share one commit with a message. */
  const setBridge = (bridge: Partial<HandsFreeBridge>) =>
    rerender(view({ active: true, listening, dispatch, ...bridge }));
  return {
    ...voice,
    onSend,
    dispatch,
    setBridge,
    box: container.querySelector("textarea") as HTMLTextAreaElement,
    mic: container.querySelector(".voice-mic") as HTMLButtonElement,
    events: () => dispatch.mock.calls.map(([event]) => event.type),
  };
}

test("leaving the mode any other way cancels the open capture, and nothing it heard is sent", async () => {
  const t = await mount(true);
  await t.capturing();
  const capture = t.id();
  act(() => t.setBridge({ active: false, listening: false }));
  await settled();
  expect(t.log).toContain(`cancel ${capture}`);
  act(() => t.final("buy milk"));
  act(() => t.done());
  await settled();
  expect(t.onSend).not.toHaveBeenCalled();
  expect(t.events()).toEqual([]);
});

test("a capture that finishes in the same render the mode ends is not sent", async () => {
  const t = await mount(true);
  await t.capturing();
  act(() => t.final("buy milk"));
  act(() => {
    t.setBridge({ active: false, listening: false });
    t.done();
  });
  await settled();
  expect(t.onSend).not.toHaveBeenCalled();
  expect(t.box.value).toBe("buy milk");
});

test("a mic press while the loop is not listening interrupts it instead of opening a capture", async () => {
  const t = await mount(false);
  fireEvent.click(t.mic, { detail: 0 });
  fireEvent.pointerDown(t.mic);
  fireEvent.pointerUp(t.mic);
  await settled();
  expect(t.events()).toEqual(["micPressed", "micPressed"]);
  expect(t.api.socket).not.toHaveBeenCalled();
  // The loop's listening edge then opens the one capture.
  act(() => t.setBridge({ listening: true }));
  await settled();
  expect(t.api.socket).toHaveBeenCalledTimes(1);
});

test("the Composer's Esc that leaves the mode marks the key handled, so nothing else acts on it", async () => {
  const t = await mount(true);
  await t.capturing();
  expect(fireEvent.keyDown(t.box, { key: "Escape" })).toBe(false);
  expect(t.events()).toEqual(["exit"]);
});

test("the box holds the spoken-answer request while the automatic Send is out, and a refusal puts the words heard back", async () => {
  const t = await mount(true);
  act(() => t.setBridge({ message: withSpokenAnswerRequest }));
  let answer: (sent: boolean) => void = () => {};
  t.onSend.mockImplementation(
    () =>
      new Promise<boolean>((resolve) => {
        answer = resolve;
      }),
  );
  await t.capturing();
  act(() => t.final("buy milk"));
  act(() => t.done());
  await settled();
  expect(t.onSend).toHaveBeenCalledWith(withSpokenAnswerRequest("buy milk"));
  expect(t.box.value).toBe(withSpokenAnswerRequest("buy milk"));
  await act(async () => answer(false));
  await settled();
  expect(t.box.value).toBe("buy milk");
  expect(t.events()).toEqual(["captureDone", "sendRefused"]);
});

const REPLY_TEXT = "The weather is fine today and you are here.";
const REPLY: Turn = {
  id: "t1",
  user: null,
  blocks: [{ kind: "text", id: "t1-b", text: REPLY_TEXT, done: true }],
  status: "success",
  usage: null,
  durationMs: 0,
  history: false,
};
const ZIRA = { name: "Zira", voiceURI: "Zira", lang: "en-US", localService: true, default: false };

function Reply(): ReactElement {
  return <div>{useReadAloudReply(REPLY).controls}</div>;
}

/** App's wiring cut down to the loop: `useHandsFree` above a toggle, a reply with its own Read, and
 * the Composer only while the chat is shown (App renders no composer on any other view). */
async function mountWindow() {
  const voice = dictation();
  const spoken: string[] = [];
  const synth = {
    paused: false,
    getVoices: () => [ZIRA],
    addEventListener: () => {},
    removeEventListener: () => {},
    speak: (u: { text: string }) => spoken.push(u.text),
    cancel: () => {},
    pause: () => {},
    resume: () => {},
  };
  const readAloud: ReadAloudSettings = {
    controller: createReadAloud({
      synth: synth as unknown as SpeechSynthesis,
      makeUtterance: (text) =>
        ({ text, voice: null, lang: "", rate: 1, onend: null, onerror: null }) as never,
      target: new EventTarget(),
      devices: null,
      setTimeout: () => 0,
      clearTimeout: () => {},
    }),
    rate: 1,
    voices: {},
    noticeSeen: true,
    acknowledge: () => {},
  };
  const onSend = vi.fn(async (_text: string) => true);
  function Window({ chatShown }: { chatShown: boolean }): ReactElement {
    const handsFree = useHandsFree({
      dictationApi: voice.api,
      view: { ...emptyView("s1"), turns: [REPLY] },
      readAloud,
      sessionKey: "s1",
      parser: null,
      chatShown,
    });
    return (
      <DictationContext.Provider value={voice.api}>
        <ReadAloudContext.Provider value={readAloud}>
          <output data-testid="phase">{handsFree.state.phase}</output>
          <button type="button" onClick={handsFree.toggle}>
            Hands-free
          </button>
          <Reply />
          {chatShown ? (
            <Composer
              running={false}
              onSend={onSend}
              onStop={vi.fn()}
              handsFree={handsFree.bridge}
            />
          ) : null}
        </ReadAloudContext.Provider>
      </DictationContext.Provider>
    );
  }
  const { rerender } = render(<Window chatShown />);
  await settled();
  const click = async (name: string) => {
    fireEvent.click(screen.getByRole("button", { name }));
    await settled();
  };
  return {
    ...voice,
    spoken,
    onSend,
    phase: () => screen.getByTestId("phase").textContent,
    enter: () => click("Hands-free"),
    read: () => click(tr("readaloud.read.label")),
    show: async (chatShown: boolean) => {
      rerender(<Window chatShown={chatShown} />);
      await settled();
    },
  };
}

test("a reply's own Read while hands-free listens ends the mode quietly: the capture is cancelled, nothing it heard is sent, and the Read plays", async () => {
  const t = await mountWindow();
  await t.enter();
  expect(t.phase()).toBe("listening");
  await t.capturing();
  const capture = t.id();
  await t.read();
  expect(t.phase()).toBe("off");
  expect(t.log).toContain(`cancel ${capture}`);
  expect(t.spoken).toEqual([REPLY_TEXT]);
  act(() => t.final(REPLY_TEXT));
  act(() => t.done());
  await settled();
  expect(t.onSend).not.toHaveBeenCalled();
});

test("leaving the chat while hands-free listens ends the mode, and coming back opens no mic", async () => {
  const t = await mountWindow();
  await t.enter();
  expect(t.phase()).toBe("listening");
  expect(t.api.socket).toHaveBeenCalledTimes(1);
  await t.show(false);
  expect(t.phase()).toBe("off");
  await t.show(true);
  expect(t.api.socket).toHaveBeenCalledTimes(1);
  expect(t.phase()).toBe("off");
});

test("the loop's automatic Send carries the spoken-answer request; a typed Send while the mode is on does not", async () => {
  const t = await mountWindow();
  await t.enter();
  await t.capturing();
  act(() => t.final("buy milk"));
  act(() => t.done());
  await settled();
  expect(t.onSend).toHaveBeenCalledWith(withSpokenAnswerRequest("buy milk"));
  expect(t.phase()).toBe("thinking");
  const box = screen.getByRole("textbox") as HTMLTextAreaElement;
  expect(box.value).toBe("");
  fireEvent.change(box, { target: { value: "and eggs" } });
  fireEvent.keyDown(box, { key: "Enter" });
  await settled();
  expect(t.onSend).toHaveBeenCalledTimes(2);
  expect(t.onSend).toHaveBeenLastCalledWith("and eggs");
  expect(t.phase()).toBe("thinking");
});
