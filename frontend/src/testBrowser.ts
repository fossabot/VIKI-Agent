import { afterEach } from "vitest";

// jsdom has no matchMedia. Keep the existing docked-shell/no-preferences defaults;
// responsive and preference tests install their own media-query fakes.
if (typeof window !== "undefined") {
  window.matchMedia ??= (query: string): MediaQueryList => {
    const events = new EventTarget();
    return Object.assign(events, {
      matches: query.startsWith("(min-width:"),
      media: query,
      onchange: null,
      addListener(listener: (event: MediaQueryListEvent) => void) {
        events.addEventListener("change", listener as EventListener);
      },
      removeListener(listener: (event: MediaQueryListEvent) => void) {
        events.removeEventListener("change", listener as EventListener);
      },
    });
  };
  // jsdom cannot paint Canvas2D. Graphic unit tests supply a context; browser tests paint for real.
  HTMLCanvasElement.prototype.getContext = () => null;
  afterEach(() => {
    // jsdom retains sheets when a containing element is removed. Clear only detached owners
    // after test cleanup; connected styles remain available to visibility and appearance checks.
    for (const sheet of [...document.styleSheets]) {
      const owner = sheet.ownerNode;
      if (owner instanceof HTMLStyleElement && !owner.isConnected) owner.textContent = "";
    }
  });
}
