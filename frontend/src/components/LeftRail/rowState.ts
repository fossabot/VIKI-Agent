/** Derives a session row's label and status from the session, vault and other-process observations. */
import type { SessionRow } from "../../protocol";
import { entrypointBadge } from "../../state/entrypoint";
import { timeLabel } from "../../state/rows";
import type { AppSnapshot } from "../../state/store";
import { presence, type SessionView } from "../../state/view";

export type RowState = "writing" | "waiting" | "elsewhere" | "live" | "plain";

export interface RowContext {
  app: AppSnapshot;
  /** session_id → the entrypoint of the process holding it, from vault.sessions others. */
  elsewhere: ReadonlyMap<string, string>;
}

/** The view the app holds for this row, or null when there is none or it is closed —
 * `unsavedWork()` treats a closed view the same way, for the same reason (session.py leaves a
 * closed session's last turn status as it was, so a mid-turn close must not read as held). */
function heldView(row: SessionRow, ctx: RowContext): SessionView | null {
  const view = ctx.app.sessions[row.session_id];
  if (!view || view.closed) return null;
  return view;
}

export function rowState(row: SessionRow, ctx: RowContext): RowState {
  const view = heldView(row, ctx);
  if (view) {
    const p = presence(view);
    if (p === "writing" || p === "thinking") return "writing";
    if (p === "waiting") return "waiting";
  }
  if (ctx.elsewhere.has(row.session_id)) return "elsewhere";
  if (view) return "live";
  return "plain";
}

/** The meta line as the design draws it: an optional `.chip` badge, an optional 6px dot,
 * then text. The design never writes a literal "·" between a chip and what follows it — that
 * separator only exists in the joined string below, which the frozen contract pins. Splitting
 * the joined string back apart at the call site would make the separator load-bearing; this
 * keeps one source for the words and lets each caller take the shape it needs. */
export interface RowMetaParts {
  /** The badge's text, "" when this state has none. */
  chip: string;
  /** The badge is the dim variant — the design uses it for a session held elsewhere. */
  chipDim: boolean;
  /** The dot before the text: filled while writing, a ring while idle-live, otherwise none. */
  dot: "none" | "filled" | "ring";
  /** What follows the chip or the dot. */
  text: string;
}

const NO_META: RowMetaParts = { chip: "", chipDim: false, dot: "none", text: "" };

export function rowMetaParts(
  row: SessionRow,
  state: RowState,
  ctx: RowContext,
  nowMs: number,
): RowMetaParts {
  switch (state) {
    case "writing":
      return { ...NO_META, dot: "filled", text: "writing…" };
    case "waiting":
      return { ...NO_META, chip: "Needs review", text: timeLabel(row.modified, nowMs) };
    case "elsewhere":
      return {
        ...NO_META,
        chip: entrypointBadge(ctx.elsewhere.get(row.session_id) ?? ""),
        chipDim: true,
        text: "live elsewhere",
      };
    case "live":
      return { ...NO_META, dot: "ring", text: "live · idle" };
    case "plain":
      return NO_META;
  }
}

export function rowMeta(row: SessionRow, state: RowState, ctx: RowContext, nowMs: number): string {
  const { chip, text } = rowMetaParts(row, state, ctx, nowMs);
  return [chip, text].filter(Boolean).join(" · ");
}

/** The others list as rowState wants it: session_id → entrypoint. */
export function elsewhereMap(app: AppSnapshot): ReadonlyMap<string, string> {
  const map = new Map<string, string>();
  for (const other of app.others) map.set(other.session_id, other.entrypoint);
  return map;
}
