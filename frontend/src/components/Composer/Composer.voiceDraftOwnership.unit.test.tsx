import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { DictationContext } from "../../voice/dictationContext";
import type {
  CaptureOptions,
  DictationApi,
  VoiceServerMessage,
  VoiceSocketHandlers,
  VoiceStatus,
} from "../../voice/dictationTypes";
import { withSpokenAnswerRequest } from "../../voice/spokenAnswer";
import { Composer } from "./Composer";

afterEach(cleanup);

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

async function setup() {
  let handlers: VoiceSocketHandlers | undefined;
  let captureId = "";
  const onSend = vi.fn(async (_text: string) => true);
  const dispatch = vi.fn();
  const api: DictationApi = {
    status: async () => READY,
    clearFallback: async () => READY,
    socket: (next) => {
      handlers = next;
      return {
        begin: (id) => {
          captureId = id;
        },
        audio: () => {},
        end: () => {},
        cancel: () => {},
        close: () => {},
      };
    },
    startCapture: async (options: CaptureOptions) => ({
      deviceId: options.deviceId,
      stop: async () => {},
    }),
    microphones: async () => [],
    workletUrl: "/worklet.js",
  };
  function Window() {
    const [text, setText] = useState("");
    return (
      <DictationContext.Provider value={api}>
        <button type="button" onClick={() => setText((current) => `${current}\n\n> Quoted reply`)}>
          Quote into draft
        </button>
        <button type="button" onClick={() => setText("replacement draft")}>
          Replace draft
        </button>
        <button type="button" onClick={() => setText("buy milk")}>
          Restore heard words
        </button>
        <Composer
          editorKey="chat-a"
          value={text}
          onEdit={setText}
          running={false}
          onSend={onSend}
          onStop={vi.fn()}
          handsFree={{ active: true, listening: true, dispatch, message: withSpokenAnswerRequest }}
        />
      </DictationContext.Provider>
    );
  }
  render(<Window />);
  await settled();
  const message = (value: VoiceServerMessage) => act(() => handlers?.message(value));
  message({
    type: "capturing",
    capture_id: captureId,
    model: "small-q8_0",
    backend: "cpu",
    device: null,
    language: "auto",
    language_unavailable: false,
    fallback: null,
  });
  message({ type: "final", capture_id: captureId, segment: 0, text: "buy milk" });
  await settled();
  return {
    onSend,
    dispatch,
    box: screen.getByRole("textbox") as HTMLTextAreaElement,
    done: async () => {
      message({ type: "done", capture_id: captureId });
      await settled();
    },
  };
}

test.each(["quote", "replace", "restore", "typed"] as const)(
  "a controlled %s after the final keeps the displayed draft for explicit Send",
  async (edit) => {
    const t = await setup();
    if (edit === "quote" || edit === "restore") {
      fireEvent.click(screen.getByRole("button", { name: "Quote into draft" }));
      if (edit === "restore")
        fireEvent.click(screen.getByRole("button", { name: "Restore heard words" }));
    } else if (edit === "replace") {
      fireEvent.click(screen.getByRole("button", { name: "Replace draft" }));
    } else {
      fireEvent.change(t.box, { target: { value: "buy milk and eggs" } });
    }
    const draft = t.box.value;
    await t.done();
    expect(t.onSend).not.toHaveBeenCalled();
    expect(t.dispatch).toHaveBeenCalledWith({ type: "sendRefused" });
    expect(t.box.value).toBe(draft);
    fireEvent.keyDown(t.box, { key: "Enter" });
    await settled();
    expect(t.onSend).toHaveBeenCalledExactlyOnceWith(draft.trimEnd());
  },
);

test("the controlled editor's unchanged final still sends exactly once", async () => {
  const t = await setup();
  expect(t.box.value).toBe("buy milk");
  await t.done();
  expect(t.onSend).toHaveBeenCalledExactlyOnceWith(withSpokenAnswerRequest("buy milk"));
});
