import { describe, expect, it } from "vitest";
import {
  begin,
  edit,
  final,
  finish,
  IDLE,
  insertAt,
  partial,
  provisionalText,
  rebase,
} from "./dictation";
import { dictationNotice } from "./dictationWords";

const ID = "11111111-1111-4111-8111-111111111111";
const OTHER = "22222222-2222-4222-8222-222222222222";

const part = (segment: number, revision: number, text: string, capture_id = ID) =>
  ({ type: "partial", capture_id, segment, revision, text }) as const;
const fin = (segment: number, text: string, capture_id = ID) =>
  ({ type: "final", capture_id, segment, text }) as const;

describe("begin", () => {
  it("keeps the caret as the anchor", () => {
    const s = begin(ID, 7);
    expect(s.capture).toBe(ID);
    expect(s.anchor).toBe(7);
    expect(s.lastFinal).toBe(-1);
  });
});

describe("partial", () => {
  it("keeps only ascending revisions of a segment", () => {
    let s = begin(ID, 0);
    s = partial(s, part(0, 2, "two"));
    expect(provisionalText(s)).toBe("two");
    expect(partial(s, part(0, 1, "one"))).toBe(s);
    expect(partial(s, part(0, 2, "again"))).toBe(s);
    s = partial(s, part(0, 3, "three"));
    expect(provisionalText(s)).toBe("three");
  });

  it("ignores a segment that is already final", () => {
    let s = begin(ID, 0);
    s = final(s, fin(1, "ok"), "")?.state ?? s;
    expect(partial(s, part(1, 5, "late"))).toBe(s);
    expect(partial(s, part(0, 5, "late"))).toBe(s);
    expect(provisionalText(partial(s, part(2, 0, "next")))).toBe("next");
  });

  it("ignores another capture and the idle state", () => {
    const s = begin(ID, 0);
    expect(partial(s, part(0, 1, "x", OTHER))).toBe(s);
    expect(partial(IDLE, part(0, 1, "x"))).toBe(IDLE);
  });
});

describe("final", () => {
  it("inserts at the anchor and moves it after the text", () => {
    const r = final(begin(ID, 0), fin(0, "hello"), "");
    expect(r?.draft).toBe("hello");
    expect(r?.state.anchor).toBe(5);
    expect(r?.state.lastFinal).toBe(0);
  });

  it("separates Latin words with one space", () => {
    const first = final(begin(ID, 5), fin(0, "world"), "hello");
    expect(first?.draft).toBe("hello world");
    const second = final(first?.state ?? IDLE, fin(1, "again"), first?.draft ?? "");
    expect(second?.draft).toBe("hello world again");
  });

  it("puts no space between Japanese texts", () => {
    const first = final(begin(ID, 0), fin(0, "こんにちは"), "");
    const second = final(first?.state ?? IDLE, fin(1, "世界"), first?.draft ?? "");
    expect(second?.draft).toBe("こんにちは世界");
  });

  it("spaces text after closing punctuation and a digit", () => {
    expect(insertAt("Hi.", 3, "Next").draft).toBe("Hi. Next");
    expect(insertAt("Item 2", 6, "done").draft).toBe("Item 2 done");
    expect(insertAt("(note)", 6, "next").draft).toBe("(note) next");
  });

  it("puts no space after an opening bracket or a space", () => {
    expect(insertAt("(", 1, "note").draft).toBe("(note");
    expect(insertAt("a ", 2, "b").draft).toBe("a b");
  });

  it("adds a space on the right when words would touch", () => {
    const r = insertAt("hello world", 5, "big");
    expect(r.draft).toBe("hello big world");
    const touching = insertAt("helloworld", 5, "big");
    expect(touching.draft).toBe("hello big world");
    expect(touching.anchor).toBe(9);
  });

  it("handles text on both sides of a Japanese anchor", () => {
    expect(insertAt("前後", 1, "中").draft).toBe("前中後");
    expect(insertAt("a後", 1, "b").draft).toBe("a b後");
  });

  it("returns null for a duplicate final and one below the last final", () => {
    const first = final(begin(ID, 0), fin(3, "one"), "");
    expect(first).not.toBeNull();
    if (!first) return;
    expect(final(first.state, fin(3, "one"), first.draft)).toBeNull();
    expect(final(first.state, fin(2, "old"), first.draft)).toBeNull();
  });

  it("returns null for another capture id", () => {
    expect(final(begin(ID, 0), fin(0, "x", OTHER), "")).toBeNull();
  });

  it("drops the provisional segments up to the final", () => {
    let s = begin(ID, 0);
    s = partial(s, part(0, 1, "one"));
    s = partial(s, part(1, 1, "two"));
    const r = final(s, fin(0, "one"), "");
    expect(provisionalText(r?.state ?? IDLE)).toBe("two");
  });

  it("closes an empty final without inserting", () => {
    let s = begin(ID, 2);
    s = partial(s, part(0, 1, "noise"));
    const r = final(s, fin(0, "  "), "ab");
    expect(r?.draft).toBe("ab");
    expect(r?.state.anchor).toBe(2);
    expect(r?.state.lastFinal).toBe(0);
    expect(provisionalText(r?.state ?? IDLE)).toBe("");
  });

  it("clamps an anchor past the end of a shorter draft", () => {
    expect(final(begin(ID, 99), fin(0, "x"), "ab")?.draft).toBe("ab x");
  });
});

