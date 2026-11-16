/**
 * Unit tests pinning behaviour of the conversation column that conversation.accept.test.tsx
 * (frozen) cannot observe on its own — see each test for why.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import {
  AT,
  type Item,
  subagentStarted,
  textDelta,
  textDone,
  turnDone,
  turnStarted,
  userMessage,
  view,
} from "../../accept/m2b/fakes";
import { applyHistoryPage } from "../../state/sessionReducer";
import { emptyView, type SessionView, type Turn } from "../../state/view";
import { readCss } from "../../testing/css";
import { DEFAULT_IDENTITY, IdentityContext } from "../Avatar/identity";
import { Conversation, Reply, UserTurn } from "./Conversation";

// Memoization is observable only as "this block was NOT rendered again", and the real Markdown
// offers a test nothing to count — a bail-out produces no DOM mutation, so the rendered tree is
// byte-identical either way. This stub stands in for it and records every render it is asked
// for. It replaces `./Markdown` for the whole file; no other test here reads markdown output.
const { markdownRenders } = vi.hoisted(() => ({
  markdownRenders: vi.fn<(text: string) => void>(),
}));

vi.mock("./Markdown", () => ({
  Markdown: ({ text }: { text: string }) => {
    markdownRenders(text);
    return <div className="md">{text}</div>;
  },
}));

afterEach(cleanup);

const NOW = Date.parse("2026-09-12T10:00:00.000Z");

// jsdom applies no CSS from stylesheets (only inline styles), so the layout fixes below are
// checked against the source, the same way Shell.test.tsx checks Shell.css.
const css = readCss(import.meta.url, "Conversation.css");

function rule(selector: string): string {
  const escaped = selector.replace(/[.:>()]/g, "\\$&");
  const match = css.match(new RegExp(`${escaped}\\s*\\{[^}]*\\}`));
  if (!match) throw new Error(`no ${selector} rule in Conversation.css`);
  return match[0];
}

test("auto-follow drives Shell's .conversation-scroll ancestor, not the non-scrolling column", () => {
  // Mirrors conversation.accept.test.tsx's own scroll test, but wrapped the way Shell actually
  // composes this column: `.conversation-list` is a flex:1 child of an auto-height ancestor, so
  // it never becomes a scroll box itself — `.conversation-scroll` is the real one.
  const v = view([userMessage("hi"), turnStarted, textDelta("one")]);
  const { container, rerender } = render(
    <div className="conversation-scroll">
      <Conversation view={v} nowMs={NOW} onOpenUrl={vi.fn()} />
    </div>,
  );
  const scroller = container.querySelector(".conversation-scroll") as HTMLElement;
  Object.defineProperty(scroller, "scrollHeight", { value: 1000, configurable: true });
  Object.defineProperty(scroller, "clientHeight", { value: 400, configurable: true });

  // Following is on by default: the next update must jump the *ancestor* to its bottom (600),
  // not leave it untouched while writing to the column (which has no scroll metrics of its own).
  rerender(
    <div className="conversation-scroll">
      <Conversation view={view([textDelta(" two")], v)} nowMs={NOW} onOpenUrl={vi.fn()} />
    </div>,
  );
  expect(scroller.scrollTop).toBe(600);

  // The user scrolls the real scroller away from the bottom: following must stop. A listener
  // on the column itself would never see this, since the event never fires there.
  scroller.scrollTop = 100;
  scroller.dispatchEvent(new Event("scroll"));
  rerender(
    <div className="conversation-scroll">
      <Conversation view={view([textDelta(" three")], v)} nowMs={NOW} onOpenUrl={vi.fn()} />
    </div>,
  );
  expect(scroller.scrollTop).toBe(100);
});

test("the scroll listener is released on unmount", () => {
  const v = view([userMessage("hi"), turnStarted, textDelta("one")]);
  const { container, unmount } = render(
    <div className="conversation-scroll">
      <Conversation view={v} nowMs={NOW} onOpenUrl={vi.fn()} />
    </div>,
  );
  const scroller = container.querySelector(".conversation-scroll") as HTMLElement;
  const removeSpy = vi.spyOn(scroller, "removeEventListener");
  unmount();
  expect(removeSpy).toHaveBeenCalledWith("scroll", expect.any(Function));
});

test("the column carries no scroll box of its own — Shell's ancestor supplies it", () => {
  expect(css).not.toMatch(/\.conversation-list\s*\{[^}]*overflow-y/);
});

test("a nested <ul> renders under its parent item, not beside it", () => {
  expect(rule(".md ul > li")).toMatch(/display:\s*block/);
  expect(rule(".md ul > li")).not.toMatch(/display:\s*flex/);
  expect(rule(".md ul > li:not(.task-list-item)::before")).toMatch(/position:\s*absolute/);
});

test("a bare fenced block's text is not shrunk by inline-code padding", () => {
  expect(rule(".md-code .md-inline-code")).toMatch(/font-size:\s*inherit/);
  expect(rule(".md-code .md-inline-code")).toMatch(/padding:\s*0/);
});

test("an opened thought's full text sits under the row and keeps the row's dim type", () => {
  expect(rule(".thought-block")).toMatch(/margin-top:\s*10px/);
  // It does not inherit from the button, so it states its own face.
  expect(rule(".thought-full")).toMatch(/font-size:\s*13px/);
  expect(rule(".thought-full")).toMatch(/white-space:\s*pre-wrap/);
});

test("a long session title is truncated to one line instead of wrapping into the header", () => {
  const titleRule = rule(".conversation-header .conversation-title");
  expect(titleRule).toMatch(/white-space:\s*nowrap/);
  expect(titleRule).toMatch(/text-overflow:\s*ellipsis/);
  expect(titleRule).toMatch(/min-width:\s*0/);
});

/* --- the conversation render path is memoized ------------------------------------------------- */

