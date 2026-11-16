/**
 * Unit tests for state/skillTriggers.ts: the three skillHint detectors at and below their
 * thresholds, the history/running/non-success gate, the SKILL.md and decision-question fence
 * scanners' block cases, and draftPrompt's lead and "Why now" line.
 */
import { expect, test } from "vitest";
import {
  type Item,
  view as reduced,
  toolDone,
  toolStarted,
  turnDone,
  userMessage,
} from "../accept/m2b/fakes";
import { localIso, sessionRow } from "../accept/m3b/fakes";
import type { SessionRow, UserMessageData } from "../protocol";
import {
  changedRefs,
  draftPrompt,
  fencedBlock,
  LONG_TOOL_ROWS,
  LONG_TURNS,
  lastFinishedTurnId,
  lookRef,
  MAX_LOOK_REFS,
  type QuestionBlock,
  questionBlock,
  type SkillHint,
  secondLookHint,
  serverTurnId,
  skillBlock,
  skillHint,
} from "./skillTriggers";
import {
  emptyView,
  type RoutingBlock,
  type SessionView,
  type TextBlock,
  type ToolBlock,
  type ToolRowView,
  type Turn,
} from "./view";

// ---- fixtures --------------------------------------------------------------------------------

function userMsg(text: string): UserMessageData {
  return { text, command: "", at: "" };
}

function turn(id: string, overrides: Partial<Turn> = {}): Turn {
  return {
    id,
    user: null,
    blocks: [],
    status: "success",
    usage: null,
    durationMs: 0,
    history: false,
    ...overrides,
  };
}

function userTurn(id: string, text: string, overrides: Partial<Turn> = {}): Turn {
  return turn(id, { user: userMsg(text), ...overrides });
}

/** `n` plain, succeeded user turns (the default `turn()` status), for the long_session count. */
function userTurns(n: number, text = "step"): Turn[] {
  return Array.from({ length: n }, (_, i) => userTurn(`u${i}`, text));
}

/** `n` user turns with `overrides` applied to only the last one, to test the gate against an
 * otherwise-qualifying long_session shape. */
function turnsWithLastOverride(n: number, overrides: Partial<Turn>): Turn[] {
  const turns = userTurns(n);
  const lastIndex = turns.length - 1;
  const last = turns[lastIndex];
  return last ? [...turns.slice(0, lastIndex), { ...last, ...overrides }] : turns;
}

function view(turns: Turn[], sessionId = "this"): SessionView {
  return { ...emptyView(sessionId), turns };
}

function textBlock(text: string, id = "b"): TextBlock {
  return { kind: "text", id, text, done: true };
}

function toolRow(id: string): ToolRowView {
  return {
    id,
    tool: "Bash",
    row: { head: "", rest: "", tail: "" },
    done: true,
    isError: false,
    summary: "",
    preview: "",
  };
}

function toolBlock(n: number, id = "t"): ToolBlock {
  return { kind: "tools", id, rows: Array.from({ length: n }, (_, i) => toolRow(`${id}-${i}`)) };
}

function routingBlock(n: number, id = "r"): RoutingBlock {
  return {
    kind: "routing",
    id,
    agent: "general-purpose",
    model: "sonnet",
    description: "",
    reason: "",
    status: "completed",
    tools: Array.from({ length: n }, (_, i) => toolRow(`${id}-${i}`)),
  };
}

function row(id: string, firstPrompt: string, modified = "2026-01-01T00:00:00.000Z"): SessionRow {
  return sessionRow({ session_id: id, first_prompt: firstPrompt, modified });
}

function skillTurn(text: string): Turn {
  return turn("t", { blocks: [textBlock(text)] });
}

// ---- lastFinishedTurnId -----------------------------------------------------------------------

