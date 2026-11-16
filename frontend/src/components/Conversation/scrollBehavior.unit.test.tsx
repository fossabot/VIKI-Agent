import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import {
  textDelta,
  textDone,
  thinkingDone,
  turnDone,
  turnStarted,
  view,
} from "../../accept/m2b/fakes";
import type { SessionView } from "../../state/view";
import { Conversation, type SavedScroll } from "./Conversation";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
const NOW = Date.parse("2026-09-12T10:00:00.000Z");

/** Captures every `ResizeObserver` callback, so a test can play a size change that jsdom, having
 * no layout engine, never reports by itself. */
class StubResizeObserver {
  static callbacks: ResizeObserverCallback[] = [];
  constructor(callback: ResizeObserverCallback) {
    StubResizeObserver.callbacks.push(callback);
  }
  observe(): void {}
  disconnect(): void {}
}

function fireResize(): void {
  for (const callback of StubResizeObserver.callbacks) {
    callback([], StubResizeObserver as unknown as ResizeObserver);
  }
}

/** What a Markdown block dispatches when a parse answer formats it after its own commit. */
function formatLate(container: HTMLElement): void {
  container.querySelector(".md")?.dispatchEvent(new Event("markdownformat", { bubbles: true }));
}

test("search windows mark omitted history before current turns", () => {
  const initial = view([turnStarted, textDone("current")]);
  const current = initial.turns[0];
  if (!current) throw new Error("expected a turn");
  const selected: SessionView = {
    ...initial,
    turns: [{ ...current, id: "selected-history", history: true }, current],
  };
  const { container, rerender } = render(
    <Conversation view={selected} historyGap nowMs={NOW} onOpenUrl={vi.fn()} />,
  );
  expect(container.querySelector('[data-turn-id="selected-history"] [role="note"]')).toBeNull();
  expect(container.querySelector(`[data-turn-id="${current.id}"] [role="note"]`)).not.toBeNull();
  rerender(<Conversation view={selected} nowMs={NOW} onOpenUrl={vi.fn()} />);
  expect(container.querySelector('[role="note"]')).toBeNull();
});

function hostMetrics(host: HTMLElement, height: number): void {
  Object.defineProperty(host, "scrollHeight", { value: height, configurable: true });
  Object.defineProperty(host, "clientHeight", { value: 400, configurable: true });
}

test("switching sessions restores each session's saved top and follow state independently", () => {
  const a = view([turnStarted, textDone("one")]);
  const b = view([turnStarted, textDone("two")]);
  const { container, rerender } = render(
    <div className="conversation-scroll">
      <Conversation
        key="a"
        view={a}
        initialScroll={{ top: 120, following: false }}
        nowMs={NOW}
        onOpenUrl={vi.fn()}
      />
    </div>,
  );
  const host = container.querySelector(".conversation-scroll") as HTMLElement;
  hostMetrics(host, 1200);
  rerender(
    <div className="conversation-scroll">
      <Conversation
        key="a"
        view={a}
        initialScroll={{ top: 120, following: false }}
        nowMs={NOW}
        onOpenUrl={vi.fn()}
      />
    </div>,
  );
  expect(host.scrollTop).toBe(120);
  rerender(
    <div className="conversation-scroll">
      <Conversation
        key="b"
        view={b}
        initialScroll={{ top: 420, following: false }}
        nowMs={NOW}
        onOpenUrl={vi.fn()}
      />
    </div>,
  );
  expect(host.scrollTop).toBe(420);
});

