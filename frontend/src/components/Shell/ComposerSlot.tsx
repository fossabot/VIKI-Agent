import { type ReactNode, useId, useLayoutEffect, useRef, useState } from "react";
import { clampResize } from "../../state/layout";
import { ResizeHandle } from "./ResizeHandle";

/** Each chat pane owns its composer's measured bounds and manual height. */
export function ComposerSlot({ children }: { children: ReactNode }) {
  const id = useId();
  const [slot, setSlot] = useState<HTMLDivElement | null>(null);
  const [size, setSize] = useState<{
    value: number;
    min: number;
    max: number;
    natural: number;
  } | null>(null);
  const drag = useRef<{ target: HTMLElement; id: number; height: string } | null>(null);
  const measureSlot = useRef(() => {});
  useLayoutEffect(() => {
    if (!slot) return;
    let active = true;
    let natural = 0;
    const measure = () => {
      if (!active) return;
      const style = getComputedStyle(slot);
      const minimum = Number.parseFloat(style.getPropertyValue("--composer-min-height"));
      const maximum = Math.floor(
        Number.parseFloat(style.getPropertyValue("--composer-max-height")),
      );
      const height = slot.offsetHeight;
      if (
        !slot.querySelector('[data-region="composer"]') ||
        !Number.isFinite(minimum) ||
        !Number.isFinite(maximum) ||
        maximum <= 0 ||
        height <= 0
      ) {
        setSize(null);
        return;
      }
      const min = Math.min(Math.max(0, Math.ceil(minimum)), maximum);
      if (!slot.style.height) natural = height;
      const next = {
        value: clampResize(height, min, maximum),
        min,
        max: maximum,
        natural: clampResize(natural || height, min, maximum),
      };
      setSize((current) =>
        current &&
        current.value === next.value &&
        current.min === next.min &&
        current.max === next.max &&
        current.natural === next.natural
          ? current
          : next,
      );
    };
    let observedWidth = slot.clientWidth;
    let observedHeight = slot.clientHeight;
    const observer =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver((entries) => {
            if (!active) return;
            const rect = entries.find((item) => item.target === slot)?.contentRect;
            const width = rect?.width ?? slot.clientWidth;
            const height = rect?.height ?? slot.clientHeight;
            if (width === observedWidth && height === observedHeight) return;
            observedWidth = width;
            observedHeight = height;
            measure();
          });
    observer?.observe(slot);
    const bounds = new MutationObserver(measure);
    bounds.observe(slot, { attributes: true, attributeFilter: ["style"], childList: true });
    measureSlot.current = measure;
    measure();
    window.addEventListener("resize", measure);
    return () => {
      active = false;
      observer?.disconnect();
      bounds.disconnect();
      drag.current = null;
      measureSlot.current = () => {};
      window.removeEventListener("resize", measure);
    };
  }, [slot]);
  const writeHeight = (height: string) => {
    if (!slot) return;
    slot.style.height = height;
    if (!height) window.dispatchEvent(new Event("resize"));
    measureSlot.current();
  };
  const reset = () => {
    const current = drag.current;
    if (current) {
      current.height = "";
      if (current.target.hasPointerCapture(current.id))
        current.target.releasePointerCapture(current.id);
    }
    if (slot) slot.style.height = "";
    window.dispatchEvent(new Event("resize"));
    measureSlot.current();
  };
  return (
    <div className="composer-slot" ref={setSlot} id={size ? `${id}-composer-slot` : undefined}>
      {children}
      {size && slot && (
        <div
          className="composer-resize-controls"
          title="Drag to resize. Double-click or press Enter to reset."
          onPointerDownCapture={(event) => {
            if (event.button !== 0 || !event.isPrimary) return;
            if (
              !(event.target instanceof HTMLElement) ||
              event.target.getAttribute("role") !== "separator"
            )
              return;
            if (drag.current?.target.hasPointerCapture(drag.current.id)) return;
            drag.current = {
              target: event.target,
              id: event.pointerId,
              height: slot.style.height,
            };
          }}
          onPointerUp={(event) => {
            const current = drag.current;
            if (
              current &&
              event.isPrimary &&
              event.pointerId === current.id &&
              !current.target.hasPointerCapture(current.id)
            ) {
              drag.current = null;
              writeHeight(current.height);
            }
          }}
        >
          <ResizeHandle
            label="Resize composer"
            controls={`${id}-composer-slot`}
            orientation="horizontal"
            growDirection={-1}
            value={size.value}
            min={size.min}
            max={size.max}
            defaultValue={size.natural}
            resetAtEqual
            onPreview={(next) => {
              const current = drag.current;
              if (current && !current.target.hasPointerCapture(current.id)) {
                drag.current = null;
                writeHeight(current.height);
              } else writeHeight(`${next}px`);
            }}
            onCommit={(next, intent) => {
              drag.current = null;
              if (intent === "reset") reset();
              else writeHeight(`${next}px`);
            }}
          />
        </div>
      )}
    </div>
  );
}
