import { expect, test, vi } from "vitest";
import type { HistoryAnchorPage } from "../clients/conversations/conversationSearchApi";
import {
  applyHistoryAnchorPage,
  currentAppHit,
  matchSnapshot,
  searchAppTurns,
} from "./conversationSearch";
import { applyHistoryPage } from "./sessionReducer";
import { emptyView } from "./view";

test("anchor window preserves live turns and invalidates older socket pages", () => {
  const before = applyHistoryPage(emptyView("s"), {
    events: [{ type: "user.message", data: { text: "old", command: "", at: "" } }],
    next_before_uuid: null,
  });
  const first = before.turns[0];
  if (!first) throw new Error("missing turn");
  const live = { ...first, id: "live", history: false };
  before.turns.push(live);
  before.historyRequestId = "pending";
  const page: HistoryAnchorPage = {
    state: "ready",
    reason: "",
    request_id: "search",
    coverage: "complete",
    events: [{ type: "user.message", data: { text: "found", command: "", at: "" } }],
    anchor_turn_index: 0,
    older_before_uuid: "older",
    newer_after_uuid: "gap",
  };
  const result = applyHistoryAnchorPage(before, page);
  expect(result?.view.turns.map((turn) => turn.user?.text)).toEqual(["found", "old"]);
  expect(result?.view.turns[1]).toBe(live);
  expect(result?.view.historyRequestId).toBeNull();
  expect(result?.turnId).toBe(result?.view.turns[0]?.id);
  expect(applyHistoryAnchorPage(before, { ...page, state: "changed" })).toBeNull();
});

test("local hit revisions refuse changed text and generations", async () => {
  const view = applyHistoryPage(emptyView("s"), {
    events: [{ type: "user.message", data: { text: "a needle", command: "", at: "" } }],
    next_before_uuid: null,
  });
  view.generation = "generation";
  view.turns = view.turns.map((turn) => ({ ...turn, history: false }));
  const [hit] = await searchAppTurns(view, "needle");
  if (!hit) throw new Error("missing hit");
  expect(await currentAppHit(view, hit)).toBe(true);
  expect(await currentAppHit({ ...view, generation: "other" }, hit)).toBe(false);
  const user = view.turns[0]?.user;
  if (!user) throw new Error("missing user");
  user.text = "changed";
  expect(await currentAppHit(view, hit)).toBe(false);
  expect(matchSnapshot("a ｔｅｓｔ z", "test")?.match).toBe("ｔｅｓｔ");
});

test("native and local matching preserve Unicode and original whitespace within bounds", () => {
  expect(matchSnapshot("a Straße z", "STRASSE")).toEqual({
    before: "a ",
    match: "Straße",
    after: " z",
  });
  expect(matchSnapshot("ΟΣ", "ος")?.match).toBe("ΟΣ");
  expect(matchSnapshot("a\n  b", "a  b")?.match).toBe("a\n  b");
  expect(
    [...(matchSnapshot(`a${"\u0301".repeat(400)}`, "á")?.match ?? "")].length,
  ).toBeLessThanOrEqual(256);
});

test("matchSnapshot stays in step with the server fold across normalization-inserted whitespace and Hangul composition", () => {
  // U+309B alone NFKC-decomposes to a leading space; the join rule merges it into the previous
  // part (a real space) before either is folded, so the run folds to one space, not two.
  expect(matchSnapshot("カ ゛ 検索です", "検索")?.match).toBe("検索");
  expect(matchSnapshot("a ¨ needle z", "needle")?.match).toBe("needle");
  // U+3131 (compatibility jamo) then U+1161 (conjoining vowel) compose under NFKC into one
  // syllable; Intl.Segmenter keeps them as two graphemes, so they must be joined before folding.
  expect(matchSnapshot("ㄱᅡneedle", "needle")?.match).toBe("needle");
});

test("a long whitespace run stays bounded: the join window, not the whole run, is folded", () => {
  const text = `${" ".repeat(5_000)}needle`;
  const original = String.prototype.normalize;
  let charsNormalized = 0;
  const spy = vi.spyOn(String.prototype, "normalize").mockImplementation(function (
    this: string,
    ...args: Parameters<typeof original>
  ) {
    charsNormalized += this.length;
    return original.apply(this, args);
  });
  try {
    expect(matchSnapshot(text, "needle")?.match).toBe("needle");
  } finally {
    spy.mockRestore();
  }
  // The unbounded rule folds the whole run again on every grapheme (about n²/2 characters
  // normalized); a bounded join window keeps the total roughly linear in the text length.
  expect(charsNormalized).toBeLessThan(100 * text.length);
});
