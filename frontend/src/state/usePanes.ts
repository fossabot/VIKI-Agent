/**
 * Owns the one `PaneLayout` a window's chat panes render from: where it starts (storage, a
 * handoff window, or the `layout.panes` setting), and where it goes (storage on every change,
 * the setting debounced, for the primary window only).
 *
 * A secondary window (`handoff`) never reads or writes `viki.panes`, and never adopts or saves
 * `layout.panes`: it marks `viki.window` itself and always starts from a single empty pane, so
 * two windows never race over the same stored layout. The primary window restores from
 * `viki.panes` first; only when that held nothing does it adopt the setting once, so a layout
 * already in play — restored or user-touched — is never silently replaced by it. Every change is
 * written to storage at once; only one that changes the layout's shape (ignoring focus and each
 * group's active tab) marks it dirty, mirrored by a `viki.panes.pending` marker (primary windows
 * only) so a reload knows whether the layout it restores still needs saving. A focus or active
 * change alone never marks the layout unsaved — it rides along with whatever structural save
 * comes next, if any. After a quiet period, a dirty layout is patched to the setting, unless by
 * then its shape already matches what the setting holds, which clears dirty without a save. A
 * successful save clears dirty too, but only if nothing has changed the layout since; a `409`
 * `stale_revision` gets one retry, after a `refresh()`, under the same condition, and any other
 * failure leaves the layout dirty for its own next debounce.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { type SettingsView, ShellApiError } from "../clients/app/shellApi";
import { parseLayout, serializeLayout } from "./paneFormat";
import { groups, type PaneLayout, type PaneNode, singlePane } from "./panes";

export const PANES_STORAGE_KEY = "viki.panes";
export const PANES_PENDING_KEY = "viki.panes.pending";
export const WINDOW_KEY = "viki.window";
export const LAYOUT_SETTING = "layout.panes";

export interface UsePanesOptions {
  enabled: boolean;
  handoff: boolean;
  settings: SettingsView | null;
  save: ((text: string) => Promise<unknown>) | null;
  refresh: () => Promise<void>;
  debounceMs?: number;
}

export interface UsePanesResult {
  layout: PaneLayout;
  update(op: (l: PaneLayout) => PaneLayout): void;
  primary: boolean;
  restored: number;
}

function readStorage(key: string): string | null {
  try {
    return window.sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStorage(key: string, value: string): void {
  try {
    window.sessionStorage.setItem(key, value);
  } catch {
    // Storage unavailable (private mode, quota): the layout still works for this tab.
  }
}

function removeStorage(key: string): void {
  try {
    window.sessionStorage.removeItem(key);
  } catch {
    // Storage unavailable: nothing to clean up.
  }
}

interface Mount {
  layout: PaneLayout;
  restored: number;
  primary: boolean;
  dirty: boolean;
}

/** The mount-time read (see the module doc). Pure apart from one idempotent write, so calling
 * it more than once — React Strict Mode double-invokes a `useState` lazy initializer — is
 * harmless: every call writes or reads the same thing and lands on the same result. A handoff
 * window never reads `PANES_PENDING_KEY`: it is always non-primary, and the key is primary-only. */
function mountState(handoff: boolean): Mount {
  if (handoff) {
    writeStorage(WINDOW_KEY, "secondary");
    return { layout: singlePane(), restored: 0, primary: false, dirty: false };
  }
  const parsed = parseLayout(readStorage(PANES_STORAGE_KEY));
  const layout = parsed ?? singlePane();
  const hadTab = groups(layout).some((g) => g.tabs.length > 0);
  const primary = readStorage(WINDOW_KEY) !== "secondary";
  const dirty = primary && readStorage(PANES_PENDING_KEY) === "1";
  return { layout, restored: hadTab ? 1 : 0, primary, dirty };
}

/** `node` with every group's `active` cleared, for `structureOf` below. */
function withoutActive(node: PaneNode): PaneNode {
  return node.kind === "group"
    ? { ...node, active: null }
    : { ...node, children: node.children.map(withoutActive) };
}

/** `l`'s shape alone: which tabs sit in which group and split, pinned, sized — with `focused`
 * and every group's `active` cleared. Comparing this instead of the full serialized text is
 * what keeps a tab click or a pane focus from ever scheduling a save, while a split, move,
 * close, pin or resize still does. Comparison only: never parsed back or stored. */
function structureOf(l: PaneLayout): string {
  return serializeLayout({ ...l, focused: "", root: withoutActive(l.root) });
}

