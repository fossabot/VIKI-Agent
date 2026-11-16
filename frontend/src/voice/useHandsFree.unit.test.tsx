/**
 * `useHandsFree`, the App-level half of the loop, driven against the real
 * read-aloud controller over a fake speechSynthesis (with a Voicebox voice, also a fake Voicebox api
 * and `AudioOut`): what it speaks and when, what it never speaks, and every way it ends. The
 * Composer's side is played through `bridge.dispatch`.
 */
import { act, cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { type ReactElement, type ReactNode, useLayoutEffect } from "react";
import { afterEach, describe, expect, test, vi } from "vitest";
import {
  type MarkdownParser,
  SETTLED_DEADLINE_MS,
} from "../components/Conversation/markdownParser";
import { type ParseResult, parseMarkdown } from "../components/Conversation/markdownTree";
import {
  ReadAloudContext,
  type ReadAloudSettings,
  useReadAloudReply,
} from "../components/Conversation/ReadAloud";
import { t as tr } from "../i18n/t";
import { type Card, emptyView, type SessionView, type Turn } from "../state/view";
import type { DictationApi, VoiceStatus } from "./dictationTypes";
import {
  HANDS_FREE_OFF,
  type HandsFreeExit,
  type HandsFreeState,
  handsFreeReducer,
  REARM_DELAY_MS,
} from "./handsFree";
import { type AudioOut, createReadAloud } from "./readAloud";
import { withSpokenAnswerRequest } from "./spokenAnswer";
import { KEEP_WARM_MS, type UseHandsFreeOptions, useHandsFree } from "./useHandsFree";
import {
  type VoiceboxApi,
  VoiceboxError,
  type VoiceboxSettings,
  type VoiceboxStatus,
} from "./voiceboxApi";
import {
  type PackState,
  type VoicePackApi,
  VoicePackContext,
  type VoicePackServices,
  type VoicePackStatus,
} from "./voicePackApi";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

const ZIRA = { name: "Zira", voiceURI: "Zira", lang: "en-US", localService: true, default: false };
const TEXT = "The weather is fine today and you are here.";
const CARD = { kind: "permission" } as unknown as Card;
const READY = { status: async () => ({ state: "ready" }) } as unknown as DictationApi;

const settled = () =>
  act(async () => {
    for (let i = 0; i < 10; i++) await Promise.resolve();
  });

function speech(voices = [ZIRA], audio: AudioOut | null = null) {
  const spoken: string[] = [];
  const synth = {
    paused: false,
    getVoices: () => voices,
    addEventListener: () => {},
    removeEventListener: () => {},
    speak: (u: { text: string }) => spoken.push(u.text),
    cancel: () => {},
    pause: () => {},
    resume: () => {},
  };
  const controller = createReadAloud({
    synth: synth as unknown as SpeechSynthesis,
    makeUtterance: (text) =>
      ({ text, voice: null, lang: "", rate: 1, onend: null, onerror: null }) as never,
    audio,
    target: new EventTarget(),
    devices: null,
    // A microtask, not 0 real work: `loadVoices`'s own fallback timer reads `timer` (its own
    // return value) inside the callback, so firing before `setTimeout` itself returns would throw.
    // With no "voiceschanged" ever coming (this fake never fires one), this is what lets
    // `loadVoices` settle promptly on a genuinely empty voice list, instead of hanging for
    // `settled()`'s microtask-only loop.
    setTimeout: (cb) => {
      Promise.resolve().then(cb);
      return 0;
    },
    clearTimeout: () => {},
  });
  return { controller, spoken };
}

function turn(id: string, status: Turn["status"], text = TEXT): Turn {
  return {
    id,
    user: null,
    blocks: [{ kind: "text", id: `${id}-b`, text, done: status !== "running" }],
    status,
    usage: null,
    durationMs: 0,
    history: false,
  };
}

function chat(turns: Turn[], cards: Card[] = []): SessionView {
  return { ...emptyView("s1"), turns, cards };
}

const T1 = turn("t1", "success");

function setup(
  over: Partial<UseHandsFreeOptions> = {},
  noticeSeen = true,
  voice = speech(),
  voicebox: VoiceboxSettings | null = null,
  pack: VoicePackServices | null = null,
) {
  const readAloud: ReadAloudSettings = {
    controller: voice.controller,
    rate: 1,
    voices: {},
    noticeSeen,
    voicebox,
    acknowledge: () => {},
  };
  let props: UseHandsFreeOptions = {
    dictationApi: READY,
    view: chat([T1]),
    readAloud,
    sessionKey: "s1",
    parser: null,
    chatShown: true,
    ...over,
  };
  const hook = renderHook((options: UseHandsFreeOptions) => useHandsFree(options), {
    initialProps: props,
    wrapper: ({ children }: { children: ReactNode }) => (
      <VoicePackContext.Provider value={pack}>{children}</VoicePackContext.Provider>
    ),
  });
  /** Re-renders with no settling, so an await already under way is still pending after it. */
  const updateNow = (next: Partial<UseHandsFreeOptions>) => {
    props = { ...props, ...next };
    hook.rerender(props);
  };
  const update = async (next: Partial<UseHandsFreeOptions>) => {
    updateNow(next);
    await settled();
  };
  const phase = () => hook.result.current.state.phase;
  const send = (type: "captureDone" | "sendAccepted") =>
    act(() => hook.result.current.bridge.dispatch({ type }));
  const enter = async () => {
    act(() => hook.result.current.toggle());
    await settled();
  };
  /** listening → sending → thinking, as the Composer reports a heard and accepted message. */
  const think = async () => {
    await enter();
    send("captureDone");
    send("sendAccepted");
  };
  return { ...voice, readAloud, hook, updateNow, update, phase, send, enter, think };
}

test.each([{ sessionKey: "s2" }, { chatShown: false }])(
  "a retained bridge's first layout drops prior capture authority for %j",
  async (change) => {
    const { controller } = speech();
    const options: UseHandsFreeOptions = {
      dictationApi: READY,
      view: chat([T1]),
      readAloud: { controller, rate: 1, voices: {}, noticeSeen: true, acknowledge: () => {} },
      sessionKey: "s1",
      parser: null,
      chatShown: true,
    };
    const observed: { key: string | null; active: boolean; listening: boolean }[] = [];
    const hook = renderHook(
      (current: UseHandsFreeOptions) => {
        const handsFree = useHandsFree(current);
        useLayoutEffect(() => {
          observed.push({
            key: current.sessionKey,
            active: handsFree.bridge.active,
            listening: handsFree.bridge.listening,
          });
        });
        return handsFree;
      },
      { initialProps: options },
    );
    act(() => hook.result.current.toggle());
    await settled();
    expect(hook.result.current.bridge.listening).toBe(true);
    observed.length = 0;
    hook.rerender({ ...options, ...change });
    expect(observed[0]).toEqual({
      key: change.sessionKey ?? options.sessionKey,
      active: false,
      listening: false,
    });
    hook.rerender({ ...options, ...change, chatShown: true });
    act(() => hook.result.current.toggle());
    await settled();
    expect(hook.result.current.bridge.listening).toBe(true);
  },
);

test.each(["ready", "error"] as const)(
  "retained voice entry ignores stale %s readiness across owner changes and cancellation",
  async (outcome) => {
    for (const way of [
      "session",
      "round trip",
      "view",
      "hidden",
      "pagehide",
      "exit",
      "cancel",
    ] as const) {
      let finish: (status: VoiceStatus) => void = () => {};
      let fail: (error: Error) => void = () => {};
      const status = vi
        .fn()
        .mockImplementationOnce(
          () =>
            new Promise<VoiceStatus>((resolve, reject) => {
              finish = resolve;
              fail = reject;
            }),
        )
        .mockImplementation(READY.status);
      const t = setup({ dictationApi: { ...READY, status } });
      act(() => t.hook.result.current.toggle());
      if (way === "session" || way === "round trip") {
        t.updateNow({ sessionKey: "s2" });
        if (way === "round trip") t.updateNow({ sessionKey: "s1" });
      } else if (way === "view") {
        t.updateNow({ chatShown: false });
        t.updateNow({ chatShown: true });
      } else if (way === "hidden") {
        const visible = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
        act(() => document.dispatchEvent(new Event("visibilitychange")));
        visible.mockRestore();
      } else if (way === "pagehide") {
        act(() => window.dispatchEvent(new Event("pagehide")));
      } else if (way === "exit") {
        act(() => t.hook.result.current.bridge.dispatch({ type: "exit", reason: "esc" }));
      } else {
        act(() => t.hook.result.current.cancelNotice());
      }
      await act(async () => {
        if (outcome === "ready") finish(await READY.status());
        else fail(new Error("stale readiness"));
      });
      await settled();
      expect(t.hook.result.current.state, way).toEqual(HANDS_FREE_OFF);
      await t.enter();
      expect(t.phase(), way).toBe("listening");
      t.hook.unmount();
    }
  },
);

test.each([
  ["ready", false],
  ["error", false],
  ["ready", true],
  ["error", true],
] as const)(
  "retained voice entry's stale %s completion cannot overtake a newer entry (owner round trip: %s)",
  async (outcome, roundTrip) => {
    let finishOld: (status: VoiceStatus) => void = () => {};
    let failOld: (error: Error) => void = () => {};
    let finishNew: (status: VoiceStatus) => void = () => {};
    const status = vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise<VoiceStatus>((resolve, reject) => {
            finishOld = resolve;
            failOld = reject;
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise<VoiceStatus>((resolve) => {
            finishNew = resolve;
          }),
      );
    const t = setup({ dictationApi: { ...READY, status } });
    act(() => t.hook.result.current.toggle());
    if (roundTrip) {
      t.updateNow({ sessionKey: "s2" });
      t.updateNow({ sessionKey: "s1" });
    }
    act(() => t.hook.result.current.toggle());
    await act(async () => {
      if (outcome === "ready") finishOld(await READY.status());
      else failOld(new Error("stale readiness"));
    });
    await settled();
    expect(t.hook.result.current.state).toEqual(HANDS_FREE_OFF);
    await act(async () => finishNew(await READY.status()));
    await settled();
    expect(t.hook.result.current.bridge.listening).toBe(true);
  },
);

test("a delayed local voice cannot re-enter across an owner round trip, but fresh entry works", async () => {
  const voice = speech();
  let finish: (voices: SpeechSynthesisVoice[]) => void = () => {};
  vi.spyOn(voice.controller, "loadVoices").mockImplementationOnce(
    () =>
      new Promise<SpeechSynthesisVoice[]>((resolve) => {
        finish = resolve;
      }),
  );
  const t = setup({}, true, voice);
  act(() => t.hook.result.current.toggle());
  await settled();
  t.updateNow({ sessionKey: "s2" });
  t.updateNow({ sessionKey: "s1" });
  await act(async () => finish([ZIRA as SpeechSynthesisVoice]));
  expect(t.hook.result.current.state).toEqual(HANDS_FREE_OFF);
  await t.enter();
  expect(t.phase()).toBe("listening");
});

test("unmount invalidates pending entry before it can request a local voice", async () => {
  let finish: (status: VoiceStatus) => void = () => {};
  const t = setup({
    dictationApi: {
      ...READY,
      status: () =>
        new Promise<VoiceStatus>((resolve) => {
          finish = resolve;
        }),
    },
  });
  const load = vi.spyOn(t.controller, "loadVoices");
  act(() => t.hook.result.current.toggle());
  t.hook.unmount();
  await act(async () => finish(await READY.status()));
  await settled();
  expect(load).not.toHaveBeenCalled();
});

/** The loop speaking t2's reply. */
async function speaking(over: Partial<UseHandsFreeOptions> = {}) {
  const t = setup(over);
  await t.think();
  await t.update({ view: chat([T1, turn("t2", "running")]) });
  await t.update({ view: chat([T1, turn("t2", "success")]) });
  return t;
}

describe("what is spoken", () => {
  test("with the mode off nothing is: not a finished turn at launch, not a new one, and the user's own Read plays on", async () => {
    const t = setup({}, false);
    await settled();
    const ticket = t.controller.begin("reply");
    t.controller.play(ticket, "reply", ["Mine."], ZIRA as never, 1);
    await t.update({ view: chat([T1, turn("t2", "running")]) });
    await t.update({ view: chat([T1, turn("t2", "success")]) });
    expect(t.spoken).toEqual(["Mine."]);
    expect(t.controller.statusOf("reply")).toBe("speaking");
    expect(t.phase()).toBe("off");
  });

  test("a turn that finishes while thinking is spoken in its own name", async () => {
    const t = await speaking();
    expect(t.phase()).toBe("speaking");
    expect(t.spoken).toEqual([TEXT]);
    expect(t.controller.statusOf("handsfree:t2")).toBe("speaking");
  });

  test("only the reply's spoken answer is spoken: its first paragraph, at most three sentences", async () => {
    const t = setup();
    await t.think();
    const answer = `${TEXT} It is warm. Take a hat.`;
    const reply = `## Weather\n\n${answer} See [the forecast](https://example.com) 😀.\n\nMore.`;
    await t.update({ view: chat([T1, turn("t2", "running", reply)]) });
    await t.update({ view: chat([T1, turn("t2", "success", reply)]) });
    expect(t.phase()).toBe("speaking");
    expect(t.spoken).toEqual([answer]);
  });

  test("the spoken answer is the final reply's, after its last tool call", async () => {
    const t = setup();
    await t.think();
    const tools = { kind: "tools", id: "t2-tools", rows: [] } as const;
    const text = (id: string, value: string) => ({ kind: "text", id, text: value, done: true });
    const answered: Turn = {
      ...turn("t2", "success"),
      blocks: [text("t2-a", "Let me check the vault."), tools, text("t2-b", TEXT)],
    } as Turn;
    await t.update({ view: chat([T1, turn("t2", "running")]) });
    await t.update({ view: chat([T1, answered]) });
    expect(t.spoken).toEqual([TEXT]);
  });

  test("the loop's automatic Send asks for a spoken answer", () => {
    const t = setup();
    expect(t.hook.result.current.bridge.message?.("buy milk")).toBe(
      withSpokenAnswerRequest("buy milk"),
    );
  });

  test("a reply still being prepared when the mode ends is never spoken", async () => {
    const t = setup();
    await t.think();
    await t.update({ view: chat([T1, turn("t2", "running")]) });
    t.updateNow({ view: chat([T1, turn("t2", "success")]) });
    act(() => {
      window.dispatchEvent(new Event("pagehide"));
    });
    await settled();
    expect(t.phase()).toBe("off");
    expect(t.spoken).toEqual([]);
  });

  test("a reply is parsed by the window's worker with the settled deadline; an unmount cancels it", async () => {
    const requests: { owner: object; deadline: number }[] = [];
    const parser: MarkdownParser = {
      cached: () => undefined,
      request: (owner, _text, deadline) => requests.push({ owner, deadline }),
      cancel: vi.fn(),
    };
    const t = await speaking({ parser });
    expect(requests).toHaveLength(1);
    expect(requests[0]?.deadline).toBe(SETTLED_DEADLINE_MS);
    expect(t.spoken).toEqual([]);
    t.hook.unmount();
    expect(parser.cancel).toHaveBeenCalledWith(requests[0]?.owner);
  });

  test("a turn that finishes before its Send resolves is still spoken; the late acceptance changes nothing", async () => {
    const t = setup();
    await t.enter();
    t.send("captureDone");
    expect(t.phase()).toBe("sending");
    await t.update({ view: chat([T1, turn("t2", "running")]) });
    await t.update({ view: chat([T1, turn("t2", "success")]) });
    expect(t.phase()).toBe("speaking");
    t.send("sendAccepted");
    expect(t.phase()).toBe("speaking");
    expect(t.spoken).toEqual([TEXT]);
  });
});

describe("a card", () => {
  test("waits and says so once", async () => {
    const t = setup();
    await t.think();
    await t.update({ view: chat([T1, turn("t2", "running")], [CARD]) });
    expect(t.phase()).toBe("waiting");
    await t.update({ view: chat([T1, turn("t2", "running")], [CARD, CARD]) });
    expect(t.spoken).toEqual(["Approval needed on screen"]);
  });

  test("that closes while the voices load is never announced", async () => {
    const t = setup();
    await t.think();
    t.updateNow({ view: chat([T1, turn("t2", "running")], [CARD]) });
    t.updateNow({ view: chat([T1, turn("t2", "running")]) });
    await settled();
    expect(t.phase()).toBe("thinking");
    expect(t.spoken).toEqual([]);
  });

  test("that opens before the Send resolves still waits", async () => {
    const t = setup();
    await t.enter();
    t.send("captureDone");
    await t.update({ view: chat([T1, turn("t2", "running")], [CARD]) });
    expect(t.phase()).toBe("waiting");
  });
});

describe("every way out", () => {
  test.each(["hidden", "pagehide", "session"] as const)(
    "%s ends the mode and stops the reply being spoken",
    async (way) => {
      const t = await speaking();
      if (way === "hidden") {
        // The answer keeps playing while hidden: only once it ends does the mode leave.
        vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
        act(() => {
          document.dispatchEvent(new Event("visibilitychange"));
        });
        expect(t.phase()).toBe("speaking");
        act(() => {
          t.controller.stop();
        });
      } else if (way === "pagehide") {
        act(() => {
          window.dispatchEvent(new Event("pagehide"));
        });
      } else {
        await t.update({ sessionKey: "s2" });
      }
      expect(t.phase()).toBe("off");
      expect(t.controller.statusOf("handsfree:t2")).toBe("idle");
    },
  );

  test("leaving the chat ends the mode quietly and stops the reply being spoken", async () => {
    const t = await speaking();
    await t.update({ chatShown: false });
    expect(t.hook.result.current.state).toEqual(HANDS_FREE_OFF);
    expect(t.controller.statusOf("handsfree:t2")).toBe("idle");
  });

  test.each(["a hidden window", "leaving the chat"] as const)(
    "%s while Enter checks dictation and voices: the mode does not start",
    async (way) => {
      let resolve: (status: { state: string }) => void = () => {};
      const dictationApi = {
        status: () =>
          new Promise((r) => {
            resolve = r;
          }),
      } as unknown as DictationApi;
      const t = setup({ dictationApi });
      act(() => t.hook.result.current.toggle());
      if (way === "a hidden window") {
        vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
        act(() => {
          document.dispatchEvent(new Event("visibilitychange"));
        });
      } else {
        t.updateNow({ chatShown: false });
      }
      await act(async () => resolve({ state: "ready" }));
      await settled();
      expect(t.hook.result.current.state).toEqual(HANDS_FREE_OFF);
    },
  );

  test("with the mode off, a hidden window or a chat switch changes nothing and says nothing", async () => {
    const t = setup();
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await t.update({ sessionKey: "s2" });
    expect(t.hook.result.current.state).toEqual(HANDS_FREE_OFF);
  });

  test("Esc anywhere leaves the mode, and the window's own Esc handler finds it handled", async () => {
    // Registered before the hook's own, as App's window listener is.
    const seen: boolean[] = [];
    const app = (event: KeyboardEvent) => {
      if (event.key === "Escape") seen.push(event.defaultPrevented);
    };
    window.addEventListener("keydown", app);
    try {
      const t = setup();
      await t.enter();
      expect(t.phase()).toBe("listening");
      act(() => {
        fireEvent.keyDown(document.body, { key: "Escape" });
      });
      expect(t.phase()).toBe("off");
      expect(seen).toEqual([true]);
    } finally {
      window.removeEventListener("keydown", app);
    }
  });

  test("an Esc a dialog already handled, an open menu's Esc or an IME's Esc leaves it on", async () => {
    const t = setup();
    await t.enter();
    const dialog = (event: KeyboardEvent) => event.preventDefault();
    window.addEventListener("keydown", dialog, true);
    fireEvent.keyDown(document.body, { key: "Escape" });
    window.removeEventListener("keydown", dialog, true);
    const menu = document.body.appendChild(document.createElement("div"));
    menu.setAttribute("role", "menu");
    fireEvent.keyDown(document.body, { key: "Escape" });
    menu.remove();
    fireEvent.keyDown(document.body, { key: "Escape", isComposing: true });
    expect(t.phase()).toBe("listening");
  });

  test("hidden while thinking still speaks, then ends at re-arm without a capture", async () => {
    const t = setup();
    await t.think();
    expect(t.phase()).toBe("thinking");
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    // Hidden mid-turn changes nothing yet: the loop keeps waiting on the turn.
    expect(t.phase()).toBe("thinking");
    await t.update({ view: chat([T1, turn("t2", "running")]) });
    await t.update({ view: chat([T1, turn("t2", "success")]) });
    expect(t.phase()).toBe("speaking");
    expect(t.spoken).toEqual([TEXT]);
    act(() => {
      t.controller.stop();
    });
    // Re-arming finds the window still hidden and leaves at once, instead of opening the mic.
    expect(t.hook.result.current.state).toEqual({ ...HANDS_FREE_OFF, notice: "window_hidden" });
  });

  test("hidden while listening ends at once", async () => {
    const t = setup();
    await t.enter();
    expect(t.phase()).toBe("listening");
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(t.hook.result.current.state).toEqual({ ...HANDS_FREE_OFF, notice: "window_hidden" });
  });

  test("hidden while rearming ends at once and never re-arms", async () => {
    vi.useFakeTimers();
    const t = setup();
    await t.think();
    await t.update({ view: chat([T1, turn("t2", "running", "")]) });
    await t.update({ view: chat([T1, turn("t2", "success", "")]) });
    expect(t.phase()).toBe("rearming");
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(t.hook.result.current.state).toEqual({ ...HANDS_FREE_OFF, notice: "window_hidden" });
    act(() => {
      vi.advanceTimersByTime(REARM_DELAY_MS);
    });
    expect(t.hook.result.current.state).toEqual({ ...HANDS_FREE_OFF, notice: "window_hidden" });
  });

  test("an exit while sending, thinking, waiting or speaking reports dropped; one while listening reports nothing", async () => {
    const listeningReport = vi.fn();
    const listening = setup({ dictationApi: { ...READY, report: listeningReport } });
    await listening.enter();
    expect(listening.phase()).toBe("listening");
    act(() => {
      fireEvent.keyDown(document.body, { key: "Escape" });
    });
    expect(listening.phase()).toBe("off");
    expect(listeningReport).not.toHaveBeenCalled();

    type Loop = ReturnType<typeof setup>;
    const reach: Record<string, (t: Loop) => Promise<void>> = {
      sending: async (t) => {
        await t.enter();
        t.send("captureDone");
      },
      thinking: (t) => t.think(),
      waiting: async (t) => {
        await t.think();
        await t.update({ view: chat([T1, turn("t2", "running")], [CARD]) });
      },
      speaking: async (t) => {
        await t.think();
        await t.update({ view: chat([T1, turn("t2", "running")]) });
        await t.update({ view: chat([T1, turn("t2", "success")]) });
      },
    };
    for (const [phase, go] of Object.entries(reach)) {
      const report = vi.fn();
      const t = setup({ dictationApi: { ...READY, report } });
      await go(t);
      expect(t.phase()).toBe(phase);
      act(() => {
        fireEvent.keyDown(document.body, { key: "Escape" });
      });
      expect(t.phase()).toBe("off");
      expect(report.mock.calls).toEqual([["dropped", "esc"]]);
    }
  });

  test("a session switch that also takes the chat off screen reports dropped once", async () => {
    const report = vi.fn();
    const t = setup({ dictationApi: { ...READY, report } });
    await t.think();
    await t.update({ sessionKey: "s2", chatShown: false });
    expect(t.hook.result.current.state).toEqual({ ...HANDS_FREE_OFF, notice: "session_switched" });
    expect(report.mock.calls).toEqual([["dropped", "session"]]);
  });
});

describe("the reducer's late events", () => {
  const SENDING: HandsFreeState = { ...HANDS_FREE_OFF, phase: "sending" };

  test("a card or the turn's end that comes before the Send resolves is read as in thinking", () => {
    expect(handsFreeReducer(SENDING, { type: "card", open: true }).phase).toBe("waiting");
    expect(handsFreeReducer(SENDING, { type: "card", open: false })).toBe(SENDING);
    expect(
      handsFreeReducer(SENDING, { type: "turnFinished", turnId: "t2", outcome: "speakable" }),
    ).toMatchObject({ phase: "speaking", turnId: "t2" });
    expect(
      handsFreeReducer(SENDING, { type: "turnFinished", turnId: "t2", outcome: "silent" }).phase,
    ).toBe("rearming");
  });

  test("an exit with the mode already off returns the state as it was", () => {
    const ended: HandsFreeState = { ...HANDS_FREE_OFF, notice: "nothing_heard" };
    for (const reason of ["esc", "toggle", "hidden", "session", "pagehide"] as HandsFreeExit[]) {
      expect(handsFreeReducer(HANDS_FREE_OFF, { type: "exit", reason })).toBe(HANDS_FREE_OFF);
      expect(handsFreeReducer(ended, { type: "exit", reason })).toBe(ended);
    }
  });
});

function Reply({ shown }: { shown: Turn }): ReactElement {
  const ui = useReadAloudReply(shown);
  return <div>{ui.controls}</div>;
}

const replyButton = (key: string) =>
  screen.queryByRole("button", { name: tr(`readaloud.${key}.label` as never) });

test("the reply's own controls show and steer its hands-free playback", async () => {
  const t = await speaking();
  render(
    <ReadAloudContext.Provider value={t.readAloud}>
      <Reply shown={turn("t2", "success")} />
    </ReadAloudContext.Provider>,
  );
  expect(replyButton("read")).toBeNull();
  act(() => replyButton("pause")?.click());
  expect(t.controller.statusOf("handsfree:t2")).toBe("paused");
  act(() => replyButton("resume")?.click());
  expect(t.controller.statusOf("handsfree:t2")).toBe("speaking");
  act(() => replyButton("stop")?.click());
  expect(t.controller.statusOf("handsfree:t2")).toBe("idle");
  expect(replyButton("read")).not.toBeNull();
  expect(t.phase()).toBe("rearming");
});

const EARLIER = "This earlier reply is the one you asked to hear again.";

test.each(["speaking", "rearming"] as const)(
  "another reply's own Read while hands-free is %s ends the mode quietly instead of re-arming over it",
  async (from) => {
    const t = from === "speaking" ? await speaking() : setup();
    if (from === "rearming") {
      await t.think();
      await t.update({ view: chat([T1, turn("t2", "running")]) });
      await t.update({ view: chat([T1, turn("t2", "success", "")]) });
    }
    expect(t.phase()).toBe(from);
    render(
      <ReadAloudContext.Provider value={t.readAloud}>
        <Reply shown={turn("t0", "success", EARLIER)} />
      </ReadAloudContext.Provider>,
    );
    act(() => replyButton("read")?.click());
    // Already, while the Read still prepares: a slow one never races the re-arm.
    expect(t.hook.result.current.state).toEqual(HANDS_FREE_OFF);
    await settled();
    expect(t.hook.result.current.state).toEqual(HANDS_FREE_OFF);
    expect(t.spoken.at(-1)).toBe(EARLIER);
  },
);

test("a Read begun while thinking (mic closed, no subscription yet) is caught the instant a silent turn enters rearming, not left for the mic's own re-open to cut", async () => {
  const t = setup();
  await t.think();
  expect(t.phase()).toBe("thinking");
  // No subscription exists yet (thinking is not a half-duplex phase): begin and play are plain
  // controller calls, same as the mode-off case above.
  const ticket = t.controller.begin("reply");
  t.controller.play(ticket, "reply", [EARLIER], ZIRA as never, 1);
  // Nothing to speak: the turn ends the loop in rearming, where the subscription first mounts.
  await t.update({ view: chat([T1, turn("t2", "running", "")]) });
  await t.update({ view: chat([T1, turn("t2", "success", "")]) });
  expect(t.hook.result.current.state).toEqual(HANDS_FREE_OFF);
  expect(t.controller.statusOf("reply")).toBe("speaking");
});

/** A Voicebox api whose answers each test hands out by hand, over a fake `AudioOut`: each answer's
 * bytes carry its text, so `played` and `cut` name what sounded and what a stop cut short.
 * `statusState` is the app's own status (down for an engine-down scenario); `engine`/`languages`
 * mirror the two fields `VoiceboxSettings` now carries. */
function voicebox(
  speakWhileWriting: boolean,
  engine?: VoiceboxSettings["engine"],
  languages?: readonly string[],
  statusState: VoiceboxStatus["state"] = "ready",
) {
  const played: string[] = [];
  const cut: string[] = [];
  const ends: (() => void)[] = [];
  const textOf = new Map<ArrayBuffer, string>();
  const audio: AudioOut = {
    decode: async (bytes) => ({ text: textOf.get(bytes) }) as unknown as AudioBuffer,
    start(buffer, onEnded) {
      const { text } = buffer as unknown as { text: string };
      played.push(text);
      let done = false;
      const end = () => {
        if (done) return;
        done = true;
        onEnded();
      };
      ends.push(end);
      return () => {
        if (done) return;
        cut.push(text);
        end();
      };
    },
    resume: async () => {},
    suspend: async () => {},
  };
  const requests: {
    text: string;
    language: string;
    signal: AbortSignal;
    answer(): void;
    fail(error: unknown): void;
  }[] = [];
  // Like the real client, a request rejects with an AbortError once its signal aborts.
  const api: VoiceboxApi = {
    status: async () => ({ state: statusState, gpu: null, profiles: [] }),
    speech: (text, language, signal) =>
      new Promise<ArrayBuffer>((resolve, reject) => {
        const bytes = new ArrayBuffer(1);
        textOf.set(bytes, text);
        requests.push({ text, language, signal, answer: () => resolve(bytes), fail: reject });
        signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), {
          once: true,
        });
      }),
  };
  const settings: VoiceboxSettings = {
    api,
    url: "http://voicebox.invalid",
    speakWhileWriting,
    ...(engine !== undefined ? { engine } : {}),
    ...(languages !== undefined ? { languages } : {}),
  };
  return { audio, settings, played, cut, requests, ends };
}