test("lastFinishedTurnId: the last turn's id when not running, else null", () => {
  expect(lastFinishedTurnId(view([]))).toBeNull();
  expect(lastFinishedTurnId(view([turn("a", { status: "running" })]))).toBeNull();
  expect(lastFinishedTurnId(view([turn("a"), turn("b", { status: "error" })]))).toBe("b");
  expect(lastFinishedTurnId(view([turn("a", { status: "cancelled" })]))).toBe("a");
});

// ---- the history/running/non-success gate ------------------------------------------------------

test("skillHint's gate beats a would-be long_session trigger", () => {
  expect(skillHint(view(turnsWithLastOverride(LONG_TURNS, { status: "running" })), [])).toBeNull();
  expect(skillHint(view(turnsWithLastOverride(LONG_TURNS, { history: true })), [])).toBeNull();
  expect(skillHint(view(turnsWithLastOverride(LONG_TURNS, { status: "error" })), [])).toBeNull();
  expect(
    skillHint(view(turnsWithLastOverride(LONG_TURNS, { status: "cancelled" })), []),
  ).toBeNull();
  expect(skillHint(view(userTurns(LONG_TURNS)), [])?.trigger).toBe("long_session");
});

// ---- correction ---------------------------------------------------------------------------------

test("correction: a user turn at index>=1 opening with a correction phrase, before a later success, triggers", () => {
  const turns = [
    userTurn("u0", "build a widget"),
    userTurn("u1", "No, do it differently"),
    userTurn("u2", "thanks"),
  ];
  expect(skillHint(view(turns), [])).toEqual({
    trigger: "correction",
    sessions: ["this"],
    line: "You corrected VIKI in this chat. A skill can keep the fix.",
    reason: "I corrected you in this chat; keep the corrected way.",
  } satisfies SkillHint);
});

test("correction: text at index 0 does not count, even though a later turn succeeded", () => {
  const turns = [
    userTurn("u0", "No, do it differently"),
    userTurn("u1", "build a widget"),
    userTurn("u2", "ok"),
  ];
  expect(skillHint(view(turns), [])).toBeNull();
});

test("correction: the last turn itself does not count (no later turn to have succeeded)", () => {
  const turns = [userTurn("u0", "build a widget"), userTurn("u1", "No, do it differently")];
  expect(skillHint(view(turns), [])).toBeNull();
});

test("correction: a Japanese opener also triggers", () => {
  const turns = [
    userTurn("u0", "build a widget"),
    userTurn("u1", "\u9055\u3046\u3001\u3053\u3046\u3057\u3066"), // a Japanese correction opener
    userTurn("u2", "ok"),
  ];
  expect(skillHint(view(turns), [])?.trigger).toBe("correction");
});

// ---- repeated_request -----------------------------------------------------------------------

const PIVOT = "aa bb cc dd ee"; // tokenizes to 5 distinct 2-letter words

test("repeated_request: 2 similar others (one exactly at the 0.6 threshold) trigger with N = others + this chat", () => {
  const turns = [userTurn("u0", PIVOT)];
  const sessions: SessionRow[] = [
    row("this", PIVOT),
    row("o1", "aa bb cc"), // subset of pivot's 5 tokens: 3/5 = 0.6, at threshold
    row("o2", "aa bb cc dd ff"), // 4/6 = 0.667, above threshold
    row("o3", "zz yy xx ww vv"), // no overlap: not similar
  ];
  expect(skillHint(view(turns), sessions)).toEqual({
    trigger: "repeated_request",
    sessions: ["this", "o1", "o2"],
    line: "You started 3 chats like this one. A skill can save repeating it.",
    reason: "I started 3 chats with a similar first message.",
  } satisfies SkillHint);
});

test("repeated_request: only 1 similar other does not trigger", () => {
  const turns = [userTurn("u0", PIVOT)];
  const sessions: SessionRow[] = [
    row("this", PIVOT),
    row("o1", "aa bb cc"),
    row("o2", "zz yy xx ww vv"),
  ];
  expect(skillHint(view(turns), sessions)).toBeNull();
});

