/**
 * Hints that nudge the user toward "make this a skill", and the drafting prompt that follows
 * one. Heuristics only: the server re-validates whatever VIKI Agent turns a reply into.
 */
import type { SessionRow } from "../protocol";
import type { Block, SessionView, TextBlock, Turn } from "./view";

export const LONG_TURNS = 8;
export const LONG_TOOL_ROWS = 40;
export const SIMILAR = 0.6;

export interface SkillHint {
  trigger: "correction" | "repeated_request" | "long_session";
  /** The chat ids this hint is about: this chat alone for correction and long_session; for
   * repeated_request, this chat plus the matching chats by newest `modified`, at most 10 total. */
  sessions: string[];
  line: string;
  reason: string;
}

const CORRECTION_PREFIXES = [
  "no,",
  "no.",
  "no ",
  "nope",
  "not like that",
  "not that",
  "that's not",
  "that is not",
  "wrong",
  "don't",
  "do not",
  "stop",
  "instead",
  "actually",
  // Japanese correction openers ("that is not it" / "no," in a few registers), as
  // Unicode escapes, never literals, so this file keeps English-only source text.
  "\u9055\u3046",
  "\u9055\u3044\u307e\u3059",
  "\u3061\u304c\u3046",
  "\u305d\u3046\u3058\u3083\u306a",
  "\u305d\u3046\u3067\u306f\u306a",
  "\u3044\u3084\u3001",
];