test("history prepend restores the reading anchor after streaming content arrived below", () => {
  const initial = view([turnStarted, textDelta("one")]);
  const { container, rerender } = render(
    <div className="conversation-scroll">
      <Conversation view={initial} nowMs={NOW} onOpenUrl={vi.fn()} />
    </div>,
  );
  const host = container.querySelector(".conversation-scroll") as HTMLElement;
  hostMetrics(host, 1400);
  rerender(
    <div className="conversation-scroll">
      <Conversation view={initial} nowMs={NOW} onOpenUrl={vi.fn()} />
    </div>,
  );
  host.scrollTop = 100;
  host.dispatchEvent(new Event("scroll"));
  Object.defineProperty(host, "scrollHeight", { value: 1800, configurable: true });
  const streamed: SessionView = {
    ...initial,
    turns: initial.turns.map((turn) => ({
      ...turn,
      blocks: turn.blocks.map((block) =>
        block.kind === "text" ? { ...block, text: `${block.text} two` } : block,
      ),
    })),
  };
  rerender(
    <div className="conversation-scroll">
      <Conversation view={streamed} nowMs={NOW} onOpenUrl={vi.fn()} />
    </div>,
  );
  expect(host.scrollTop).toBe(100);
  Object.defineProperty(host, "scrollHeight", { value: 2400, configurable: true });
  const firstTurn = initial.turns[0];
  if (!firstTurn) throw new Error("expected a turn");
  const currentTurns = initial.turns.map((turn, index) => ({ ...turn, id: `current-${index}` }));
  const older: SessionView = {
    ...initial,
    turns: [{ ...firstTurn, id: "older" }, ...currentTurns],
    historyCursor: null,
  };
  rerender(
    <div className="conversation-scroll">
      <Conversation view={older} nowMs={NOW} onOpenUrl={vi.fn()} />
    </div>,
  );
  expect(host.scrollTop).toBe(700);
});

test("a search jump scrolls the target turn into view and focuses it", () => {
  const initial = view([turnStarted, textDone("one")]);
  const target = initial.turns[0];
  if (!target) throw new Error("expected a turn");
  const withHistory: SessionView = {
    ...initial,
    turns: [{ ...target, id: "found", history: true }, target],
  };
  const { container, rerender } = render(
    <div className="conversation-scroll">
      <Conversation
        view={withHistory}
        initialScroll={{ top: 10, following: false }}
        nowMs={NOW}
        onOpenUrl={vi.fn()}
      />
    </div>,
  );
  const host = container.querySelector(".conversation-scroll") as HTMLElement;
  hostMetrics(host, 1200);
  const turnNode = container.querySelector('[data-turn-id="found"]') as HTMLElement;
  turnNode.getBoundingClientRect = () => ({ top: 300 }) as DOMRect;
  host.getBoundingClientRect = () => ({ top: 0 }) as DOMRect;
  const focus = vi.spyOn(turnNode, "focus");
  rerender(
    <div className="conversation-scroll">
      <Conversation
        view={withHistory}
        jumpTo={{ turnId: "found", request: 1 }}
        nowMs={NOW}
        onOpenUrl={vi.fn()}
      />
    </div>,
  );
  expect(host.scrollTop).toBe(10 + 300 - 12);
  expect(focus).toHaveBeenCalledWith({ preventScroll: true });
});

test("streaming while away from the live edge does not drag the reader to bottom", () => {
  const first = view([turnStarted, textDelta("one")]);
  const { container, rerender } = render(
    <div className="conversation-scroll">
      <Conversation view={first} nowMs={NOW} onOpenUrl={vi.fn()} />
    </div>,
  );
  const host = container.querySelector(".conversation-scroll") as HTMLElement;
  hostMetrics(host, 1000);
  rerender(
    <div className="conversation-scroll">
      <Conversation view={first} nowMs={NOW} onOpenUrl={vi.fn()} />
    </div>,
  );
  host.scrollTop = 100;
  host.dispatchEvent(new Event("scroll"));
  Object.defineProperty(host, "scrollHeight", { value: 1900, configurable: true });
  rerender(
    <div className="conversation-scroll">
      <Conversation view={view([textDelta(" two")], first)} nowMs={NOW} onOpenUrl={vi.fn()} />
    </div>,
  );
  expect(host.scrollTop).toBe(100);
});

