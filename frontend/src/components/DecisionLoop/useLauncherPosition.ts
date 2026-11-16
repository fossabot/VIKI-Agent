/**
 * useLauncherPosition: where the chat launcher sits, in CSS pixels from the window's
 * bottom-right corner ("right"/"bottom", the same pair a caller passes straight into
 * `style={{ right, bottom }}`). Owns dragging (Pointer Events, a 4 px threshold before a press
 * counts as a drag), Alt+arrow and Home keyboard moves, clamping the whole `size` px circle at
 * least 8 px inside the window, and debouncing the "R,B" save handed to `onSave`.
 *
 * Those pixels are the page's own: the appearance setting scales the page with
 * `html { zoom: var(--font-scale) }`, which scales `right`, `bottom` and `size` but neither
 * `innerWidth`/`innerHeight` nor a pointer's `clientX`/`clientY`. The clamp and the drag
 * therefore divide the window's size and the pointer's travel by that scale; without it the
 * launcher could be parked, and saved, outside the window at every scale above 1.
 *
 * `saved` is adopted at mount, the same moment it is clamped ("on load"), and again whenever it
 * later changes — settings load asynchronously, so the real `saved` value routinely arrives a
 * moment after this hook (and the launcher) have already mounted with none. A missing or
 * malformed value starts from (20, 176), the same default the backend falls back to; a later
 * change that is missing or malformed instead keeps the current position (there is no "on load"
 * fallback to revert to once something real is already showing).
 *
 * A later `saved` change is adopted (parsed, then clamped) unless: a drag or pointer press is
 * in progress, so an external change never yanks the launcher out from under a held pointer; the
 * value is exactly the last string this hook itself handed to `onSave`, its own echo coming back
 * through settings, so a successful save never reads back as a jump; or the value is malformed.
 * An adopted value is a sync, never a move: it is never itself handed back to `onSave`.
 *
 * Every window `resize`, and every change of the scale, re-clamps the current position to fit,
 * but a clamp alone is never a reason to save: only a real move (a drag past the threshold, an
 * arrow nudge or a Home reset) schedules the debounced save, and only while `writable`.
 * `onPointerCancel` restores the position held at the drag's start and drops any save that drag
 * had already scheduled.
 *
 * `endedDrag()` is a read-and-clear flag: it answers `true` exactly once, right after a
 * pointer-up that ended a real drag, so the launcher's click handler (which a browser fires
 * right after that pointer-up) can skip the toggle it would otherwise open. A press-release
 * under the 4 px threshold is a click: it never sets the flag and never moves the launcher.
 */
import { type KeyboardEvent, type PointerEvent, useEffect, useRef, useState } from "react";
import { clampResize } from "../../state/layout";
import { rootScale, subscribeRootScale } from "../Shell/useLayoutMode";

export interface UseLauncherPositionOptions {
  /** The persisted "R,B" string (each 0-9999), or null/undefined when there is none yet. */
  saved: string | null | undefined;
  /** Whether a save can run right now (settings ready and not read-only). */
  writable: boolean;
  /** Called with the next "R,B" string, debounced 300 ms, only while `writable`. */
  onSave: (value: string) => void;
  /** The launcher's diameter in CSS pixels. Default 60. */
  size?: number;
}

export interface LauncherPositionBind {
  onPointerDown: (event: PointerEvent<HTMLButtonElement>) => void;
  onPointerMove: (event: PointerEvent<HTMLButtonElement>) => void;
  onPointerUp: (event: PointerEvent<HTMLButtonElement>) => void;
  onPointerCancel: (event: PointerEvent<HTMLButtonElement>) => void;
  onKeyDown: (event: KeyboardEvent<HTMLButtonElement>) => void;
}

export interface UseLauncherPositionResult {
  right: number;
  bottom: number;
  dragging: boolean;
  bind: LauncherPositionBind;
  /** Reads and clears the "a real drag just ended" flag; see the module doc above. */
  endedDrag: () => boolean;
}

interface Position {
  right: number;
  bottom: number;
}

