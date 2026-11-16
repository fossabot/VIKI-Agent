import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { t, tf } from "../../i18n/t";
import type { Turn } from "../../state/view";
import { createReadAloud, type ReadAloud } from "../../voice/readAloud";
import { MAX_MARKDOWN_BYTES } from "./Markdown";
import { type MarkdownParser, MarkdownParserContext } from "./markdownParser";
import { ReadAloudContext, type ReadAloudSettings, useReadAloudReply } from "./ReadAloud";

interface Spoken {
  text: string;
  voice: string | null;
  lang: string;
  rate: number;
}

function voice(name: string, lang: string, localService = true) {
  return { name, voiceURI: name, lang, localService, default: false };
}

const DAVID = voice("David", "en-US");
const ZIRA = voice("Zira", "en-US");
const HARUKA = voice("Haruka", "ja-JP");
const ONLINE = voice("Aria Online (Natural)", "en-US", false);

function makeSynth(voices: ReturnType<typeof voice>[]) {
  const spoken: Spoken[] = [];
  const utterances: { onend: (() => void) | null; onerror: (() => void) | null }[] = [];
  const calls = { cancel: 0, pause: 0, resume: 0 };
  const synth = {
    paused: false,
    getVoices: () => voices,
    addEventListener: () => {},
    removeEventListener: () => {},
    speak(u: { text: string; voice: { voiceURI: string } | null; lang: string; rate: number }) {
      spoken.push({ text: u.text, voice: u.voice?.voiceURI ?? null, lang: u.lang, rate: u.rate });
      utterances.push(u as never);
    },
    cancel: () => {
      calls.cancel += 1;
    },
    pause: () => {
      calls.pause += 1;
    },
    resume: () => {
      calls.resume += 1;
    },
  };
  return { synth, spoken, utterances, calls };
}

function makeController(voices: ReturnType<typeof voice>[], timers: (() => void)[] = []) {
  const fake = makeSynth(voices);
  const controller = createReadAloud({
    synth: fake.synth as unknown as SpeechSynthesis,
    makeUtterance: (text) =>
      ({ text, voice: null, lang: "", rate: 1, onend: null, onerror: null }) as never,
    target: new EventTarget(),
    devices: null,
    setTimeout: (cb) => timers.push(cb) - 1,
    clearTimeout: () => {},
  });
  return { ...fake, controller };
}

function turn(text: string | null, status: Turn["status"] = "success"): Turn {
  return {
    id: "t1",
    user: null,
    blocks: text === null ? [] : [{ kind: "text", id: "b1", text, done: true }],
    status,
    usage: null,
    durationMs: 0,
    history: false,
  };
}

function Harness({ turn: shown }: { turn: Turn }): ReactElement {
  const ui = useReadAloudReply(shown);
  return (
    <div>
      {ui.controls}
      {ui.note}
    </div>
  );
}

function settings(controller: ReadAloud, over: Partial<ReadAloudSettings> = {}) {
  return { controller, rate: 1.5, voices: {}, noticeSeen: true, acknowledge: () => {}, ...over };
}

function view(shown: Turn, value: ReadAloudSettings, parser: MarkdownParser | null = null) {
  return (
    <MarkdownParserContext.Provider value={parser}>
      <ReadAloudContext.Provider value={value}>
        <Harness turn={shown} />
      </ReadAloudContext.Provider>
    </MarkdownParserContext.Provider>
  );
}

const button = (key: string) =>
  screen.getByRole("button", { name: t(`readaloud.${key}.label` as never) });
const queryButton = (key: string) =>
  screen.queryByRole("button", { name: t(`readaloud.${key}.label` as never) });

