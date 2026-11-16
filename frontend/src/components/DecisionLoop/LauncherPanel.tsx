/**
 * The launcher panel: the dialog the launcher
 * opens beside itself. Purely presentational — it draws `view` and `checking` as given; the
 * caller (App) owns calling `openLauncher()` when the panel opens and keeping `checking`
 * true meanwhile, same as `anchor` and `nowMs` are the caller's own live values, not read here.
 *
 * Positioning: opens toward whichever side — above or
 * below, left or right of the draggable launcher (`anchor`, the same `{right, bottom, size}` px
 * useLauncherPosition.ts already keeps) — currently has more room, flips as the launcher
 * moves near an edge, and keeps the whole panel at least `PANEL_INSET` px inside the window (the
 * same figure useLauncherPosition.ts keeps for the launcher itself). Below a 560x480 window it
 * fills the window instead (`dloop-panel--full`); `placePanel`'s own rect goes unused then. Both
 * react to `window`'s `resize`, tracked in `viewport` state — `anchor` alone cannot see the
 * window shrink around a launcher that has not itself moved.
 *
 * Resizing: `wanted` is the user's preferred size, read once from `savedSize` at
 * mount (this component remounts per open); `preview` holds a per-axis value while a resize
 * handle is mid-drag, else null. `placePanel` clamps `preview ?? wanted` into whichever side's
 * room it placed the panel in (`maxWidth`/`maxHeight`) for the actual render — a render-time clamp
 * this file never itself saves. Not full-window and given `onSaveSize`, the dialog carries
 * `data-grow-x`/`data-grow-y` (which outer edge actually moves when that axis grows) and a
 * ResizeHandle (Shell/ResizeHandle.tsx) per axis, flush with that edge (LauncherPanel.css); a
 * commit sets `wanted` for that axis and calls `onSaveSize` with the pair.
 *
 * `useModalFocus` (Dialog/useModalFocus.ts) gives the focus trap and inert background; closing
 * returns focus to `returnFocus()` (the launcher button). This file's own `onDismiss` gives
 * `onEscape` first look at an Escape keypress (true keeps the panel open; used to end a
 * voice conversation on the first Escape and keep the panel for the second); `initialFocus`
 * `"message"` sends the very first mount's focus into `chat`'s own Message VIKI box instead of
 * the first control. The backdrop is transparent by design (unlike Search/SearchPalette.css's
 * dimmed one): opening the panel must not darken the rest of the window. The header's "⋯" menu
 * copies ItemCard.tsx's own small-menu pattern (itself copied from TopBar/HelpMenu.tsx): a
 * role="menu" of role="menuitem" buttons, Escape/outside-click handled locally so useModalFocus's
 * own Escape (which steps aside whenever a role="menu" is open inside the dialog) reaches the
 * dialog only once this menu has closed.
 */