/** Two finished turns' worth of prose and a third turn still streaming — the shape a resumed
 * session is in the moment the user sends a message. `view` opens a new turn at every
 * user.message, so the second `turn.started` needs an id of its own. */
function streamingAfterHistory() {
  const earlier = view([
    userMessage("one"),
    turnStarted,
    textDone("a finished reply", "b1"),
    turnDone(),
  ]);
  return view(
    [
      userMessage("two"),
      { type: "turn.started", data: { turn_id: "t2" } },
      textDone("an earlier paragraph", "b2"),
      textDelta("still ", "b3"),
    ],
    earlier,
  );
}

test("a delta re-parses only the block it changed, not every block on screen", () => {
  // react-markdown builds its processor and parses inside the component body on every render, so
  // an unmemoized column re-parses the whole transcript once per `text.delta` — ~250 blocks and
  // 50-100ms on one resumed history page, at the rate the CLI emits deltas.
  const base = streamingAfterHistory();
  // One reference for both renders, as App gives it (`viki.actions.openUrl`, off an object built
  // once): a fresh arrow per render would defeat the memo and this test would be measuring the
  // test's own mistake.
  const onOpenUrl = vi.fn();
  const { rerender } = render(<Conversation view={base} nowMs={NOW} onOpenUrl={onOpenUrl} />);
  expect(markdownRenders.mock.calls.map(([text]) => text)).toEqual([
    "a finished reply",
    "an earlier paragraph",
    "still ",
  ]);

  markdownRenders.mockClear();
  rerender(
    <Conversation view={view([textDelta("more", "b3")], base)} nowMs={NOW} onOpenUrl={onOpenUrl} />,
  );
  // The earlier turn is skipped whole, and so is the finished block of the turn in progress.
  expect(markdownRenders.mock.calls).toEqual([["still more"]]);
});

test("an unchanged turn is skipped whole, tool rows and thoughts included", () => {
  // Reply's memo is what saves the ~1,150 tool rows and ~520 thought rows a resumed page carries;
  // unlike the markdown above, skipping their reconciliation produces no observable difference in
  // jsdom, so this pins the two facts it rests on instead: that Reply is memoized at all, and
  // that the reducer really does hand back the same turn object for everything a frame did not
  // touch (`replaceTurn` slices and writes one index).
  expect((Reply as unknown as { $$typeof: symbol }).$$typeof).toBe(Symbol.for("react.memo"));

  const base = streamingAfterHistory();
  const next = view([textDelta("more", "b3")], base);
  expect(next.turns).toHaveLength(2);
  expect(next.turns[0]).toBe(base.turns[0]);
  expect(next.turns[1]).not.toBe(base.turns[1]);
});

test("a Bot's reply names it without the revision and Profile chips", () => {
  const turn = view([userMessage("hi"), turnStarted, textDone("hello")]).turns[0];
  if (!turn) throw new Error("expected a reply");
  const { container } = render(
    <IdentityContext.Provider
      value={{
        ...DEFAULT_IDENTITY,
        assistantName: "Chief",
        profile: {
          name: "Chief",
          avatarId: null,
          accent: "blue",
          shape: "circle",
          revision: 3,
          botProfileId: "p1",
        },
      }}
    >
      <Reply turn={turn} onOpenUrl={vi.fn()} />
    </IdentityContext.Provider>,
  );
  expect(container.querySelector(".reply-name")?.textContent).toBe("Chief");
  expect(container.querySelectorAll(".chip")).toHaveLength(0);
});

test("a plain profile's reply keeps r3 and Profile", () => {
  const turn = view([userMessage("hi"), turnStarted, textDone("hello")]).turns[0];
  if (!turn) throw new Error("expected a reply");
  const { container } = render(
    <IdentityContext.Provider
      value={{
        ...DEFAULT_IDENTITY,
        assistantName: "Planner",
        profile: {
          name: "Planner",
          avatarId: null,
          accent: "blue",
          shape: "circle",
          revision: 3,
        },
      }}
    >
      <Reply turn={turn} onOpenUrl={vi.fn()} />
    </IdentityContext.Provider>,
  );
  expect(Array.from(container.querySelectorAll(".chip")).map((chip) => chip.textContent)).toEqual([
    "r3",
    "Profile",
  ]);
});