export function usePanes(options: UsePanesOptions): UsePanesResult {
  const { enabled, handoff, settings, save, refresh } = options;
  const debounceMs = options.debounceMs ?? 1000;

  const [initial] = useState(() => mountState(handoff));
  const [layout, setLayout] = useState(initial.layout);
  const [restored, setRestored] = useState(initial.restored);
  const primary = initial.primary;

  // Mirrors `layout` for the save effect's retry below, which must compare against whatever is
  // current when `refresh()` resolves, not the value its own closure started with.
  const layoutRef = useRef(layout);
  layoutRef.current = layout;
  const touchedRef = useRef(false);
  // True whenever `layout` holds a change the setting hasn't been confirmed to match yet; the
  // save effect's only gate (besides `primary`/`enabled`/`hasSave`). Starts from the reload
  // marker below, so a window closed mid-debounce still saves its last change once it comes back.
  const dirtyRef = useRef(initial.dirty);

  // Mirrors the latest settings/save/refresh so the debounce timer below resets only on an
  // actual layout change, never merely because a re-rendering caller recreated these.
  const settingsRef = useRef(settings);
  settingsRef.current = settings;
  const saveRef = useRef(save);
  saveRef.current = save;
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;

  // Functional so two `update` calls in the same event both apply: each composes on the latest
  // pending layout instead of this render's stale closure. `touchedRef` is set whenever `op`
  // returns a new object; `dirtyRef` only when that object's shape actually differs (see
  // `structureOf` and the save effect below) — a focus or active change alone never marks the
  // layout unsaved, it rides along with whatever structural save comes next. The storage write
  // lives in the effect below, not here, so React's dev-only double-invocation of a setState
  // updater (Strict Mode) never writes twice.
  const update = useCallback((op: (l: PaneLayout) => PaneLayout) => {
    setLayout((prev) => {
      const next = op(prev);
      if (next !== prev) {
        touchedRef.current = true;
        if (structureOf(next) !== structureOf(prev)) dirtyRef.current = true;
      }
      return next;
    });
  }, []);

  // Mirror every committed layout to storage, gated only by `enabled`. A primary window also
  // mirrors whether that layout is still unsaved, so a reload within the debounce window knows
  // to try again; a secondary window never touches this key.
  useEffect(() => {
    if (!enabled) return;
    writeStorage(PANES_STORAGE_KEY, serializeLayout(layout));
    if (!primary) return;
    if (dirtyRef.current) writeStorage(PANES_PENDING_KEY, "1");
    else removeStorage(PANES_PENDING_KEY);
  }, [layout, enabled, primary]);

  // Adopt once: only while nothing has been restored or touched, so a layout already in play
  // is never replaced. `restored` moving off 0 is what stops a second adoption.
  useEffect(() => {
    if (!primary || !enabled || restored !== 0 || touchedRef.current) return;
    const adopted = parseLayout(settings?.effective[LAYOUT_SETTING]);
    if (!adopted) return;
    setLayout(adopted);
    setRestored((count) => count + 1);
  }, [primary, enabled, restored, settings]);

  // Save: debounced on `layout` alone, primary-only, scheduled only while `dirtyRef` holds a
  // change the setting hasn't been confirmed to match (never merely because something was once
  // touched or restored — a settings refresh flipping `save` null and back must not resurrect an
  // old, already-settled layout). `update` sets `dirtyRef` only when a change alters the
  // layout's shape (ignoring focus and each group's active tab — see `structureOf`); a focus or
  // active change alone never marks the layout unsaved, it rides along with whatever structural
  // save comes next. Skipped, and dirty cleared without a save, when the shape alone already
  // matches the setting by the time the timer fires. A successful save clears dirty too, but
  // only if nothing has changed `layout` since (a change made while this save was in flight keeps
  // dirty set and gets its own debounce). A stale-revision conflict gets one retry, after a
  // `refresh()`, under the same condition; any other failure leaves dirty set.
  // `save`/`refresh`/`settings` come from the refs above, read when the timer fires, so a caller
  // that recreates them every render does not reset the debounce.
  const hasSave = save !== null;
  useEffect(() => {
    if (!primary || !enabled || !hasSave || !dirtyRef.current) return;
    const target = layout;
    function clearDirty(): void {
      dirtyRef.current = false;
      if (primary) removeStorage(PANES_PENDING_KEY);
    }
    const timer = setTimeout(() => {
      const runSave = saveRef.current;
      if (!runSave) return;
      const settingLayout = parseLayout(settingsRef.current?.effective[LAYOUT_SETTING]);
      if (settingLayout && structureOf(target) === structureOf(settingLayout)) {
        clearDirty();
        return;
      }
      const text = serializeLayout(target);
      runSave(text)
        .then(() => {
          if (layoutRef.current === target) clearDirty();
        })
        .catch((error: unknown) => {
          if (!(error instanceof ShellApiError) || error.code !== "stale_revision") return;
          refreshRef.current().then(() => {
            if (layoutRef.current !== target) return;
            saveRef
              .current?.(text)
              .then(() => {
                if (layoutRef.current === target) clearDirty();
              })
              .catch(() => {});
          });
        });
    }, debounceMs);
    return () => clearTimeout(timer);
  }, [primary, enabled, hasSave, layout, debounceMs]);

  return { layout, update, primary, restored };
}