import {
  type CSSProperties,
  type MouseEvent,
  type ReactElement,
  type ReactNode,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import type {
  FeedbackReason,
  FeedbackValue,
  ItemAction,
  ItemView,
  LauncherView,
} from "../../clients/decisionLoop/types";
import { clampResize } from "../../state/layout";
import { useModalFocus } from "../Dialog/useModalFocus";
import { Loading } from "../Loading/Loading";
import { ResizeHandle, type ResizeHandleProps } from "../Shell/ResizeHandle";
import { rootScale } from "../Shell/useLayoutMode";
import { ItemCard } from "./ItemCard";
import { statusLine } from "./wording";
import "./LauncherPanel.css";

export type LauncherMenuChoice =
  | "pause_hour"
  | "pause_tomorrow"
  | "resume"
  | "focus_hour"
  | "end_focus"
  | "open_decision";

/** The weekly check card's two buttons: Check and Skip this week. */
export type ReviewChoice = "check" | "skip";

export interface LauncherPanelAnchor {
  right: number;
  bottom: number;
  size: number;
}

export interface LauncherPanelProps {
  view: LauncherView;
  checking: boolean;
  anchor: LauncherPanelAnchor;
  /** The launcher button to return focus to on close; see useModalFocus's own `restoreFocus`. */
  returnFocus: () => HTMLElement | null;
  onClose: () => void;
  onAction: (action: ItemAction, item: ItemView) => void;
  onFeedback: (item: ItemView, value: FeedbackValue, reason?: FeedbackReason) => void;
  onMenu: (choice: LauncherMenuChoice) => void;
  /** The weekly check card's Check/Skip this week, shown only while `view.review.due`. */
  onReview: (choice: ReviewChoice) => void;
  /** VIKI's chat (LauncherChat), rendered as-is in its own region at the panel's foot. */
  chat: ReactNode;
  nowMs: number;
  /** "W,H" pixels, read once at mount (this component remounts per open); missing or malformed
   * falls back to 380x480, each axis clamped up to at least MIN_PANEL_WIDTH/MIN_PANEL_HEIGHT. */
  savedSize?: string | null;
  /** Called with the next "W,H" once a resize handle commits a resize or a reset. Absent: no
   * resize handles render at all, even with room beside the launcher. */
  onSaveSize?: (value: string) => void;
  /** Escape's first stop: true keeps the panel open (e.g. to end a voice conversation
   * first); anything else (false, or no `onEscape` at all) closes the panel. */
  onEscape?: () => boolean;
  /** Where the very first mount's focus lands: "message" focuses the Message VIKI textarea
   * inside `chat`; absent, or "first", leaves focus to useModalFocus's own
   * first-focusable fallback. Read once, at mount. */
  initialFocus?: "first" | "message";
}

interface Viewport {
  width: number;
  height: number;
}

/** The panel's own preferred width/height, independent of how much room is actually beside the
 * launcher right now (`PanelRect`'s own width/height below is this, render-time clamped). */
interface PanelSize {
  width: number;
  height: number;
}

/** Per-axis "mid-drag" value; `null` on an axis that is not currently being dragged, so the
 * render can fall back to `wanted` for it (`preview ?? wanted`). */
interface PanelPreview {
  width: number | null;
  height: number | null;
}

interface PanelRect extends PanelSize {
  left: number;
  top: number;
  /** The larger side's room on this axis — the width handle's `max`. */
  maxWidth: number;
  /** The larger side's room on this axis, minus PANEL_GAP — the height handle's `max`. */
  maxHeight: number;
  /** Which edge grows when width grows: "left" iff spaceLeft >= spaceRight. */
  growX: "left" | "right";
  /** Which edge grows when height grows: "up" iff spaceAbove >= spaceBelow. */
  growY: "up" | "down";
}

/** `ResizeHandle`'s own `onCommit` intent; this file's two handles never set `collapsible`, so
 * only "resize" (a drag or an arrow key) and "reset" (Enter or a double-click) ever arrive. */
type ResizeIntent = Parameters<ResizeHandleProps["onCommit"]>[1];

/** Keeps the whole panel inside the window; the same figure useLauncherPosition.ts keeps for the
 * launcher itself. */
const PANEL_INSET = 8;
/** Space left between the launcher and the panel when they do not already share an edge
 * (horizontally the panel's near edge aligns flush with the launcher's own, so only the
 * above/below gap needs this). */
const PANEL_GAP = 8;
/** Preferred size. Only the 560x480 full-window threshold and the 8px inset above are fixed,
 * not these: picked so the panel visibly sits beside the launcher above that threshold. Also the
 * resize handles' own `defaultValue` and the size a reset restores. */
const PANEL_WIDTH = 380;
const PANEL_HEIGHT = 480;
/** Below this window width or height, the panel can no longer sit beside the launcher with
 * sensible margins and fills the window instead. */
const FULL_WINDOW_MIN_WIDTH = 560;
const FULL_WINDOW_MIN_HEIGHT = 480;
/** Smallest size a saved size or a resize handle can shrink the panel to. Exported so
 * a caller (App) can share this floor rather than hand-rolling its own. MIN_PANEL_HEIGHT must
 * fit the chat region's own content floor, since `.dloop-panel-chat` does not
 * shrink below it: header about 70 + chat padding 20 + actions about 33 + messages 96 + a
 * hands-free notice or strip up to 64 + the Composer about 130, about 413 — below that, the
 * Composer (and Message VIKI) would overflow the panel and get clipped. */
export const MIN_PANEL_WIDTH = 320;
export const MIN_PANEL_HEIGHT = 440;
const SAVED_SIZE_PATTERN = /^(\d{1,4}),(\d{1,4})$/;

/** The window in the page's own pixels: the appearance setting zooms the root by `--font-scale`,
 * which scales `anchor` (see useLauncherPosition.ts) but not `innerWidth`/`innerHeight`. Read by
 * the hook's own reader and floored, so the launcher's clamp and this placement agree. */
function readViewport(): Viewport {
  const divisor = rootScale();
  return {
    width: Math.floor(window.innerWidth / divisor),
    height: Math.floor(window.innerHeight / divisor),
  };
}

function isFullWindow(viewport: Viewport): boolean {
  return viewport.width < FULL_WINDOW_MIN_WIDTH || viewport.height < FULL_WINDOW_MIN_HEIGHT;
}

/** Clamps a `[pos, pos + size]` box so it stays at least PANEL_INSET px inside `[0, limit]`,
 * shrinking the margin — never flipping the box outside the window — if `size` alone leaves no
 * room for both insets. */
function clampBox(pos: number, size: number, limit: number): number {
  const maxPos = Math.max(PANEL_INSET, limit - PANEL_INSET - size);
  return clampResize(pos, PANEL_INSET, maxPos);
}

/** `null` for a missing or malformed value, the same split useLauncherPosition.ts's own
 * tryParsePosition makes, so a caller can tell "nothing usable" apart from "use the defaults"
 * below. Each axis clamps up only to its own minimum — the pattern's four digits already cap it
 * at 9999. */
function tryParseSize(saved: string | null | undefined): PanelSize | null {
  const match = saved ? SAVED_SIZE_PATTERN.exec(saved) : null;
  const width = match?.[1];
  const height = match?.[2];
  if (width === undefined || height === undefined) return null;
  return {
    width: clampResize(Number(width), MIN_PANEL_WIDTH, 9999),
    height: clampResize(Number(height), MIN_PANEL_HEIGHT, 9999),
  };
}

function parseSize(saved: string | null | undefined): PanelSize {
  return tryParseSize(saved) ?? { width: PANEL_WIDTH, height: PANEL_HEIGHT };
}

/** Where the panel sits beside the launcher: the side (above/below, left/right) with more room
 * right now, and that side's own room (`maxWidth`/`maxHeight`) and growing edge (`growX`/`growY`)
 * for the resize handles. Only meaningful once `isFullWindow` is false. `preferred` is
 * `preview ?? wanted`; the returned `width`/`height` is `preferred` clamped into that
 * room — a render-time clamp this file never itself saves. */
function placePanel(
  anchor: LauncherPanelAnchor,
  viewport: Viewport,
  preferred: PanelSize,
): PanelRect {
  const launcherLeft = viewport.width - anchor.right - anchor.size;
  const launcherTop = viewport.height - anchor.bottom - anchor.size;
  const launcherRight = launcherLeft + anchor.size;
  const launcherBottom = launcherTop + anchor.size;

  const spaceAbove = launcherTop;
  const spaceBelow = viewport.height - launcherBottom;
  const spaceLeft = launcherLeft;
  const spaceRight = viewport.width - launcherRight;

  const growX: "left" | "right" = spaceLeft >= spaceRight ? "left" : "right";
  const growY: "up" | "down" = spaceAbove >= spaceBelow ? "up" : "down";
  const maxWidth = Math.max(0, Math.max(spaceLeft, spaceRight));
  // Never shorter than MIN_PANEL_HEIGHT, since the Composer clips below that (this runs only
  // in a window of 480 px or more, which always holds it): with less room on both sides of the
  // launcher, the panel covers part or, in a window under about 625 px at the default spot,
  // all of the launcher instead. Esc, Alt+V and a click outside the panel still close it.
  const maxHeight = Math.max(MIN_PANEL_HEIGHT, Math.max(spaceAbove, spaceBelow) - PANEL_GAP);

  const height = clampResize(preferred.height, 0, maxHeight);
  const width = clampResize(preferred.width, 0, maxWidth);

  const top =
    growY === "up"
      ? clampBox(launcherTop - PANEL_GAP - height, height, viewport.height)
      : clampBox(launcherBottom + PANEL_GAP, height, viewport.height);
  const left =
    growX === "left"
      ? clampBox(launcherRight - width, width, viewport.width)
      : clampBox(launcherLeft, width, viewport.width);

  return { left, top, width, height, maxWidth, maxHeight, growX, growY };
}

interface MenuEntry {
  choice: LauncherMenuChoice;
  label: string;
}

/** The header "⋯" menu's rows, by state: "Resume"
 * replaces the two pause rows while paused; "End focus" replaces "Focus for 1 hour" while a
 * focus spell is on; "Open Decision" is always last. */
function menuEntries(view: LauncherView): MenuEntry[] {
  const entries: MenuEntry[] =
    view.state === "paused"
      ? [{ choice: "resume", label: "Resume" }]
      : [
          { choice: "pause_hour", label: "Pause for 1 hour" },
          { choice: "pause_tomorrow", label: "Pause until tomorrow" },
        ];
  entries.push(
    view.focus_until !== null
      ? { choice: "end_focus", label: "End focus" }
      : { choice: "focus_hour", label: "Focus for 1 hour" },
  );
  entries.push({ choice: "open_decision", label: "Open Decision" });
  return entries;
}

/** "N things"/"1 thing" for the weekly check card. */
function heldBackCount(count: number): string {
  return count === 1 ? "1 thing" : `${count} things`;
}

/** The weekly check card's question, shown only while `view.review.due`. */
function reviewCardText(count: number): string {
  return `Weekly check: ${heldBackCount(count)} VIKI held back last week. Was it right?`;
}

export function LauncherPanel({
  view,
  checking,
  anchor,
  returnFocus,
  onClose,
  onAction,
  onFeedback,
  onMenu,
  onReview,
  chat,
  nowMs,
  savedSize,
  onSaveSize,
  onEscape,
  initialFocus,
}: LauncherPanelProps): ReactElement {
  const dialogRef = useRef<HTMLDivElement>(null);
  const dialogId = useId();
  const [viewport, setViewport] = useState<Viewport>(readViewport);
  const [menuOpen, setMenuOpen] = useState(false);
  const menuWrapRef = useRef<HTMLDivElement>(null);
  const menuToggleRef = useRef<HTMLButtonElement>(null);
  const menuItemRefs = useRef<(HTMLButtonElement | null)[]>([]);
  // Read once at mount ("W, H"): the panel remounts per open, so (unlike
  // useLauncherPosition.ts's own `saved`, which lives through the launcher's whole mounted life)
  // a later `savedSize` change while this panel is already open needs no adoption effect.
  const [wanted, setWanted] = useState<PanelSize>(() => parseSize(savedSize));
  const [preview, setPreview] = useState<PanelPreview>({ width: null, height: null });

  function handleEscape(): void {
    if (onEscape?.()) return;
    onClose();
  }

  // "message" focuses VIKI's own composer (inside `chat`, below) instead of the first
  // control — e.g. Alt+T/Alt+V opening the panel by keyboard straight into typing or talking.
  // Mount-time only, the same reason `wanted` above reads `savedSize` only once. A layout effect,
  // not a plain one: it must populate this ref before useModalFocus's own layout effect just
  // below reads `initialFocusRef.current` to place the first focus, and React flushes a single
  // component's layout effects in the order they were called.
  const messageFocusRef = useRef<HTMLElement | null>(null);
  // biome-ignore lint/correctness/useExhaustiveDependencies: mount-time only, per initialFocus's own contract (see above).
  useLayoutEffect(() => {
    if (initialFocus === "message") {
      messageFocusRef.current =
        dialogRef.current?.querySelector<HTMLElement>('textarea[aria-label="Message VIKI"]') ??
        null;
    }
  }, []);

  // No preferred initial focus otherwise: `dialogRef`'s own element carries tabIndex={-1}, so
  // useModalFocus's `items.includes(preferred)` check always excludes it and this falls through
  // to its natural fallback, the first focusable control inside (the header's "More" toggle, the
  // "Turn on in Decision" button while off, or the first item/chat control).
  useModalFocus({
    active: true,
    dialogRef,
    initialFocusRef: initialFocus === "message" ? messageFocusRef : dialogRef,
    onDismiss: handleEscape,
    restoreFocus: returnFocus,
  });

  // `anchor` alone cannot see the window shrink around a launcher that has not itself moved.
  useEffect(() => {
    function onResize(): void {
      setViewport(readViewport());
    }
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // Copied from ItemCard.tsx's own "⋯" menu effect (itself copied from TopBar/HelpMenu.tsx):
  // Escape closes and returns focus to the toggle, Up/Down cycle the menu's own items, an
  // outside click or focus move closes it, and the first item takes focus as soon as it opens.
  useEffect(() => {
    if (!menuOpen) return;
    function onWindowKeyDown(event: globalThis.KeyboardEvent): void {
      if (event.isComposing || event.keyCode === 229) return;
      if (event.key === "Escape") {
        event.stopImmediatePropagation();
        setMenuOpen(false);
        menuToggleRef.current?.focus();
        return;
      }
      if (!menuWrapRef.current?.contains(event.target as Node)) return;
      if (event.key === "Tab") {
        setMenuOpen(false);
        return;
      }
      const items = menuItemRefs.current.filter(
        (el): el is HTMLButtonElement => el?.isConnected === true,
      );
      if (items.length === 0) return;
      const current = items.indexOf(document.activeElement as HTMLButtonElement);
      if (event.key === "ArrowDown") {
        event.preventDefault();
        items[current < 0 ? 0 : (current + 1) % items.length]?.focus();
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        items[
          current < 0 ? items.length - 1 : (current - 1 + items.length) % items.length
        ]?.focus();
      }
    }
    function onOutside(event: globalThis.MouseEvent | FocusEvent): void {
      if (menuWrapRef.current && !menuWrapRef.current.contains(event.target as Node)) {
        setMenuOpen(false);
      }
    }
    window.addEventListener("keydown", onWindowKeyDown);
    window.addEventListener("mousedown", onOutside);
    window.addEventListener("focusin", onOutside);
    menuItemRefs.current[0]?.focus();
    return () => {
      window.removeEventListener("keydown", onWindowKeyDown);
      window.removeEventListener("mousedown", onOutside);
      window.removeEventListener("focusin", onOutside);
    };
  }, [menuOpen]);

  function closeMenu(): void {
    setMenuOpen(false);
    menuToggleRef.current?.focus();
  }

  // A mousedown directly on the transparent backdrop (not on the panel or anything inside it)
  // dismisses it, the same gesture Search/SearchPalette.tsx and Shell/RailDrawer.tsx use.
  function handleBackdropMouseDown(event: MouseEvent<HTMLDivElement>): void {
    if (event.target === event.currentTarget) onClose();
  }

  const full = isFullWindow(viewport);
  const preferred: PanelSize = {
    width: preview.width ?? wanted.width,
    height: preview.height ?? wanted.height,
  };
  const rect = full ? null : placePanel(anchor, viewport, preferred);
  const style: CSSProperties = rect
    ? { left: rect.left, top: rect.top, width: rect.width, height: rect.height }
    : {};
  const entries = menuEntries(view);
  // The resize handles need both: not full window, and a caller that actually wants a size
  // saved. Carrying `rect`'s own placement maths through one variable means every attribute
  // and handle below reads (and TypeScript narrows) one value instead of re-deriving "is this
  // the resizable case" at each site.
  const resizeRect = onSaveSize ? rect : null;

  function previewWidth(next: number): void {
    setPreview((current) => ({ ...current, width: next }));
  }

  function previewHeight(next: number): void {
    setPreview((current) => ({ ...current, height: next }));
  }

  function commitWidth(next: number, intent: ResizeIntent): void {
    setPreview((current) => ({ ...current, width: null }));
    const width = intent === "reset" ? PANEL_WIDTH : next;
    setWanted((current) => ({ ...current, width }));
    onSaveSize?.(`${width},${wanted.height}`);
  }

  function commitHeight(next: number, intent: ResizeIntent): void {
    setPreview((current) => ({ ...current, height: null }));
    const height = intent === "reset" ? PANEL_HEIGHT : next;
    setWanted((current) => ({ ...current, height }));
    onSaveSize?.(`${wanted.width},${height}`);
  }

  return (
    // The backdrop is a dismiss-on-click-outside shield, not a control — the dialog is the panel
    // below, which already carries role="dialog". It stays a static div (SearchPalette's own
    // backdrop is the same shape).
    // biome-ignore lint/a11y/noStaticElementInteractions: see above
    <div className="dloop-panel-backdrop" onMouseDown={handleBackdropMouseDown}>
      <div
        ref={dialogRef}
        id={resizeRect ? dialogId : undefined}
        className={full ? "dloop-panel dloop-panel--full" : "dloop-panel"}
        style={style}
        role="dialog"
        aria-modal="true"
        aria-label="VIKI"
        tabIndex={-1}
        data-grow-x={resizeRect?.growX}
        data-grow-y={resizeRect?.growY}
      >
        <header className="dloop-panel-header">
          <div className="dloop-panel-heading">
            <h2 className="dloop-panel-title">VIKI</h2>
            <div className="dloop-panel-menu-wrap" ref={menuWrapRef}>
              <button
                type="button"
                ref={menuToggleRef}
                className="btn txt sm dloop-panel-menu-toggle"
                aria-label="More"
                aria-haspopup="menu"
                aria-expanded={menuOpen}
                onClick={() => setMenuOpen((shown) => !shown)}
              >
                ⋯
              </button>
              {menuOpen ? (
                <div className="dloop-panel-menu menu-panel" role="menu" aria-label="More">
                  {entries.map((entry, index) => (
                    <button
                      key={entry.choice}
                      type="button"
                      role="menuitem"
                      className="mi"
                      ref={(el) => {
                        menuItemRefs.current[index] = el;
                      }}
                      onClick={() => {
                        closeMenu();
                        onMenu(entry.choice);
                      }}
                    >
                      {entry.label}
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
          </div>
          <p className="dloop-panel-status">{statusLine(view, nowMs)}</p>
          {view.state === "off" ? (
            <button
              type="button"
              className="btn txt sm dloop-panel-turn-on"
              onClick={() => onMenu("open_decision")}
            >
              Turn on in Decision
            </button>
          ) : null}
        </header>
        <div className="dloop-panel-body">
          {checking ? (
            <Loading>Checking…</Loading>
          ) : view.items.length > 0 ? (
            <div className="dloop-panel-items">
              {view.items.map((item) => (
                <ItemCard
                  key={item.item_key}
                  item={item}
                  onAction={(action) => onAction(action, item)}
                  onFeedback={(value, reason) => onFeedback(item, value, reason)}
                />
              ))}
            </div>
          ) : (
            <p className="dloop-panel-empty">Nothing needs you right now.</p>
          )}
        </div>
        {view.review.due ? (
          <div className="dloop-panel-review">
            <p className="dloop-panel-review-text">{reviewCardText(view.review.count)}</p>
            <div className="dloop-panel-review-actions">
              <button type="button" className="btn go sm" onClick={() => onReview("check")}>
                Check
              </button>
              <button type="button" className="btn alt sm" onClick={() => onReview("skip")}>
                Skip this week
              </button>
            </div>
          </div>
        ) : null}
        <div className="dloop-panel-chat">{chat}</div>
        {resizeRect ? (
          <>
            <ResizeHandle
              label="Resize panel width"
              controls={dialogId}
              orientation="vertical"
              growDirection={resizeRect.growX === "left" ? -1 : 1}
              value={resizeRect.width}
              min={Math.min(MIN_PANEL_WIDTH, resizeRect.maxWidth)}
              max={resizeRect.maxWidth}
              defaultValue={PANEL_WIDTH}
              onPreview={previewWidth}
              onCommit={commitWidth}
            />
            <ResizeHandle
              label="Resize panel height"
              controls={dialogId}
              orientation="horizontal"
              growDirection={resizeRect.growY === "up" ? -1 : 1}
              value={resizeRect.height}
              min={Math.min(MIN_PANEL_HEIGHT, resizeRect.maxHeight)}
              max={resizeRect.maxHeight}
              defaultValue={PANEL_HEIGHT}
              onPreview={previewHeight}
              onCommit={commitHeight}
            />
          </>
        ) : null}
      </div>
    </div>
  );
}
