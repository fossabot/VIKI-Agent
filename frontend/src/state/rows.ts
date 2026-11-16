/**
 * Pure helpers behind the rails' rows (src/accept/m3b/rows.accept.test.ts pins
 * them). No React, no I/O; `nowMs` is passed in so tests fix the clock.
 *
 * Rules:
 * - groupSessions(rows, nowMs) → {groups, other}. `other` = the rows with `other` true (the
 *   list shows them collapsed; search never includes them). The rest fall into groups labelled
 *   "Today", "Yesterday · MM-DD" (yesterday's local month and day, zero-padded) and "Earlier",
 *   in that order, empty groups omitted. Every list is newest `modified` first. Days are local
 *   calendar days relative to `nowMs`.
 * - timeLabel(iso, nowMs): "HH:MM" (local, 24-hour, zero-padded) when `iso` falls on the local
 *   day of `nowMs`, else "MM-DD"; "" when `iso` is "" or unparseable.
 * - rowTitle(row): `title`, else `first_prompt`, else "Untitled session".
 * - runningAgents(snapshot): the distinct `agent` names of routing blocks whose status is
 *   "running", over every session view (key order) and every turn, in first-seen order.
 * - agentStatus(agent, running, nowMs): "here now" when `running` includes the agent's name;
 *   else "never" when last_run is ""; else "idle · " + timeLabel(last_run, nowMs).
 * - hereNowLabel(vikiOnline, running): "N here now" with N = running.length + (vikiOnline ? 1
 *   : 0), or "" when N is 0.
 * - rosterRunning(agents, running): `running` narrowed to the names `agents` actually lists.
 *   A routing block's `agent` can be a built-in subagent_type ("general-purpose"), the literal
 *   "workflow", or a name from a ~/.claude/agents file — none of which rails.py puts in the
 *   vault-scoped roster it returns as `agents`. Callers must run `running` through this before
 *   handing it to hereNowLabel or AgentsSection: an off-roster name has no row to draw, so
 *   left unfiltered it inflates "N here now" with nothing on screen to account for the N.
 *   Consequence worth knowing: while the rails have not loaded — and permanently, if /api/rails
 *   keeps failing — `agents` is empty, so every running agent is narrowed away and the count
 *   reads "1 here now". That keeps the count honest about the rows on screen, which are also
 *   empty, but it means a failing rails endpoint hides subagent activity entirely rather than
 *   only its names. Telling the user the roster could not be loaded belongs in the rail itself.
 *
 * One more (App docstring's composer rule; src/state/rows.unit.test.ts pins it):
 * - menuCommands(commands): the session's own `/` list, minus the terminal-only ones (driver.py's
 *   TERMINAL_ONLY: clear, doctor, color — the CLI already drops these before session.started
 *   arrives, so this is a defensive re-application of the same rule, not the primary filter),
 *   vault skills (`skill: true`) first and the rest alphabetically, each group alphabetical by
 *   code point — the same ordering driver.py's own `sorted(commands, key=lambda c: (not c.skill,
 *   c.name))` produces (Python's default string comparison is by code point too), so this
 *   function's output never disagrees with the server's about a roster mixing cases or carrying
 *   non-ASCII names. What the user sees is a separate question and not settled here: SlashMenu
 *   re-filters by the typed query and re-sorts both groups with localeCompare before rendering,
 *   so the display order is that comparison's, not this one's. A leading "/" on a name (none of
 *   the CLI's own names carry one) is ignored for both the terminal-only check and the sort, so
 *   the rule reads the same whichever form arrives.
 */
import type { AgentInfo, Command, SessionRow } from "../protocol";
import type { AppSnapshot } from "./store";

export interface SessionGroup {
  label: string;
  rows: SessionRow[];
}

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/** Date.parse, but "" and garbage both come back as null rather than NaN. */
function parseTime(iso: string): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t;
}

