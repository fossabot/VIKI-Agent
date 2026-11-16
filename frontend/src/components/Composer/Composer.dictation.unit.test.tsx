/**
 * The composer under `DictationContext`: the mic button replaces the inert glyph, Send and
 * Enter wait for the capture to finish, Escape cancels dictation before it stops a turn, and a
 * dictated final reaches only the draft. The api is a scripted fake: no socket, no microphone.
 */
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { DictationContext } from "../../voice/dictationContext";
import type {
  CaptureOptions,
  DictationApi,
  VoiceServerMessage,
  VoiceSocketHandlers,
  VoiceStatus,
} from "../../voice/dictationTypes";
import { dictationNotice } from "../../voice/dictationWords";
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

function fakeApi() {
  const log: string[] = [];
  const sockets: VoiceSocketHandlers[] = [];
  const captures: CaptureOptions[] = [];
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
    startCapture: vi.fn(async (options: CaptureOptions) => {
      captures.push(options);
      return { deviceId: options.deviceId, stop: vi.fn(async () => {}) };
    }),
    microphones: vi.fn(async () => []),
    workletUrl: "/worklet.js",
  } satisfies DictationApi;
  const id = () => log.find((line) => line.startsWith("begin "))?.slice(6) ?? "";
  const send = (message: VoiceServerMessage) => act(() => sockets.at(-1)?.message(message));
  return {
    api,
    log,
    id,
    capturing: () =>
      send({
        type: "capturing",
        capture_id: id(),
        model: "small-q8_0",
        backend: "cpu",
        device: null,
        language: "auto",
        language_unavailable: false,
        fallback: null,
      }),
    final: (text: string) => send({ type: "final", capture_id: id(), segment: 0, text }),
    partial: (text: string) =>
      send({ type: "partial", capture_id: id(), segment: 0, revision: 1, text }),
    done: () => send({ type: "done", capture_id: id() }),
  };
}

async function mount(props: Partial<Parameters<typeof Composer>[0]> = {}, withApi = true) {
  const fake = fakeApi();
  const onSend = vi.fn(async () => true);
  const onStop = vi.fn();
  const { container } = render(
    withApi ? (
      <DictationContext.Provider value={fake.api}>
        <Composer running={false} onSend={onSend} onStop={onStop} {...props} />
      </DictationContext.Provider>
    ) : (
      <Composer running={false} onSend={onSend} onStop={onStop} {...props} />
    ),
  );
  await settled();
  const box = container.querySelector("textarea") as HTMLTextAreaElement;
  const mic = () => container.querySelector(".voice-mic") as HTMLButtonElement;
  const type = (value: string, caret = value.length) => {
    fireEvent.change(box, { target: { value } });
    box.setSelectionRange(caret, caret);
  };
  const record = async () => {
    fireEvent.click(mic(), { detail: 0 });
    await settled();
    await fake.capturing();
  };
  return { ...fake, container, box, mic, onSend, onStop, type, record };
}

const enter = async (box: HTMLElement) =>
  act(async () => {
    fireEvent.keyDown(box, { key: "Enter" });
  });

test("without a provider the inert glyph stays and no mic button draws", async () => {
  const t = await mount({}, false);
  expect(t.container.querySelector("svg.composer-mic")).not.toBeNull();
  expect(t.container.querySelector(".voice-mic")).toBeNull();
  expect(t.container.querySelector(".voice-strip")).toBeNull();
});

test("a ready provider draws the mic button in place of the glyph", async () => {
  const t = await mount();
  expect(t.container.querySelector(".composer-mic")).toBeNull();
  expect(t.mic().classList.contains("voice-mic-ready")).toBe(true);
  expect(t.api.status).toHaveBeenCalledTimes(1);
});

test("a keyboard click starts dictation and the strip shows its partials", async () => {
  const t = await mount();
  await t.record();
  expect(t.mic().getAttribute("aria-pressed")).toBe("true");
  await t.partial("hello");
  expect(t.container.querySelector(".voice-provisional")?.textContent).toBe("hello");
  expect(t.box.value).toBe("");
});

test("Enter while recording sends nothing", async () => {
  const t = await mount();
  t.type("typed");
  await t.record();
  await enter(t.box);
  expect(t.onSend).not.toHaveBeenCalled();
  expect(t.container.querySelector(".composer-blocked")?.textContent).toBe(
    dictationNotice("finish_first"),
  );
  expect(t.container.querySelector<HTMLButtonElement>(".composer-submit")?.disabled).toBe(true);
});

test("a final lands at the caret, not at the end", async () => {
  const t = await mount();
  t.type("hello world", 5);
  await t.record();
  await t.final("there");
  expect(t.box.value).toBe("hello there world");
});

test("Escape while recording cancels dictation and does not stop the turn", async () => {
  const t = await mount({ running: true });
  await t.record();
  fireEvent.keyDown(t.box, { key: "Escape" });
  expect(t.log.at(-1)).toBe(`cancel ${t.id()}`);
  expect(t.onStop).not.toHaveBeenCalled();
  // Cancelling is no longer dictating: the next Escape reaches the running turn.
  fireEvent.keyDown(t.box, { key: "Escape" });
  expect(t.onStop).toHaveBeenCalledTimes(1);
});

test("after done, Enter sends the dictated text once", async () => {
  const t = await mount();
  t.type("note:");
  await t.record();
  await t.final("buy milk");
  await t.done();
  await enter(t.box);
  expect(t.onSend).toHaveBeenCalledTimes(1);
  expect(t.onSend).toHaveBeenCalledWith("note: buy milk");
});

test("a final after cancel leaves the box alone", async () => {
  const t = await mount();
  t.type("kept");
  await t.record();
  fireEvent.keyDown(t.box, { key: "Escape" });
  await t.final("late words");
  expect(t.box.value).toBe("kept");
});

test("a final during an IME composition waits for compositionend", async () => {
  const t = await mount();
  t.type("abc");
  await t.record();
  fireEvent.compositionStart(t.box);
  await t.final("def");
  expect(t.box.value).toBe("abc");
  await act(async () => {
    fireEvent.compositionEnd(t.box);
  });
  expect(t.box.value).toBe("abc def");
});

test("a dictated final is an edit: a send still in flight does not clear it", async () => {
  let finish: (handedOff: boolean) => void = () => {};
  const t = await mount({
    onSend: () =>
      new Promise<boolean>((resolve) => {
        finish = resolve;
      }),
  });
  t.type("first");
  await enter(t.box);
  await t.record();
  await t.final("second");
  await act(async () => {
    finish(true);
  });
  expect(t.box.value).toBe("first second");
});
