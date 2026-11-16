import { type RefObject, useLayoutEffect, useRef } from "react";

const FOCUSABLE =
  'a[href], area[href], button, input, select, textarea, details > summary, [contenteditable="true"], [tabindex]';

const modalStack: symbol[] = [];
const backgroundInert = new WeakMap<Element, { owners: number; original: string | null }>();

export interface ModalFocusOptions {
  active: boolean;
  dialogRef: RefObject<HTMLElement | null>;
  initialFocusRef: RefObject<HTMLElement | null>;
  onDismiss: () => void;
  /** Used when the invoking control was removed while the dialog was open. */
  restoreFocus?: (() => HTMLElement | null) | undefined;
}

function focusableInside(dialog: HTMLElement): HTMLElement[] {
  return [...dialog.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((element) => {
    if (element.matches(":disabled") || element.closest("[hidden], [inert]")) return false;
    if (
      element.matches("details > summary") &&
      element.parentElement?.querySelector(":scope > summary") !== element
    )
      return false;
    if (element.tabIndex < 0) return false;
    for (
      let node: HTMLElement | null = element;
      node && node !== dialog;
      node = node.parentElement
    ) {
      const parent: HTMLElement | null = node.parentElement;
      if (
        parent instanceof HTMLDetailsElement &&
        !parent.open &&
        node !== parent.querySelector(":scope > summary")
      )
        return false;
      const style = window.getComputedStyle(node);
      if (style.display === "none" || style.visibility === "hidden") return false;
    }
    return true;
  });
}

/** Keep a dialog in its own DOM position while providing modal keyboard and focus behavior. */
export function useModalFocus({
  active,
  dialogRef,
  initialFocusRef,
  onDismiss,
  restoreFocus,
}: ModalFocusOptions): void {
  const dismissRef = useRef(onDismiss);
  const restoreRef = useRef(restoreFocus);
  dismissRef.current = onDismiss;
  restoreRef.current = restoreFocus;

  useLayoutEffect(() => {
    const dialog = dialogRef.current;
    if (!active || !dialog) return;
    const panel: HTMLElement = dialog;

    const id = Symbol("modal");
    const opener =
      document.activeElement instanceof HTMLElement && document.activeElement !== document.body
        ? document.activeElement
        : null;
    const inerted = new Set<Element>();
    const isTop = () => modalStack[modalStack.length - 1] === id;
    modalStack.push(id);

    // A dialog may be nested inside the app shell or next to it (SearchPalette).
    // Inert the siblings at each ancestor, preserving any pre-existing inert attribute.
    function inertBackground(): void {
      let branch: Element | null = panel;
      while (branch?.parentElement) {
        const parent: HTMLElement = branch.parentElement;
        for (const sibling of parent.children) {
          if (sibling === branch || inerted.has(sibling)) continue;
          inerted.add(sibling);
          // Nested dialogs can release in either order. Keep the first owner's original value
          // until the final dialog releases this background branch.
          const state = backgroundInert.get(sibling);
          if (state) state.owners += 1;
          else backgroundInert.set(sibling, { owners: 1, original: sibling.getAttribute("inert") });
          sibling.setAttribute("inert", "");
        }
        if (parent === document.body) break;
        branch = parent;
      }
    }

    inertBackground();
    // React may mount a sibling after the dialog. Newly added background branches must be inert
    // for the same lifetime as the branches present at entry.
    const observer = new MutationObserver(inertBackground);
    observer.observe(document.body, { childList: true, subtree: true });

    function focusFirst(): void {
      const preferred = initialFocusRef.current;
      const items = focusableInside(panel);
      const first = items[0];
      // preventScroll: this runs in useLayoutEffect, before a right-side rail-drawer's
      // slide-in has painted a frame, so its Close button still sits off-screen at
      // translateX(100%). A plain focus() scrolls .shell (the nearest scroll container,
      // Shell.css's overflow: hidden does not stop it being scrolled to) to reveal it,
      // jerking the whole window 226-340px left and back over the 160ms slide. The entry
      // target (Close, or a dialog's first control) is already the one thing guaranteed
      // visible once the panel lands, so nothing here needs the browser's scroll-into-view.
      if (preferred?.isConnected && items.includes(preferred))
        preferred.focus({ preventScroll: true });
      else if (first) first.focus({ preventScroll: true });
      else panel.focus({ preventScroll: true });
    }

    function onFocusIn(event: FocusEvent): void {
      if (isTop() && !panel.contains(event.target as Node)) focusFirst();
    }

    function onKeyDown(event: KeyboardEvent): void {
      if (!isTop() || event.isComposing || event.keyCode === 229) return;
      if (event.key === "Escape") {
        // Escape closes only the top transient surface. A menu open inside the
        // panel (a session row's menu in a rail drawer) owns this Escape; its own window
        // listener closes it, and the next Escape reaches the dialog. A resize handle mid-drag
        // (`.resize-handle[data-dragging]`, ResizeHandle.tsx) owns it the same way; its own
        // window listener cancels the drag, and the next Escape reaches the dialog.
        if (panel.querySelector('[role="menu"], .resize-handle[data-dragging]')) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        dismissRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const items = focusableInside(panel);
      event.preventDefault();
      event.stopImmediatePropagation();
      if (items.length === 0) {
        panel.focus();
        return;
      }
      const index = items.indexOf(document.activeElement as HTMLElement);
      const next =
        index < 0
          ? event.shiftKey
            ? items.length - 1
            : 0
          : (index + (event.shiftKey ? -1 : 1) + items.length) % items.length;
      items[next]?.focus();
    }

    document.addEventListener("focusin", onFocusIn, true);
    window.addEventListener("keydown", onKeyDown, true);
    focusFirst();

    return () => {
      observer.disconnect();
      document.removeEventListener("focusin", onFocusIn, true);
      window.removeEventListener("keydown", onKeyDown, true);
      const wasTop = isTop();
      const index = modalStack.indexOf(id);
      if (index >= 0) modalStack.splice(index, 1);
      for (const element of inerted) {
        const state = backgroundInert.get(element);
        if (!state) continue;
        state.owners -= 1;
        if (state.owners > 0) continue;
        if (state.original === null) element.removeAttribute("inert");
        else element.setAttribute("inert", state.original);
        backgroundInert.delete(element);
      }
      if (!wasTop) return;
      const target = opener?.isConnected ? opener : restoreRef.current?.();
      // preventScroll, the same reason focusFirst above gives it: a background element's own
      // geometry can still be mid-transition (or, for a bar past its own local width under zoom,
      // genuinely overflowing) when a dialog closes, and a plain focus() would scroll the nearest
      // ancestor to reveal it — leaving .shell scrolled sideways after the dialog is long gone.
      if (target?.isConnected) target.focus({ preventScroll: true });
    };
  }, [active, dialogRef, initialFocusRef]);
}