test("repeated_request: a pivot under 3 tokens never triggers, even with identical other rows", () => {
  const turns = [userTurn("u0", "aa bb")];
  const sessions: SessionRow[] = [row("this", "aa bb"), row("o1", "aa bb"), row("o2", "aa bb")];
  expect(skillHint(view(turns), sessions)).toBeNull();
});

test("repeated_request: empty and slash-led prompts are skipped as candidates", () => {
  const turns = [userTurn("u0", PIVOT)];
  const sessions: SessionRow[] = [
    row("this", PIVOT),
    row("o1", "aa bb cc"), // 1 real match, at threshold
    row("o2", ""), // empty: skipped
    row("o3", "/aa bb cc dd ee"), // slash-led: skipped, though textually a perfect match
  ];
  expect(skillHint(view(turns), sessions)).toBeNull();
});

test("repeated_request: falls back to the first turn's user text when this chat has no SessionRow", () => {
  const turns = [userTurn("u0", PIVOT)];
  const sessions: SessionRow[] = [row("o1", "aa bb cc"), row("o2", "aa bb cc dd ff")];
  const hint = skillHint(view(turns), sessions);
  expect(hint?.trigger).toBe("repeated_request");
  expect(hint?.sessions).toEqual(["this", "o1", "o2"]);
});

test("repeated_request: only the 10 most recently modified other rows are considered", () => {
  const turns = [userTurn("u0", PIVOT)];
  const recentNonMatches = Array.from({ length: 10 }, (_, i) =>
    row(`recent${i}`, "zz yy xx ww vv", localIso(2026, 10, 9, 12, i)),
  );
  const oldMatches = [
    row("old1", "aa bb cc", localIso(2026, 1, 1)),
    row("old2", "aa bb cc dd ff", localIso(2026, 1, 2)),
  ];
  const sessions: SessionRow[] = [
    row("this", PIVOT, localIso(2026, 10, 9, 13, 0)),
    ...recentNonMatches,
    ...oldMatches,
  ];
  // The 2 real matches are older than the 10 non-matching rows, so the cap excludes them.
  expect(skillHint(view(turns), sessions)).toBeNull();
});

test("repeated_request: CJK bigram similarity matches a repeated Japanese first message", () => {
  const pivotJa = "\u30d0\u30b0\u3092\u4fee\u6b63\u3057\u3066\u6b32\u3057\u3044"; // a Japanese request, as \u escapes (never a literal)
  const similarJa = "\u30d0\u30b0\u3092\u4fee\u6b63\u3057\u3066\u307b\u3057\u3044"; // a near-duplicate phrasing, as \u escapes
  const turns = [userTurn("u0", pivotJa)];
  const sessions: SessionRow[] = [row("this", pivotJa), row("o1", similarJa), row("o2", similarJa)];
  expect(skillHint(view(turns), sessions)?.trigger).toBe("repeated_request");
});

test("repeated_request: sessions are this chat first, then the matches newest first, at most 10 ids", () => {
  const turns = [userTurn("u0", PIVOT)];
  const matches = Array.from({ length: 10 }, (_, i) =>
    row(`m${i}`, PIVOT, localIso(2026, 10, 9, 12, i)),
  );
  const sessions: SessionRow[] = [row("this", PIVOT), ...matches];
  const hint = skillHint(view(turns), sessions);
  expect(hint?.sessions).toEqual(["this", "m9", "m8", "m7", "m6", "m5", "m4", "m3", "m2", "m1"]);
});

// ---- long_session -------------------------------------------------------------------------------

test("long_session: user-turn count at and just below LONG_TURNS", () => {
  expect(skillHint(view(userTurns(LONG_TURNS)), [])?.trigger).toBe("long_session");
  expect(skillHint(view(userTurns(LONG_TURNS - 1)), [])).toBeNull();
});

