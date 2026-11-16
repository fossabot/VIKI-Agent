/**
 * Unit tests for components/Sources/saveAsNote.ts: replyWords' blank-line join and trim, the
 * title extracted from a reply's first line (every stripped marker and character, the 60-code-
 * point cut, and its `Saved reply` fallback), the ref's own `validWriteRef` fallback, the
 * `## Sources` line for every source kind, the hand-off message's shape, and null with no words.
 */
import { expect, test } from "vitest";
import type {
  ToolSource,
  ToolSourceGrepGlob,
  ToolSourceWebFetch,
} from "../../clients/conversations/conversationSearchApi";
import type { Source } from "../../state/sources";
import type { Block, TextBlock, Turn } from "../../state/view";
import { handoffText, noteDraft, replyWords } from "./saveAsNote";

const TODAY = "2026-10-09";

function turn(blocks: Block[], id = "t1"): Turn {
  return { id, user: null, blocks, status: "success", usage: null, durationMs: 0, history: false };
}

function textBlock(text: string, id = "b1"): TextBlock {
  return { kind: "text", id, text, done: true };
}

/** A turn whose only block is this one (possibly multi-line) piece of reply text. */
function replyTurn(text: string): Turn {
  return turn([textBlock(text)]);
}

function noteSource(label: string, ref: string, toolUseId = "n1"): Source {
  return { kind: "note", key: `note:${ref}`, label, ref, toolUseId, tool: "Read", agent: "" };
}
function searchSource(label: string, toolUseId: string): Source {
  return {
    kind: "search",
    key: `search:${toolUseId}`,
    label,
    ref: "",
    toolUseId,
    tool: "Grep",
    agent: "",
  };
}
function webSource(label: string, toolUseId: string): Source {
  return {
    kind: "web",
    key: `web:${toolUseId}`,
    label,
    ref: "",
    toolUseId,
    tool: "WebFetch",
    agent: "",
  };
}
function grepMatch(
  toolUseId: string,
  pattern: string,
  path: string,
  pathState: ToolSourceGrepGlob["path_state"] = "workspace_relative",
): [string, ToolSource] {
  return [
    toolUseId,
    { tool_use_id: toolUseId, tool: "Grep", pattern, path, path_state: pathState },
  ];
}
function webMatch(toolUseId: string, url: string): [string, ToolSource] {
  const entry: ToolSourceWebFetch = { tool_use_id: toolUseId, tool: "WebFetch", url };
  return [toolUseId, entry];
}

// --- replyWords ----------------------------------------------------------------------------

test("replyWords joins text blocks with a blank line and skips other block kinds", () => {
  const withNotice = turn([
    textBlock("Hello", "b1"),
    { kind: "notice", id: "n1", tone: "hook", subject: "x", detail: "y" },
    textBlock("World", "b2"),
  ]);
  expect(replyWords(withNotice)).toBe("Hello\n\nWorld");
});

test("replyWords trims the outer whitespace of the joined words", () => {
  expect(replyWords(replyTurn("  Hello  "))).toBe("Hello");
});

test("replyWords is empty with no text blocks", () => {
  expect(replyWords(turn([]))).toBe("");
});

// --- title edge cases ------------------------------------------------------------------------

test.each([
  ["# Deny rules hold even in bypass mode", "Deny rules hold even in bypass mode"],
  ["## Second-level heading", "Second-level heading"],
  ["> A quoted first line", "A quoted first line"],
  ["- A bullet first line", "A bullet first line"],
  ["* A starred first line", "A starred first line"],
  ["1. A numbered first line", "A numbered first line"],
  ['Weird*Title_Name`[x]\\/:?"<>|End', "WeirdTitleNamexEnd"],
  ["Weird\u0007Control\u0000Chars", "WeirdControlChars"],
  ["Trailing dots and spaces....   ", "Trailing dots and spaces"],
  ["Plain title needs nothing stripped", "Plain title needs nothing stripped"],
])("titles the draft from the reply's first line: %j", (firstLine, expected) => {
  const draft = noteDraft(replyTurn(firstLine), [], new Map(), TODAY);
  expect(draft?.ref).toBe(`${expected}.md`);
  expect(draft?.content.startsWith(`# ${expected}\n`)).toBe(true);
});

test("cuts a long title to 60 code points at the last space", () => {
  const words = ["A", "B", "C", "D", "E", "F"].map((c) => c.repeat(10)).join(" ");
  const expected = ["A", "B", "C", "D", "E"].map((c) => c.repeat(10)).join(" ");
  const draft = noteDraft(replyTurn(words), [], new Map(), TODAY);
  expect(draft?.ref).toBe(`${expected}.md`);
});

