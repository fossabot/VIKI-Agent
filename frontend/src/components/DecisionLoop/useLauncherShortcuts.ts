/**
 * The two in-app shortcuts that work from anywhere in the window, a text box included:
 * Alt+V opens or closes VIKI's panel, Alt+T starts or ends a voice conversation with VIKI. Both
 * are matched on the physical key (`KeyboardEvent.code`), not the character it produces, so
 * macOS's own Option-key remap (⌥V types "√", ⌥T types "†" in a text box) never stops the match.
 * There is no text-entry guard: the match already requires Alt held with no other modifier,
 * which plain typing never produces.
 *
 * `launcherShortcutOf` is a pure function of one event, independent of the hook below, so a
 * caller — or a test — that only needs the matching logic can use it without mounting anything.
 */
import { useEffect, useRef } from "react";
import { isMac } from "../../platform";

export type LauncherShortcut = "panel" | "voice";

/** The `aria-keyshortcuts` value and the on-platform label for one of the two shortcuts, wired
 * onto the launcher and the panel's Hands-free button. */
export function launcherShortcutHint(
  which: LauncherShortcut,
  mac: boolean = isMac(),
): { aria: string; label: string } {
  return which === "panel"
    ? { aria: "Alt+V", label: mac ? "⌥V" : "Alt+V" }
    : { aria: "Alt+T", label: mac ? "⌥T" : "Alt+T" };
}

/** Which shortcut, if any, a keydown `event` is for. Refuses an IME candidate window (either
 * signal browsers use for it: `isComposing` or a `keyCode` of 229), a held-key repeat, or any
 * modifier besides a lone Alt — which also rules out AltGr (Ctrl+Alt) on Windows. Matched by
 * physical key (`code`), not `key`, so the character a layout or macOS's Option key produces
 * never matters; see the module doc. */
export function launcherShortcutOf(event: KeyboardEvent): LauncherShortcut | null {
  if (event.isComposing || event.keyCode === 229 || event.repeat) return null;
  if (event.ctrlKey || event.metaKey || event.shiftKey || !event.altKey) return null;
  if (event.code === "KeyV") return "panel";
  if (event.code === "KeyT") return "voice";
  return null;
}

/**
 * One `window` keydown listener (bubble phase), present only while `enabled`. `handlers` is read
 * through a ref rather than closed over directly, so a caller that passes a new object every
 * render (an inline literal, as every caller here does) never tears the listener down and rebuilds
 * it — only a change in `enabled` does.
 *
 * A match with no handler for it, an already `defaultPrevented` event (another handler claimed
 * the key first), or any open `[aria-modal="true"]` element besides VIKI's own panel
 * (`.dloop-panel`) all do nothing. The last of those is why both keys keep working while VIKI's
 * panel itself is the open dialog without also firing under an unrelated modal
 * (the command palette, a confirm dialog) that should keep its own keys to itself.
 */
export function useLauncherShortcuts(
  enabled: boolean,
  handlers: Partial<Record<LauncherShortcut, () => void>>,
): void {
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;

  useEffect(() => {
    if (!enabled) return;

    function onKeyDown(event: KeyboardEvent): void {
      const which = launcherShortcutOf(event);
      if (which === null) return;
      const handler = handlersRef.current[which];
      if (!handler) return;
      if (event.defaultPrevented) return;
      if (document.querySelector('[aria-modal="true"]:not(.dloop-panel)')) return;
      event.preventDefault();
      handler();
    }

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [enabled]);
}