test("long_session: tool-row count at and just below LONG_TOOL_ROWS, routing's nested rows included", () => {
  const atThreshold = turn("u0", {
    user: userMsg("go"),
    blocks: [toolBlock(20), routingBlock(LONG_TOOL_ROWS - 20)],
  });
  expect(skillHint(view([atThreshold]), [])?.trigger).toBe("long_session");

  const belowThreshold = turn("u0", {
    user: userMsg("go"),
    blocks: [toolBlock(20), routingBlock(LONG_TOOL_ROWS - 21)],
  });
  expect(skillHint(view([belowThreshold]), [])).toBeNull();
});

// ---- skillBlock -----------------------------------------------------------------------------------

test("skillBlock: no fenced block returns null", () => {
  expect(skillBlock(skillTurn("just prose, no fences here"))).toBeNull();
});

test("skillBlock: exactly one complete SKILL.md block returns its content plus a trailing newline", () => {
  const text = [
    "before",
    "```SKILL.md",
    "---",
    "name: foo",
    "---",
    "do the thing",
    "```",
    "after",
  ].join("\n");
  expect(skillBlock(skillTurn(text))).toBe("---\nname: foo\n---\ndo the thing\n");
});

test("skillBlock: two complete SKILL.md blocks returns null", () => {
  const text = ["```SKILL.md", "one", "```", "```SKILL.md", "two", "```"].join("\n");
  expect(skillBlock(skillTurn(text))).toBeNull();
});

test("skillBlock: an unclosed SKILL.md block returns null", () => {
  const text = ["```SKILL.md", "name: foo", "no closing fence"].join("\n");
  expect(skillBlock(skillTurn(text))).toBeNull();
});

test("skillBlock: a SKILL.md opener inside another fence is just content, not a nested block", () => {
  const text = [
    "```text",
    "example:",
    "```SKILL.md",
    "this is not real, just shown as an example",
    "```",
    "```SKILL.md",
    "name: real",
    "```",
  ].join("\n");
  expect(skillBlock(skillTurn(text))).toBe("name: real\n");
});

test("skillBlock: a 4-backtick SKILL.md fence keeps inner triple-backtick lines as content", () => {
  const text = ["````SKILL.md", "name: foo", "```", "inner code", "```", "````"].join("\n");
  expect(skillBlock(skillTurn(text))).toBe("name: foo\n```\ninner code\n```\n");
});

test("skillBlock: a tilde fence works the same as a backtick fence", () => {
  const text = ["~~~SKILL.md", "name: foo", "~~~"].join("\n");
  expect(skillBlock(skillTurn(text))).toBe("name: foo\n");
});

test("skillBlock: content over 131,072 UTF-8 bytes returns null", () => {
  const big = "a".repeat(131_073);
  const text = ["```SKILL.md", big, "```"].join("\n");
  expect(skillBlock(skillTurn(text))).toBeNull();
});

test("skillBlock: a same-length inner fence in the steps cuts the block short and must fail closed, not offer the partial", () => {
  const text = [
    "```SKILL.md",
    "---",
    "name: foo",
    "---",
    "```bash",
    "echo hi",
    "```",
    "more steps",
    "```",
  ].join("\n");
  expect(skillBlock(skillTurn(text))).toBeNull();
});

test("skillBlock: fails closed even when a later unrelated fence pair reuses the dangling closer", () => {
  // Same mis-nesting as above, but the leftover closer (line 9) now opens a second, unrelated,
  // properly-closed fence further down. A fix that only generalizes the `closeIndex === -1`
  // branch never reaches that branch here (both outer searches below find a closer), so it would
  // still return the truncated content; only checking the SKILL.md block's own content for a
  // dangling inner opener catches it.
  const text = [
    "```SKILL.md",
    "---",
    "name: foo",
    "---",
    "```bash",
    "echo hi",
    "```",
    "more steps",
    "```",
    "unrelated later fence content",
    "```",
  ].join("\n");
  expect(skillBlock(skillTurn(text))).toBeNull();
});