test("hard-cuts to 60 code points when the first line has no space to break at", () => {
  const draft = noteDraft(replyTurn("X".repeat(70)), [], new Map(), TODAY);
  expect(draft?.ref).toBe(`${"X".repeat(60)}.md`);
});

test.each(["### ", "????", "...."])(
  "falls back to Saved reply %s when the first line leaves nothing usable",
  (firstLine) => {
    const draft = noteDraft(replyTurn(firstLine), [], new Map(), TODAY);
    expect(draft?.ref).toBe(`Saved reply ${TODAY}.md`);
    expect(draft?.content.startsWith(`# Saved reply ${TODAY}\n`)).toBe(true);
  },
);

test("falls back the ref, but not the title, when the title would not pass validWriteRef", () => {
  const draft = noteDraft(replyTurn("CON"), [], new Map(), TODAY);
  expect(draft?.ref).toBe(`Saved reply ${TODAY}.md`);
  expect(draft?.content.startsWith("# CON\n")).toBe(true);
});

// --- sources section -------------------------------------------------------------------------

test("lists a note, resolved and unresolved search, and resolved and unresolved web sources", () => {
  const sources = [
    noteSource("claim", "30-knowledge/claim.md", "b1"),
    searchSource("TODO src/", "b2"),
    searchSource("**/*.md", "b3"),
    webSource("example.com", "b4"),
    webSource("example.org", "b5"),
    searchSource("TODO docs/", "b6"),
  ];
  const resolved = new Map([
    grepMatch("b2", "TODO", "src", "workspace_relative"),
    grepMatch("b3", "**/*.md", "", "root"),
    webMatch("b4", "https://example.com/page"),
  ]);
  const draft = noteDraft(replyTurn("Body text for the sources test"), sources, resolved, TODAY);
  const lines = draft?.content.split("## Sources\n")[1]?.split("\n") ?? [];
  expect(lines).toEqual([
    "- [[claim]]",
    '- Searched src for "TODO"',
    '- Searched the whole vault for "**/*.md"',
    "- https://example.com/page",
    "- A page on example.org",
    "- Searched: TODO docs/",
  ]);
});

test("a turn with no sources gets the 'nothing read' sentence instead of a list", () => {
  const draft = noteDraft(replyTurn("Body text"), [], new Map(), TODAY);
  expect(draft?.content.split("## Sources\n")[1]).toBe(
    "No vault note or web page was read for this reply.",
  );
});

// --- hand-off text -----------------------------------------------------------------------------

test("the hand-off asks VIKI to save the reply, lists sources with the note's ref, then fences the words", () => {
  const sources = [
    noteSource("claim", "30-knowledge/claim.md", "b1"),
    webSource("example.org", "b2"),
  ];
  const words = "Here is the reply, with a ```fenced``` aside.";
  const text = handoffText(replyTurn(words), sources, new Map());
  expect(text).toBe(
    [
      "Please save the reply below as a knowledge note in this vault. Follow the vault's own note conventions, and link the sources listed.",
      "",
      "Sources read for this reply:",
      "- [[claim]] (30-knowledge/claim.md)",
      "- A page on example.org",
      "",
      "Reply:",
      "",
      "````markdown",
      words,
      "````",
    ].join("\n"),
  );
});

test("the hand-off's sources section also falls back to the 'nothing read' sentence", () => {
  const text = handoffText(replyTurn("Body text"), [], new Map());
  expect(text).toBe(
    [
      "Please save the reply below as a knowledge note in this vault. Follow the vault's own note conventions, and link the sources listed.",
      "",
      "Sources read for this reply:",
      "No vault note or web page was read for this reply.",
      "",
      "Reply:",
      "",
      "```markdown",
      "Body text",
      "```",
    ].join("\n"),
  );
});

// --- no words → null -------------------------------------------------------------------------

test("noteDraft and handoffText are both null when the turn has no words", () => {
  const empty = turn([]);
  const blank = replyTurn("   ");
  expect(noteDraft(empty, [], new Map(), TODAY)).toBeNull();
  expect(noteDraft(blank, [], new Map(), TODAY)).toBeNull();
  expect(handoffText(empty, [], new Map())).toBeNull();
  expect(handoffText(blank, [], new Map())).toBeNull();
});
