import { expect, test } from "vitest";
import {
  createReadAloud,
  localVoices,
  parseVoiceMap,
  pickVoice,
  primaryLanguage,
  serializeVoiceMap,
  VOICES_WAIT_MS,
  voicesFor,
} from "./readAloud";

function voice(name: string, lang: string, localService = true): SpeechSynthesisVoice {
  return { name, voiceURI: name, lang, localService, default: false };
}

const ZIRA = voice("Zira", "en-US");
const DAVID = voice("David", "en-GB");
const ONLINE = voice("Online (Natural)", "en-US", false);
const HARUKA = voice("Haruka", "ja-JP");

interface FakeUtterance {
  text: string;
  voice: SpeechSynthesisVoice | null;
  lang: string;
  rate: number;
  onend: (() => void) | null;
  onerror: (() => void) | null;
}

class FakeSynth extends EventTarget {
  voiceList: SpeechSynthesisVoice[] = [];
  calls: string[] = [];
  spoken: FakeUtterance[] = [];
  paused = false;
  throwOnSpeak = false;
  getVoices() {
    return this.voiceList;
  }
  speak(u: FakeUtterance) {
    if (this.throwOnSpeak) throw new Error("engine");
    this.calls.push("speak");
    this.spoken.push(u);
  }
  cancel() {
    this.calls.push("cancel");
  }
  pause() {
    this.calls.push("pause");
    this.paused = true;
  }
  resume() {
    this.calls.push("resume");
    this.paused = false;
  }
}

function setup(voiceList: SpeechSynthesisVoice[] = [ZIRA, DAVID, ONLINE, HARUKA]) {
  const synth = new FakeSynth();
  synth.voiceList = voiceList;
  const target = new EventTarget();
  const devices = new EventTarget();
  const timers = new Map<number, () => void>();
  let nextTimer = 1;
  const controller = createReadAloud({
    synth: synth as unknown as SpeechSynthesis,
    makeUtterance: (text) =>
      ({
        text,
        voice: null,
        lang: "",
        rate: 1,
        onend: null,
        onerror: null,
      }) as unknown as SpeechSynthesisUtterance,
    target,
    devices,
    setTimeout: (cb) => {
      const id = nextTimer++;
      timers.set(id, cb);
      return id;
    },
    clearTimeout: (id) => {
      timers.delete(id);
    },
  });
  const start = (owner: string, chunks: string[], v = ZIRA, rate = 1.25) => {
    const ticket = controller.begin(owner);
    return { ticket, ok: controller.play(ticket, owner, chunks, v, rate) };
  };
  return { synth, target, devices, timers, controller, start };
}

test("an online voice is never listed, picked or assigned, even as the stored choice", () => {
  const { controller } = setup();
  expect(localVoices([ZIRA, ONLINE]).map((v) => v.name)).toEqual(["Zira"]);
  expect(controller.voices().map((v) => v.name)).toEqual(["Zira", "David", "Haruka"]);
  expect(voicesFor("en", [ONLINE, ZIRA]).map((v) => v.name)).toEqual(["Zira"]);
  expect(pickVoice("en", [ONLINE, ZIRA], { en: ONLINE.voiceURI })?.name).toBe("Zira");
  expect(pickVoice("en", [ONLINE], {})).toBeNull();
});

test("an online voice handed to play speaks nothing", () => {
  const { synth, controller } = setup();
  const ticket = controller.begin("a");
  expect(controller.play(ticket, "a", ["hello"], ONLINE, 1)).toBe(false);
  expect(synth.spoken).toEqual([]);
  expect(controller.statusOf("a")).toBe("idle");
});

test("a local voice that left the engine is not spoken either", () => {
  const { synth, controller } = setup([ZIRA]);
  const ticket = controller.begin("a");
  expect(controller.play(ticket, "a", ["hello"], DAVID, 1)).toBe(false);
  expect(synth.spoken).toEqual([]);
});