test("skillBlock: a properly-closed inner fence with an info string, inside a longer outer fence, returns the full content", () => {
  // Pins the positive case `hasDanglingInnerOpener` must let through: an inner ```bash fence
  // that IS closed within the block's own content must not be treated as dangling just because
  // it carries an info string. A mutant that treats any info-string opener as dangling — closed
  // or not — would wrongly return null here.
  const text = [
    "````SKILL.md",
    "frontmatter",
    "```bash",
    "npm run build",
    "```",
    "2. Push.",
    "````",
  ].join("\n");
  expect(skillBlock(skillTurn(text))).toBe("frontmatter\n```bash\nnpm run build\n```\n2. Push.\n");
});

test("skillBlock: an indented inner fence slips past the scanner, but the stray closer it leaves behind still fails closed", () => {
  // The inner ```bash fence is indented 3 spaces, so FENCE_OPEN_RE (anchored at column 0) never
  // sees it as an opener; the bare, unindented "```" meant to close it is read as the SKILL.md
  // block's own closer instead, leaving the real outer "```" with nothing to open it: a second,
  // unindented "```" starts a fresh, unclosed fence. That is caught by the generalized
  // `closeIndex === -1` check, not by `hasDanglingInnerOpener` (which never sees an opener here).
  // A mutant that un-generalizes that branch back to `if (isSkill) failClosed = true` would wrongly
  // return the truncated content, since this stray opener is not itself a SKILL.md fence.
  const text = [
    "```SKILL.md",
    "frontmatter",
    "1. Build:",
    "   ```bash",
    "   npm run build",
    "```",
    "2. Push.",
    "```",
  ].join("\n");
  expect(skillBlock(skillTurn(text))).toBeNull();
});

