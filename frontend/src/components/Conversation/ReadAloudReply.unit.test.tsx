import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { t } from "../../i18n/t";
import { emptyView, type Turn } from "../../state/view";
import { createReadAloud } from "../../voice/readAloud";
import { Conversation, Reply } from "./Conversation";
import { ReadAloudContext, type ReadAloudSettings } from "./ReadAloud";

afterEach(cleanup);

const turn: Turn = {
  id: "t1",
  user: null,
  blocks: [{ kind: "text", id: "b1", text: "Hello there.", done: true }],
  status: "success",
  usage: null,
  durationMs: 0,
  history: false,
};

/** A controller that is supported and has no voices: enough to show the controls. */
function provider(over: Partial<ReadAloudSettings> = {}): ReadAloudSettings {
  const synth = {
    getVoices: () => [],
    addEventListener: () => {},
    removeEventListener: () => {},
  } as unknown as SpeechSynthesis;
  const controller = createReadAloud({
    synth,
    makeUtterance: () => ({}) as never,
    target: new EventTarget(),
    devices: null,
    setTimeout: () => 0,
    clearTimeout: () => {},
  });
  return { controller, rate: 1, voices: {}, noticeSeen: true, acknowledge: () => {}, ...over };
}

const readName = () => t("readaloud.read.label" as never);

describe("Reply read aloud", () => {
  it("puts Read after the copy control in the name row", () => {
    render(
      <ReadAloudContext.Provider value={provider()}>
        <Reply turn={turn} onOpenUrl={() => {}} />
      </ReadAloudContext.Provider>,
    );
    const row = document.querySelector(".reply-name-row") as HTMLElement;
    const copy = row.querySelector(".copy-btn") as HTMLElement;
    const read = screen.getByRole("button", { name: readName() });
    expect(row.contains(read)).toBe(true);
    expect(copy.compareDocumentPosition(read) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("puts the notice right after the row", () => {
    render(
      <ReadAloudContext.Provider value={provider({ noticeSeen: false })}>
        <Reply turn={turn} onOpenUrl={() => {}} />
      </ReadAloudContext.Provider>,
    );
    screen.getByRole("button", { name: readName() }).click();
    const row = document.querySelector(".reply-name-row") as HTMLElement;
    return screen.findByRole("group").then((note) => {
      expect(row.nextElementSibling).toBe(note.closest(".read-aloud-note"));
    });
  });

  it("hands a readAloud prop on Conversation to the reply's controls", () => {
    render(
      <Conversation
        view={{ ...emptyView("s1"), turns: [turn] }}
        nowMs={0}
        onOpenUrl={() => {}}
        readAloud={provider()}
      />,
    );
    expect(screen.getByRole("button", { name: readName() })).toBeTruthy();
  });

  it("shows no read-aloud control without a provider (no speech synthesis here)", () => {
    render(<Reply turn={turn} onOpenUrl={() => {}} />);
    expect(screen.queryByRole("button", { name: readName() })).toBeNull();
  });
});