const FENCE_OPEN_RE = /^(`{3,}|~{3,})(.*)$/;
const MAX_SKILL_BYTES = 131_072;
const SKILL_INFO = "SKILL.md";
const MAX_QUESTION_BYTES = 8_192;
const QUESTION_INFO = "decision-question";

/** The last turn's id when it is not running, else null. */
export function lastFinishedTurnId(view: SessionView): string | null {
  const turn = view.turns.at(-1);
  return turn && turn.status !== "running" ? turn.id : null;
}

function isCorrection(text: string): boolean {
  const lowered = text.trim().toLowerCase();
  return CORRECTION_PREFIXES.some((prefix) => lowered.startsWith(prefix));
}

/** A user turn before the last one opens with a correction phrase; the last turn (already known
 * to have succeeded, by `skillHint`'s own gate) stands as the later turn that succeeded. */
function correctionHint(view: SessionView): SkillHint | null {
  const turns = view.turns;
  for (let i = 1; i < turns.length - 1; i++) {
    const user = turns[i]?.user;
    if (user && isCorrection(user.text)) {
      return {
        trigger: "correction",
        sessions: [view.sessionId],
        line: "You corrected VIKI in this chat. A skill can keep the fix.",
        reason: "I corrected you in this chat; keep the corrected way.",
      };
    }
  }
  return null;
}

function isCjk(ch: string): boolean {
  return /\p{Script=Han}|\p{Script=Hiragana}|\p{Script=Katakana}/u.test(ch);
}
const WORD_CHAR_RE = /[\p{L}\p{N}]/u;

/** NFKC, lower-cased text split into word tokens (>=2 `\p{L}\p{N}` chars), except Han/Hiragana/
 * Katakana runs, which become overlapping character bigrams instead of whole-run tokens. */
function tokenize(text: string): string[] {
  const tokens: string[] = [];
  let run = "";
  let runIsCjk = false;
  const flush = () => {
    if (run.length === 0) return;
    if (runIsCjk) {
      for (let i = 0; i < run.length - 1; i++) tokens.push(run.slice(i, i + 2));
    } else if (run.length >= 2) {
      tokens.push(run);
    }
    run = "";
  };
  for (const ch of text.normalize("NFKC").toLowerCase()) {
    const cjk = isCjk(ch);
    const word = cjk || WORD_CHAR_RE.test(ch);
    if (!word) {
      flush();
      continue;
    }
    if (run !== "" && cjk !== runIsCjk) flush();
    run += ch;
    runIsCjk = cjk;
  }
  flush();
  return tokens;
}

function jaccard(a: readonly string[], b: readonly string[]): number {
  const setA = new Set(a);
  const setB = new Set(b);
  let intersection = 0;
  for (const token of setA) if (setB.has(token)) intersection++;
  const union = setA.size + setB.size - intersection;
  return union === 0 ? 0 : intersection / union;
}

/** This chat's first prompt (its own `SessionRow`, else its first turn's user text) matches at
 * least 2 of the up-to-10 other rows (newest `modified` first), skipping empty or slash prompts. */
function repeatedRequestHint(view: SessionView, sessions: readonly SessionRow[]): SkillHint | null {
  const thisRow = sessions.find((row) => row.session_id === view.sessionId);
  const pivotText = thisRow ? thisRow.first_prompt : (view.turns[0]?.user?.text ?? "");
  const pivotTokens = tokenize(pivotText);
  if (pivotTokens.length < 3) return null;

  const others = sessions
    .filter((row) => row.session_id !== view.sessionId)
    .sort((a, b) => parseModified(b.modified) - parseModified(a.modified))
    .slice(0, 10);

  const matches: string[] = [];
  for (const row of others) {
    const prompt = row.first_prompt.trim();
    if (prompt === "" || prompt.startsWith("/")) continue;
    if (jaccard(pivotTokens, tokenize(prompt)) >= SIMILAR) matches.push(row.session_id);
  }
  if (matches.length < 2) return null;

  const ids = [view.sessionId, ...matches].slice(0, 10);
  const n = ids.length;
  return {
    trigger: "repeated_request",
    sessions: ids,
    line: `You started ${n} chats like this one. A skill can save repeating it.`,
    reason: `I started ${n} chats with a similar first message.`,
  };
}

function parseModified(iso: string): number {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? -Infinity : t;
}

function toolRowCount(view: SessionView): number {
  let count = 0;
  for (const turn of view.turns) {
    for (const block of turn.blocks) {
      if (block.kind === "tools") count += block.rows.length;
      else if (block.kind === "routing") count += block.tools.length;
    }
  }
  return count;
}

function longSessionHint(view: SessionView): SkillHint | null {
  const userTurns = view.turns.filter((turn) => turn.user !== null).length;
  if (userTurns < LONG_TURNS && toolRowCount(view) < LONG_TOOL_ROWS) return null;
  return {
    trigger: "long_session",
    sessions: [view.sessionId],
    line: "This chat ran a long task to the end. Worth keeping as a skill?",
    reason: "this chat ran a long task to the end.",
  };
}

/** Null unless the last turn is live (not from history) and succeeded; else the first of
 * correction, repeated_request, long_session (in that order) that fires. */
export function skillHint(view: SessionView, sessions: readonly SessionRow[]): SkillHint | null {
  const last = view.turns.at(-1);
  if (!last || last.history || last.status !== "success") return null;
  return correctionHint(view) ?? repeatedRequestHint(view, sessions) ?? longSessionHint(view);
}

export const MAX_LOOK_REFS = 10;
export const MAX_LOOK_REF_BYTES = 1024;
const MAX_LOOK_REF_CHARS = 512;
// \p{Cs} with the `u` flag matches only an unpaired surrogate, so a paired one passes.
const UNSAFE_REF_RE = /[\p{Cc}\p{Cs}\p{Zl}\p{Zp}]/u;
const NOTE_SUFFIX_RE = /\.(?:md|markdown)$/i;

/** True when a tool row's `rest` can ride as a second-look note ref: a relative path of 1 to 512
 * characters that names a Markdown note and needs no cleanup. "…" at its end marks a path the tool
 * row cut short, which would name the wrong file. The server re-checks the real path. */
export function lookRef(rest: string): boolean {
  if (rest.length === 0 || Array.from(rest).length > MAX_LOOK_REF_CHARS) return false;
  if (rest.endsWith("…") || UNSAFE_REF_RE.test(rest)) return false;
  if (rest.includes("\\") || rest.includes(":")) return false;
  if (rest.split("/").some((part) => part === "" || part === "." || part === "..")) return false;
  return NOTE_SUFFIX_RE.test(rest);
}

/** The turn's distinct note refs from successful Edit and Write rows (main thread and subagent),
 * in order, at most `MAX_LOOK_REFS` and `MAX_LOOK_REF_BYTES` UTF-8 bytes in all: it stops at the
 * first ref that would pass either cap. */
export function changedRefs(turn: Turn): string[] {
  const refs: string[] = [];
  const encoder = new TextEncoder();
  let bytes = 0;
  for (const block of turn.blocks) {
    if (block.kind !== "tools" && block.kind !== "routing") continue;
    for (const row of block.kind === "tools" ? block.rows : block.tools) {
      const ref = row.row.rest;
      if ((row.tool !== "Edit" && row.tool !== "Write") || row.isError) continue;
      if (!lookRef(ref) || refs.includes(ref)) continue;
      bytes += encoder.encode(ref).length;
      if (refs.length >= MAX_LOOK_REFS || bytes > MAX_LOOK_REF_BYTES) return refs;
      refs.push(ref);
    }
  }
  return refs;
}

/** The server's id for the turn a client turn id belongs to. A message sent while a turn runs
 * opens its continuation as `<id>:steer` (sessionReducer); the CLI announces no turn for it, so
 * on the server it is still the turn `<id>` names. */
export function serverTurnId(id: string): string {
  return id.replace(/(?::steer)+$/, "");
}

/** The chat's second-look trigger, or null: the last turn is live and succeeded, the chat was
 * corrected (first) or is long, and that turn changed at least one note. `turn` is the server's
 * id for it (`serverTurnId`). */
export function secondLookHint(
  view: SessionView,
): { trigger: "correction" | "long_session"; turn: string; refs: string[] } | null {
  const last = view.turns.at(-1);
  if (!last || last.history || last.status !== "success") return null;
  const trigger = correctionHint(view) ? "correction" : longSessionHint(view) && "long_session";
  const refs = changedRefs(last);
  if (!trigger || refs.length === 0) return null;
  return { trigger, turn: serverTurnId(last.id), refs };
}

function isCloseLine(line: string, fenceChar: string, minLen: number): boolean {
  const re = fenceChar === "`" ? /^(`+)([ \t]*)$/ : /^(~+)([ \t]*)$/;
  const match = re.exec(line);
  return match !== null && match[1] !== undefined && match[1].length >= minLen;
}

