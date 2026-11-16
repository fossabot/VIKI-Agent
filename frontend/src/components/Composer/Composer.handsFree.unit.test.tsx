/**
 * Frozen acceptance tests for the Composer's hands-free bridge: the loop's `listening` edge
 * starts a toggled capture, and on `done` the Composer sends through its own Send path exactly
 * once, only when the draft is the dictated final, unedited, with Send open and no IME
 * composition. Anything else is reported as `sendRefused` and the draft stays. The api is a
 * scripted fake: no socket, no microphone.
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
import type { HandsFreeBridge, HandsFreeComposerEvent } from "../../voice/handsFree";
import { Composer, type ComposerProps } from "./Composer";

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
  // The capture id of the latest `begin`.
  const id = () =>
    log
      .filter((line) => line.startsWith("begin "))
      .at(-1)
      ?.slice(6) ?? "";
  const send = (message: VoiceServerMessage) => act(() => sockets.at(-1)?.message(message));
  return {
    api,
    log,
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
    done: () => send({ type: "done", capture_id: id() }),
  };
}

interface MountOptions {
  listening?: boolean;
  props?: Partial<ComposerProps>;
}

async function mount({ listening = true, props = {} }: MountOptions = {}) {
  const fake = fakeApi();
  const onSend = vi.fn(async (_text: string) => true);
  const onStop = vi.fn();
  const dispatch = vi.fn((_event: HandsFreeComposerEvent) => undefined);
  const view = (bridge: HandsFreeBridge, extra: Partial<ComposerProps> = {}) => (
    <DictationContext.Provider value={fake.api}>
      <Composer
        running={false}
        onSend={onSend}
        onStop={onStop}
        {...props}
        {...extra}
        handsFree={bridge}
      />
    </DictationContext.Provider>
  );
  const { container, rerender } = render(view({ active: true, listening, dispatch }));
  await settled();
  const box = container.querySelector("textarea") as HTMLTextAreaElement;
  const setBridge = async (bridge: Partial<HandsFreeBridge>, extra?: Partial<ComposerProps>) => {
    rerender(view({ active: true, listening, dispatch, ...bridge }, extra));
    await settled();
  };
  const type = (value: string) => {
    fireEvent.change(box, { target: { value } });
    box.setSelectionRange(value.length, value.length);
  };
  const events = () => dispatch.mock.calls.map(([event]) => event.type);
  return { ...fake, container, box, onSend, onStop, dispatch, setBridge, type, events };
}

test("listening starts one toggled capture on its own", async () => {
  const t = await mount();
  expect(t.api.socket).toHaveBeenCalledTimes(1);
  expect(t.log.filter((line) => line.startsWith("begin "))).toHaveLength(1);
  await t.capturing();
  expect(t.container.querySelector(".voice-mic")?.getAttribute("aria-pressed")).toBe("true");
});

test("no capture starts while the loop is not listening; the next rising edge starts one", async () => {
  const t = await mount({ listening: false });
  expect(t.api.socket).not.toHaveBeenCalled();
  await t.setBridge({ listening: true });
  expect(t.api.socket).toHaveBeenCalledTimes(1);
});

test("the dictated final sends itself once on done, through Send", async () => {
  const t = await mount();
  await t.capturing();
  await t.final("buy milk");
  await t.done();
  await settled();
  expect(t.onSend).toHaveBeenCalledTimes(1);
  expect(t.onSend).toHaveBeenCalledWith("buy milk");
  expect(t.events()).toEqual(["captureDone", "sendAccepted"]);
  // The handed-off draft clears as a typed send's does.
  expect(t.box.value).toBe("");
  await settled();
  expect(t.onSend).toHaveBeenCalledTimes(1);
  // A listening flag that never fell does not start another capture.
  expect(t.api.socket).toHaveBeenCalledTimes(1);
});

test("an edit after the final is not sent; the draft stays", async () => {
  const t = await mount();
  await t.capturing();
  await t.final("buy milk");
  t.type("buy milk and eggs");
  await t.done();
  await settled();
  expect(t.onSend).not.toHaveBeenCalled();
  expect(t.dispatch).toHaveBeenLastCalledWith({ type: "sendRefused" });
  expect(t.events()).not.toContain("sendAccepted");
  expect(t.box.value).toBe("buy milk and eggs");
});

test("a final held by an IME composition is not sent, even after compositionend", async () => {
  const t = await mount();
  await t.capturing();
  fireEvent.compositionStart(t.box);
  await t.final("buy milk");
  await t.done();
  await settled();
  expect(t.onSend).not.toHaveBeenCalled();
  await act(async () => {
    fireEvent.compositionEnd(t.box);
  });
  await settled();
  expect(t.onSend).not.toHaveBeenCalled();
  expect(t.dispatch).toHaveBeenLastCalledWith({ type: "sendRefused" });
  expect(t.box.value).toBe("buy milk");
});

test("a blocked Send is not bypassed; the draft stays", async () => {
  const t = await mount({ props: { submitDisabled: true, blockedReason: "Draft conflict." } });
  await t.capturing();
  await t.final("buy milk");
  await t.done();
  await settled();
  expect(t.onSend).not.toHaveBeenCalled();
  expect(t.dispatch).toHaveBeenLastCalledWith({ type: "sendRefused" });
  expect(t.events()).not.toContain("sendAccepted");
  expect(t.box.value).toBe("buy milk");
});

test("a refused handoff is reported as refused and keeps the draft", async () => {
  const t = await mount({ props: { onSend: vi.fn(async () => false) } });
  await t.capturing();
  await t.final("buy milk");
  await t.done();
  await settled();
  expect(t.dispatch).toHaveBeenLastCalledWith({ type: "sendRefused" });
  expect(t.box.value).toBe("buy milk");
});

test("a capture that ends with no final sends nothing and reports it empty", async () => {
  const t = await mount();
  await t.capturing();
  await t.done();
  await settled();
  expect(t.onSend).not.toHaveBeenCalled();
  expect(t.events()).toEqual(["captureEmpty"]);
});

test("Escape leaves hands-free: the capture is cancelled and the turn is not stopped", async () => {
  const t = await mount({ props: { running: true } });
  await t.capturing();
  const id = t.log.find((line) => line.startsWith("begin "))?.slice(6);
  fireEvent.keyDown(t.box, { key: "Escape" });
  expect(t.log).toContain(`cancel ${id}`);
  expect(t.dispatch).toHaveBeenCalledWith({ type: "exit", reason: "esc" });
  expect(t.onStop).not.toHaveBeenCalled();
  expect(t.events()).not.toContain("captureEmpty");
});

test("the first Escape during a running turn leaves hands-free and does not stop the turn", async () => {
  const t = await mount({ listening: false, props: { running: true } });
  fireEvent.keyDown(t.box, { key: "Escape" });
  expect(t.dispatch).toHaveBeenCalledWith({ type: "exit", reason: "esc" });
  expect(t.onStop).not.toHaveBeenCalled();
  // Once the mode is off, Escape keeps its usual meaning.
  await t.setBridge({ active: false });
  fireEvent.keyDown(t.box, { key: "Escape" });
  expect(t.onStop).toHaveBeenCalledTimes(1);
});

test("with the mode off, a final stays in the draft and nothing sends", async () => {
  const t = await mount({ listening: false });
  await t.setBridge({ active: false });
  fireEvent.click(t.container.querySelector(".voice-mic") as HTMLButtonElement, { detail: 0 });
  await settled();
  await t.capturing();
  await t.final("buy milk");
  await t.done();
  await settled();
  expect(t.onSend).not.toHaveBeenCalled();
  expect(t.dispatch).not.toHaveBeenCalled();
  expect(t.box.value).toBe("buy milk");
});