test("skillBlock: many same-shaped inner openers resolve in roughly linear time, not quadratic", () => {
  // n lines of "```a" (each an inner opener with an info string), then one bare "```", then the
  // outer "````" closer. The first "```a" opener's own forward scan finds that lone "```" as its
  // closer and jumps straight past the rest (lines 2..n never get individually re-scanned), so
  // this is well-formed either way — what the old per-opener `.slice(i + 1).some(...)` rescan
  // went quadratic on (measured ~730 ms at n=8,000 and ~12.3 s at n=25,000 against this same
  // shape) the linear skip-ahead resolves in low single-digit milliseconds at n=25,000.
  const n = 25_000;
  const text = ["````SKILL.md", ...Array.from({ length: n }, () => "```a"), "```", "````"].join(
    "\n",
  );
  const result = skillBlock(skillTurn(text));
  expect(result).toBe(`${"```a\n".repeat(n)}\`\`\`\n`);
});

// ---- fencedBlock (the shared algorithm) ----------------------------------------------------------

test("fencedBlock: a custom info string and byte cap behave like skillBlock's own constants", () => {
  const text = ["```my-info", "hello", "```"].join("\n");
  expect(fencedBlock(skillTurn(text), "my-info", 100)).toBe("hello\n");
  expect(fencedBlock(skillTurn(text), "my-info", 3)).toBeNull(); // content over the byte cap
  expect(fencedBlock(skillTurn(text), "other-info", 100)).toBeNull(); // info string does not match
});

// ---- questionBlock --------------------------------------------------------------------------------

function questionText(obj: unknown): string {
  return ["```decision-question", JSON.stringify(obj), "```"].join("\n");
}

test("questionBlock: no fenced block returns null", () => {
  expect(questionBlock(skillTurn("just prose, no fences here"))).toBeNull();
});

test("questionBlock: a well-formed block returns its name and question", () => {
  const text = questionText({ name: "worth_note", question: { ok: true } });
  expect(questionBlock(skillTurn(text))).toEqual({
    name: "worth_note",
    question: { ok: true },
  } satisfies QuestionBlock);
});

test("questionBlock: two decision-question blocks returns null", () => {
  const text = [
    questionText({ name: "a", question: {} }),
    questionText({ name: "b", question: {} }),
  ].join("\n");
  expect(questionBlock(skillTurn(text))).toBeNull();
});

test("questionBlock: content that is not valid JSON returns null", () => {
  const text = ["```decision-question", "not json", "```"].join("\n");
  expect(questionBlock(skillTurn(text))).toBeNull();
});

test("questionBlock: a key besides name and question returns null", () => {
  const text = questionText({ name: "a", question: {}, extra: 1 });
  expect(questionBlock(skillTurn(text))).toBeNull();
});

test("questionBlock: a missing key returns null", () => {
  const text = questionText({ name: "a" });
  expect(questionBlock(skillTurn(text))).toBeNull();
});

test("questionBlock: a non-string name returns null", () => {
  const text = questionText({ name: 1, question: {} });
  expect(questionBlock(skillTurn(text))).toBeNull();
});

test("questionBlock: an array question returns null", () => {
  const text = questionText({ name: "a", question: [] });
  expect(questionBlock(skillTurn(text))).toBeNull();
});

test("questionBlock: a string question returns null", () => {
  const text = questionText({ name: "a", question: "nope" });
  expect(questionBlock(skillTurn(text))).toBeNull();
});

test("questionBlock: a null question returns null", () => {
  const text = questionText({ name: "a", question: null });
  expect(questionBlock(skillTurn(text))).toBeNull();
});

test("questionBlock: content over 8,192 UTF-8 bytes returns null, even when otherwise well-formed", () => {
  const text = questionText({ name: "a", question: { filler: "a".repeat(9_000) } });
  expect(questionBlock(skillTurn(text))).toBeNull();
});

// ---- draftPrompt -------------------------------------------------------------------------------

test("draftPrompt: without a hint there is no 'Why now' line, and a short lead is used verbatim", () => {
  const result = draftPrompt(skillTurn("fix the login bug"), null);
  expect(result).toContain('up to your reply that starts "fix the login bug".');
  expect(result).not.toContain("Why now:");
});

test("draftPrompt: with a hint, the prompt ends in a 'Why now' line built from hint.reason", () => {
  const hint: SkillHint = {
    trigger: "long_session",
    sessions: ["this"],
    line: "x",
    reason: "this chat ran a long task to the end.",
  };
  const result = draftPrompt(skillTurn("fix the login bug"), hint);
  expect(result.split("\n").at(-1)).toBe("Why now: this chat ran a long task to the end.");
});

test("draftPrompt: a lead over 60 characters is cut to 60 plus an ellipsis", () => {
  const long = "x".repeat(70);
  const result = draftPrompt(skillTurn(long), null);
  expect(result).toContain(`up to your reply that starts "${"x".repeat(60)}\u2026"`);
});

test("draftPrompt: no text block at all leaves LEAD out entirely", () => {
  const result = draftPrompt(turn("t", { blocks: [] }), null);
  expect(result.split("\n")[0]).toBe(
    "Make this a skill. Draft a reusable Claude Code skill from the work in this conversation.",
  );
});

test("draftPrompt: finds the first text block even when it is not the first block", () => {
  const t = turn("t", { blocks: [routingBlock(0), textBlock("  \nreal first line\nmore")] });
  const result = draftPrompt(t, null);
  expect(result).toContain('up to your reply that starts "real first line".');
});

test("draftPrompt: asks VIKI to wrap the file in a fence longer than any fence inside it", () => {
  const result = draftPrompt(skillTurn("fix the login bug"), null);
  expect(result).toContain("fence longer than any fence inside it");
});

test("draftPrompt: a lead cut at 60 keeps a surrogate-pair character whole, never a lone surrogate", () => {
  const prefix = "x".repeat(59);
  const emoji = "\u{1F600}"; // one code point, two UTF-16 units; straddles the old slice(0, 60) cut
  const long = `${prefix}${emoji}${"y".repeat(10)}`;
  const result = draftPrompt(skillTurn(long), null);
  expect(result).toContain(`up to your reply that starts "${prefix}${emoji}…"`);
  expect(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])/.test(result)).toBe(false);
});

