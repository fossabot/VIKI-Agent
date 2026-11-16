import { type KeyboardEvent, type PointerEvent, useLayoutEffect, useRef } from "react";
import { clampResize } from "../../state/layout";
import { rootScale } from "./useLayoutMode";
import "./ResizeHandle.css";

export interface ResizeHandleProps {
  label: string;
  controls: string;
  orientation: "vertical" | "horizontal";
  growDirection: 1 | -1;
  value: number;
  min: number;
  max: number;
  defaultValue: number;
  collapsible?: boolean;
  /** Commit "reset" on a double-click even when the bounded default already equals the current
   * value; only the composer's handle wants that — a squeezed rail would otherwise commit
   * a visible no-op and drop the user's saved width. Default false. */
  resetAtEqual?: boolean;
  onPreview: (value: number) => void;
  onCommit: (value: number, intent: "resize" | "collapse" | "restore" | "reset") => void;
}

export function ResizeHandle(props: ResizeHandleProps) {
  const { value, min, max } = props;
  const remembered = useRef(value || props.defaultValue);
  const bounded = (next: number) => clampResize(next, min, max);
  const latest = useRef(props);
  latest.current = props;
  const drag = useRef<{
    id: number;
    at: number;
    scale: number;
    start: number;
    preview: number;
    release(): void;
  } | null>(null);

  function stop(intent: "commit" | "cancel" | "discard", final?: number) {
    const active = drag.current;
    if (!active) return;
    drag.current = null;
    active.release();
    if (intent === "discard") return;
    const current = latest.current;
    const next =
      final ??
      (active.start === 0 && current.collapsible
        ? 0
        : clampResize(active.start, current.min, current.max));
    if (next !== active.preview || intent === "cancel") current.onPreview(next);
    if (intent === "commit" && next !== active.start) current.onCommit(next, "resize");
  }

  function coordinate(event: PointerEvent<HTMLDivElement>) {
    return props.orientation === "vertical" ? event.clientX : event.clientY;
  }

  function onPointerDown(event: PointerEvent<HTMLDivElement>) {
    if (event.button !== 0 || !event.isPrimary || drag.current) return;
    const target = event.currentTarget;
    const id = event.pointerId;
    target.focus({ preventScroll: true });
    event.preventDefault();
    target.setPointerCapture(id);
    // Marks the element already captured above, for the drag's duration only (cleared in
    // release() below): a dialog's useModalFocus checks for `.resize-handle[data-dragging]`
    // inside it the same way it already does for an open `[role="menu"]`, so Escape cancels this
    // drag instead of dismissing the dialog.
    target.setAttribute("data-dragging", "");
    const cancel = () => stop("cancel");
    const onEscape = (key: globalThis.KeyboardEvent) => {
      if (key.isComposing || key.keyCode === 229) return;
      if (key.ctrlKey || key.altKey || key.metaKey) return;
      if (key.key !== "Escape") return;
      key.preventDefault();
      cancel();
    };
    window.addEventListener("blur", cancel);
    window.addEventListener("keydown", onEscape);
    drag.current = {
      id,
      at: coordinate(event),
      scale: rootScale(),
      start: value,
      preview: value,
      release() {
        window.removeEventListener("blur", cancel);
        window.removeEventListener("keydown", onEscape);
        if (target.hasPointerCapture(id)) target.releasePointerCapture(id);
        target.removeAttribute("data-dragging");
      },
    };
  }

  function onPointer(
    event: PointerEvent<HTMLDivElement>,
    intent: "move" | "commit" | "cancel" = "move",
  ) {
    const active = drag.current;
    if (!active || !event.isPrimary || active.id !== event.pointerId) return;
    if (intent === "cancel" || rootScale() !== active.scale) return stop("cancel");
    // Pointer coordinates are viewport pixels; controlled sizes are local layout pixels.
    const raw =
      active.start + ((coordinate(event) - active.at) / active.scale) * props.growDirection;
    const next = active.start === 0 && props.collapsible && raw <= 0 ? 0 : bounded(raw);
    if (intent === "commit") stop("commit", next);
    else if (next !== active.preview) {
      active.preview = next;
      props.onPreview(next);
    }
  }

  // biome-ignore lint/correctness/useExhaustiveDependencies: stop reads refs; only a changed controlled value cancels.
  useLayoutEffect(() => {
    const active = drag.current;
    if (active && active.preview !== value) stop("cancel");
  }, [value]);
  // biome-ignore lint/correctness/useExhaustiveDependencies: configuration changes cancel; stop reads current refs.
  useLayoutEffect(
    () => stop("cancel"),
    [min, max, props.controls, props.orientation, props.growDirection, props.collapsible],
  );
  // biome-ignore lint/correctness/useExhaustiveDependencies: unmount-only cleanup reads current drag and props refs.
  useLayoutEffect(() => () => stop("discard"), []);

  function change(next: number, intent: Parameters<ResizeHandleProps["onCommit"]>[1]) {
    if (next === value) return;
    props.onPreview(next);
    props.onCommit(next, intent);
  }

  function reset() {
    if (drag.current) return;
    remembered.current = bounded(props.defaultValue);
    if (props.resetAtEqual && remembered.current === value) props.onCommit(value, "reset");
    else change(remembered.current, "reset");
  }

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (drag.current) return;
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    if (event.ctrlKey || event.altKey || event.metaKey) return;
    const forward = props.orientation === "vertical" ? "ArrowRight" : "ArrowDown";
    const backward = props.orientation === "vertical" ? "ArrowLeft" : "ArrowUp";
    let intent: Parameters<ResizeHandleProps["onCommit"]>[1] = "resize";
    let next: number;
    if (event.key === forward || event.key === backward) {
      const direction = event.key === forward ? 1 : -1;
      next = bounded(value + direction * props.growDirection * (event.shiftKey ? 50 : 10));
    } else if (event.key === "Home") {
      next = min;
    } else if (event.key === "End") {
      next = max;
    } else if (event.key === "Enter" && props.collapsible) {
      if (value !== 0) remembered.current = value;
      next = value === 0 ? bounded(remembered.current) : 0;
      intent = value === 0 ? "restore" : "collapse";
    } else if (event.key === "Enter" && !props.collapsible) {
      event.preventDefault();
      reset();
      return;
    } else return;
    event.preventDefault();
    change(next, intent);
  }

  return (
    // biome-ignore lint/a11y/useSemanticElements: this adjustable separator exposes a controlled pane size.
    <div
      className="resize-handle"
      role="separator"
      tabIndex={0}
      aria-label={props.label}
      aria-controls={props.controls}
      aria-orientation={props.orientation}
      aria-valuenow={value}
      aria-valuemin={props.collapsible ? 0 : min}
      aria-valuemax={max}
      aria-valuetext={value === 0 ? "Collapsed" : `${value} pixels`}
      onPointerDown={onPointerDown}
      onPointerMove={onPointer}
      onPointerUp={(event) => onPointer(event, "commit")}
      onPointerCancel={(event) => onPointer(event, "cancel")}
      onLostPointerCapture={(event) => onPointer(event, "cancel")}
      onKeyDown={onKeyDown}
      onDoubleClick={reset}
    />
  );
}