/** A `VoicePackContext` whose status each test sets up front and can still mutate (`status.state`)
 * to try a later Enter against a recovered (or newly failed) pack. */
function fakePack(state: PackState) {
  const acted: string[] = [];
  const status: VoicePackStatus = {
    state,
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
  };
  const api: VoicePackApi = {
    status: async () => ({ ...status }),
    act: async (action) => {
      acted.push(action);
      return { ...status };
    },
    openSpeechSettings: async () => {},
  };
  const services: VoicePackServices = { api, openUrl: () => {} };
  return { services, acted, status };
}

/** The loop with a Voicebox voice chosen, over `voices` as the installed system voices. `engine`
 * defaults to the own Voicebox app (`VoiceboxSettings.engine`'s own default); `pack` wires a
 * `VoicePackContext` for the `engine: "pack"` tests. */
function withVoicebox({
  voices = [ZIRA],
  speakWhileWriting = false,
  parser = null as MarkdownParser | null,
  engine = undefined as VoiceboxSettings["engine"],
  languages = undefined as readonly string[] | undefined,
  status = "ready" as VoiceboxStatus["state"],
  pack = null as VoicePackServices | null,
} = {}) {
  const box = voicebox(speakWhileWriting, engine, languages, status);
  const t = setup({ parser }, true, speech(voices, box.audio), box.settings, pack);
  return {
    ...t,
    ...box,
    asked: () => box.requests.map((request) => request.text),
    /** Answers request `index` with its text's bytes. */
    async answer(index: number) {
      box.requests[index]?.answer();
      await settled();
    },
    async fail(index: number, code: string) {
      box.requests[index]?.fail(new VoiceboxError(code));
      await settled();
    },
    /** Ends the chunk now sounding. */
    async end() {
      box.ends.at(-1)?.();
      await settled();
    },
  };
}