/** A key identifying `ms`'s local calendar day, comparable with === . */
function localDayKey(ms: number): string {
  const d = new Date(ms);
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

function byModifiedDesc(a: SessionRow, b: SessionRow): number {
  return (parseTime(b.modified) ?? -Infinity) - (parseTime(a.modified) ?? -Infinity);
}

export function groupSessions(
  rows: readonly SessionRow[],
  nowMs: number,
): { groups: SessionGroup[]; other: SessionRow[] } {
  const other: SessionRow[] = [];
  const today: SessionRow[] = [];
  const yesterday: SessionRow[] = [];
  const earlier: SessionRow[] = [];

  const todayKey = localDayKey(nowMs);
  const yesterdayDate = new Date(nowMs);
  yesterdayDate.setDate(yesterdayDate.getDate() - 1); // the previous local calendar day, DST or not
  const yesterdayKey = localDayKey(yesterdayDate.getTime());
  const yesterdayLabel = `Yesterday · ${pad2(yesterdayDate.getMonth() + 1)}-${pad2(yesterdayDate.getDate())}`;

  for (const row of rows) {
    if (row.other) {
      other.push(row);
      continue;
    }
    const t = parseTime(row.modified);
    const key = t === null ? null : localDayKey(t);
    if (key === todayKey) today.push(row);
    else if (key === yesterdayKey) yesterday.push(row);
    else earlier.push(row);
  }

  today.sort(byModifiedDesc);
  yesterday.sort(byModifiedDesc);
  earlier.sort(byModifiedDesc);
  other.sort(byModifiedDesc);

  const groups: SessionGroup[] = [];
  if (today.length > 0) groups.push({ label: "Today", rows: today });
  if (yesterday.length > 0) groups.push({ label: yesterdayLabel, rows: yesterday });
  if (earlier.length > 0) groups.push({ label: "Earlier", rows: earlier });

  return { groups, other };
}

export function timeLabel(iso: string, nowMs: number): string {
  const t = parseTime(iso);
  if (t === null) return "";
  const d = new Date(t);
  if (localDayKey(t) === localDayKey(nowMs)) return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

export function rowTitle(row: SessionRow): string {
  return row.title || row.first_prompt || "Untitled session";
}

/**
 * Whether `row.title` carries nothing beyond the server's bare first-prompt fallback, rather
 * than a real custom or AI title. catalog.py's `_row_from_state` sets `title =
 * _clip(custom_title or ai_title or first_prompt_raw, TITLE_LEN)` and `first_prompt =
 * _clip(first_prompt_raw, PROMPT_LEN)` (TITLE_LEN 80, PROMPT_LEN 200, catalog.py ~668); `_clip`
 * (catalog.py ~546) collapses whitespace runs to one space and cuts to its limit with "…" as the
 * last character when cut. So without a custom or AI title, `title` is `first_prompt`'s own text
 * collapsed the same way and cut to the shorter limit: short enough to need no cut, the two
 * fields are byte-for-byte equal; long enough to end in "…", `first_prompt` starts with `title`
 * minus that trailing "…" (both cuts come from the same collapsed string, 80 characters before
 * 200). Neither shape is reachable for a title someone actually chose unless it happens to
 * coincide with the prompt — which this still calls a prompt: a caller that must never leak chat
 * content (the Decision presence report) should prefer hiding a real title over leaking a first
 * message, the only direction this mistake is safe to make in.
 */
export function titleIsPrompt(row: Pick<SessionRow, "title" | "first_prompt">): boolean {
  const { title, first_prompt } = row;
  if (!title) return false;
  if (title === first_prompt) return true;
  return title.endsWith("…") && first_prompt.startsWith(title.slice(0, -1));
}

export function runningAgents(snapshot: AppSnapshot): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const view of Object.values(snapshot.sessions)) {
    for (const turn of view.turns) {
      for (const block of turn.blocks) {
        if (block.kind === "routing" && block.status === "running" && !seen.has(block.agent)) {
          seen.add(block.agent);
          result.push(block.agent);
        }
      }
    }
  }
  return result;
}

