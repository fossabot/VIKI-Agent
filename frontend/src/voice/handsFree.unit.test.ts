/**
 * Frozen acceptance tests for the hands-free loop's reducer. Every transition of
 * the state machine, and the rules that keep it half-duplex: a card pauses the loop with
 * no capture, nothing re-arms by itself outside `rearming`, and every way out reaches `off`.
 */
import { describe, expect, test } from "vitest";
import {
  HANDS_FREE_OFF,
  type HandsFreeEvent,
  type HandsFreeExit,
  type HandsFreePhase,
  type HandsFreeState,
  handsFreeReducer,
  REARM_DELAY_MS,
  speechOwner,
} from "./handsFree";

const ENTER: HandsFreeEvent = {
  type: "enter",
  dictationReady: true,
  localVoice: true,
  noticeSeen: true,
};

/** Each event applied in turn to a frozen state, so a reducer that mutates its input throws. */
function run(events: readonly HandsFreeEvent[], from: HandsFreeState = HANDS_FREE_OFF) {
  let state = Object.freeze({ ...from });
  for (const event of events) state = Object.freeze(handsFreeReducer(state, event));
  return state;
}

const PATHS: Record<Exclude<HandsFreePhase, "off">, HandsFreeEvent[]> = {
  listening: [ENTER],
  sending: [ENTER, { type: "captureDone" }],
  thinking: [ENTER, { type: "captureDone" }, { type: "sendAccepted" }],
  waiting: [ENTER, { type: "captureDone" }, { type: "sendAccepted" }, { type: "card", open: true }],
  speaking: [
    ENTER,
    { type: "captureDone" },
    { type: "sendAccepted" },
    { type: "turnFinished", turnId: "t1", outcome: "speakable" },
  ],
  rearming: [
    ENTER,
    { type: "captureDone" },
    { type: "sendAccepted" },
    { type: "turnFinished", turnId: "t1", outcome: "speakable" },
    { type: "speechEnded", turnId: "t1" },
  ],
};

const at = (phase: Exclude<HandsFreePhase, "off">) => run(PATHS[phase]);

test("the re-arm delay is short, about 400 ms", () => {
  expect(REARM_DELAY_MS).toBeGreaterThanOrEqual(300);
  expect(REARM_DELAY_MS).toBeLessThanOrEqual(600);
});

test("the controller owner is handsfree:<turnId>", () => {
  expect(speechOwner("t1")).toBe("handsfree:t1");
});

test("the paths above reach the phases they are named for", () => {
  for (const [phase, events] of Object.entries(PATHS)) expect(run(events).phase).toBe(phase);
});

describe("enter", () => {
  test("with dictation ready, a local voice and the notice seen, it listens", () => {
    expect(run([ENTER])).toEqual({
      phase: "listening",
      noticeOpen: false,
      notice: null,
      turnId: null,
    });
  });

  test("without dictation ready it stays off and says why", () => {
    const state = run([{ ...ENTER, dictationReady: false }]);
    expect(state.phase).toBe("off");
    expect(state.notice).toBe("dictation_unavailable");
    expect(state.noticeOpen).toBe(false);
  });

  test("without a local voice it stays off and says why", () => {
    const state = run([{ ...ENTER, localVoice: false }]);
    expect(state.phase).toBe("off");
    expect(state.notice).toBe("no_voice");
    expect(state.noticeOpen).toBe(false);
  });

  test("an unready dictation is named before a missing voice", () => {
    expect(run([{ ...ENTER, dictationReady: false, localVoice: false }]).notice).toBe(
      "dictation_unavailable",
    );
  });

  test("before the read-aloud notice was seen, the notice opens first and nothing listens", () => {
    const state = run([{ ...ENTER, noticeSeen: false }]);
    expect(state.phase).toBe("off");
    expect(state.noticeOpen).toBe(true);
  });

  test("confirming the notice listens; cancelling it stays off", () => {
    const asked = run([{ ...ENTER, noticeSeen: false }]);
    expect(run([{ type: "noticeConfirmed" }], asked)).toMatchObject({
      phase: "listening",
      noticeOpen: false,
    });
    expect(run([{ type: "noticeCancelled" }], asked)).toMatchObject({
      phase: "off",
      noticeOpen: false,
    });
  });

  test("a confirm with no notice open does nothing", () => {
    expect(run([{ type: "noticeConfirmed" }])).toEqual(HANDS_FREE_OFF);
  });

  test("an unready dictation does not open the notice", () => {
    expect(run([{ ...ENTER, dictationReady: false, noticeSeen: false }]).noticeOpen).toBe(false);
  });

  test("enter clears an earlier notice", () => {
    const ended = run([ENTER, { type: "captureEmpty" }]);
    expect(ended.notice).toBe("nothing_heard");
    expect(run([ENTER], ended)).toMatchObject({ phase: "listening", notice: null });
  });

  test("enter while on changes nothing", () => {
    for (const phase of Object.keys(PATHS) as (keyof typeof PATHS)[]) {
      expect(run([ENTER], at(phase))).toEqual(at(phase));
    }
  });
});