// ---- second look: lookRef, changedRefs, secondLookHint ---------------------------------------

function editRow(rest: string, overrides: Partial<ToolRowView> = {}): ToolRowView {
  return { ...toolRow(`e-${rest}`), tool: "Edit", row: { head: "", rest, tail: "" }, ...overrides };
}

function turnWith(rows: ToolRowView[], overrides: Partial<Turn> = {}): Turn {
  return userTurn("last", "go", { blocks: [{ kind: "tools", id: "t", rows }], ...overrides });
}

/** A corrected chat: the second turn opens with "no,", the last turn edits `rows`. */
function correctedView(rows: ToolRowView[], overrides: Partial<Turn> = {}): SessionView {
  return view([userTurn("a", "write"), userTurn("b", "no, again"), turnWith(rows, overrides)]);
}

test.each([
  "notes/garden-plan.md",
  "a.markdown",
  "NOTES/A.MD",
  "x/y z/é.md",
  "😀/n.md",
  `${"a".repeat(509)}.md`,
])("lookRef takes %s", (rest) => {
  expect(lookRef(rest)).toBe(true);
});

test.each([
  "",
  `${"a".repeat(510)}.md`,
  "/abs/n.md",
  "C:/n.md",
  "a\\b.md",
  "a//b.md",
  "./n.md",
  "a/./n.md",
  "../n.md",
  "a/../n.md",
  "n.md/",
  "n.txt",
  "n.mdx",
  "a\u0000b.md",
  "a\nb.md",
  "a\u007fb.md",
  "a\u0085b.md",
  "a\u2028b.md",
  "a\u2029b.md",
  "a\ud800b.md",
  "a\udc00b.md",
])("lookRef refuses %j", (rest) => {
  expect(lookRef(rest)).toBe(false);
});

test("lookRef refuses a path the tool row cut short with an ellipsis", () => {
  expect(lookRef("notes/a-very-long-name\u2026")).toBe(false);
  expect(lookRef("notes/a-very-long-name.md\u2026")).toBe(false);
});

test("changedRefs keeps Edit and Write refs of tools and routing blocks, distinct, in order", () => {
  const routing: RoutingBlock = {
    ...routingBlock(0),
    tools: [editRow("sub/c.md"), editRow("a.md")],
  };
  const t = userTurn("last", "go", {
    blocks: [
      { kind: "tools", id: "t", rows: [editRow("a.md"), editRow("b.md", { tool: "Write" })] },
      routing,
    ],
  });
  expect(changedRefs(t)).toEqual(["a.md", "b.md", "sub/c.md"]);
});

test("changedRefs leaves out failed rows, other tools, and unsafe or non-note paths", () => {
  const t = turnWith([
    editRow("failed.md", { isError: true }),
    editRow("read.md", { tool: "Read" }),
    editRow("/abs.md"),
    editRow("data.json"),
    editRow("ok.md"),
  ]);
  expect(changedRefs(t)).toEqual(["ok.md"]);
});

test("changedRefs stops at 10 refs", () => {
  const rows = Array.from({ length: MAX_LOOK_REFS + 1 }, (_, i) => editRow(`n${i}.md`));
  expect(changedRefs(turnWith(rows))).toEqual(rows.slice(0, MAX_LOOK_REFS).map((r) => r.row.rest));
});