export function agentStatus(agent: AgentInfo, running: readonly string[], nowMs: number): string {
  if (running.includes(agent.name)) return "here now";
  if (agent.last_run === "") return "never";
  return `idle · ${timeLabel(agent.last_run, nowMs)}`;
}

export function hereNowLabel(vikiOnline: boolean, running: readonly string[]): string {
  const n = running.length + (vikiOnline ? 1 : 0);
  return n === 0 ? "" : `${n} here now`;
}

export function rosterRunning(agents: readonly AgentInfo[], running: readonly string[]): string[] {
  const names = new Set(agents.map((agent) => agent.name));
  return running.filter((name) => names.has(name));
}

/** Never offered in the / menu: driver.py's own TERMINAL_ONLY frozenset, mirrored rather than
 * imported (Python and TS builds don't share modules) — the same relationship WRITE_TOOLS in
 * unload.ts has to session.py's copy.
 *
 * It now has that relationship's sync check too (rows.unit.test.ts reads driver.py). The
 * original note here declined one because "a menu that occasionally shows one extra
 * terminal-only command is a cosmetic slip" — true, but only of drift in that direction. The
 * other direction is the one that costs: offering /color from the server and having this stale
 * copy filter it back out leaves a command that never appears, with every Python test green and
 * the debugger pointed at a server behaving correctly. */
const TERMINAL_ONLY = new Set(["clear", "doctor", "color"]);

/** Command names off the wire never carry a leading slash; strip one anyway so the rule reads
 * the same if that ever changes, rather than silently keeping a slashed name out of both the
 * terminal-only check and the sort. */
function bareCommandName(name: string): string {
  return name.startsWith("/") ? name.slice(1) : name;
}

/** Plain code-point comparison — not localeCompare, which is case-insensitive and
 * locale-dependent with no locale argument and so can reorder what driver.py's Python `sorted`
 * (code-point order) already settled. */
function compareByCodePoint(a: string, b: string): number {
  if (a < b) return -1;
  if (a > b) return 1;
  return 0;
}

export function menuCommands(commands: readonly Command[]): Command[] {
  const offered = commands.filter((c) => !TERMINAL_ONLY.has(bareCommandName(c.name)));
  return [...offered].sort((a, b) => {
    if (a.skill !== b.skill) return a.skill ? -1 : 1;
    return compareByCodePoint(bareCommandName(a.name), bareCommandName(b.name));
  });
}

/**
 * Was this transcript written by a newer Claude Code than the one answering now?
 *
 * Both halves of the comparison are on the wire: catalog reads each transcript's own `version`
 * onto its row, and `session.started` reports the CLI we actually spawned. Resuming a
 * transcript written by a newer build can mean entry shapes this one has never seen, and what
 * to do about that is the user's call — so it is said and nothing is refused.
 *
 * Compared field by field as numbers, because "2.1.259" and "2.1.266" sort the wrong way as
 * strings: "2.1.9" is greater than "2.1.10" alphabetically. A field that is not a number at all
 * (a "-rc1" suffix, an empty string, undefined) makes the whole comparison false rather than a
 * guess — an unfamiliar version format must not produce a notice claiming a staleness nobody
 * established.
 */
export function isNewerCli(transcript: string | undefined, running: string): boolean {
  const a = numericFields(transcript);
  const b = numericFields(running);
  if (a === null || b === null) return false;
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const left = a[i] ?? 0;
    const right = b[i] ?? 0;
    if (left !== right) return left > right;
  }
  return false;
}

function numericFields(version: string | undefined): number[] | null {
  if (!version) return null;
  const fields = version.split(".").map((field) => Number(field));
  return fields.every((field) => Number.isInteger(field) && field >= 0) ? fields : null;
}