async function click(element: HTMLElement) {
  await act(async () => {
    fireEvent.click(element);
  });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("the controls", () => {
  it("shows nothing for a running reply, one without text, or no speechSynthesis", () => {
    const { controller } = makeController([DAVID]);
    const value = settings(controller);
    const { rerender } = render(view(turn("Hello there.", "running"), value));
    expect(queryButton("read")).toBeNull();
    rerender(view(turn(null), value));
    expect(queryButton("read")).toBeNull();
    const unsupported = createReadAloud({
      synth: null,
      makeUtterance: () => {
        throw new Error("unused");
      },
      target: new EventTarget(),
      devices: null,
      setTimeout: () => 0,
      clearTimeout: () => {},
    });
    rerender(view(turn("Hello there."), settings(unsupported)));
    expect(queryButton("read")).toBeNull();
  });

  it("speaks the chosen voice at the rate and leaves fenced code out", async () => {
    const { controller, spoken } = makeController([DAVID, ZIRA, ONLINE]);
    render(
      view(
        turn("The weather is fine today and you are here.\n\n```js\nsecretCode()\n```"),
        settings(controller, { voices: { en: "Zira" } }),
      ),
    );
    await click(button("read"));
    expect(spoken).toHaveLength(1);
    expect(spoken[0]).toMatchObject({ voice: "Zira", lang: "en-US", rate: 1.5 });
    expect(spoken[0]?.text).toContain("The weather is fine");
    expect(spoken[0]?.text).not.toContain("secretCode");
  });

  it("never speaks with an online voice, even as the stored choice", async () => {
    const { controller, spoken } = makeController([ONLINE, DAVID]);
    render(
      view(
        turn("The weather is fine today and you are here."),
        settings(controller, { voices: { en: ONLINE.voiceURI } }),
      ),
    );
    await click(button("read"));
    expect(spoken.map((s) => s.voice)).toEqual(["David"]);
  });

  it("pauses, resumes and stops", async () => {
    const { controller, spoken, calls } = makeController([DAVID]);
    render(view(turn("The weather is fine today and you are here."), settings(controller)));
    await click(button("read"));
    expect(spoken).toHaveLength(1);
    expect(queryButton("read")).toBeNull();
    await click(button("pause"));
    expect(calls.pause).toBe(1);
    expect(queryButton("pause")).toBeNull();
    await click(button("resume"));
    expect(calls.resume).toBe(1);
    await click(button("stop"));
    expect(calls.cancel).toBe(1);
    expect(button("read")).toBeTruthy();
    expect(screen.queryByRole("status")).toBeNull();
  });

  it('shows "AI voice" only while speaking or paused', async () => {
    const { controller } = makeController([DAVID]);
    render(view(turn("The weather is fine today and you are here."), settings(controller)));
    expect(screen.queryByText("AI voice")).toBeNull();
    await click(button("read"));
    const chip = screen.getByText("AI voice");
    expect(chip.tagName).toBe("SPAN");
    expect(chip.className).toBe("chip dim");
    expect(chip.getAttribute("role")).toBeNull();
    expect(document.querySelector(".read-aloud")?.firstElementChild).toBe(chip);
    await click(button("pause"));
    expect(screen.getByText("AI voice")).toBeTruthy();
    await click(button("stop"));
    expect(screen.queryByText("AI voice")).toBeNull();
  });

  it("returns to Read with no note when something else stops the playback", async () => {
    const { controller } = makeController([DAVID]);
    render(view(turn("The weather is fine today and you are here."), settings(controller)));
    await click(button("read"));
    act(() => controller.stop());
    expect(button("read")).toBeTruthy();
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("stops the first reply when a second reply is read", async () => {
    const { controller, calls } = makeController([DAVID]);
    const value = settings(controller);
    render(
      <>
        {view(turn("The weather is fine today and you are here."), value)}
        {view(turn("The garden is green and the sun is out today."), value)}
      </>,
    );
    const [first, second] = screen.getAllByRole("button", { name: t("readaloud.read.label") });
    await click(first as HTMLElement);
    expect(screen.getAllByRole("button", { name: t("readaloud.stop.label") })).toHaveLength(1);
    await click(second as HTMLElement);
    expect(calls.cancel).toBe(1);
    expect(screen.getAllByRole("button", { name: t("readaloud.stop.label") })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: t("readaloud.read.label") })).toHaveLength(1);
  });

  it("shows the failure note, and Close clears it", async () => {
    const { controller, utterances } = makeController([DAVID]);
    render(view(turn("The weather is fine today and you are here."), settings(controller)));
    await click(button("read"));
    act(() => utterances[0]?.onerror?.());
    expect(screen.getByRole("status").textContent).toContain(t("readaloud.failed"));
    await click(screen.getByRole("button", { name: t("common.button.close") }));
    expect(screen.queryByRole("status")).toBeNull();
    expect(button("read")).toBeTruthy();
  });
});

describe("the notice", () => {
  it("shows before the first Read, and confirm saves and speaks", async () => {
    const { controller, spoken } = makeController([DAVID]);
    const acknowledge = vi.fn();
    render(
      view(
        turn("The weather is fine today and you are here."),
        settings(controller, { noticeSeen: false, acknowledge }),
      ),
    );
    await click(button("read"));
    expect(spoken).toHaveLength(0);
    expect(screen.getByRole("group").textContent).toContain(t("readaloud.notice"));
    const confirm = screen.getByRole("button", { name: t("readaloud.notice.confirm") });
    // The notice is a step to take, not a quiet line: its confirm takes focus and is the primary.
    expect(document.activeElement).toBe(confirm);
    expect(confirm.classList.contains("read-aloud-confirm")).toBe(true);
    await click(confirm);
    expect(acknowledge).toHaveBeenCalledTimes(1);
    expect(spoken).toHaveLength(1);
    expect(screen.queryByRole("group")).toBeNull();
  });

  it.each([
    ["Win32", false],
    ["Linux x86_64", true],
  ] as const)("on %s the Linux sentence is shown: %s", async (platform, linux) => {
    vi.spyOn(navigator, "platform", "get").mockReturnValue(platform);
    const { controller } = makeController([DAVID]);
    render(
      view(
        turn("The weather is fine today and you are here."),
        settings(controller, { noticeSeen: false }),
      ),
    );
    await click(screen.getByRole("button", { name: t("readaloud.read.label") }));
    const text = screen.getByRole("group").textContent ?? "";
    expect(text).toContain(t("readaloud.notice"));
    expect(text.includes(t("readaloud.notice.linux"))).toBe(linux);
  });

  it("speaks nothing on Cancel and does not save", async () => {
    const { controller, spoken } = makeController([DAVID]);
    const acknowledge = vi.fn();
    render(
      view(
        turn("The weather is fine today and you are here."),
        settings(controller, { noticeSeen: false, acknowledge }),
      ),
    );
    await click(button("read"));
    await click(screen.getByRole("button", { name: t("common.button.cancel") }));
    expect(spoken).toHaveLength(0);
    expect(acknowledge).not.toHaveBeenCalled();
    expect(screen.queryByRole("group")).toBeNull();
  });
});

describe("what cannot be read", () => {
  it("refuses a block over 256 KiB, and clears the pending owner", async () => {
    const { controller, spoken } = makeController([DAVID]);
    render(view(turn("a ".repeat(MAX_MARKDOWN_BYTES / 2 + 1)), settings(controller)));
    await click(button("read"));
    expect(spoken).toHaveLength(0);
    expect(screen.getByRole("status").textContent).toContain(t("readaloud.unreadable"));
    expect(controller.owner()).toBeNull();
  });

  it("says there is no text when only code is left, and clears the pending owner", async () => {
    const { controller, spoken } = makeController([DAVID]);
    render(view(turn("```js\nonlyCode()\n```"), settings(controller)));
    await click(button("read"));
    expect(spoken).toHaveLength(0);
    expect(screen.getByRole("status").textContent).toContain(t("readaloud.nothing"));
    expect(controller.owner()).toBeNull();
  });

  it.each([
    ["Win32", "readaloud.how.windows"],
    ["MacIntel", "readaloud.how.mac"],
    ["Linux x86_64", "readaloud.how.linux"],
  ] as const)(
    "names the language and the %s steps when no voice is installed, and clears the pending owner (F15)",
    async (platform, steps) => {
      vi.spyOn(navigator, "platform", "get").mockReturnValue(platform);
      const { controller, spoken } = makeController([DAVID, ONLINE]);
      const japanese = turn("今日は天気がとても良いので、散歩に行きます。");
      render(view(japanese, settings(controller)));
      await click(button("read"));
      expect(spoken).toHaveLength(0);
      const note = screen.getByRole("status").textContent ?? "";
      expect(note).toContain(tf("readaloud.no_voice", { language: "Japanese" }));
      expect(note).toContain(t(steps));
      // Hands-free reads controller.owner() to know whose reply is pending; a no-voice outcome
      // speaks nothing, so nothing should still report this reply as owning playback.
      expect(controller.owner()).toBeNull();
    },
  );

  it("speaks Japanese in an installed Japanese voice", async () => {
    const { controller, spoken } = makeController([DAVID, HARUKA]);
    render(view(turn("今日は天気がとても良いので、散歩に行きます。"), settings(controller)));
    await click(button("read"));
    expect(spoken[0]).toMatchObject({ voice: "Haruka", lang: "ja-JP" });
  });

  it("says no voice at all once the wait for voices ends", async () => {
    const timers: (() => void)[] = [];
    const { controller, spoken } = makeController([], timers);
    render(view(turn("The weather is fine today and you are here."), settings(controller)));
    await click(button("read"));
    expect(timers).toHaveLength(1);
    await act(async () => timers[0]?.());
    expect(spoken).toHaveLength(0);
    expect(screen.getByRole("status").textContent).toContain(t("readaloud.no_voices"));
  });
});

describe("leaving the screen", () => {
  it("cancels playback on unmount", async () => {
    const { controller, calls } = makeController([DAVID]);
    const { unmount } = render(
      view(turn("The weather is fine today and you are here."), settings(controller)),
    );
    await click(button("read"));
    unmount();
    expect(calls.cancel).toBe(1);
  });

  it("adds no note and speaks nothing when a stop comes while preparing", async () => {
    const { controller, spoken } = makeController([DAVID]);
    const requests: { onResult: (r: never) => void }[] = [];
    const parser: MarkdownParser = {
      cached: () => undefined,
      request: (_owner, _text, _deadline, onResult) => requests.push({ onResult }),
      cancel: () => {},
    };
    render(
      view(turn("今日は天気がとても良いので、散歩に行きます。"), settings(controller), parser),
    );
    await click(button("read"));
    expect(requests).toHaveLength(1);
    act(() => controller.stop());
    await act(async () => {
      requests[0]?.onResult({
        kind: "tree",
        nodes: 1,
        tree: { type: "root", children: [{ type: "text", value: "今日は天気がとても良いので。" }] },
      } as never);
    });
    expect(screen.queryByRole("status")).toBeNull();
    expect(spoken).toHaveLength(0);
  });

  it("speaks nothing when it unmounts while preparing", async () => {
    const { controller, spoken } = makeController([DAVID]);
    const requests: { owner: object; onResult: (r: never) => void }[] = [];
    const cancel = vi.fn();
    const parser: MarkdownParser = {
      cached: () => undefined,
      request: (owner, _text, _deadline, onResult) => requests.push({ owner, onResult }),
      cancel,
    };
    const { unmount } = render(
      view(turn("The weather is fine today and you are here."), settings(controller), parser),
    );
    await click(button("read"));
    expect(requests).toHaveLength(1);
    expect(button("read").getAttribute("aria-busy")).toBe("true");
    unmount();
    expect(cancel).toHaveBeenCalledWith(requests[0]?.owner);
    await act(async () => {
      requests[0]?.onResult({
        kind: "tree",
        nodes: 1,
        tree: { type: "root", children: [{ type: "text", value: "The weather is fine." }] },
      } as never);
    });
    expect(spoken).toHaveLength(0);
  });
});