const JAPANESE = "今日は天気がとても良いので、散歩に行きます。";
const A = "The first sentence is ready now.";
const B = "The second one follows right after.";

describe("with a Voicebox voice", () => {
  test("speaks the answer through Voicebox, then re-arms once its audio ends", async () => {
    const t = withVoicebox();
    await t.think();
    await t.update({ view: chat([T1, turn("t2", "running")]) });
    await t.update({ view: chat([T1, turn("t2", "success")]) });
    expect(t.requests.map(({ text, language }) => [text, language])).toEqual([[TEXT, "en"]]);
    expect(t.phase()).toBe("thinking");
    await t.answer(0);
    expect(t.phase()).toBe("speaking");
    expect(t.played).toEqual([TEXT]);
    expect(t.spoken).toEqual([]);
    expect(t.controller.statusOf("handsfree:t2")).toBe("speaking");
    await t.end();
    expect(t.phase()).toBe("rearming");
    expect(t.cut).toEqual([]);
  });

  test("an unreachable Voicebox falls back to the English system voice", async () => {
    const t = withVoicebox();
    await t.think();
    await t.update({ view: chat([T1, turn("t2", "running")]) });
    await t.update({ view: chat([T1, turn("t2", "success")]) });
    expect(t.asked()).toEqual([TEXT]);
    expect(t.spoken).toEqual([]);
    await t.fail(0, "voicebox_unreachable");
    expect(t.phase()).toBe("speaking");
    expect(t.spoken).toEqual([TEXT]);
    expect(t.played).toEqual([]);
    expect(t.controller.statusOf("handsfree:t2")).toBe("speaking");
  });

  test("an unreachable Voicebox with no system voice for a Japanese answer ends the mode: speech_failed", async () => {
    const t = withVoicebox();
    await t.think();
    await t.update({ view: chat([T1, turn("t2", "running", JAPANESE)]) });
    await t.update({ view: chat([T1, turn("t2", "success", JAPANESE)]) });
    expect(t.requests.map(({ language }) => language)).toEqual(["ja"]);
    await t.fail(0, "voicebox_unreachable");
    expect(t.hook.result.current.state).toEqual({ ...HANDS_FREE_OFF, notice: "speech_failed" });
    expect(t.spoken).toEqual([]);
    expect(t.controller.owner()).toBeNull();
  });

  test("Enter with no system voice installed starts listening", async () => {
    const t = withVoicebox({ voices: [] });
    await t.enter();
    expect(t.phase()).toBe("listening");
  });

  test("early speech already speaking the reply owns it: nothing is requested a second time", async () => {
    const t = withVoicebox({ speakWhileWriting: true });
    await t.think();
    await t.update({ view: chat([T1, turn("t2", "running", `${A} The sec`)]) });
    expect(t.asked()).toEqual([A]);
    await t.answer(0);
    expect(t.phase()).toBe("thinking");
    await t.update({ view: chat([T1, turn("t2", "success", `${A} ${B}`)]) });
    expect(t.phase()).toBe("speaking");
    expect(t.asked()).toEqual([A, B]);
    await t.answer(1);
    await t.end();
    await t.end();
    expect(t.played).toEqual([A, B]);
    expect(t.cut).toEqual([]);
    expect(t.asked()).toEqual([A, B]);
    expect(t.phase()).toBe("rearming");
  });

  test("Esc while Voicebox prepares the answer aborts the request, and its late answer plays nothing", async () => {
    const t = withVoicebox();
    await t.think();
    await t.update({ view: chat([T1, turn("t2", "running")]) });
    await t.update({ view: chat([T1, turn("t2", "success")]) });
    expect(t.asked()).toEqual([TEXT]);
    act(() => {
      fireEvent.keyDown(document.body, { key: "Escape" });
    });
    await settled();
    expect(t.phase()).toBe("off");
    expect(t.requests[0]?.signal.aborted).toBe(true);
    await t.answer(0);
    expect(t.played).toEqual([]);
    expect(t.spoken).toEqual([]);
    expect(t.controller.owner()).toBeNull();
    expect(t.phase()).toBe("off");
  });

  test("a newer turn that finishes while Voicebox prepares the answer leaves the older one unspoken", async () => {
    // The window's worker answers each parse by hand: the newer turn's own stays out.
    const parses: ((result: ParseResult) => void)[] = [];
    const parser: MarkdownParser = {
      cached: () => undefined,
      request: (_owner, _text, _deadline, done) => parses.push(done),
      cancel: () => {},
    };
    const t = withVoicebox({ parser });
    await t.think();
    await t.update({ view: chat([T1, turn("t2", "running")]) });
    await t.update({ view: chat([T1, turn("t2", "success")]) });
    parses[0]?.(parseMarkdown(TEXT));
    await settled();
    expect(t.asked()).toEqual([TEXT]);
    await t.update({ view: chat([T1, turn("t2", "success"), turn("t3", "success")]) });
    await t.answer(0);
    expect(t.phase()).toBe("thinking");
    expect(t.controller.statusOf("handsfree:t2")).toBe("idle");
  });

  test("a newer turn that finishes while early speech awaits its first audio: the loop waits on the newer one", async () => {
    // The newer turn's parse never comes back, so only the older reply's finish can move the loop.
    const parser: MarkdownParser = { cached: () => undefined, request: () => {}, cancel: () => {} };
    const t = withVoicebox({ speakWhileWriting: true, parser });
    await t.think();
    await t.update({ view: chat([T1, turn("t2", "running", `${A} The sec`)]) });
    await t.update({ view: chat([T1, turn("t2", "success", A)]) });
    await t.update({ view: chat([T1, turn("t2", "success", A), turn("t3", "success")]) });
    await t.answer(0);
    expect(t.played).toEqual([A]);
    expect(t.phase()).toBe("thinking");
  });

  test("a card opening while Voicebox prepares the answer neither cuts it nor is announced over it", async () => {
    const t = withVoicebox();
    await t.think();
    await t.update({ view: chat([T1, turn("t2", "running")]) });
    await t.update({ view: chat([T1, turn("t2", "success")]) });
    await t.update({ view: chat([T1, turn("t2", "success")], [CARD]) });
    expect(t.requests[0]?.signal.aborted).toBe(false);
    await t.answer(0);
    expect(t.played).toEqual([TEXT]);
    expect(t.spoken).toEqual([]);
    expect(t.phase()).toBe("speaking");
  });

  test("a newer turn starting while Voicebox prepares a one-sentence answer does not cut it", async () => {
    const short = "Sure, it is three in the afternoon.";
    const t = withVoicebox({ speakWhileWriting: true });
    await t.think();
    await t.update({ view: chat([T1, turn("t2", "running", "Sure, it is three in the after")]) });
    await t.update({ view: chat([T1, turn("t2", "success", short)]) });
    expect(t.asked()).toEqual([short]);
    await t.update({
      view: chat([T1, turn("t2", "success", short), turn("t3", "running", "Wor")]),
    });
    expect(t.requests[0]?.signal.aborted).toBe(false);
    await t.answer(0);
    expect(t.played).toEqual([short]);
    expect(t.phase()).toBe("speaking");
  });
});