test("voices come in order and keep their language and choice rules", () => {
  expect(primaryLanguage("EN_us")).toBe("en");
  expect(primaryLanguage("in-ID")).toBe("id");
  expect(primaryLanguage("cmn-Hans-CN")).toBe("zh");
  expect(pickVoice("en", [ZIRA, DAVID], {})?.name).toBe("Zira");
  expect(pickVoice("en", [ZIRA, DAVID], { en: "David" })?.name).toBe("David");
  expect(pickVoice("en", [ZIRA, DAVID], { en: "Gone" })?.name).toBe("Zira");
  expect(pickVoice("ja", [ZIRA, DAVID], {})).toBeNull();
});

test("chunks play in order, one at a time, with the voice, its lang and the rate", () => {
  const { synth, controller, start } = setup();
  const { ok } = start("a", ["one", "two", "three"], DAVID, 1.5);
  expect(ok).toBe(true);
  expect(synth.spoken).toHaveLength(1);
  const first = synth.spoken[0];
  expect(first).toMatchObject({ text: "one", lang: "en-GB", rate: 1.5 });
  expect(first?.voice).toBe(DAVID);
  expect(controller.statusOf("a")).toBe("speaking");
  first?.onend?.();
  expect(synth.spoken.map((u) => u.text)).toEqual(["one", "two"]);
  synth.spoken[1]?.onend?.();
  synth.spoken[2]?.onend?.();
  expect(synth.spoken.map((u) => u.text)).toEqual(["one", "two", "three"]);
  expect(controller.statusOf("a")).toBe("idle");
  expect(synth.calls).not.toContain("cancel");
});

test("pause, resume and stop drive the synth and the status, and notify", () => {
  const { synth, controller, start } = setup();
  let notified = 0;
  const unsubscribe = controller.subscribe(() => {
    notified += 1;
  });
  start("a", ["one", "two"]);
  controller.pause();
  expect(controller.statusOf("a")).toBe("paused");
  controller.resume();
  expect(controller.statusOf("a")).toBe("speaking");
  expect(synth.calls).toEqual(["speak", "pause", "resume"]);
  controller.stop();
  expect(controller.statusOf("a")).toBe("idle");
  expect(synth.calls).toEqual(["speak", "pause", "resume", "cancel"]);
  expect(notified).toBeGreaterThanOrEqual(4);
  unsubscribe();
  const before = notified;
  start("a", ["x"]);
  expect(notified).toBe(before);
});

test("stop while paused cancels, then resumes the synth so the next Read speaks", () => {
  const { synth, controller, start } = setup();
  start("a", ["one"]);
  controller.pause();
  controller.stop();
  expect(synth.calls).toEqual(["speak", "pause", "cancel", "resume"]);
  expect(synth.paused).toBe(false);
  expect(start("b", ["two"]).ok).toBe(true);
  expect(synth.spoken.at(-1)?.text).toBe("two");
});

test("pause and resume do nothing in the wrong status", () => {
  const { synth, controller, start } = setup();
  controller.pause();
  controller.resume();
  start("a", ["one"]);
  controller.resume();
  expect(synth.calls).toEqual(["speak"]);
});

test("a stale ticket, a spent ticket, empty chunks or an unknown voice speak nothing", () => {
  const { synth, controller } = setup();
  const stale = controller.begin("a");
  controller.begin("b");
  expect(controller.isCurrent(stale)).toBe(false);
  expect(controller.play(stale, "a", ["x"], ZIRA, 1)).toBe(false);
  const ticket = controller.begin("c");
  expect(controller.isCurrent(ticket)).toBe(true);
  expect(controller.play(ticket, "c", [], ZIRA, 1)).toBe(false);
  expect(controller.play(ticket, "c", ["x"], voice("Nobody", "en-US"), 1)).toBe(false);
  expect(controller.play(ticket, "other", ["x"], ZIRA, 1)).toBe(false);
  expect(controller.play(ticket, "c", ["x"], ZIRA, 1)).toBe(true);
  expect(controller.play(ticket, "c", ["y"], ZIRA, 1)).toBe(false);
  expect(synth.spoken.map((u) => u.text)).toEqual(["x"]);
});