const MIN_INSET = 8;
const DRAG_THRESHOLD = 4;
const ARROW_STEP = 16;
const SAVE_DEBOUNCE_MS = 300;
// 176 keeps the circle above the message box at rest in a narrow window, so it covers none of
// Send, Steer or Stop there (measured at 400 px and at 720 px zoomed to 200%).
const DEFAULT_POSITION: Position = { right: 20, bottom: 176 };
const SAVED_PATTERN = /^(\d{1,4}),(\d{1,4})$/;

const ARROW_DELTAS: Record<string, Position> = {
  ArrowLeft: { right: ARROW_STEP, bottom: 0 },
  ArrowRight: { right: -ARROW_STEP, bottom: 0 },
  ArrowUp: { right: 0, bottom: ARROW_STEP },
  ArrowDown: { right: 0, bottom: -ARROW_STEP },
};

/** `null` for a missing or malformed value; never defaults, so a caller can tell "nothing
 * usable here" apart from "start from (20, 176)" and choose which one applies. */
function tryParsePosition(saved: string | null | undefined): Position | null {
  const match = saved ? SAVED_PATTERN.exec(saved) : null;
  const right = match?.[1];
  const bottom = match?.[2];
  if (right === undefined || bottom === undefined) return null;
  return { right: Number(right), bottom: Number(bottom) };
}

function parseSaved(saved: string | null | undefined): Position {
  return tryParsePosition(saved) ?? DEFAULT_POSITION;
}

/** Keeps the whole `size` px circle at least `MIN_INSET` px inside the current window. */
function clampPosition(position: Position, size: number): Position {
  const scale = rootScale();
  // Floored: the window is rarely a whole number of scaled pixels wide, and a fractional bound
  // would reach the "R,B" save, which takes whole numbers only and is refused otherwise.
  const maxRight = Math.max(MIN_INSET, Math.floor(window.innerWidth / scale) - size - MIN_INSET);
  const maxBottom = Math.max(MIN_INSET, Math.floor(window.innerHeight / scale) - size - MIN_INSET);
  return {
    right: clampResize(position.right, MIN_INSET, maxRight),
    bottom: clampResize(position.bottom, MIN_INSET, maxBottom),
  };
}

function samePosition(a: Position, b: Position): boolean {
  return a.right === b.right && a.bottom === b.bottom;
}

interface Drag {
  pointerId: number;
  startX: number;
  startY: number;
  startPosition: Position;
  moved: boolean;
}

