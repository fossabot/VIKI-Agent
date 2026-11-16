import { type DragEvent, type ReactElement, useEffect, useState } from "react";
import { isValidRef, isValidWorkspaceId } from "../../state/paneFormat";
import type { Edge, Zone } from "../../state/panes";
import "./PaneDrop.css";

/** The HTML5 drag type a dragged tab carries (a chat's session id or a file tab's id); shared
 * with SessionTabs, which sets it on drag start. */
export const CHAT_DRAG_TYPE = "text/x-viki-tab";

/** The HTML5 drag type a dragged file row carries: a JSON `{ workspace_id, ref }`; shared with
 * FileTree, which sets it on drag start. */
export const FILE_DRAG_TYPE = "application/x-viki-file";

/** A point within this fraction of an allowed edge's own size counts as that edge; otherwise
 * it is the centre. */
export const EDGE_BAND = 0.25;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
/** A file tab's id, `f1`–`f64`, as `state/panes.ts` gives them. */
const FILE_TAB = /^f([1-9]|[1-5][0-9]|6[0-4])$/;

/** What a drag over a pane carries: an existing chat or file tab by id, or a file not yet open
 * anywhere. */
export type DropPayload =
  | { kind: "chat"; id: string }
  | { kind: "fileTab"; id: string }
  | { kind: "file"; workspaceId: string; ref: string };

/**
 * Reads the dragged payload off `dataTransfer`: for `CHAT_DRAG_TYPE` a canonical UUID (a chat) or
 * exactly `f1`–`f64` (a file tab), or for `FILE_DRAG_TYPE` an exact `{ workspace_id, ref }` JSON
 * object within `paneFormat.ts`'s own bounds. Drag payloads are untrusted input (any page or app
 * can start a drag carrying these types) — anything else (wrong shape, extra or missing keys, an
 * out-of-bounds field, a parse error, or neither type present) fails closed to `null`. Never
 * throws. A file tab id names no file: the caller moves it only when its own layout holds it.
 */
export function parseDrop(dataTransfer: DataTransfer): DropPayload | null {
  if (dataTransfer.types.includes(CHAT_DRAG_TYPE)) {
    const id = dataTransfer.getData(CHAT_DRAG_TYPE);
    if (UUID.test(id)) return { kind: "chat", id };
    return FILE_TAB.test(id) ? { kind: "fileTab", id } : null;
  }
  if (dataTransfer.types.includes(FILE_DRAG_TYPE)) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(dataTransfer.getData(FILE_DRAG_TYPE));
    } catch {
      return null;
    }
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return null;
    const record = parsed as Record<string, unknown>;
    if (Object.keys(record).length !== 2 || !("workspace_id" in record) || !("ref" in record))
      return null;
    const { workspace_id: workspaceId, ref } = record;
    if (!isValidWorkspaceId(workspaceId) || !isValidRef(ref)) return null;
    return { kind: "file", workspaceId, ref };
  }
  return null;
}

/**
 * The drop zone for point `(x, y)` within `rect`, restricted to the allowed `edges` (row:
 * left/right, column: top/bottom): the nearest allowed edge under `EDGE_BAND` wins; otherwise,
 * and always over a 0-size rect, `"center"`.
 */
export function dropZone(
  rect: { left: number; top: number; width: number; height: number },
  x: number,
  y: number,
  edges: { row: boolean; column: boolean },
): Zone {
  if (rect.width <= 0 || rect.height <= 0) return "center";
  const candidates: { zone: Edge; distance: number }[] = [];
  if (edges.row) {
    const left = (x - rect.left) / rect.width;
    candidates.push({ zone: "left", distance: left }, { zone: "right", distance: 1 - left });
  }
  if (edges.column) {
    const top = (y - rect.top) / rect.height;
    candidates.push({ zone: "top", distance: top }, { zone: "bottom", distance: 1 - top });
  }
  let nearest: { zone: Edge; distance: number } | null = null;
  for (const candidate of candidates) {
    if (candidate.distance < EDGE_BAND && (!nearest || candidate.distance < nearest.distance)) {
      nearest = candidate;
    }
  }
  return nearest ? nearest.zone : "center";
}

export interface UsePaneDropOptions {
  /** Which edges a drop over this element's own `getBoundingClientRect()` may land on, for the
   * kind of item currently dragged over it (a split's new group needs room for that kind's own
   * floor — chat and file panes have different minimums). The kind comes from the drag type
   * alone, since a drag's data is unreadable until the drop: a dragged file tab counts as a chat. */
  edges: (rect: DOMRect, kind: "chat" | "file") => { row: boolean; column: boolean };
  onDrop: (payload: DropPayload, zone: Zone) => void;
}

export interface PaneDropHandlers {
  zone: Zone | null;
  onDragOver: (event: DragEvent<HTMLElement>) => void;
  onDragLeave: (event: DragEvent<HTMLElement>) => void;
  onDrop: (event: DragEvent<HTMLElement>) => void;
}

function overTabStrip(event: DragEvent<HTMLElement>): boolean {
  return (event.target as Element).closest(".session-tabs") !== null;
}

/**
 * Tracks the drop zone for a tab or file dragged over this element. A drag that carries
 * neither `CHAT_DRAG_TYPE` nor `FILE_DRAG_TYPE` is ignored outright (no `preventDefault`, so the
 * browser's own default applies); one over a nested `.session-tabs` strip leaves the zone null
 * so the strip's own drop handling applies instead.
 */
export function usePaneDrop({ edges, onDrop: notifyDrop }: UsePaneDropOptions): PaneDropHandlers {
  const [zone, setZone] = useState<Zone | null>(null);

  // A drag that ends outside any drop target (released over nothing, or cancelled) still has
  // to clear our zone; dragleave alone cannot be relied on for that.
  useEffect(() => {
    const clear = () => setZone(null);
    window.addEventListener("dragend", clear);
    return () => window.removeEventListener("dragend", clear);
  }, []);

  return {
    zone,
    onDragOver(event: DragEvent<HTMLElement>) {
      const types = event.dataTransfer.types;
      const kind: "chat" | "file" | null = types.includes(CHAT_DRAG_TYPE)
        ? "chat"
        : types.includes(FILE_DRAG_TYPE)
          ? "file"
          : null;
      if (kind === null) return;
      if (overTabStrip(event)) {
        setZone(null);
        return;
      }
      event.preventDefault();
      const rect = event.currentTarget.getBoundingClientRect();
      setZone(dropZone(rect, event.clientX, event.clientY, edges(rect, kind)));
    },
    onDragLeave(event: DragEvent<HTMLElement>) {
      if (!event.currentTarget.contains(event.relatedTarget as Node)) setZone(null);
    },
    onDrop(event: DragEvent<HTMLElement>) {
      const target = zone;
      setZone(null);
      if (target === null) return;
      event.preventDefault();
      const payload = parseDrop(event.dataTransfer);
      if (payload) notifyDrop(payload, target);
    },
  };
}

/** The edge or centre highlight shown over a pane while a tab or file is dragged over it. */
export function PaneDropOverlay({ zone }: { zone: Zone | null }): ReactElement | null {
  return zone === null ? null : <div className="pane-drop" data-zone={zone} aria-hidden="true" />;
}