test("a late Markdown format pins a followed column to the bottom", () => {
  const initial = view([turnStarted, textDelta("one")]);
  const { container } = render(
    <div className="conversation-scroll">
      <Conversation view={initial} nowMs={NOW} onOpenUrl={vi.fn()} />
    </div>,
  );
  const host = container.querySelector(".conversation-scroll") as HTMLElement;
  hostMetrics(host, 1200);
  formatLate(container);
  expect(host.scrollTop).toBe(1200 - 400);
});

test("a late format above a reader who scrolled away leaves scrollTop alone, and a later prepended page restores against the refreshed anchor", () => {
  const initial = view([turnStarted, textDelta("one")]);
  const { container, rerender } = render(
    <div className="conversation-scroll">
      <Conversation view={initial} nowMs={NOW} onOpenUrl={vi.fn()} />
    </div>,
  );
  const host = container.querySelector(".conversation-scroll") as HTMLElement;
  hostMetrics(host, 1200);
  rerender(
    <div className="conversation-scroll">
      <Conversation view={initial} nowMs={NOW} onOpenUrl={vi.fn()} />
    </div>,
  );
  host.scrollTop = 100;
  host.dispatchEvent(new Event("scroll"));
  Object.defineProperty(host, "scrollHeight", { value: 1900, configurable: true });
  formatLate(container);
  expect(host.scrollTop).toBe(100);
  const firstTurn = initial.turns[0];
  if (!firstTurn) throw new Error("expected a turn");
  const currentTurns = initial.turns.map((turn, index) => ({ ...turn, id: `current-${index}` }));
  const older: SessionView = {
    ...initial,
    turns: [{ ...firstTurn, id: "older" }, ...currentTurns],
    historyCursor: null,
  };
  Object.defineProperty(host, "scrollHeight", { value: 2600, configurable: true });
  rerender(
    <div className="conversation-scroll">
      <Conversation view={older} nowMs={NOW} onOpenUrl={vi.fn()} />
    </div>,
  );
  // The format refreshed the anchor to 1900 - 100 = 1800 while the reader stayed put; the
  // prepend restores against that, not a stale pre-format value.
  expect(host.scrollTop).toBe(2600 - 1800);
});

// The launcher panel's own chat has no `.conversation-scroll` three-layer
// wrapper, only `[data-scroll-host]` on its `.dloop-chat-messages` (LauncherChat.tsx). These mirror
// "a late Markdown format pins a followed column to the bottom" and "streaming while away from the
// live edge does not drag the reader to bottom" above, with that marker in place of the class.
test("a data-scroll-host ancestor snaps a followed column to the bottom when a reply grows it", () => {
  const first = view([turnStarted, textDelta("one")]);
  const { container, rerender } = render(
    <div data-scroll-host>
      <Conversation view={first} nowMs={NOW} onOpenUrl={vi.fn()} />
    </div>,
  );
  const host = container.querySelector("[data-scroll-host]") as HTMLElement;
  hostMetrics(host, 1000);
  rerender(
    <div data-scroll-host>
      <Conversation view={first} nowMs={NOW} onOpenUrl={vi.fn()} />
    </div>,
  );
  expect(host.scrollTop).toBe(1000 - 400);

  // A reply lands, growing the content the way VIKI's answer does in the panel.
  Object.defineProperty(host, "scrollHeight", { value: 1900, configurable: true });
  rerender(
    <div data-scroll-host>
      <Conversation view={view([textDelta(" two")], first)} nowMs={NOW} onOpenUrl={vi.fn()} />
    </div>,
  );
  expect(host.scrollTop).toBe(1900 - 400);
});

test("a data-scroll-host ancestor does not drag a reader who scrolled away when a reply lands", () => {
  const first = view([turnStarted, textDelta("one")]);
  const { container, rerender } = render(
    <div data-scroll-host>
      <Conversation view={first} nowMs={NOW} onOpenUrl={vi.fn()} />
    </div>,
  );
  const host = container.querySelector("[data-scroll-host]") as HTMLElement;
  hostMetrics(host, 1000);
  rerender(
    <div data-scroll-host>
      <Conversation view={first} nowMs={NOW} onOpenUrl={vi.fn()} />
    </div>,
  );
  host.scrollTop = 100;
  host.dispatchEvent(new Event("scroll"));
  Object.defineProperty(host, "scrollHeight", { value: 1900, configurable: true });

  rerender(
    <div data-scroll-host>
      <Conversation view={view([textDelta(" two")], first)} nowMs={NOW} onOpenUrl={vi.fn()} />
    </div>,
  );
  expect(host.scrollTop).toBe(100);
});

