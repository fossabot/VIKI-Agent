import type { VoiceServerMessage } from "./dictationTypes";

type PartialMessage = Extract<VoiceServerMessage, { type: "partial" }>;
type FinalMessage = Extract<VoiceServerMessage, { type: "final" }>;

interface Segment {
  revision: number;
  text: string;
}

// The draft belongs to the user. A capture owns only an anchor into it and the finals it has
// inserted there; every edit the user makes moves the anchor, never the other way round.
export interface DictationState {
  capture: string | null;
  anchor: number;
  lastFinal: number;
  segments: ReadonlyMap<number, Segment>;
}

export const IDLE: DictationState = {
  capture: null,
  anchor: 0,
  lastFinal: -1,
  segments: new Map(),
};

export function begin(capture: string, anchor: number): DictationState {
  return { capture, anchor, lastFinal: -1, segments: new Map() };
}

export function partial(state: DictationState, m: PartialMessage): DictationState {
  if (state.capture !== m.capture_id || m.segment <= state.lastFinal) return state;
  const known = state.segments.get(m.segment);
  if (known && m.revision <= known.revision) return state;
  const segments = new Map(state.segments);
  segments.set(m.segment, { revision: m.revision, text: m.text });
  return { ...state, segments };
}

export function final(
  state: DictationState,
  m: FinalMessage,
  draft: string,
): { state: DictationState; draft: string } | null {
  if (state.capture !== m.capture_id || m.segment <= state.lastFinal) return null;
  const segments = new Map([...state.segments].filter(([segment]) => segment > m.segment));
  const closed = { ...state, lastFinal: m.segment, segments };
  const text = m.text.trim();
  if (text === "") return { state: closed, draft };
  const inserted = insertAt(draft, state.anchor, text);
  return { state: { ...closed, anchor: inserted.anchor }, draft: inserted.draft };
}

export function edit(state: DictationState, before: string, after: string): DictationState {
  if (state.capture === null) return state;
  const anchor = rebase(state.anchor, before, after);
  return anchor === state.anchor ? state : { ...state, anchor };
}

export function finish(_state: DictationState): DictationState {
  return IDLE;
}

export function provisionalText(state: DictationState): string {
  let joined = "";
  for (const [, { text }] of [...state.segments].sort(([a], [b]) => a - b)) {
    if (text === "") continue;
    joined += (joined !== "" && spaceBetween(lastChar(joined), firstChar(text)) ? " " : "") + text;
  }
  return joined;
}

// The one changed region is found from the common prefix and suffix, so an edit before the
// anchor shifts it, an edit after leaves it, and an edit touching it puts it after the new text.
export function rebase(anchor: number, before: string, after: string): number {
  const shortest = Math.min(before.length, after.length);
  let prefix = 0;
  while (prefix < shortest && before[prefix] === after[prefix]) prefix++;
  let suffix = 0;
  while (
    suffix < shortest - prefix &&
    before[before.length - 1 - suffix] === after[after.length - 1 - suffix]
  ) {
    suffix++;
  }
  const oldEnd = before.length - suffix;
  const newEnd = after.length - suffix;
  if (anchor < prefix) return anchor;
  if (anchor > oldEnd) return anchor + newEnd - oldEnd;
  return newEnd;
}

export function insertAt(
  draft: string,
  anchor: number,
  text: string,
): { draft: string; anchor: number } {
  const at = Math.max(0, Math.min(anchor, draft.length));
  const left = draft.slice(0, at);
  const right = draft.slice(at);
  const before = left !== "" && spaceBetween(lastChar(left), firstChar(text)) ? " " : "";
  const after = right !== "" && spaceBetween(lastChar(text), firstChar(right)) ? " " : "";
  return {
    draft: left + before + text + after + right,
    anchor: at + before.length + text.length,
  };
}

// Scripts that separate words with spaces. Kanji and kana do not, so they get none.
const SPACED = /^[\p{Script=Latin}\p{Script=Cyrillic}\p{Script=Greek}\p{Script=Hangul}\p{Nd}]$/u;
const CLOSING = new Set([".", ",", "!", "?", ";", ":", ")", "]", '"', "'"]);

function spaceBetween(left: string, right: string): boolean {
  return (SPACED.test(left) || CLOSING.has(left)) && SPACED.test(right);
}

function firstChar(s: string): string {
  return String.fromCodePoint(s.codePointAt(0) ?? 0);
}

function lastChar(s: string): string {
  return [...s.slice(-2)].at(-1) ?? "";
}