describe("listening", () => {
  test("a finished capture with text sends", () => {
    expect(run([{ type: "captureDone" }], at("listening")).phase).toBe("sending");
  });

  test("a capture that ended empty ends the mode with a notice", () => {
    expect(run([{ type: "captureEmpty" }], at("listening"))).toMatchObject({
      phase: "off",
      notice: "nothing_heard",
    });
  });

  test("a mic press only ends the capture: the loop waits for its result", () => {
    expect(run([{ type: "micPressed" }], at("listening"))).toEqual(at("listening"));
  });

  test("turn and speech events do not move it", () => {
    const listening = at("listening");
    for (const event of [
      { type: "turnFinished", turnId: "t1", outcome: "speakable" },
      { type: "speechEnded", turnId: "t1" },
      { type: "card", open: true },
      { type: "rearm" },
      { type: "sendAccepted" },
    ] satisfies HandsFreeEvent[]) {
      expect(run([event], listening)).toEqual(listening);
    }
  });
});

describe("sending", () => {
  test("an accepted Send thinks", () => {
    expect(run([{ type: "sendAccepted" }], at("sending")).phase).toBe("thinking");
  });

  test("a refused or blocked Send ends the mode with a notice", () => {
    expect(run([{ type: "sendRefused" }], at("sending"))).toMatchObject({
      phase: "off",
      notice: "send_blocked",
    });
  });
});

describe("thinking and waiting", () => {
  test("a card waits, with no capture, and its end goes back to thinking", () => {
    const waiting = run([{ type: "card", open: true }], at("thinking"));
    expect(waiting.phase).toBe("waiting");
    expect(run([{ type: "card", open: false }], waiting).phase).toBe("thinking");
  });

  test("nothing listens while a card is open", () => {
    const waiting = at("waiting");
    for (const event of [
      { type: "rearm" },
      { type: "micPressed" },
      { type: "captureDone" },
      { type: "card", open: true },
    ] satisfies HandsFreeEvent[]) {
      expect(run([event], waiting)).toEqual(waiting);
    }
  });

  test("a speakable reply speaks, holding its turn", () => {
    const speaking = run(
      [{ type: "turnFinished", turnId: "t9", outcome: "speakable" }],
      at("thinking"),
    );
    expect(speaking).toMatchObject({ phase: "speaking", turnId: "t9" });
  });

  test("a turn with nothing to speak re-arms after the delay instead of ending", () => {
    const rearming = run(
      [{ type: "turnFinished", turnId: "t1", outcome: "silent" }],
      at("thinking"),
    );
    expect(rearming).toMatchObject({ phase: "rearming", notice: null });
    expect(run([{ type: "rearm" }], rearming).phase).toBe("listening");
  });

  test("a failed or stopped turn ends the mode with a notice", () => {
    expect(
      run([{ type: "turnFinished", turnId: "t1", outcome: "failed" }], at("thinking")),
    ).toMatchObject({ phase: "off", notice: "turn_failed" });
    expect(
      run([{ type: "turnFinished", turnId: "t1", outcome: "stopped" }], at("thinking")),
    ).toMatchObject({ phase: "off", notice: "turn_stopped" });
  });

  test("a turn that finishes while its card is still counted is read the same way", () => {
    const waiting = at("waiting");
    expect(
      run([{ type: "turnFinished", turnId: "t2", outcome: "speakable" }], waiting),
    ).toMatchObject({ phase: "speaking", turnId: "t2" });
    expect(run([{ type: "turnFinished", turnId: "t2", outcome: "failed" }], waiting).phase).toBe(
      "off",
    );
  });

  test("a mic press while thinking does nothing", () => {
    expect(run([{ type: "micPressed" }], at("thinking"))).toEqual(at("thinking"));
  });
});