test("events of a stopped generation change nothing", () => {
  const { synth, controller, start } = setup();
  start("a", ["one", "two"]);
  const first = synth.spoken[0];
  controller.stop();
  first?.onend?.();
  first?.onerror?.();
  expect(synth.spoken).toHaveLength(1);
  expect(controller.statusOf("a")).toBe("idle");
  expect(synth.calls).toEqual(["speak", "cancel"]);
});

test("an error ends the playback as failed, once, and a stop clears it", () => {
  const { synth, controller, start } = setup();
  start("a", ["one", "two"]);
  const first = synth.spoken[0];
  first?.onerror?.();
  expect(controller.statusOf("a")).toBe("failed");
  expect(synth.calls).toEqual(["speak", "cancel"]);
  first?.onerror?.();
  first?.onend?.();
  expect(synth.calls).toEqual(["speak", "cancel"]);
  expect(synth.spoken).toHaveLength(1);
  controller.stop();
  expect(controller.statusOf("a")).toBe("idle");
  expect(synth.calls).toEqual(["speak", "cancel"]);
});

test("an engine that reports its own cancel as an error does not loop", () => {
  const { synth, controller, start } = setup();
  start("a", ["one"]);
  const first = synth.spoken[0];
  synth.cancel = () => {
    synth.calls.push("cancel");
    first?.onerror?.();
  };
  first?.onerror?.();
  expect(synth.calls).toEqual(["speak", "cancel"]);
  expect(controller.statusOf("a")).toBe("failed");
});

test("a voice gone before the second chunk fails and leaves it unspoken", () => {
  const { synth, controller, start } = setup();
  start("a", ["one", "two"]);
  synth.voiceList = [DAVID, HARUKA];
  synth.spoken[0]?.onend?.();
  expect(controller.statusOf("a")).toBe("failed");
  expect(synth.spoken.map((u) => u.text)).toEqual(["one"]);
});

test("a voice that turns online before the second chunk is not spoken with", () => {
  const { synth, controller, start } = setup();
  start("a", ["one", "two"]);
  synth.voiceList = [{ ...ZIRA, localService: false }];
  synth.spoken[0]?.onend?.();
  expect(controller.statusOf("a")).toBe("failed");
  expect(synth.spoken).toHaveLength(1);
});

test("a throwing speak fails the playback", () => {
  const { synth, controller, start } = setup();
  synth.throwOnSpeak = true;
  expect(start("a", ["one"]).ok).toBe(true);
  expect(controller.statusOf("a")).toBe("failed");
});

test("pagehide stops and devicechange pauses", () => {
  const { synth, target, devices, controller, start } = setup();
  start("a", ["one"]);
  devices.dispatchEvent(new Event("devicechange"));
  expect(controller.statusOf("a")).toBe("paused");
  expect(synth.calls).toEqual(["speak", "pause"]);
  target.dispatchEvent(new Event("pagehide"));
  expect(controller.statusOf("a")).toBe("idle");
  expect(synth.calls).toEqual(["speak", "pause", "cancel", "resume"]);
});

test("devicechange leaves a hands-free playback speaking, and still pauses any other", () => {
  const { synth, devices, controller, start } = setup();
  start("handsfree:t2", ["one"]);
  devices.dispatchEvent(new Event("devicechange"));
  expect(controller.statusOf("handsfree:t2")).toBe("speaking");
  expect(synth.calls).toEqual(["speak"]);
  start("a", ["two"]);
  devices.dispatchEvent(new Event("devicechange"));
  expect(controller.statusOf("a")).toBe("paused");
  expect(synth.calls.at(-1)).toBe("pause");
});

test("pagehide cancels a pending start", () => {
  const { synth, target, controller, start } = setup();
  start("a", ["one"]);
  const ticket = controller.begin("b");
  target.dispatchEvent(new Event("pagehide"));
  expect(controller.isCurrent(ticket)).toBe(false);
  expect(controller.play(ticket, "b", ["x"], ZIRA, 1)).toBe(false);
  expect(synth.spoken).toHaveLength(1);
});

test("stop with nothing playing makes no synth call, and a pending start is cancelled", () => {
  const { synth, controller } = setup();
  controller.stop();
  const ticket = controller.begin("a");
  controller.stop();
  expect(synth.calls).toEqual([]);
  expect(controller.play(ticket, "a", ["x"], ZIRA, 1)).toBe(false);
});