/* --- frontend-5: auto-follow reads position, not only the scroll event ------------------------- */

/** The column as Shell composes it, with the ancestor's scroll metrics faked in (jsdom reports
 * zero heights). Returns the real scroll box and a way to commit another delta into it. */
function mounted(onOpenUrl = vi.fn()) {
  const base = view([userMessage("hi"), turnStarted, textDelta("one")]);
  let current = base;
  const { container, rerender } = render(
    <div className="conversation-scroll">
      <Conversation view={base} nowMs={NOW} onOpenUrl={onOpenUrl} />
    </div>,
  );
  const host = container.querySelector(".conversation-scroll") as HTMLElement;
  Object.defineProperty(host, "scrollHeight", { value: 1000, configurable: true });
  Object.defineProperty(host, "clientHeight", { value: 400, configurable: true });
  // `then` lets one commit carry more than the delta (frontend-2 lands a history page in it).
  function delta(text: string, then: (next: SessionView) => SessionView = (next) => next): void {
    current = then(view([textDelta(text)], current));
    rerender(
      <div className="conversation-scroll">
        <Conversation view={current} nowMs={NOW} onOpenUrl={onOpenUrl} />
      </div>,
    );
  }
  return { container, host, delta };
}

test("a delta landing before the scroll event does not yank the column back", () => {
  const { container, host, delta } = mounted();
  delta(" two");
  expect(host.scrollTop).toBe(600); // following, at the live edge

  // The user wheels up. The browser moves scrollTop at once but only queues the `scroll` event
  // for the next rendering step, and a delta commits in between — which is why this test moves
  // the position WITHOUT dispatching the event. The frozen acceptance test and the older test
  // above both dispatch it synchronously, the one ordering a real browser never gives, so
  // neither can see this: both would still pass without the position check.
  host.scrollTop = 100;
  delta(" three");
  expect(host.scrollTop).toBe(100);
  expect(container.querySelector(".jump-pill")).not.toBeNull();

  // The queued event finally arrives and reads the position the user actually left, rather
  // than one the layout effect wrote over it.
  host.dispatchEvent(new Event("scroll"));
  delta(" four");
  expect(host.scrollTop).toBe(100);
});

test("a wheel toward the top stops following before the scroll event exists", () => {
  const { host, delta } = mounted();
  delta(" two");
  expect(host.scrollTop).toBe(600);

  // On the first frame of a gesture the compositor may not have handed the new scrollTop to the
  // main thread yet, so the position check above can still lose that one frame. The wheel event
  // is dispatched before the scroll it causes, and covers it.
  host.dispatchEvent(new WheelEvent("wheel", { deltaY: -120 }));
  Object.defineProperty(host, "scrollHeight", { value: 1400, configurable: true });
  delta(" three");
  expect(host.scrollTop).toBe(600); // not 1000, the new live edge
});

test("content arriving under a followed column still follows it", () => {
  // The guard against the position check over-firing: a whole tool panel landing at the bottom
  // grows scrollHeight by far more than the 80px threshold, but it never moves scrollTop — which
  // is exactly why scrollTop, not the distance from the edge, is what says the user moved.
  const { container, host, delta } = mounted();
  delta(" two");
  expect(host.scrollTop).toBe(600);

  Object.defineProperty(host, "scrollHeight", { value: 1400, configurable: true });
  delta(" three");
  expect(host.scrollTop).toBe(1000);
  expect(container.querySelector(".jump-pill")).toBeNull();
});

/* --- containment: untrusted text cannot widen the column -------------------------------------- */

test("a long unbroken token wraps instead of widening the centre column", () => {
  // Everything in this column is text the app did not write; one 400-character URL or hash would
  // otherwise paint through the 720px .message-column and turn on the conversation pane's own
  // horizontal scrollbar (Shell.css sets overflow-y, which computes the other axis to auto too).
  expect(rule(".conversation-list")).toMatch(/overflow-wrap:\s*anywhere/);
  // Restated on .md itself, which Cards renders outside this column for a plan.
  expect(rule(".md")).toMatch(/overflow-wrap:\s*anywhere/);
  // Fenced code is the exception and keeps its own horizontal scroll.
  expect(rule(".md-code")).toMatch(/overflow-x:\s*auto/);
});

// The frozen acceptance test passes the label explicitly, so only these can see what happens
// when nothing passes one, and that Conversation hands its own prop down rather than dropping
// it.
test("a sent message is labelled You until the shell supplies a display name", () => {
  const { container } = render(
    <UserTurn user={{ text: "hello", command: "", at: AT }} nowMs={NOW} />,
  );
  expect(container.querySelector(".user-at")?.textContent).toMatch(/^You · \d\d:\d\d$/);
});