describe("speaking", () => {
  test("the end of speech re-arms after the delay", () => {
    const rearming = run([{ type: "speechEnded", turnId: "t1" }], at("speaking"));
    expect(rearming).toMatchObject({ phase: "rearming", turnId: null });
    expect(run([{ type: "rearm" }], rearming).phase).toBe("listening");
  });

  test("another turn's speech events are ignored", () => {
    const speaking = at("speaking");
    expect(run([{ type: "speechEnded", turnId: "t0" }], speaking)).toEqual(speaking);
    expect(run([{ type: "speechFailed", turnId: "t0" }], speaking)).toEqual(speaking);
  });

  test("a speech error ends the mode with a notice", () => {
    expect(run([{ type: "speechFailed", turnId: "t1" }], at("speaking"))).toMatchObject({
      phase: "off",
      notice: "speech_failed",
      turnId: null,
    });
  });

  test("a mic press interrupts: it listens at once", () => {
    expect(run([{ type: "micPressed" }], at("speaking"))).toMatchObject({
      phase: "listening",
      turnId: null,
    });
  });
});

describe("rearming", () => {
  test("rearm is the only event that turns the delay into listening", () => {
    expect(run([{ type: "rearm" }], at("rearming")).phase).toBe("listening");
    expect(run([{ type: "rearm" }], at("thinking"))).toEqual(at("thinking"));
    expect(run([{ type: "rearm" }], at("speaking"))).toEqual(at("speaking"));
    expect(run([{ type: "rearm" }])).toEqual(HANDS_FREE_OFF);
  });
});

describe("every way out", () => {
  const REASONS: HandsFreeExit[] = ["esc", "toggle", "hidden", "session", "pagehide"];
  // Built inside each test, so a broken reducer fails the test rather than the file.
  const STATES: [string, () => HandsFreeState][] = [
    ["off", () => HANDS_FREE_OFF],
    ["the notice", () => run([{ ...ENTER, noticeSeen: false }])],
    ...(Object.keys(PATHS) as (keyof typeof PATHS)[]).map(
      (phase): [string, () => HandsFreeState] => [phase, () => at(phase)],
    ),
  ];

  for (const reason of REASONS) {
    test.each(STATES)(`exit ${reason} from %s is off`, (_name, from) => {
      const state = run([{ type: "exit", reason }], from());
      expect(state.phase).toBe("off");
      expect(state.noticeOpen).toBe(false);
      expect(state.turnId).toBeNull();
    });
  }

  test("Esc and the toggle leave quietly; a hidden window and a session switch say why", () => {
    const speaking = at("speaking");
    expect(run([{ type: "exit", reason: "esc" }], speaking).notice).toBeNull();
    expect(run([{ type: "exit", reason: "toggle" }], speaking).notice).toBeNull();
    expect(run([{ type: "exit", reason: "hidden" }], speaking).notice).toBe("window_hidden");
    expect(run([{ type: "exit", reason: "session" }], speaking).notice).toBe("session_switched");
  });
});

describe("off", () => {
  test("loop events are ignored while off", () => {
    for (const event of [
      { type: "captureDone" },
      { type: "captureEmpty" },
      { type: "sendAccepted" },
      { type: "sendRefused" },
      { type: "card", open: true },
      { type: "turnFinished", turnId: "t1", outcome: "speakable" },
      { type: "speechEnded", turnId: "t1" },
      { type: "speechFailed", turnId: "t1" },
      { type: "micPressed" },
      { type: "rearm" },
    ] satisfies HandsFreeEvent[]) {
      expect(run([event])).toEqual(HANDS_FREE_OFF);
    }
  });

  test("dismiss clears the notice and nothing else", () => {
    const ended = run([ENTER, { type: "captureEmpty" }]);
    expect(run([{ type: "dismiss" }], ended)).toEqual(HANDS_FREE_OFF);
  });
});