/** True when `lines` — a candidate SKILL.md block's own content — holds a fence opener with a
 * non-empty info string (for example ```bash) that never closes within those same lines: a sign
 * the block's apparent closing fence actually belongs to that inner opener, not to the block.
 * One linear pass: after an opener, jump straight to its closer (or bail) the way the main loop
 * does, instead of rescanning the tail for every opener (which went quadratic on long input). */
function hasDanglingInnerOpener(lines: readonly string[]): boolean {
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const open = line === undefined ? null : FENCE_OPEN_RE.exec(line);
    if (!open || open[1] === undefined || (open[2] ?? "").trim() === "") continue;
    const fenceRun = open[1];
    const fenceChar = fenceRun[0] ?? "`";
    let j = i + 1;
    while (j < lines.length && !isCloseLine(lines[j] ?? "", fenceChar, fenceRun.length)) j++;
    if (j === lines.length) return true;
    i = j;
  }
  return false;
}

/** The turn's text blocks, joined and scanned for fenced code blocks. Exactly one complete block
 * whose info string equals `info` (trimmed) returns its content (plus a final newline); any other
 * fence is skipped whole (a matching opener inside it is just content). None, two, an unclosed
 * fence of any kind, a matching block whose own lines leave one of its inner openers dangling, or
 * content over `maxBytes` UTF-8 bytes all return null: fail closed whenever the fence structure is
 * ambiguous. `skillBlock` and `questionBlock` are this same algorithm, fixed to their own info
 * string and byte cap. */
