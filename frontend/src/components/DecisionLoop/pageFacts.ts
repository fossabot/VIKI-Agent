/**
 * The page's own facts as a `PageFacts`, and
 * the run to open from "Show run". The chat in view's context share and what its
 * last reply fetched ride along as `context` and `sources`. Pure and React-free; `usePresence` calls
 * `pageFacts` and sends its result as a presence report's `page`.
 */
import type { PageFacts as PageFactsView } from "../../clients/decisionLoop/types";
import type { GitChip, SessionRow } from "../../protocol";
import { collectDecisions } from "../../state/decisions";
import type { RoutinesSnapshot } from "../../state/routines";
import { titleIsPrompt } from "../../state/rows";
import { groupRuns } from "../../state/runGroups";
import { secondLookHint, serverTurnId, skillHint } from "../../state/skillTriggers";
import { turnSources } from "../../state/sources";
import type { SessionView } from "../../state/view";

export type PageInputs = {
  sessions: Readonly<Record<string, SessionView>>;
  reviewed: ReadonlySet<string>;
  git: GitChip;
  railsStatus: "loading" | "ready" | "error";
  routines: RoutinesSnapshot;
  routinesEnabled: boolean;
  /** The open chat to check for a "make this a skill" hint and a skill suggestion, the rows to
   * match them against, and whether self-improvement hints are off. Omitted, a null `view`, or
   * `off` all report no hint and no suggestion (`skillFacts` and `suggestFacts` below) — the
   * caller passes the active chat only for a vault workspace, not after Not now. */
  skill?: {
    view: SessionView | null;
    rows: readonly SessionRow[];
    off: boolean;
  };
  /** The open vault chat to check for a second-look trigger, and the rows to name it from.
   * Omitted or a null `view` reports none (`lookFacts`, `contextFacts`, `sourceFacts`); unlike
   * `skill`, Not now does not null it. */
  look?: {
    view: SessionView | null;
    rows: readonly SessionRow[];
  };
};

const MAX_SKILL_TITLE = 120;
const MAX_SUGGEST_USED = 32;
/** The vault's own skill-name shape (`native_assets.SKILL_NAME`,
 * src/viki_agent/profiles/native_assets.py): lowercase letters, digits and single hyphens, kept
 * together with the 64-character cap that pattern's own caller also enforces. */
const SKILL_NAME_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const MAX_SKILL_NAME = 64;

/** A control character (Cc: U+0000-U+001F, U+007F-U+009F) or a lone surrogate (Cs: unpaired, so
 * `codePointAt` on it answers its own 0xD800-0xDFFF value rather than a combined code point) —
 * the same two classes `usePresence.ts`'s own chat-title clipping drops for presence; ported
 * locally since this file stays pure and React-free. `ch` is one element of `Array.from(text)`,
 * so a valid surrogate pair already reads as a single (non-surrogate-range) code point here. */
function isUnsafeCodePoint(ch: string): boolean {
  const code = ch.codePointAt(0) ?? 0;
  return code <= 0x1f || (code >= 0x7f && code <= 0x9f) || (code >= 0xd800 && code <= 0xdfff);
}

/** `raw` with every unsafe code point removed, then cut to `MAX_SKILL_TITLE` code points. */
function sanitizeSkillTitle(raw: string): string {
  return Array.from(raw)
    .filter((ch) => !isUnsafeCodePoint(ch))
    .slice(0, MAX_SKILL_TITLE)
    .join("");
}

/** The "make this a skill" hint to report in page facts, or null while there is none: no `skill`
 * input, no open chat (`view` null), hints off, or `skillHint` itself finds nothing. Titles line
 * up with the hint's own `sessions`, one per id — the matching row's title, or "" when none
 * matches or the row's own title is just its first-prompt fallback (`titleIsPrompt`: the same
 * withholding `App.tsx::presenceChatTitle` already applies to the chat title it sends)
 * — sanitized and clipped the way a chat title already is for presence (`usePresence.ts`). */
function skillFacts(skill: PageInputs["skill"]): PageFactsView["skill"] {
  if (!skill || skill.off || !skill.view) return null;
  const hint = skillHint(skill.view, skill.rows);
  if (!hint) return null;
  const titleById = new Map(
    skill.rows.map((row) => [row.session_id, titleIsPrompt(row) ? "" : row.title]),
  );
  return {
    trigger: hint.trigger,
    sessions: hint.sessions,
    titles: hint.sessions.map((id) => sanitizeSkillTitle(titleById.get(id) ?? "")),
  };
}

/** The open chat's second-look trigger to report, or null while there is none: no `look` input, no
 * open chat, or `secondLookHint` finds nothing. `title` is the matching row's, withheld ("") when
 * it is just the first prompt and sanitized the way `skillFacts` does. */
function lookFacts(look: PageInputs["look"]): PageFactsView["second_look"] {
  const view = look?.view;
  if (!look || !view) return null;
  const hint = secondLookHint(view);
  if (!hint) return null;
  const row = look.rows.find((candidate) => candidate.session_id === view.sessionId);
  const title = row && !titleIsPrompt(row) ? sanitizeSkillTitle(row.title) : "";
  return { session: view.sessionId, ...hint, title };
}

/** The open chat's context share to report, or null while there is none: no `look` input, no open
 * chat, or a `contextPercent` that is not a whole number from 0 to 100. The client sends no
 * threshold; the server decides what counts as nearly full. */
function contextFacts(look: PageInputs["look"]): PageFactsView["context"] {
  const view = look?.view;
  const percent = view?.contextPercent;
  if (!view || percent == null || !Number.isInteger(percent) || percent < 0 || percent > 100) {
    return null;
  }
  return { session: view.sessionId, percent };
}