test("changedRefs stops before the ref that would pass 1,024 UTF-8 bytes in all", () => {
  const big = (i: number) => `${"x".repeat(500)}-${i}.md`; // 506 bytes each
  expect(changedRefs(turnWith([editRow(big(1)), editRow(big(2)), editRow(big(3))]))).toEqual([
    big(1),
    big(2),
  ]);
  const wide = (i: number) => `${"\u3042".repeat(167)}${i}.md`; // 3 * 167 + 4 = 505 bytes
  expect(
    changedRefs(turnWith([editRow(wide(1)), editRow(wide(2)), editRow(wide(3))])),
  ).toHaveLength(2);
});

test("secondLookHint reports a correction with the last turn's id and refs", () => {
  expect(secondLookHint(correctedView([editRow("a.md")]))).toEqual({
    trigger: "correction",
    turn: "last",
    refs: ["a.md"],
  });
});

// contract-1: a message sent while the turn ran folds into it as `<uuid>:steer`; the presence
// report drops any turn that is not a canonical UUID, so the offer was lost.
const SERVER_TURN = "0b8f3c1e-6d2a-4f7b-9c4e-2a1d5e8f7b3c";

test("secondLookHint names the server's turn when the last turn is a steered continuation", () => {
  const turnStarted = (id: string): Item => ({ type: "turn.started", data: { turn_id: id } });
  const items: Item[] = [
    userMessage("write the plan"),
    turnStarted("11111111-2222-4333-8444-555555555555"),
    turnDone("success"),
    userMessage("no, again"),
    turnStarted(SERVER_TURN),
    userMessage("also update the index note"), // sent while the turn runs: steered into it
    toolStarted("e1", "Edit", { head: "Edit", rest: "10-projects/garden/tasks.md" }),
    toolDone("e1"),
    turnDone("success"),
  ];
  const steered = reduced(items);
  expect(steered.turns.at(-1)?.id).toBe(`${SERVER_TURN}:steer`);

  expect(secondLookHint(steered)).toEqual({
    trigger: "correction",
    turn: SERVER_TURN,
    refs: ["10-projects/garden/tasks.md"],
  });
});

test.each([
  [`${SERVER_TURN}:steer`, SERVER_TURN],
  [`${SERVER_TURN}:steer:steer`, SERVER_TURN],
  [SERVER_TURN, SERVER_TURN],
  ["local-3:steer", "local-3"], // never renamed: still not a UUID, so the presence drops it
  ["a:steer:b", "a:steer:b"],
])("serverTurnId(%s) is %s", (id, expected) => {
  expect(serverTurnId(id)).toBe(expected);
});

test("secondLookHint reports a long chat, and the correction wins when both hold", () => {
  const long = view([...userTurns(LONG_TURNS - 1), turnWith([editRow("a.md")])]);
  expect(secondLookHint(long)?.trigger).toBe("long_session");
  const both = view([
    userTurn("a", "write"),
    userTurn("b", "no, again"),
    ...userTurns(LONG_TURNS),
    turnWith([editRow("a.md")]),
  ]);
  expect(secondLookHint(both)?.trigger).toBe("correction");
});

test("secondLookHint counts a long chat by tool rows too", () => {
  const rows = Array.from({ length: LONG_TOOL_ROWS }, (_, i) => editRow(`n${i}.md`));
  expect(secondLookHint(view([turnWith(rows)]))?.refs).toHaveLength(MAX_LOOK_REFS);
});

test("secondLookHint is null for a running, history or failed last turn", () => {
  for (const overrides of [
    { status: "running" as const },
    { history: true },
    { status: "error" as const },
  ]) {
    expect(secondLookHint(correctedView([editRow("a.md")], overrides))).toBeNull();
  }
});

test("secondLookHint is null without a trigger or without an admitted ref", () => {
  expect(secondLookHint(view([userTurn("a", "write"), turnWith([editRow("a.md")])]))).toBeNull();
  expect(secondLookHint(correctedView([]))).toBeNull();
  expect(secondLookHint(correctedView([editRow("a.md", { isError: true })]))).toBeNull();
  expect(secondLookHint(correctedView([editRow("/a.md"), editRow("cut\u2026")]))).toBeNull();
});