test("the conversation passes its userLabel down to every sent message", () => {
  const { container } = render(
    <Conversation
      view={view([userMessage("hello"), turnStarted, textDone("hi")])}
      nowMs={NOW}
      onOpenUrl={() => {}}
      userLabel="Sora"
    />,
  );
  expect(container.querySelector(".user-at")?.textContent).toMatch(/^Sora · \d\d:\d\d$/);
});

/* --- frontend-1: a page that does not fill the box asks for the one before it ----------------- */

/** The column with a page already landed and `cursor` left to page through. */
function paged(cursor: string | null, historyLoading = false): SessionView {
  return {
    ...view([userMessage("hi"), turnStarted, textDone("one")]),
    historyCursor: cursor,
    historyLoading,
  };
}

function mountPaged(v: SessionView, onLoadOlder: () => void, scrollHeight: number) {
  const wrap = (next: SessionView) => (
    <div className="conversation-scroll">
      <Conversation view={next} nowMs={NOW} onOpenUrl={vi.fn()} onLoadOlder={onLoadOlder} />
    </div>
  );
  const { container, rerender } = render(wrap(v));
  const host = container.querySelector(".conversation-scroll") as HTMLElement;
  Object.defineProperty(host, "scrollHeight", { value: scrollHeight, configurable: true });
  Object.defineProperty(host, "clientHeight", { value: 400, configurable: true });
  return { host, show: (next: SessionView) => rerender(wrap(next)) };
}

test("a page too short to scroll asks for the one before it, once per page", () => {
  const onLoadOlder = vi.fn();
  // The heights are only in place after the first render, so the second commit is the first
  // one that can measure; the mount itself sees jsdom's 0/0 and also asks — that is the
  // "first page" case, and it is what the loading rerender below must not repeat.
  const { show } = mountPaged(paged("c1"), onLoadOlder, 300);
  expect(onLoadOlder).toHaveBeenCalledTimes(1);
  // The request is out: nothing more until its page lands, however many deltas commit.
  show(paged("c1", true));
  show(paged("c1", true));
  expect(onLoadOlder).toHaveBeenCalledTimes(1);
  // It lands, still short of the box, with a cursor left: ask again.
  show(paged("c2"));
  expect(onLoadOlder).toHaveBeenCalledTimes(2);
});

test("a page that fills the box waits for the user to scroll to the top", () => {
  const onLoadOlder = vi.fn();
  const { show } = mountPaged(paged("c1", true), onLoadOlder, 1000);
  show(paged("c1"));
  expect(onLoadOlder).not.toHaveBeenCalled();
});

test("with no older page there is nothing to ask for", () => {
  const onLoadOlder = vi.fn();
  const { show } = mountPaged(paged(null), onLoadOlder, 300);
  show(paged(null));
  expect(onLoadOlder).not.toHaveBeenCalled();
});

/* --- frontend-2: the prepend anchor follows content that streams in below ---------------------- */

test("a page landing after content streamed in below restores what the user was reading", () => {
  const { host, delta } = mounted();
  delta(" two");
  // The user scrolls up; the listener records the anchor: 1000 - 100 = 900 from the bottom.
  host.scrollTop = 100;
  host.dispatchEvent(new Event("scroll"));
  // Then the reply keeps streaming under them: the content grows, scrollTop stays, and no
  // scroll event is dispatched for growth the user did not cause.
  Object.defineProperty(host, "scrollHeight", { value: 1400, configurable: true });
  delta(" three");
  expect(host.scrollTop).toBe(100);
  // An older page lands on top and adds 600px. The same content stays under their eyes only if
  // the anchor was re-read after the growth (1300 from the bottom), not left at 900.
  Object.defineProperty(host, "scrollHeight", { value: 2000, configurable: true });
  delta(" four", (current) =>
    applyHistoryPage(current, {
      events: [userMessage("older"), turnStarted, textDone("earlier", "h1")],
      next_before_uuid: null,
    } as unknown as Parameters<typeof applyHistoryPage>[1]),
  );
  expect(host.scrollTop).toBe(700);
});

/* --- turn actions: quote, new chat, the renamed reuse, and retry ------------------------------- */

function failedTurn(status: "error" | "cancelled" = "error"): SessionView {
  return view([userMessage("hi"), turnStarted, textDone("x"), turnDone(status)]);
}

test("Quote into draft calls onQuoteTurn with the turn", () => {
  const onQuoteTurn = vi.fn();
  const v = failedTurn();
  render(
    <Conversation
      view={v}
      nowMs={NOW}
      onOpenUrl={vi.fn()}
      onSelectTurn={vi.fn()}
      onQuoteTurn={onQuoteTurn}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "More actions" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Quote into draft" }));
  expect(onQuoteTurn).toHaveBeenCalledWith(v.turns[0]);
});

test("New chat with this turn calls onBranchTurn with the turn", () => {
  const onBranchTurn = vi.fn();
  const v = failedTurn();
  render(
    <Conversation
      view={v}
      nowMs={NOW}
      onOpenUrl={vi.fn()}
      onSelectTurn={vi.fn()}
      onBranchTurn={onBranchTurn}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "More actions" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "New chat with this turn" }));
  expect(onBranchTurn).toHaveBeenCalledWith(v.turns[0]);
});