describe("finish", () => {
  it("a late final after finish does not change the draft", () => {
    let s = begin(ID, 0);
    s = partial(s, part(0, 1, "words"));
    s = finish(s);
    expect(s).toBe(IDLE);
    expect(provisionalText(s)).toBe("");
    expect(final(s, fin(0, "late"), "typed")).toBeNull();
    expect(partial(s, part(1, 1, "late"))).toBe(s);
  });
});

describe("rebase and edit", () => {
  it("leaves an anchor before the edit alone", () => {
    expect(rebase(2, "abcdef", "abcXXdef")).toBe(2);
  });

  it("shifts an anchor after the edit", () => {
    expect(rebase(6, "abcdef", "abXXXcdef")).toBe(9);
    expect(rebase(6, "abcdef", "abef")).toBe(4);
  });

  it("moves an anchor at the edit after the new text", () => {
    expect(rebase(3, "abcdef", "abcXYdef")).toBe(5);
  });

  it("moves an anchor inside a replaced region to its end", () => {
    expect(rebase(3, "abcdef", "aXf")).toBe(2);
  });

  it("puts an anchor inside a deleted region at the deletion", () => {
    expect(rebase(3, "abcdef", "af")).toBe(1);
    expect(rebase(5, "abcdef", "abcdef")).toBe(5);
  });

  it("handles typing into an empty draft and a full clear", () => {
    expect(rebase(0, "", "hey")).toBe(3);
    expect(rebase(3, "hey", "")).toBe(0);
  });

  it("edit rebases the anchor and ignores the idle state", () => {
    const s = begin(ID, 4);
    expect(edit(s, "abcd", "xxabcd").anchor).toBe(6);
    expect(edit(s, "abcd", "abcd")).toBe(s);
    expect(edit(IDLE, "a", "bb")).toBe(IDLE);
  });

  it("keeps the user's typing when a final lands after an edit before the anchor", () => {
    let s = begin(ID, 5);
    s = edit(s, "hello", ">> hello");
    const r = final(s, fin(0, "world"), ">> hello");
    expect(r?.draft).toBe(">> hello world");
  });
});

describe("provisionalText", () => {
  it("joins segments in order on the same spacing rule", () => {
    let s = begin(ID, 0);
    s = partial(s, part(2, 1, "again"));
    s = partial(s, part(0, 1, "hello"));
    s = partial(s, part(1, 1, "world"));
    expect(provisionalText(s)).toBe("hello world again");
  });

  it("joins Japanese segments without a space and skips empty ones", () => {
    let s = begin(ID, 0);
    s = partial(s, part(0, 1, "今日は"));
    s = partial(s, part(1, 1, ""));
    s = partial(s, part(2, 1, "晴れ"));
    expect(provisionalText(s)).toBe("今日は晴れ");
  });
});

describe("dictationNotice", () => {
  const CODES = [
    "setup_needed",
    "model_mismatch",
    "backend_missing",
    "backend_mismatch",
    "engine_unavailable",
    "worker_unavailable",
    "worker_timeout",
    "engine_mismatch",
    "model_unavailable",
    "backend_unavailable",
    "worker_crashed",
    "protocol_error",
    "decode_failed",
    "internal",
    "invalid_input",
    "overloaded",
    "capture_cap",
    "final_timeout",
    "busy",
    "refused",
    "timed_out",
    "connection_lost",
    "still_starting",
    "mic_denied",
    "mic_missing",
    "mic_busy",
    "mic_failed",
    "mic_lost",
    "mic_stalled",
    "unavailable",
    "finish_first",
  ];

  it("gives every code its own sentence, not the generic one", () => {
    for (const code of CODES) {
      const notice = dictationNotice(code);
      expect(notice, code).toMatch(/^[\x20-\x7e]+$/);
      expect(notice, code).not.toContain(`(${code})`);
    }
  });

  it("names the setup and check commands", () => {
    expect(dictationNotice("setup_needed")).toContain("uv run python scripts/voice_setup.py");
    expect(dictationNotice("model_mismatch")).toContain("--check");
  });

  it("states the two-minute limit and the other window", () => {
    expect(dictationNotice("capture_cap")).toContain("2 minutes");
    expect(dictationNotice("busy")).toContain("Another window");
  });

  it("names an unknown code in the generic sentence", () => {
    expect(dictationNotice("surprise")).toContain("surprise");
    expect(dictationNotice("constructor")).toContain("constructor");
  });
});