describe("the engine preflight, and a session where it goes down", () => {
  test("engine pack: Enter warms the pack once its own status is up", async () => {
    const fake = fakePack("stopped");
    const t = withVoicebox({ engine: "pack", pack: fake.services });
    await t.enter();
    expect(t.phase()).toBe("listening");
    expect(fake.acted).toEqual(["warm"]);
  });

  test("engine pack: a rejected warm is ignored", async () => {
    const fake = fakePack("ready");
    fake.services.api.act = () => Promise.reject(new Error("boom"));
    const t = withVoicebox({ engine: "pack", pack: fake.services });
    await t.enter();
    expect(t.phase()).toBe("listening");
  });

  test("engine pack: a down preflight with no local voice refuses with today's no_voice notice", async () => {
    const fake = fakePack("not_installed");
    const t = withVoicebox({ engine: "pack", pack: fake.services, voices: [] });
    await t.enter();
    expect(t.hook.result.current.state).toEqual({ ...HANDS_FREE_OFF, notice: "no_voice" });
    expect(fake.acted).toEqual([]);
  });

  test("engine voicebox: a down own app at Enter with a local voice enters and speaks the answer with the system voice", async () => {
    const t = withVoicebox({ engine: "voicebox", status: "unreachable" });
    await t.think();
    await t.update({ view: chat([T1, turn("t2", "running")]) });
    await t.update({ view: chat([T1, turn("t2", "success")]) });
    expect(t.phase()).toBe("speaking");
    expect(t.spoken).toEqual([TEXT]);
    expect(t.requests).toEqual([]);
  });

  test("engine pack: an answer in a language the pack does not speak goes to the system voice", async () => {
    const fake = fakePack("ready");
    const t = withVoicebox({ engine: "pack", pack: fake.services, languages: ["ja"] });
    await t.think();
    await t.update({ view: chat([T1, turn("t2", "running")]) });
    await t.update({ view: chat([T1, turn("t2", "success")]) });
    expect(t.phase()).toBe("speaking");
    expect(t.spoken).toEqual([TEXT]);
    expect(t.requests).toEqual([]);
  });

  test("after early speech fails unreachable, the answer makes no second Voicebox request", async () => {
    const t = withVoicebox({ speakWhileWriting: true });
    await t.think();
    await t.update({ view: chat([T1, turn("t2", "running", `${A} The sec`)]) });
    expect(t.asked()).toEqual([A]);
    await t.fail(0, "voicebox_unreachable");
    expect(t.phase()).toBe("thinking");
    await t.update({ view: chat([T1, turn("t2", "success", A)]) });
    expect(t.phase()).toBe("speaking");
    expect(t.spoken).toEqual([A]);
    expect(t.played).toEqual([]);
    expect(t.asked()).toEqual([A]);
  });

  test("a non-down-set failure code keeps today's behaviour: the answer still tries Voicebox fresh", async () => {
    const t = withVoicebox({ speakWhileWriting: true });
    await t.think();
    await t.update({ view: chat([T1, turn("t2", "running", `${A} The sec`)]) });
    await t.fail(0, "voicebox_timeout");
    await t.update({ view: chat([T1, turn("t2", "success", A)]) });
    expect(t.asked()).toEqual([A, A]);
    expect(t.phase()).toBe("thinking");
    await t.answer(1);
    expect(t.phase()).toBe("speaking");
    expect(t.played).toEqual([A]);
    expect(t.spoken).toEqual([]);
  });

  test("a later Enter re-checks the engine and is not stuck down from an earlier refusal", async () => {
    const fake = fakePack("not_installed");
    const t = withVoicebox({ engine: "pack", pack: fake.services, voices: [] });
    await t.enter();
    expect(t.hook.result.current.state.notice).toBe("no_voice");
    fake.status.state = "ready";
    await t.think();
    await t.update({ view: chat([T1, turn("t2", "running")]) });
    await t.update({ view: chat([T1, turn("t2", "success")]) });
    expect(t.asked()).toEqual([TEXT]);
  });
});

