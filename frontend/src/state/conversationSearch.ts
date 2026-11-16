import type {
  HistoryAnchorPage,
  MatchSnapshot,
} from "../clients/conversations/conversationSearchApi";
import { applyHistoryPage } from "./sessionReducer";
import type { SessionView, Turn } from "./view";

export interface AppSearchHit {
  kind: "app_turn";
  generation: string;
  turnId: string;
  role: "user" | "assistant";
  revision: string;
  snapshot: MatchSnapshot;
}

function content(turn: Turn, role: AppSearchHit["role"]): string {
  return role === "user"
    ? (turn.user?.text ?? "")
    : turn.blocks
        .filter((block) => block.kind === "text")
        .map((block) => block.text)
        .join("\n\n");
}
const fold = (value: string): string =>
  value
    .normalize("NFKC")
    .toLowerCase()
    .replace(/ß/g, "ss")
    .replace(/ς/g, "σ")
    .replace(/\s+/gu, " ");

/** How many trailing graphemes of the current part decide whether the next grapheme joins it.
 * Testing the whole part is quadratic on a run that never breaks (a long whitespace run, a long
 * combining sequence); NFKC normalization never reaches this far, so a bounded window gives the
 * same join decision. Mirrors the server's `_segments` window (src/viki_agent/conversations/conversation_search.py), which
 * uses the same rule over 32 code points. */
const JOIN_WINDOW = 32;

/** Map normalized graphemes back to the original text; never highlight normalized substitutes.
 * Mirrors the server's `_segments`/`_fold` pairing (src/viki_agent/conversations/conversation_search.py) so both folds stay
 * equal: graphemes join while combining them changes the folded length within the join window
 * (Hangul compatibility jamo, a spacing mark whose NFKC form starts with a space). A part's own
 * fold is computed once, when the part is complete. */
export function matchSnapshot(value: string, query: string): MatchSnapshot | null {
  const needle = fold(query).trim();
  if (!needle) return null;
  const at = fold(value).indexOf(needle);
  if (at < 0) return null;
  let offset = 0;
  let start: number | undefined;
  let end: number | undefined;
  let open = false;
  let pendingStart = 0;
  let pendingEnd = 0;
  const recentStarts: number[] = [];
  function flush(): void {
    const folded = fold(value.slice(pendingStart, pendingEnd));
    offset += folded.length;
    if (start === undefined && offset > at) start = pendingStart;
    if (end === undefined && offset >= at + needle.length) end = pendingEnd;
  }
  function beginPart(index: number, length: number): void {
    pendingStart = index;
    pendingEnd = index + length;
    recentStarts.length = 0;
    recentStarts.push(index);
    open = true;
  }
  for (const item of new Intl.Segmenter(undefined, { granularity: "grapheme" }).segment(value)) {
    if (!open) {
      beginPart(item.index, item.segment.length);
      continue;
    }
    const tail = value.slice(recentStarts[0] ?? pendingStart, pendingEnd);
    if (fold(tail + item.segment).length !== fold(tail).length + fold(item.segment).length) {
      pendingEnd = item.index + item.segment.length;
      recentStarts.push(item.index);
      if (recentStarts.length > JOIN_WINDOW) recentStarts.shift();
      continue;
    }
    flush();
    if (end !== undefined) break;
    beginPart(item.index, item.segment.length);
  }
  if (end === undefined && open) flush();
  if (start === undefined || end === undefined) return null;
  return {
    before: [...value.slice(Math.max(0, start - 240), start)].slice(-120).join(""),
    match: [...value.slice(start, end)].slice(0, 256).join(""),
    after: [...value.slice(end, end + 240)].slice(0, 120).join(""),
  };
}
function appTurnSource(view: SessionView, turn: Turn, role: AppSearchHit["role"]): string {
  return JSON.stringify([
    "viki-app-search-v1",
    view.sessionId,
    view.generation,
    turn.id,
    role,
    content(turn, role),
  ]);
}
async function revision(source: string): Promise<string> {
  const raw = new TextEncoder().encode(source);
  return [...new Uint8Array(await crypto.subtle.digest("SHA-256", raw))]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}
export async function searchAppTurns(view: SessionView, query: string): Promise<AppSearchHit[]> {
  if (!view.generation) return [];
  const hits: AppSearchHit[] = [];
  for (const turn of view.turns) {
    if (turn.history) continue;
    for (const role of ["user", "assistant"] as const) {
      const snapshot = matchSnapshot(content(turn, role), query);
      if (snapshot)
        hits.push({
          kind: "app_turn",
          generation: view.generation,
          turnId: turn.id,
          role,
          revision: await revision(appTurnSource(view, turn, role)),
          snapshot,
        });
      if (hits.length === 100) return hits;
    }
  }
  return hits;
}
/** The exact hash input, or null once this hit no longer names a live turn in its generation. */
export function appHitSource(view: SessionView, hit: AppSearchHit): string | null {
  const turn = view.turns.find((item) => !item.history && item.id === hit.turnId);
  return turn && view.generation === hit.generation ? appTurnSource(view, turn, hit.role) : null;
}
export async function currentAppHit(view: SessionView, hit: AppSearchHit): Promise<boolean> {
  const source = appHitSource(view, hit);
  return source !== null && (await revision(source)) === hit.revision;
}

/** A searched window replaces historical projection only. In-flight socket pages are invalidated. */
export function applyHistoryAnchorPage(
  view: SessionView,
  page: HistoryAnchorPage,
): { view: SessionView; turnId: string } | null {
  if (page.state !== "ready" || page.anchor_turn_index === null) return null;
  const next = applyHistoryPage(
    { ...view, turns: view.turns.filter((turn) => !turn.history) },
    {
      events: page.events,
      next_before_uuid: page.older_before_uuid,
      ...(page.coverage === "tail" ? { partial: "tail_budget" as const } : {}),
    },
  );
  const turn = next.turns.filter((item) => item.history)[page.anchor_turn_index];
  return turn ? { view: next, turnId: turn.id } : null;
}