test("the reuse action is truthfully relabelled and still calls onReusePrompt with the turn", () => {
  const onReusePrompt = vi.fn();
  const v = failedTurn();
  render(
    <Conversation
      view={v}
      nowMs={NOW}
      onOpenUrl={vi.fn()}
      onSelectTurn={vi.fn()}
      onReusePrompt={onReusePrompt}
    />,
  );
  expect(screen.queryByText("Reuse prompt")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "More actions" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Edit in a new chat" }));
  expect(onReusePrompt).toHaveBeenCalledWith(v.turns[0]);
});

test("Retry… offers on a failed live turn once onRetryTurn is wired, and calls it with the turn", () => {
  const onRetryTurn = vi.fn();
  const v = failedTurn("error");
  render(<Conversation view={v} nowMs={NOW} onOpenUrl={vi.fn()} onRetryTurn={onRetryTurn} />);
  fireEvent.click(screen.getByRole("button", { name: "Retry…" }));
  expect(onRetryTurn).toHaveBeenCalledWith(v.turns[0]);
});

test("Retry… never offers on an errored live turn with no user message", () => {
  const v = view([turnStarted, textDone("x"), turnDone("error")]);
  render(<Conversation view={v} nowMs={NOW} onOpenUrl={vi.fn()} onRetryTurn={vi.fn()} />);
  expect(screen.queryByRole("button", { name: "Retry…" })).toBeNull();
});

test("Retry… also offers on a cancelled live turn", () => {
  const v = failedTurn("cancelled");
  render(<Conversation view={v} nowMs={NOW} onOpenUrl={vi.fn()} onRetryTurn={vi.fn()} />);
  expect(screen.queryByRole("button", { name: "Retry…" })).not.toBeNull();
});

test("the turn-actions row appears with no onSelectTurn when a retry is offered, but no menu", () => {
  const v = failedTurn();
  const { container } = render(
    <Conversation view={v} nowMs={NOW} onOpenUrl={vi.fn()} onRetryTurn={vi.fn()} />,
  );
  expect(container.querySelector(".turn-actions")).not.toBeNull();
  // Nothing offers a menu item (no onSelectTurn/onQuoteTurn/onBranchTurn/onReusePrompt), so the
  // "More actions" trigger itself must not render, rather than open an empty menu.
  expect(screen.queryByRole("button", { name: "More actions" })).toBeNull();
});

test("with neither onSelectTurn nor onRetryTurn, a finished turn offers no actions row", () => {
  const v = failedTurn();
  const { container } = render(<Conversation view={v} nowMs={NOW} onOpenUrl={vi.fn()} />);
  expect(container.querySelector(".turn-actions")).toBeNull();
});

test("Retry… never offers on a turn that succeeded", () => {
  const v = view([userMessage("hi"), turnStarted, textDone("x"), turnDone("success")]);
  render(<Conversation view={v} nowMs={NOW} onOpenUrl={vi.fn()} onRetryTurn={vi.fn()} />);
  expect(screen.queryByRole("button", { name: "Retry…" })).toBeNull();
});

test("Retry… never offers on a history turn, even one carrying a live-looking error status", () => {
  const v = failedTurn();
  const historic: SessionView = {
    ...v,
    turns: v.turns.map((turn) => ({ ...turn, history: true })),
  };
  render(<Conversation view={historic} nowMs={NOW} onOpenUrl={vi.fn()} onRetryTurn={vi.fn()} />);
  expect(screen.queryByRole("button", { name: "Retry…" })).toBeNull();
});

/* --- the finished-turn "More actions" (⋯) menu ------------------------------------------------- */

test("the turn menu trigger advertises itself as a menu and closes again on Escape", async () => {
  const v = failedTurn();
  render(<Conversation view={v} nowMs={NOW} onOpenUrl={vi.fn()} onQuoteTurn={vi.fn()} />);
  const trigger = screen.getByRole("button", { name: "More actions" });
  expect(trigger.getAttribute("aria-haspopup")).toBe("menu");
  expect(trigger.getAttribute("aria-expanded")).toBe("false");

  fireEvent.click(trigger);
  expect(trigger.getAttribute("aria-expanded")).toBe("true");
  expect(screen.getByRole("menu")).toBeTruthy();

  // Radix's Escape dismissal resolves a tick after the keydown itself (OrganizedSessionMenu's own
  // Escape test notes the same flush); selecting an item instead closes synchronously, below.
  fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
  await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
  expect(screen.queryByRole("menu")).toBeNull();
  expect(trigger.getAttribute("aria-expanded")).toBe("false");
});

test("the menu holds all four turn actions in order when every one is offered", () => {
  const v = failedTurn();
  render(
    <Conversation
      view={v}
      nowMs={NOW}
      onOpenUrl={vi.fn()}
      onSelectTurn={vi.fn()}
      onQuoteTurn={vi.fn()}
      onBranchTurn={vi.fn()}
      onReusePrompt={vi.fn()}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "More actions" }));
  const items = Array.from(screen.getByRole("menu").querySelectorAll(".mi"));
  expect(items.map((item) => item.textContent)).toEqual([
    "Quote into draft",
    "New chat with this turn",
    "Edit in a new chat",
    "Select for export",
  ]);
});