export function fencedBlock(turn: Turn, info: string, maxBytes: number): string | null {
  const text = turn.blocks
    .filter((block): block is TextBlock => block.kind === "text")
    .map((block) => block.text)
    .join("\n");
  const lines = text.split("\n");

  const completeBlocks: string[][] = [];
  let failClosed = false;

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const open = line === undefined ? null : FENCE_OPEN_RE.exec(line);
    if (!open || open[1] === undefined) {
      i++;
      continue;
    }
    const fenceRun = open[1];
    const fenceChar = fenceRun[0] ?? "`";
    const isMatch = (open[2] ?? "").trim() === info;

    let closeIndex = -1;
    for (let j = i + 1; j < lines.length; j++) {
      const candidate = lines[j];
      if (candidate !== undefined && isCloseLine(candidate, fenceChar, fenceRun.length)) {
        closeIndex = j;
        break;
      }
    }

    if (closeIndex === -1) {
      failClosed = true;
      break;
    }
    if (isMatch) {
      const content = lines.slice(i + 1, closeIndex);
      if (hasDanglingInnerOpener(content)) {
        failClosed = true;
        break;
      }
      completeBlocks.push(content);
    }
    i = closeIndex + 1;
  }

  if (failClosed || completeBlocks.length !== 1) return null;
  const content = `${completeBlocks[0]?.join("\n") ?? ""}\n`;
  return new TextEncoder().encode(content).length > maxBytes ? null : content;
}

/** `fencedBlock` fixed to one complete `SKILL.md` block, at most `MAX_SKILL_BYTES`. */
export function skillBlock(turn: Turn): string | null {
  return fencedBlock(turn, SKILL_INFO, MAX_SKILL_BYTES);
}

/** One parsed `decision-question` block: the reply contract the question_tune draft asks for. */
export interface QuestionBlock {
  name: string;
  question: Record<string, unknown>;
}

/** Null unless `fencedBlock` finds exactly one `decision-question` block (at most
 * `MAX_QUESTION_BYTES`) and its content is JSON holding exactly the two keys `name` (a string)
 * and `question` (an object, not an array or null): the server's own `check_question` is what
 * validates the wording itself. */
export function questionBlock(turn: Turn): QuestionBlock | null {
  const content = fencedBlock(turn, QUESTION_INFO, MAX_QUESTION_BYTES);
  if (content === null) return null;

  let parsed: unknown;
  try {
    parsed = JSON.parse(content);
  } catch {
    return null;
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  const keys = Object.keys(parsed as Record<string, unknown>);
  if (keys.length !== 2 || !keys.includes("name") || !keys.includes("question")) return null;

  const { name, question } = parsed as { name: unknown; question: unknown };
  if (typeof name !== "string") return null;
  if (question === null || typeof question !== "object" || Array.isArray(question)) return null;
  return { name, question: question as Record<string, unknown> };
}

function firstLead(blocks: readonly Block[]): string | null {
  const block = blocks.find((candidate): candidate is TextBlock => candidate.kind === "text");
  if (!block) return null;
  for (const rawLine of block.text.split("\n")) {
    const line = rawLine.trim();
    if (line === "") continue;
    const codePoints = Array.from(line);
    return codePoints.length > 60 ? `${codePoints.slice(0, 60).join("")}\u2026` : line;
  }
  return null;
}

/** The skill-drafting prompt: what the turn's work was (cut to its first text block's first
 * line when there is one), the SKILL.md reply contract, and why now when there is a hint. */
export function draftPrompt(turn: Turn, hint: SkillHint | null): string {
  const lead = firstLead(turn.blocks);
  const leadPart = lead === null ? "" : ` up to your reply that starts "${lead}"`;
  const lines = [
    `Make this a skill. Draft a reusable Claude Code skill from the work in this conversation${leadPart}.`,
    "Reply with exactly one fenced code block whose info string is SKILL.md, holding the whole file:",
    "Wrap it in a fence longer than any fence inside it (for example four backticks), so a bare inner fence cannot cut it short.",
    "- first the frontmatter between --- lines, with name (lowercase letters, digits and single hyphens, at most 64 characters) and description (one line of at most 1,536 characters: what it does and when to use it);",
    "- then the steps, written so that a new session can follow them without this conversation.",
    "If this work refines one of your existing skills, give that skill's whole updated file under its existing name.",
    "Do not create or change any file: VIKI Agent turns your block into a proposal that I review in Approvals.",
  ];
  if (hint) lines.push(`Why now: ${hint.reason}`);
  return lines.join("\n");
}