test("opening a disclosure at the live edge moves nothing", () => {
  vi.stubGlobal("ResizeObserver", StubResizeObserver);
  StubResizeObserver.callbacks = [];
  const settled = view([
    turnStarted,
    thinkingDone("a long thought"),
    textDone("done."),
    turnDone(),
  ]);
  const { container, rerender } = render(
    <div className="conversation-scroll">
      <Conversation view={settled} nowMs={NOW} onOpenUrl={vi.fn()} />
    </div>,
  );
  const host = container.querySelector(".conversation-scroll") as HTMLElement;
  hostMetrics(host, 1000);
  rerender(
    <div className="conversation-scroll">
      <Conversation view={settled} nowMs={NOW} onOpenUrl={vi.fn()} />
    </div>,
  );
  expect(host.scrollTop).toBe(1000 - 400);

  // The thought opens below the button the reader just pressed; the column must stay put.
  fireEvent.click(container.querySelector("button.thought") as HTMLElement);
  expect(container.querySelector(".thought-full")?.textContent).toBe("a long thought");
  Object.defineProperty(host, "scrollHeight", { value: 1600, configurable: true });
  fireResize();
  expect(host.scrollTop).toBe(1000 - 400);
});

test("a remount puts the first visible turn back at its offset when the content above it changed height", () => {
  const first = view([turnStarted, textDone("one")]).turns[0];
  if (!first) throw new Error("expected a turn");
  const twoTurns: SessionView = {
    ...view([turnStarted, textDone("one")]),
    turns: [
      { ...first, id: "a" },
      { ...first, id: "b" },
    ],
  };
  // A layout jsdom does not have: each turn is 900 px tall at its own top, the box is 400 px tall
  // at 0, and a turn's rect moves with the box's scrollTop.
  const layoutTop: Record<string, number> = { a: 0, b: 1000 };
  const rect = vi
    .spyOn(HTMLElement.prototype, "getBoundingClientRect")
    .mockImplementation(function (this: HTMLElement) {
      const turnId = this.dataset.turnId;
      if (turnId === undefined) return { top: 0, bottom: 400 } as DOMRect;
      const host = this.closest(".conversation-scroll") as HTMLElement;
      const top = (layoutTop[turnId] ?? 0) - host.scrollTop;
      return { top, bottom: top + 900 } as DOMRect;
    });
  let remembered: SavedScroll | undefined;
  const remember = (value: SavedScroll) => {
    remembered = value;
  };
  const { container, rerender } = render(
    <div className="conversation-scroll">
      <Conversation
        key="first"
        view={twoTurns}
        initialScroll={{ top: 0, following: false }}
        onRememberScroll={remember}
        nowMs={NOW}
        onOpenUrl={vi.fn()}
      />
    </div>,
  );
  const host = container.querySelector(".conversation-scroll") as HTMLElement;
  hostMetrics(host, 3000);
  host.scrollTop = 1010;
  host.dispatchEvent(new Event("scroll"));
  expect(remembered).toEqual({ top: 1010, following: false, turnId: "b", offset: -10 });

  // Back after a switch: everything above "b" now lays out 300 px taller (a late format, say).
  const saved = remembered;
  layoutTop.b = 1300;
  rerender(
    <div className="conversation-scroll">
      <Conversation
        key="again"
        view={twoTurns}
        initialScroll={saved}
        onRememberScroll={remember}
        nowMs={NOW}
        onOpenUrl={vi.fn()}
      />
    </div>,
  );
  expect(host.scrollTop).toBe(1310);
  rect.mockRestore();
});