export function useLauncherPosition({
  saved,
  writable,
  onSave,
  size = 60,
}: UseLauncherPositionOptions): UseLauncherPositionResult {
  const [position, setPosition] = useState<Position>(() => clampPosition(parseSaved(saved), size));
  const [dragging, setDragging] = useState(false);

  // Mirrors the latest writable/onSave for the debounce timer below, which fires after this
  // render (and any number of later ones) have come and gone.
  const writableRef = useRef(writable);
  writableRef.current = writable;
  const onSaveRef = useRef(onSave);
  onSaveRef.current = onSave;

  const dragRef = useRef<Drag | null>(null);
  const endedDragRef = useRef(false);
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // The last string this hook itself handed to `onSave`, so the adoption effect below can tell
  // its own save's eventual echo (through settings) apart from someone else's change.
  const lastSavedRef = useRef<string | null>(null);
  // False only for the very first run of the adoption effect: the lazy `useState` above already
  // adopted `saved` for the mount itself, so that first run must not redo (or second-guess) it.
  const mountedRef = useRef(false);

  function clearSaveTimer(): void {
    if (saveTimer.current === null) return;
    clearTimeout(saveTimer.current);
    saveTimer.current = null;
  }

  // Checked only at fire time, not here: `writable` can still change during the debounce
  // window, and the moment that matters is the one where `onSave` would actually run.
  function scheduleSave(next: Position): void {
    clearSaveTimer();
    saveTimer.current = setTimeout(() => {
      saveTimer.current = null;
      if (!writableRef.current) return;
      const value = `${next.right},${next.bottom}`;
      lastSavedRef.current = value;
      onSaveRef.current(value);
    }, SAVE_DEBOUNCE_MS);
  }

  /** A real move: updates the visible position and (re)starts the save debounce. */
  function commit(next: Position): void {
    setPosition(next);
    scheduleSave(next);
  }

  // Re-clamp to fit the window on every resize. A clamp alone never schedules a save.
  useEffect(() => {
    function handleResize(): void {
      setPosition((current) => {
        const next = clampPosition(current, size);
        return samePosition(next, current) ? current : next;
      });
    }
    window.addEventListener("resize", handleResize);
    // A scale change shrinks the room without a `resize` event. The scale is also written
    // after this hook mounts at startup, so the mount-time clamp can have run against scale 1.
    const stopScale = subscribeRootScale(handleResize);
    return () => {
      window.removeEventListener("resize", handleResize);
      stopScale();
    };
  }, [size]);

  useEffect(() => {
    return () => {
      if (saveTimer.current !== null) clearTimeout(saveTimer.current);
    };
  }, []);

  // Adopts a `saved` that changes after mount (settings finishing their load, or another window
  // saving) — see the module doc above for the three exceptions. `size` is read fresh but is not
  // itself a reason to re-adopt: only a `saved` change is.
  // biome-ignore lint/correctness/useExhaustiveDependencies: size intentionally not a trigger.
  useEffect(() => {
    if (!mountedRef.current) {
      mountedRef.current = true;
      return;
    }
    if (dragRef.current) return;
    if (saved === lastSavedRef.current) return;
    const parsed = tryParsePosition(saved);
    if (!parsed) return;
    setPosition(clampPosition(parsed, size));
  }, [saved]);

  function onPointerDown(event: PointerEvent<HTMLButtonElement>): void {
    if (event.button !== 0 || !event.isPrimary || dragRef.current) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture?.(event.pointerId);
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      startPosition: position,
      moved: false,
    };
  }

  function onPointerMove(event: PointerEvent<HTMLButtonElement>): void {
    const drag = dragRef.current;
    if (!drag || !event.isPrimary || drag.pointerId !== event.pointerId) return;
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;
    if (!drag.moved) {
      if (Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
      drag.moved = true;
      setDragging(true);
    }
    // The pointer travels in window pixels; the position is in the page's scaled pixels.
    const scale = rootScale();
    commit(
      clampPosition(
        {
          right: drag.startPosition.right - dx / scale,
          bottom: drag.startPosition.bottom - dy / scale,
        },
        size,
      ),
    );
  }

  function onPointerUp(event: PointerEvent<HTMLButtonElement>): void {
    const drag = dragRef.current;
    if (!drag || !event.isPrimary || drag.pointerId !== event.pointerId) return;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    dragRef.current = null;
    if (drag.moved) {
      endedDragRef.current = true;
      setDragging(false);
    }
  }

  function onPointerCancel(event: PointerEvent<HTMLButtonElement>): void {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    dragRef.current = null;
    clearSaveTimer();
    if (drag.moved) {
      setPosition(drag.startPosition);
      setDragging(false);
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLButtonElement>): void {
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    if (event.ctrlKey || event.metaKey) return;
    if (event.key === "Home") {
      event.preventDefault();
      commit(clampPosition(DEFAULT_POSITION, size));
      return;
    }
    const delta = event.altKey ? ARROW_DELTAS[event.key] : undefined;
    if (!delta) return;
    event.preventDefault();
    commit(
      clampPosition(
        { right: position.right + delta.right, bottom: position.bottom + delta.bottom },
        size,
      ),
    );
  }

  function endedDrag(): boolean {
    const was = endedDragRef.current;
    endedDragRef.current = false;
    return was;
  }

  return {
    right: position.right,
    bottom: position.bottom,
    dragging,
    bind: { onPointerDown, onPointerMove, onPointerUp, onPointerCancel, onKeyDown },
    endedDrag,
  };
}