/** What the open chat's last reply fetched, or null while there is none: no `look` input, no open
 * chat, the last turn not live (from history) or not succeeded, or no fetched web page in it.
 * `notes` is whether that reply also read a vault note. Only ids and a boolean — no URL or ref. */
function sourceFacts(look: PageInputs["look"]): PageFactsView["sources"] {
  const view = look?.view;
  const last = view?.turns.at(-1);
  if (!view || !last || last.history || last.status !== "success") return null;
  const sources = turnSources(last, true);
  if (!sources.some((source) => source.kind === "web")) return null;
  return {
    session: view.sessionId,
    // A steered reply's client id is `<id>:steer`; the server knows the turn as `<id>`.
    turn: serverTurnId(last.id),
    notes: sources.some((source) => source.kind === "note"),
  };
}

/** True when `name` fits the vault's own skill-name shape and length cap (`SKILL_NAME_RE`,
 * `MAX_SKILL_NAME`). */
function fitsSkillName(name: string): boolean {
  return name.length <= MAX_SKILL_NAME && SKILL_NAME_RE.test(name);
}

/** The vault skill names already run in this chat: a Skill tool call's own name (`row.row.rest`,
 * trimmed) from a "tools" or "routing" block, or a slash command (`user.command` less its leading
 * "/"). Kept when they fit `fitsSkillName`, distinct, first-seen order (oldest turn first; within
 * a turn, its own prompt before its blocks), at most `MAX_SUGGEST_USED`. */
function usedSkillNames(view: SessionView): string[] {
  const used: string[] = [];
  const seen = new Set<string>();
  const add = (name: string) => {
    if (used.length >= MAX_SUGGEST_USED || seen.has(name) || !fitsSkillName(name)) return;
    seen.add(name);
    used.push(name);
  };
  for (const turn of view.turns) {
    if (turn.user?.command) add(turn.user.command.slice(1));
    for (const block of turn.blocks) {
      if (block.kind !== "tools" && block.kind !== "routing") continue;
      const rows = block.kind === "tools" ? block.rows : block.tools;
      for (const row of rows) {
        if (row.tool === "Skill") add(row.row.rest.trim());
      }
    }
  }
  return used;
}

/** The open chat's own facts for a possible skill suggestion, or null while there is none: no
 * `skill` input, no open chat (`view` null), hints off, the last turn not live (from history) or
 * not succeeded (still running or failed), or a blank title. `title` is the matching session
 * row's title, sanitized and clipped the same way `skillFacts` already does; `used` is
 * `usedSkillNames` over the whole chat. */
function suggestFacts(skill: PageInputs["skill"]): PageFactsView["suggest"] {
  if (!skill || skill.off || !skill.view) return null;
  const view = skill.view;
  const last = view.turns.at(-1);
  if (!last || last.history || last.status !== "success") return null;
  const row = skill.rows.find((candidate) => candidate.session_id === view.sessionId);
  const title = sanitizeSkillTitle(row?.title ?? "");
  if (title === "") return null;
  return { session: view.sessionId, title, used: usedSkillNames(view) };
}

/** The page's current state as a `PageFacts`, or null while a watched source has not settled:
 * rails still loading, or routines enabled but their store still idle or loading. `force` skips
 * that wait; a source that is still unsettled then comes through as its own "not known yet"
 * shape (git `unknown` with no dirty count, no routine) rather than blocking the page any
 * longer. */
export function pageFacts(
  inputs: PageInputs,
  { force = false }: { force?: boolean } = {},
): PageFactsView | null {
  const routinesUnsettled =
    inputs.routinesEnabled &&
    (inputs.routines.status === "idle" || inputs.routines.status === "loading");
  if (!force && (inputs.railsStatus === "loading" || routinesUnsettled)) return null;

  const native_waiting = collectDecisions(inputs.sessions, null).length;
  const { working, review } = groupRuns(inputs.sessions, inputs.reviewed);

  const git: PageFactsView["git"] =
    inputs.git.state === "known"
      ? { state: "known", dirty: inputs.git.dirty }
      : { state: inputs.git.state, dirty: null };

  let found: { routine_id: string; next_run: string } | null = null;
  let earliestAt = Number.POSITIVE_INFINITY;
  for (const item of inputs.routines.items) {
    const nextRun = item.next_run;
    if (item.state !== "enabled" || nextRun === null) continue;
    const at = new Date(nextRun).getTime();
    if (Number.isNaN(at) || at >= earliestAt) continue;
    earliestAt = at;
    found = { routine_id: item.routine_id, next_run: nextRun };
  }
  const routine: PageFactsView["routine"] = found
    ? { ...found, paused: inputs.routines.scheduler?.paused === true }
    : null;

  return {
    native_waiting,
    runs: { working: working.length, review: review.length },
    git,
    routine,
    skill: skillFacts(inputs.skill),
    suggest: suggestFacts(inputs.skill),
    second_look: lookFacts(inputs.look),
    context: contextFacts(inputs.look),
    sources: sourceFacts(inputs.look),
  };
}

/** The run to open from "Show run": the first row waiting for review, else the first still
 * working; null when there is neither. */
export function firstRun(
  sessions: Readonly<Record<string, SessionView>>,
  reviewed: ReadonlySet<string>,
): { sessionId: string; blockId: string } | null {
  const groups = groupRuns(sessions, reviewed);
  const row = groups.review.at(0) ?? groups.working.at(0);
  return row ? { sessionId: row.sessionId, blockId: row.block.id } : null;
}