test("checking Select for export in the menu notifies the parent, and the chip shows once selected", () => {
  const onSelectTurn = vi.fn();
  const v = failedTurn();
  const turnId = v.turns[0]?.id;
  if (!turnId) throw new Error("expected a turn");
  const { rerender } = render(
    <Conversation view={v} nowMs={NOW} onOpenUrl={vi.fn()} onSelectTurn={onSelectTurn} />,
  );
  expect(screen.queryByText("Selected for export")).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "More actions" }));
  const item = screen.getByRole("menuitemcheckbox", { name: "Select for export" });
  expect(item.getAttribute("aria-checked")).toBe("false");
  fireEvent.click(item);
  expect(onSelectTurn).toHaveBeenCalledWith(turnId, true);

  // Conversation holds no selection state of its own — the parent decides and hands the id back.
  rerender(
    <Conversation
      view={v}
      nowMs={NOW}
      onOpenUrl={vi.fn()}
      onSelectTurn={onSelectTurn}
      selectedTurns={new Set([turnId])}
    />,
  );
  expect(screen.getByText("Selected for export")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "More actions" }));
  expect(
    screen
      .getByRole("menuitemcheckbox", { name: "Select for export" })
      .getAttribute("aria-checked"),
  ).toBe("true");
});

// A turn the app sent with a vault excerpt in front of it; the plain rows below pin that a turn
// without one renders the same DOM with and without the citation props.
const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const excerptApi = { excerpt: vi.fn() };

test("a plain user turn is one bare text node whether or not citations are wired", () => {
  const user = { text: "- milk\n- eggs **not bold**", command: "", at: AT };
  const bareHtml = render(<UserTurn user={user} nowMs={NOW} />).container.innerHTML;
  cleanup();
  const wired = render(
    <UserTurn user={user} nowMs={NOW} excerptApi={excerptApi} workspaceId={WORKSPACE} />,
  ).container;
  const bubble = wired.querySelector(".user-turn") as HTMLElement;
  expect(bubble.childNodes).toHaveLength(1);
  expect(bubble.firstChild?.nodeType).toBe(Node.TEXT_NODE);
  expect(bubble.firstChild?.nodeValue).toBe(user.text);
  expect(wired.innerHTML).toBe(bareHtml);
});

test("a plain turn with a command chip keeps the chip and one text node", () => {
  const user = { text: "yesterday", command: "/capture", at: AT };
  const { container } = render(
    <UserTurn user={user} nowMs={NOW} excerptApi={excerptApi} workspaceId={WORKSPACE} />,
  );
  const nodes = [...(container.querySelector(".user-turn") as HTMLElement).childNodes];
  expect(nodes.map((node) => node.nodeName)).toEqual(["SPAN", "#text"]);
});

test("Conversation shows a sent excerpt as a citation once the api and workspace arrive", () => {
  const text = "[vault excerpt] notes/plan.md, lines 3-5, sha256 0123456789ab\n```\nbody\n```\nask";
  const v = view([userMessage(text)]);
  const plain = render(<Conversation view={v} nowMs={NOW} onOpenUrl={vi.fn()} />).container;
  expect(plain.querySelector("details")).toBeNull();
  expect(plain.querySelector(".user-turn")?.textContent).toBe(text);
  cleanup();
  const { container } = render(
    <Conversation
      view={v}
      nowMs={NOW}
      onOpenUrl={vi.fn()}
      excerptApi={excerptApi}
      workspaceId={WORKSPACE}
    />,
  );
  expect(container.querySelector(".user-turn details.excerpt-cite")).not.toBeNull();
  expect(container.querySelector(".user-turn pre")?.textContent).toBe("body");
});

/* --- renderBlock's `background` prop tracks the turn around the row, not the row itself ------- */

/* --- a block-level jump moves the highlight to one routing row, not the whole turn ------------- */