describe("keeping the pack warm", () => {
  test("engine pack: a KEEP_WARM_MS interval re-warms the pack while on, cleared when it leaves", async () => {
    vi.useFakeTimers();
    const fake = fakePack("ready");
    const t = withVoicebox({ engine: "pack", pack: fake.services });
    await t.enter();
    expect(t.phase()).toBe("listening");
    expect(fake.acted).toEqual(["warm"]);

    act(() => {
      vi.advanceTimersByTime(KEEP_WARM_MS);
    });
    expect(fake.acted).toEqual(["warm", "warm"]);

    act(() => {
      vi.advanceTimersByTime(KEEP_WARM_MS);
    });
    expect(fake.acted).toEqual(["warm", "warm", "warm"]);

    act(() => {
      fireEvent.keyDown(document.body, { key: "Escape" });
    });
    expect(t.phase()).toBe("off");

    act(() => {
      vi.advanceTimersByTime(KEEP_WARM_MS * 2);
    });
    expect(fake.acted).toEqual(["warm", "warm", "warm"]);
  });

  test("engine pack: the keep-warm interval is cleared on unmount", async () => {
    vi.useFakeTimers();
    const fake = fakePack("ready");
    const t = withVoicebox({ engine: "pack", pack: fake.services });
    await t.enter();
    expect(fake.acted).toEqual(["warm"]);

    t.hook.unmount();
    act(() => {
      vi.advanceTimersByTime(KEEP_WARM_MS * 2);
    });
    expect(fake.acted).toEqual(["warm"]);
  });

  test("engine voicebox: no keep-warm interval runs for the pack", async () => {
    vi.useFakeTimers();
    const fake = fakePack("ready");
    const t = withVoicebox({ engine: "voicebox", pack: fake.services });
    await t.enter();
    expect(fake.acted).toEqual([]);

    act(() => {
      vi.advanceTimersByTime(KEEP_WARM_MS * 2);
    });
    expect(fake.acted).toEqual([]);
  });

  test("engine pack: no pack context runs no keep-warm interval and does not throw", async () => {
    vi.useFakeTimers();
    const t = withVoicebox({ engine: "pack" });
    await t.enter();
    expect(t.phase()).toBe("listening");

    expect(() => {
      act(() => {
        vi.advanceTimersByTime(KEEP_WARM_MS * 2);
      });
    }).not.toThrow();
  });
});