test("another owner's begin stops the first", () => {
  const { synth, controller, start } = setup();
  start("a", ["one"]);
  controller.begin("b");
  expect(controller.statusOf("a")).toBe("idle");
  expect(synth.calls).toEqual(["speak", "cancel"]);
});

test("stopIf stops only its own playback or pending start", () => {
  const { synth, controller, start } = setup();
  start("a", ["one"]);
  controller.stopIf("b");
  expect(controller.statusOf("a")).toBe("speaking");
  controller.stopIf("a");
  expect(controller.statusOf("a")).toBe("idle");
  const ticket = controller.begin("c");
  controller.stopIf("c");
  expect(controller.isCurrent(ticket)).toBe(false);
  expect(synth.calls).toEqual(["speak", "cancel"]);
});

test("loadVoices answers at once with voices, waits for voiceschanged, and gives up", async () => {
  const ready = setup();
  expect((await ready.controller.loadVoices()).length).toBe(3);

  const late = setup([]);
  const waiting = late.controller.loadVoices();
  late.synth.dispatchEvent(new Event("voiceschanged"));
  late.synth.voiceList = [ONLINE];
  late.synth.dispatchEvent(new Event("voiceschanged"));
  expect(late.timers.size).toBe(1);
  late.synth.voiceList = [ZIRA, ONLINE];
  late.synth.dispatchEvent(new Event("voiceschanged"));
  expect((await waiting).map((v) => v.name)).toEqual(["Zira"]);
  expect(late.timers.size).toBe(0);

  const none = setup([]);
  const gaveUp = none.controller.loadVoices();
  expect(VOICES_WAIT_MS).toBe(3000);
  for (const cb of none.timers.values()) cb();
  expect(await gaveUp).toEqual([]);
});

test("voices keeps its array until the voices change, and a change notifies", () => {
  const { synth, controller } = setup([ZIRA]);
  let notified = 0;
  controller.subscribe(() => {
    notified += 1;
  });
  const first = controller.voices();
  expect(controller.voices()).toBe(first);
  synth.dispatchEvent(new Event("voiceschanged"));
  expect(notified).toBe(0);
  synth.voiceList = [ZIRA, HARUKA];
  synth.dispatchEvent(new Event("voiceschanged"));
  expect(notified).toBe(1);
  expect(controller.voices()).not.toBe(first);
  expect(controller.voices()).toHaveLength(2);
});

test("without speechSynthesis nothing is supported and nothing throws", async () => {
  const controller = createReadAloud({
    synth: null,
    makeUtterance: () => {
      throw new Error("no engine");
    },
    target: new EventTarget(),
    devices: null,
    setTimeout: () => 0,
    clearTimeout: () => {},
  });
  expect(controller.supported).toBe(false);
  expect(controller.voices()).toEqual([]);
  expect(await controller.loadVoices()).toEqual([]);
  const ticket = controller.begin("a");
  expect(controller.play(ticket, "a", ["x"], ZIRA, 1)).toBe(false);
  controller.pause();
  controller.resume();
  controller.stop();
});

test("the voice map round-trips and refuses what the server would", () => {
  const map = { ja: "Haruka", en: "Zira" };
  expect(serializeVoiceMap(map)).toBe('{"en":"Zira","ja":"Haruka"}');
  expect(parseVoiceMap(serializeVoiceMap(map))).toEqual(map);
  expect(serializeVoiceMap({})).toBeNull();
  for (const bad of [
    undefined,
    null,
    1,
    "",
    "[]",
    "null",
    "{",
    '{"JA":"x"}',
    '{"jpn1":"x"}',
    '{"ja":""}',
    '{"ja":1}',
    JSON.stringify({ ja: "x".repeat(257) }),
  ]) {
    expect(parseVoiceMap(bad)).toEqual({});
  }
  expect(parseVoiceMap(JSON.stringify({ ja: "x".repeat(256) }))).toEqual({ ja: "x".repeat(256) });
});