test("jumpTo with a blockId focuses and outlines that routing row, not the whole turn", () => {
  const withRouting = view([
    userMessage("hi"),
    turnStarted,
    subagentStarted("a1", "researcher", "opus", "", "Find daily note template"),
  ]);
  const turnId = withRouting.turns[0]?.id;
  if (!turnId) throw new Error("expected a turn");
  const { container, rerender } = render(
    <div className="conversation-scroll">
      <Conversation
        view={withRouting}
        initialScroll={{ top: 10, following: false }}
        nowMs={NOW}
        onOpenUrl={vi.fn()}
      />
    </div>,
  );
  const host = container.querySelector(".conversation-scroll") as HTMLElement;
  Object.defineProperty(host, "scrollHeight", { value: 1200, configurable: true });
  Object.defineProperty(host, "clientHeight", { value: 400, configurable: true });
  const row = container.querySelector('[data-routing-id="a1"]') as HTMLElement;
  row.getBoundingClientRect = () => ({ top: 300 }) as DOMRect;
  host.getBoundingClientRect = () => ({ top: 0 }) as DOMRect;
  const focus = vi.spyOn(row, "focus");
  rerender(
    <div className="conversation-scroll">
      <Conversation
        view={withRouting}
        jumpTo={{ turnId, request: 1, blockId: "a1" }}
        nowMs={NOW}
        onOpenUrl={vi.fn()}
      />
    </div>,
  );
  expect(host.scrollTop).toBe(10 + 300 - 12);
  expect(focus).toHaveBeenCalledWith({ preventScroll: true });
  expect(row.className).toBe("routing routing-target");
  expect(container.querySelector(".turn-search-target")).toBeNull();
});

test("an unknown blockId falls back to the plain turn-level jump", () => {
  const withRouting = view([
    userMessage("hi"),
    turnStarted,
    subagentStarted("a1", "researcher", "opus", "", "Find daily note template"),
  ]);
  const turnId = withRouting.turns[0]?.id;
  if (!turnId) throw new Error("expected a turn");
  const { container, rerender } = render(
    <div className="conversation-scroll">
      <Conversation
        view={withRouting}
        initialScroll={{ top: 10, following: false }}
        nowMs={NOW}
        onOpenUrl={vi.fn()}
      />
    </div>,
  );
  const host = container.querySelector(".conversation-scroll") as HTMLElement;
  Object.defineProperty(host, "scrollHeight", { value: 1200, configurable: true });
  Object.defineProperty(host, "clientHeight", { value: 400, configurable: true });
  const turnEl = container.querySelector(`[data-turn-id="${turnId}"]`) as HTMLElement;
  turnEl.getBoundingClientRect = () => ({ top: 300 }) as DOMRect;
  host.getBoundingClientRect = () => ({ top: 0 }) as DOMRect;
  const focus = vi.spyOn(turnEl, "focus");
  rerender(
    <div className="conversation-scroll">
      <Conversation
        view={withRouting}
        jumpTo={{ turnId, request: 1, blockId: "does-not-exist" }}
        nowMs={NOW}
        onOpenUrl={vi.fn()}
      />
    </div>,
  );
  expect(host.scrollTop).toBe(10 + 300 - 12);
  expect(focus).toHaveBeenCalledWith({ preventScroll: true });
  // blockId was given but never resolved, so this falls all the way back to the plain
  // turn-level jump, highlight included — see Conversation.tsx's "Block-level jump" header note.
  expect(container.querySelector(".turn-search-target")).not.toBeNull();
  expect(container.querySelector(".routing-target")).toBeNull();
});

test("a running routing block reads Running while its turn runs, and Running in background once the turn ends", () => {
  const started = view([
    userMessage("hi"),
    turnStarted,
    subagentStarted("a1", "researcher", "opus", "", "Find daily note template"),
  ]);
  const { container, rerender } = render(
    <Conversation view={started} nowMs={NOW} onOpenUrl={vi.fn()} />,
  );
  expect(container.querySelector(".routing-meta")?.textContent).toBe(
    "Find daily note template — Running",
  );

  // The turn ends, but the subagent never got its own `subagent.done` — the row's own status
  // stays "running" (same gap Blocks.tsx's RoutingRow header documents), so only the turn having
  // ended tells the row to call itself background.
  const ended = view([turnDone()], started);
  rerender(<Conversation view={ended} nowMs={NOW} onOpenUrl={vi.fn()} />);
  expect(container.querySelector(".routing-meta")?.textContent).toBe(
    "Find daily note template — Running in background",
  );
});

/* --- turn-actions slot for custom actions on finished turns ------------------------------------ */

test("renderTurnActions renders custom actions in the turn-actions row for a finished turn", () => {
  const v = failedTurn();
  const renderTurnActions = vi.fn((turn) => (
    <button type="button" data-testid="custom-action">
      Custom {turn.id}
    </button>
  ));
  const { container } = render(
    <Conversation view={v} nowMs={NOW} onOpenUrl={vi.fn()} renderTurnActions={renderTurnActions} />,
  );
  expect(renderTurnActions).toHaveBeenCalledWith(v.turns[0]);
  expect(container.querySelector('[data-testid="custom-action"]')).not.toBeNull();
  expect(container.querySelector('[data-testid="custom-action"]')?.textContent).toContain(
    `Custom ${v.turns[0]?.id}`,
  );
});