const ENTRY_BLOCKS = ["permission", "question", "running"] as const;

function entryBlockedView(kind: (typeof ENTRY_BLOCKS)[number]): SessionView {
  return kind === "running" ? chat([turn("t2", "running")]) : chat([T1], [{ kind } as Card]);
}

test.each(ENTRY_BLOCKS)("an existing %s blocks entry until a fresh idle click", async (kind) => {
  const t = setup({ view: entryBlockedView(kind) });
  await t.enter();
  expect(t.phase()).toBe("off");
  expect(t.hook.result.current.bridge.listening).toBe(false);
  await t.update({ view: chat([T1]) });
  expect(t.phase()).toBe("off");
  await t.enter();
  expect(t.phase()).toBe("listening");
});

test.each(ENTRY_BLOCKS)("a %s arriving during readiness blocks entry", async (kind) => {
  let resolve!: (status: { state: string }) => void;
  const dictationApi = {
    status: () => new Promise((ready) => (resolve = ready)),
  } as unknown as DictationApi;
  const t = setup({ dictationApi });
  act(() => t.hook.result.current.toggle());
  await t.update({ view: entryBlockedView(kind) });
  await act(async () => resolve({ state: "ready" }));
  await settled();
  expect(t.phase()).toBe("off");
  expect(t.hook.result.current.bridge.listening).toBe(false);
});

test.each([...ENTRY_BLOCKS, "hidden"] as const)(
  "a %s before notice confirmation cannot start listening",
  async (kind) => {
    const t = setup({}, false);
    await t.enter();
    expect(t.hook.result.current.state.noticeOpen).toBe(true);
    if (kind === "hidden") {
      vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    } else {
      await t.update({ view: entryBlockedView(kind) });
    }
    act(() => t.hook.result.current.confirmNotice());
    expect(t.phase()).toBe("off");
    expect(t.hook.result.current.bridge.listening).toBe(false);
    expect(t.hook.result.current.state.noticeOpen).toBe(false);
  },
);

test("ordinary idle notice confirmation starts listening", async () => {
  const t = setup({}, false);
  await t.enter();
  act(() => t.hook.result.current.confirmNotice());
  expect(t.phase()).toBe("listening");
});
