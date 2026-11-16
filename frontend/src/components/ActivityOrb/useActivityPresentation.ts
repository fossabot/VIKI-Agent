import { type RefObject, useEffect, useState } from "react";

const MOTION_QUERIES = [
  "(prefers-reduced-motion: reduce)",
  "(forced-colors: active)",
  "(prefers-contrast: more)",
];

/** App-level presentation gates for the decorative orb and beam: motion, contrast and theme. */
export function useActivityPresentation(ref: RefObject<HTMLElement | null>) {
  const [presentation, setPresentation] = useState(() => ({
    visible: !document.hidden,
    motion:
      !document.hidden &&
      document.documentElement.dataset.motion !== "reduce" &&
      !MOTION_QUERIES.some(
        (query) => typeof matchMedia === "function" && matchMedia(query).matches,
      ),
    theme:
      document.documentElement.dataset.theme === "light" ? ("light" as const) : ("dark" as const),
  }));
  useEffect(() => {
    let disposed = false;
    let intersecting = true;
    const preferences = MOTION_QUERIES.flatMap((query) =>
      typeof matchMedia === "function" ? [matchMedia(query)] : [],
    );
    const update = () => {
      if (disposed) return;
      const visible = !document.hidden && intersecting;
      const motion =
        visible &&
        document.documentElement.dataset.motion !== "reduce" &&
        !preferences.some((preference) => preference.matches);
      const theme = document.documentElement.dataset.theme === "light" ? "light" : "dark";
      setPresentation((previous) =>
        previous.visible === visible && previous.motion === motion && previous.theme === theme
          ? previous
          : { visible, motion, theme },
      );
    };
    const observer =
      typeof IntersectionObserver === "undefined"
        ? null
        : new IntersectionObserver(([entry]) => {
            if (disposed || !entry) return;
            intersecting = entry.isIntersecting;
            update();
          });
    if (ref.current) observer?.observe(ref.current);
    const appearance = new MutationObserver(update);
    appearance.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-motion", "data-theme"],
    });
    for (const preference of preferences) preference.addEventListener("change", update);
    document.addEventListener("visibilitychange", update);
    update();
    return () => {
      disposed = true;
      observer?.disconnect();
      appearance.disconnect();
      document.removeEventListener("visibilitychange", update);
      for (const preference of preferences) preference.removeEventListener("change", update);
    };
  }, [ref]);
  return presentation;
}