test("renderTurnActions does not render for a running turn", () => {
  const v = view([userMessage("hi"), turnStarted, textDelta("one")]);
  const renderTurnActions = vi.fn();
  const { container } = render(
    <Conversation view={v} nowMs={NOW} onOpenUrl={vi.fn()} renderTurnActions={renderTurnActions} />,
  );
  expect(renderTurnActions).not.toHaveBeenCalled();
  expect(container.querySelector(".turn-actions")).toBeNull();
});

test("without renderTurnActions, a finished turn with no other action has no turn-actions row", () => {
  const v = failedTurn();
  const { container } = render(<Conversation view={v} nowMs={NOW} onOpenUrl={vi.fn()} />);
  expect(container.querySelector(".turn-actions")).toBeNull();
});

test("renderTurnActions appears in the turn-actions row even when no other action is present", () => {
  const v = failedTurn();
  const renderTurnActions = vi.fn(() => <span data-testid="only-action">Only action</span>);
  const { container } = render(
    <Conversation view={v} nowMs={NOW} onOpenUrl={vi.fn()} renderTurnActions={renderTurnActions} />,
  );
  expect(container.querySelector(".turn-actions")).not.toBeNull();
  expect(container.querySelector('[data-testid="only-action"]')).not.toBeNull();
});

/* --- waiting steers render below the last turn, and the announcer skips a folded turn ---------- */

function steerPending(steerId: string, text: string, images = 0, at = AT): Item {
  return { type: "steer.pending", data: { steer_id: steerId, text, command: "", images, at } };
}

test("a waiting steer renders below the last turn with its text, image count and caption", () => {
  const v = view([
    userMessage("hi"),
    turnStarted,
    textDone("working on it"),
    steerPending("s1", "also check the budget", 2),
  ]);
  const { container } = render(<Conversation view={v} nowMs={NOW} onOpenUrl={vi.fn()} />);
  const rows = container.querySelectorAll(".turn, .steer-waiting");
  expect(rows).toHaveLength(2); // the turn, then the waiting row, in that order
  expect(rows[0]?.className).toContain("turn");
  const row = rows[1] as HTMLElement;
  expect(row.className).toBe("steer-waiting");
  expect(row.textContent).toContain("also check the budget");
  expect(row.textContent).toContain("+2 images");
  expect(row.textContent).toContain("Waiting — Claude reads this after its current step.");
});

test("a waiting steer with no images shows no image count", () => {
  const v = view([userMessage("hi"), turnStarted, steerPending("s2", "no attachments here", 0)]);
  const { container } = render(<Conversation view={v} nowMs={NOW} onOpenUrl={vi.fn()} />);
  expect(container.querySelector(".steer-waiting")?.textContent).not.toMatch(/images/);
});

test("the waiting steer's text and caption are muted via the column's existing dim tokens", () => {
  expect(rule(".steer-waiting-text")).toMatch(/color:\s*var\(--t2\)/);
  expect(rule(".steer-waiting-text")).toMatch(/-webkit-line-clamp:\s*3/);
  expect(rule(".steer-waiting-caption")).toMatch(/color:\s*var\(--t3\)/);
});

// Hand-built rather than folded through the real reducer (contrast the render tests above):
// this isolates the announcer's own `!turn.steered` guard, so it fails for the right reason if
// that guard is ever lost, rather than passing by coincidence off a fixture only the reducer's
// own steer.* handling (tested separately in sessionReducer.steer.unit.test.ts) could shape.
test("the announcer skips a turn whose running -> success flip is a steer fold (Turn.steered)", () => {
  const t1: Turn = {
    id: "t1",
    user: { text: "hi", command: "", at: AT },
    blocks: [],
    status: "running",
    usage: null,
    durationMs: 0,
    history: false,
  };
  const base: SessionView = { ...emptyView("s1"), turns: [t1] };
  const { container, rerender } = render(
    <Conversation view={base} nowMs={NOW} onOpenUrl={vi.fn()} />,
  );
  const liveRegion = () => container.querySelector('[role="status"]')?.textContent;

  // t1 flips running -> success, but carries `steered: true`: a later message folded into it
  // (sessionReducer.ts's user.message case) rather than it finishing on its own.
  const t1Steered: Turn = { ...t1, status: "success", steered: true };
  const continuation: Turn = {
    id: "t1:steer",
    user: null,
    blocks: [],
    status: "running",
    usage: null,
    durationMs: 0,
    history: false,
  };
  const folded: SessionView = { ...base, turns: [t1Steered, continuation] };
  rerender(<Conversation view={folded} nowMs={NOW} onOpenUrl={vi.fn()} />);
  expect(liveRegion()).toBe("");

  // The continuation turn then really does finish on its own: it gets the real announcement.
  const finished: SessionView = {
    ...folded,
    turns: [t1Steered, { ...continuation, status: "success" }],
  };
  rerender(<Conversation view={finished} nowMs={NOW} onOpenUrl={vi.fn()} />);
  expect(liveRegion()).toBe("Reply finished");
});
