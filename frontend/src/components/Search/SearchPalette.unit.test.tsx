/**
 * Unit tests pinning SearchPalette behaviour that palette.accept.test.tsx leaves implicit:
 * arrow keys and Enter with no hits at all, clearing the input before a scheduled debounce
 * fires cancelling that search outright rather than merely skipping the next one, and
 * the search observation states (partial, superseded, typed error, request ids).
 */
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { SearchHit, SearchResult } from "../../protocol";
import { SearchPalette } from "./SearchPalette";

const NOW = new Date(2026, 8, 12, 18, 0).getTime();

/** What the server answers when it searched everything and found `hits`. `request_id` is ""
 * unless the test is about the echo: that is what the server sends when it attributed the
 * request to no id, and what the frozen suite's helper sends. */
function ready(hits: SearchHit[], request_id = ""): SearchResult {
  return { hits, state: "ready", reason: "", scanned: 1, total: 1, request_id };
}

interface Pending {
  q: string;
  /** The id the palette generated for this request. */
  id: string;
  /** Answer with a plain complete search, the shape most of these tests care about. */
  resolve: (hits: SearchHit[]) => void;
  /** Answer with a result the test builds itself: partial, superseded, a foreign id. */
  answer: (result: SearchResult) => void;
  reject: (error: unknown) => void;
}

function scriptedSearch() {
  const pending: Pending[] = [];
  const search = (q: string, id: string) =>
    new Promise<SearchResult>((resolve, reject) => {
      pending.push({ q, id, resolve: (hits) => resolve(ready(hits)), answer: resolve, reject });
    });
  return { search, pending };
}

function mount() {
  const { search, pending } = scriptedSearch();
  const onOpen = vi.fn();
  const onClose = vi.fn();
  const utils = render(
    <SearchPalette open search={search} onOpen={onOpen} onClose={onClose} nowMs={NOW} />,
  );
  const input = utils.container.querySelector("input") as HTMLInputElement;
  return { ...utils, pending, onOpen, onClose, input };
}

function type(input: HTMLInputElement, value: string): void {
  fireEvent.change(input, { target: { value } });
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

test("arrow keys and Enter with no hits do nothing (no crash, no stray onOpen)", () => {
  const { input, onOpen, container } = mount();
  fireEvent.keyDown(input, { key: "ArrowDown" });
  fireEvent.keyDown(input, { key: "ArrowUp" });
  fireEvent.keyDown(input, { key: "Enter" });
  expect(container.querySelector(".hit-active")).toBeNull();
  expect(onOpen).not.toHaveBeenCalled();
});

test("clearing the input before the debounce fires cancels that search outright", () => {
  const { input, pending } = mount();
  type(input, "milk");
  act(() => void vi.advanceTimersByTime(100)); // debounce not yet due
  type(input, "");
  act(() => void vi.advanceTimersByTime(150));
  expect(pending).toHaveLength(0); // never fired, not even for the emptied-out text
});

test("hits sharing a session_id (multiple transcript matches in one session) all render, in order, across responses", async () => {
  const { input, pending, container } = mount();
  const mk = (session_id: string, n: number): SearchHit => ({
    session_id,
    title: `T${n}`,
    role: "user",
    snippet: `snippet ${n}`,
    at: "",
  });
  type(input, "a");
  act(() => void vi.advanceTimersByTime(150));
  pending[0]?.resolve([mk("s1", 1), mk("s1", 2), mk("s2", 3)]);
  await act(async () => {
    await Promise.resolve();
  });
  expect(
    [...container.querySelectorAll(".hit")].map((r) => r.getAttribute("data-session-id")),
  ).toEqual(["s1", "s1", "s2"]);
  // A later response reorders and repeats session ids differently (catalog.py's title hit plus
  // up to HITS_PER_SESSION transcript hits per session): every hit must still render, in the
  // response's order, with none dropped or duplicated onto the wrong row.
  type(input, "ab");
  act(() => void vi.advanceTimersByTime(150));
  pending[1]?.resolve([mk("s2", 4), mk("s1", 5), mk("s1", 6)]);
  await act(async () => {
    await Promise.resolve();
  });
  const rows = [...container.querySelectorAll<HTMLElement>(".hit")];
  expect(rows.map((r) => r.getAttribute("data-session-id"))).toEqual(["s2", "s1", "s1"]);
  expect(rows.map((r) => r.querySelector(".hit-title")?.textContent)).toEqual(["T4", "T5", "T6"]);
});

test("a rejected search clears the previous query's hits, so Enter opens nothing stale", async () => {
  const { input, pending, container, onOpen } = mount();
  type(input, "milk");
  act(() => void vi.advanceTimersByTime(150));
  pending[0]?.resolve([
    { session_id: "milk-1", title: "Milk 1", role: "user", snippet: "", at: "" },
    { session_id: "milk-2", title: "Milk 2", role: "user", snippet: "", at: "" },
  ]);
  await act(async () => {
    await Promise.resolve();
  });
  expect(container.querySelectorAll(".hit")).toHaveLength(2);

  type(input, "eggs");
  act(() => void vi.advanceTimersByTime(150));
  pending[1]?.reject(new Error("network down"));
  await act(async () => {
    await Promise.resolve();
  });

  expect(container.querySelector(".palette-note")?.textContent).toBe("search failed");
  // The milk rows must not still be mounted under the failure note.
  expect(container.querySelectorAll(".hit")).toHaveLength(0);
  expect(container.querySelector(".hit-active")).toBeNull();
  // Enter must not fall back to a stale milk row now that the list is empty.
  fireEvent.keyDown(input, { key: "Enter" });
  expect(onOpen).not.toHaveBeenCalled();
});

test("control case: ArrowDown after a rejection has no stale list left to walk", async () => {
  const { input, pending, container } = mount();
  type(input, "milk");
  act(() => void vi.advanceTimersByTime(150));
  pending[0]?.resolve([
    { session_id: "milk-1", title: "Milk 1", role: "user", snippet: "", at: "" },
  ]);
  await act(async () => {
    await Promise.resolve();
  });
  type(input, "eggs");
  act(() => void vi.advanceTimersByTime(150));
  pending[1]?.reject(new Error("network down"));
  await act(async () => {
    await Promise.resolve();
  });
  fireEvent.keyDown(input, { key: "ArrowDown" });
  expect(container.querySelector(".hit-active")).toBeNull();
});

test("a one-character ASCII query answered with catalog.py's [] reads distinctly from a real 'no matches'", async () => {
  // The query still reaches search() unchanged (an e2e suite exercises the palette with
  // single-character hostile strings and expects a real response, not a client-side no-op);
  // what must change is that an empty result for a query the server refused does not read the
  // same as an empty result for a real, full-length search.
  const { input, pending, container } = mount();
  type(input, "a");
  act(() => void vi.advanceTimersByTime(150));
  expect(pending).toHaveLength(1);
  expect(pending[0]?.q).toBe("a");
  pending[0]?.resolve([]);
  await act(async () => {
    await Promise.resolve();
  });
  expect(container.querySelector(".palette-note")?.textContent).toBe("type 2+ characters");
});

test.each(["本", "雨"])(
  "G3C-1: a one-character CJK query ('%s') was really searched, so [] reads 'no matches'",
  async (ch) => {
    // catalog.py sets the floor to two characters only while the query is pure ASCII, so a
    // single kanji runs a real search across every transcript. Telling the user to "type 2+
    // characters" here would refuse a query the server already answered.
    const { input, pending, container } = mount();
    type(input, ch);
    act(() => void vi.advanceTimersByTime(150));
    expect(pending).toHaveLength(1);
    expect(pending[0]?.q).toBe(ch);
    pending[0]?.resolve([]);
    await act(async () => {
      await Promise.resolve();
    });
    expect(container.querySelector(".palette-note")?.textContent).toBe("no matches");
  },
);

test("the floor is applied to the NFKC-normalized query, as catalog.py applies it", async () => {
  // "ｘ" (U+FF58, fullwidth) normalizes to the ASCII "x": one Latin letter, which catalog.py
  // refuses. Reading the raw character instead would call it non-ASCII and promise a search
  // that never runs.
  const { input, pending, container } = mount();
  type(input, "ｘ");
  act(() => void vi.advanceTimersByTime(150));
  pending[0]?.resolve([]);
  await act(async () => {
    await Promise.resolve();
  });
  expect(container.querySelector(".palette-note")?.textContent).toBe("type 2+ characters");
});

test("control case: an empty result for a two-character (or longer) query still reads 'no matches'", async () => {
  const { input, pending, container } = mount();
  type(input, "牛乳");
  act(() => void vi.advanceTimersByTime(150));
  expect(pending).toHaveLength(1);
  expect(pending[0]?.q).toBe("牛乳");
  pending[0]?.resolve([]);
  await act(async () => {
    await Promise.resolve();
  });
  expect(container.querySelector(".palette-note")?.textContent).toBe("no matches");
});

test("control case: a one-character query that DOES come back with hits shows them normally", async () => {
  const { input, pending, container } = mount();
  type(input, "a");
  act(() => void vi.advanceTimersByTime(150));
  pending[0]?.resolve([{ session_id: "s1", title: "A", role: "user", snippet: "", at: "" }]);
  await act(async () => {
    await Promise.resolve();
  });
  expect(container.querySelectorAll(".hit")).toHaveLength(1);
  expect(container.querySelector(".palette-note")).toBeNull();
});

test("IME contract: a composing Enter never opens a hit, and a composing ArrowDown never moves the highlight", async () => {
  const { input, pending, container, onOpen, onClose } = mount();
  const mk = (session_id: string, n: number): SearchHit => ({
    session_id,
    title: `T${n}`,
    role: "user",
    snippet: `s${n}`,
    at: "",
  });
  type(input, "牛乳");
  act(() => void vi.advanceTimersByTime(150));
  pending[0]?.resolve([mk("s1", 1), mk("s2", 2)]);
  await act(async () => {
    await Promise.resolve();
  });
  // The Enter that commits a Japanese conversion (isComposing true, keyCode 229) must be the
  // IME's, not ours: no hit opens, the palette stays open.
  fireEvent.keyDown(input, { key: "Enter", isComposing: true, keyCode: 229 });
  expect(onOpen).not.toHaveBeenCalled();
  expect(onClose).not.toHaveBeenCalled();
  // isComposing alone is enough, even with an unrelated keyCode.
  fireEvent.keyDown(input, { key: "Enter", isComposing: true });
  expect(onOpen).not.toHaveBeenCalled();
  // A composing ArrowDown must not drive the candidate-window navigation into ours.
  fireEvent.keyDown(input, { key: "ArrowDown", isComposing: true, keyCode: 229 });
  expect(container.querySelector(".hit-active")).toBeNull();
  // A composing Escape (cancelling the IME's own candidate window) must not close the palette.
  fireEvent.keyDown(input, { key: "Escape", isComposing: true, keyCode: 229 });
  expect(onClose).not.toHaveBeenCalled();
  // Once composition ends, the same keys behave normally again.
  fireEvent.keyDown(input, { key: "ArrowDown" });
  expect(container.querySelector(".hit-active")?.getAttribute("data-session-id")).toBe("s1");
  fireEvent.keyDown(input, { key: "Enter" });
  expect(onOpen).toHaveBeenLastCalledWith("s1");
  expect(onClose).toHaveBeenCalledTimes(1);
});

test("dismissal: a mousedown on the dim backdrop closes the palette; one on the panel does not; Escape closes from a hit button too", async () => {
  const { input, pending, container, onClose } = mount();
  type(input, "milk");
  act(() => void vi.advanceTimersByTime(150));
  pending[0]?.resolve([{ session_id: "s1", title: "Milk", role: "user", snippet: "", at: "" }]);
  await act(async () => {
    await Promise.resolve();
  });
  const backdrop = container.querySelector(".palette-backdrop") as HTMLElement;
  const panel = container.querySelector(".palette") as HTMLElement;
  // A mousedown on the panel itself (inside the modal) must not dismiss it.
  fireEvent.mouseDown(panel);
  expect(onClose).not.toHaveBeenCalled();
  // Focus moves off the input (e.g. onto a hit button); Escape must still close the palette,
  // because it is handled at the window, not scoped to the input.
  const hitButton = container.querySelector(".hit") as HTMLElement;
  hitButton.focus();
  fireEvent.keyDown(hitButton, { key: "Escape" });
  expect(onClose).toHaveBeenCalledTimes(1);
  // A mousedown directly on the backdrop (outside the panel) dismisses it too.
  fireEvent.mouseDown(backdrop);
  expect(onClose).toHaveBeenCalledTimes(2);
});

test("NF-1: Enter on a focused hit button does not race the panel handler and open the wrong (highlighted/first) hit", async () => {
  // jsdom does not simulate a real browser's default action (Enter on a focused <button>
  // triggers its click) for a bare fireEvent.keyDown, so this pins the two halves that
  // together prove the fix: (1) our own keydown handling must not intercept Enter once it
  // reaches a focused hit button — that is what raced the button's default activation and
  // opened `hits[activeIndex ?? 0]` (s1) instead of the focused one (s3); (2) the button's own
  // onClick — what a real browser's default action for Enter-on-a-button fires — still opens
  // exactly the hit it belongs to.
  const { input, pending, container, onOpen, onClose } = mount();
  type(input, "milk");
  act(() => void vi.advanceTimersByTime(150));
  pending[0]?.resolve([
    { session_id: "s1", title: "T1", role: "user", snippet: "", at: "" },
    { session_id: "s2", title: "T2", role: "user", snippet: "", at: "" },
    { session_id: "s3", title: "T3", role: "user", snippet: "", at: "" },
  ]);
  await act(async () => {
    await Promise.resolve();
  });
  const buttons = [...container.querySelectorAll<HTMLElement>(".hit")];
  expect(buttons).toHaveLength(3);
  buttons[2]?.focus();
  expect(document.activeElement).toBe(buttons[2]);
  fireEvent.keyDown(buttons[2] as HTMLElement, { key: "Enter" });
  expect(onOpen).not.toHaveBeenCalled(); // no interception racing the button's own activation
  fireEvent.click(buttons[2] as HTMLElement); // the real browser's default action for that Enter
  expect(onOpen).toHaveBeenCalledWith("s3");
  expect(onOpen).not.toHaveBeenCalledWith("s1");
  expect(onClose).toHaveBeenCalledTimes(1);
});

test("a stray response arriving after the query was cleared does not repopulate the hits", async () => {
  const { input, pending, container } = mount();
  type(input, "milk");
  act(() => void vi.advanceTimersByTime(150));
  expect(pending).toHaveLength(1);
  type(input, ""); // cleared before the in-flight response comes back
  pending[0]?.resolve([{ session_id: "s1", title: "Milk", role: "title", snippet: "", at: "" }]);
  await act(async () => {
    await Promise.resolve();
  });
  expect(container.querySelectorAll(".hit")).toHaveLength(0);
});

test("V-1: Escape still closes after a click on the panel's own chrome blurs focus to the body", () => {
  const { container, onClose } = mount();
  // A mousedown on the panel's padding or its note line lands on nothing focusable, so the
  // browser blurs to document.body — outside the panel's subtree. A panel-scoped listener would
  // never see the key again, leaving a modal that swallows clicks and ignores Escape.
  const panel = container.querySelector(".palette") as HTMLElement;
  fireEvent.mouseDown(panel);
  (document.activeElement as HTMLElement | null)?.blur();
  expect(panel.contains(document.activeElement)).toBe(false);

  fireEvent.keyDown(document.body, { key: "Escape" });
  expect(onClose).toHaveBeenCalledTimes(1);
});

test("V-1: that window listener still obeys the IME, and is gone once the palette closes", () => {
  const { onClose, unmount } = mount();
  fireEvent.keyDown(document.body, { key: "Escape", keyCode: 229 });
  fireEvent.keyDown(document.body, { key: "Escape", isComposing: true });
  expect(onClose).not.toHaveBeenCalled();

  unmount();
  fireEvent.keyDown(document.body, { key: "Escape" });
  expect(onClose).not.toHaveBeenCalled();
});

test("Tab follows the current hit list and wraps inside the modal as results change", async () => {
  const { input, pending, container } = mount();
  const dialog = container.querySelector('[role="dialog"]');
  expect(dialog?.getAttribute("aria-modal")).toBe("true");
  type(input, "milk");
  act(() => void vi.advanceTimersByTime(150));
  pending[0]?.resolve([
    { session_id: "s1", title: "Milk", role: "title", snippet: "", at: "" },
    { session_id: "s2", title: "Eggs", role: "title", snippet: "", at: "" },
  ]);
  await act(async () => {
    await Promise.resolve();
  });
  const hits = [...container.querySelectorAll<HTMLButtonElement>(".hit")];
  fireEvent.keyDown(input, { key: "Tab", shiftKey: true });
  expect(document.activeElement).toBe(hits[1]);
  fireEvent.keyDown(hits[1] as HTMLButtonElement, { key: "Tab" });
  expect(document.activeElement).toBe(input);

  type(input, "none");
  act(() => void vi.advanceTimersByTime(150));
  pending[1]?.resolve([]);
  await act(async () => {
    await Promise.resolve();
  });
  fireEvent.keyDown(input, { key: "Tab" });
  expect(document.activeElement).toBe(input);
});

// --- observation states -------------------------------------------------------------------------

const hit = (session_id: string, title = session_id): SearchHit => ({
  session_id,
  title,
  role: "user",
  snippet: "",
  at: "",
});

async function settle(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
  });
}

function sessionIds(container: HTMLElement): (string | null)[] {
  return [...container.querySelectorAll(".hit")].map((row) => row.getAttribute("data-session-id"));
}

test("announces and scrolls keyboard selection without moving input focus", async () => {
  const { input, pending, container, getByRole } = mount();
  expect(getByRole("textbox", { name: "Search sessions" })).toBe(input);
  const list = getByRole("list", { name: "Session search results" });
  const status = getByRole("status");
  expect(input.getAttribute("aria-controls")).toBe(list.id);
  expect(input.getAttribute("aria-describedby")).toBe(status.id);

  type(input, "milk");
  act(() => void vi.advanceTimersByTime(150));
  expect(status.textContent).toBe("searching…");
  pending[0]?.resolve([hit("s1", "Milk notes"), hit("s2", "Milk delivery")]);
  await settle();
  expect(status.textContent).toContain("2 results");
  const buttons = [...container.querySelectorAll<HTMLButtonElement>(".hit")];
  const scrolls = buttons.map((button) => {
    const scroll = vi.fn();
    Object.defineProperty(button, "scrollIntoView", { value: scroll, configurable: true });
    return scroll;
  });

  fireEvent.keyDown(input, { key: "ArrowDown", isComposing: true });
  fireEvent.keyDown(input, { key: "ArrowDown", keyCode: 229 });
  expect(container.querySelector('[aria-current="true"]')).toBeNull();
  expect(scrolls[0]).not.toHaveBeenCalled();

  fireEvent.keyDown(input, { key: "ArrowDown" });
  expect(buttons[0]?.getAttribute("aria-current")).toBe("true");
  expect(status.textContent).toBe("Result 1 of 2: Milk notes");
  expect(scrolls[0]).toHaveBeenCalledExactlyOnceWith({
    block: "nearest",
    inline: "nearest",
    behavior: "instant",
  });
  expect(document.activeElement).toBe(input);

  fireEvent.keyDown(input, { key: "ArrowUp" });
  expect(buttons[0]?.hasAttribute("aria-current")).toBe(false);
  expect(buttons[1]?.getAttribute("aria-current")).toBe("true");
  expect(status.textContent).toBe("Result 2 of 2: Milk delivery");
  expect(scrolls[1]).toHaveBeenCalledExactlyOnceWith({
    block: "nearest",
    inline: "nearest",
    behavior: "instant",
  });
  expect(document.activeElement).toBe(input);
});

test("replacing results clears a selection made while the next search was pending", async () => {
  const { input, pending, container, getByRole, onOpen } = mount();
  type(input, "milk");
  act(() => void vi.advanceTimersByTime(150));
  pending[0]?.resolve([hit("old-1"), hit("old-2"), hit("old-3")]);
  await settle();

  type(input, "eggs");
  act(() => void vi.advanceTimersByTime(150));
  fireEvent.keyDown(input, { key: "ArrowUp" });
  expect(container.querySelector(".hit-active")?.getAttribute("data-session-id")).toBe("old-3");
  pending[1]?.resolve([hit("new-1"), hit("new-2")]);
  await settle();

  expect(container.querySelector(".hit-active")).toBeNull();
  expect(container.querySelector('[aria-current="true"]')).toBeNull();
  fireEvent.keyDown(input, { key: "Enter" });
  expect(onOpen).toHaveBeenCalledExactlyOnceWith("new-1");
  expect(getByRole("status").textContent).toContain("2 results");
  expect(getByRole("status").textContent).not.toContain("old-3");
});

test("a pending request says so, instead of leaving the last query's answer on screen", () => {
  const { input, pending, container } = mount();
  type(input, "milk");
  act(() => void vi.advanceTimersByTime(150));
  expect(pending).toHaveLength(1);
  expect(container.querySelector(".palette-note")?.textContent).toBe("searching…");
});

test("partial results say how many sessions were searched", async () => {
  const { input, pending, container } = mount();
  type(input, "milk");
  act(() => void vi.advanceTimersByTime(150));
  const first = pending[0];
  if (!first) throw new Error("no request");
  first.answer({
    hits: [hit("s1")],
    state: "partial",
    reason: "time_budget",
    scanned: 17,
    total: 41,
    request_id: first.id,
  });
  await settle();
  expect(sessionIds(container)).toEqual(["s1"]);
  expect(container.querySelector(".palette-note")?.textContent).toBe(
    "partial results · 17 of 41 sessions searched",
  );

  // The same budget with nothing found must not read as a complete "no matches": the sessions
  // it never opened are exactly where the user's transcript may be.
  type(input, "eggs");
  act(() => void vi.advanceTimersByTime(150));
  const second = pending[1];
  if (!second) throw new Error("no second request");
  second.answer({
    hits: [],
    state: "partial",
    reason: "scan_budget",
    scanned: 3,
    total: 41,
    request_id: second.id,
  });
  await settle();
  expect(container.querySelector(".palette-note")?.textContent).toBe(
    "no matches in 3 of 41 sessions · search incomplete",
  );
});

test("a superseded result is never painted", async () => {
  const { input, pending, container } = mount();
  type(input, "milk");
  act(() => void vi.advanceTimersByTime(150));
  pending[0]?.resolve([hit("s1")]);
  await settle();
  expect(sessionIds(container)).toEqual(["s1"]);

  // The app-wide gate can answer a query it abandoned for a longer one that starts with it.
  // It never scanned, so its empty hits are not an answer: wiping the list and saying "no
  // matches" would report a search nobody ran.
  type(input, "milk shake");
  act(() => void vi.advanceTimersByTime(150));
  const second = pending[1];
  if (!second) throw new Error("no second request");
  second.answer({
    hits: [],
    state: "superseded",
    reason: "",
    scanned: 0,
    total: 0,
    request_id: second.id,
  });
  await settle();
  expect(sessionIds(container)).toEqual(["s1"]);
  expect(container.querySelector(".palette-note")?.textContent).not.toBe("no matches");
  // And the pending claim goes with it: this answer is the only one this request will get, so
  // "searching…" would assert a search that no longer exists anywhere.
  expect(container.querySelector(".palette-note")).toBeNull();
});

test("a late response for an older request id is dropped even if its text matches", async () => {
  const { input, pending, container, getByRole } = mount();
  type(input, "milk");
  act(() => void vi.advanceTimersByTime(150)); // request A, "milk"
  type(input, "mil");
  act(() => void vi.advanceTimersByTime(150)); // request B, "mil"
  type(input, "milk");
  act(() => void vi.advanceTimersByTime(150)); // request C, "milk" again
  expect(pending.map((p) => p.q)).toEqual(["milk", "mil", "milk"]);

  const third = pending[2];
  if (!third) throw new Error("no third request");
  third.answer(ready([hit("fresh")], third.id));
  await settle();
  expect(sessionIds(container)).toEqual(["fresh"]);
  fireEvent.keyDown(input, { key: "ArrowDown" });
  expect(getByRole("status").textContent).toBe("Result 1 of 1: fresh");

  // A finally answers. Its text is the text on screen again, so only its id can tell the
  // palette that this is an answer to a question the user has already had answered.
  const first = pending[0];
  if (!first) throw new Error("no first request");
  first.answer(ready([hit("stale")], first.id));
  await settle();
  expect(sessionIds(container)).toEqual(["fresh"]);
  expect(container.querySelector('[aria-current="true"]')?.getAttribute("data-session-id")).toBe(
    "fresh",
  );
  expect(getByRole("status").textContent).toBe("Result 1 of 1: fresh");
});

test("a result stamped with a request id we never sent is not this palette's answer", async () => {
  const { input, pending, container } = mount();
  type(input, "milk");
  act(() => void vi.advanceTimersByTime(150));
  pending[0]?.answer(ready([hit("s1")], "99999999-9999-4999-8999-999999999999"));
  await settle();
  expect(sessionIds(container)).toEqual([]);
});

test("a result echoing this request's own id is painted", async () => {
  const { input, pending, container } = mount();
  type(input, "milk");
  act(() => void vi.advanceTimersByTime(150));
  const first = pending[0];
  if (!first) throw new Error("no request");
  expect(first.id).not.toBe("");
  first.answer(ready([hit("s1")], first.id));
  await settle();
  expect(sessionIds(container)).toEqual(["s1"]);
  expect(container.querySelector(".palette-note")).toBeNull();
});

test("a typed error reads as a failure and takes the previous query's rows with it", async () => {
  const { input, pending, container, onOpen, getByRole } = mount();
  type(input, "milk");
  act(() => void vi.advanceTimersByTime(150));
  pending[0]?.resolve([hit("s1"), hit("s2")]);
  await settle();
  expect(sessionIds(container)).toEqual(["s1", "s2"]);

  type(input, "eggs");
  act(() => void vi.advanceTimersByTime(150));
  fireEvent.keyDown(input, { key: "ArrowDown" });
  expect(container.querySelector(".hit-active")?.getAttribute("data-session-id")).toBe("s1");
  const second = pending[1];
  if (!second) throw new Error("no second request");
  second.answer({
    hits: [],
    state: "error",
    reason: "project_unreadable",
    scanned: 0,
    total: 0,
    request_id: second.id,
  });
  await settle();
  expect(container.querySelector(".palette-note")?.textContent).toBe("search failed");
  expect(getByRole("status").textContent).toBe("search failed");
  expect(container.querySelector('[aria-current="true"]')).toBeNull();
  expect(sessionIds(container)).toEqual([]);
  fireEvent.keyDown(input, { key: "Enter" });
  expect(onOpen).not.toHaveBeenCalled();
});

test("a body that is not a SearchResult reads as a failure, never as an empty search", async () => {
  const { input, pending, container } = mount();
  type(input, "milk");
  act(() => void vi.advanceTimersByTime(150));
  // What a proxy, or a server older than this bundle, could answer on the same route.
  pending[0]?.answer([hit("s1")] as unknown as SearchResult);
  await settle();
  expect(container.querySelector(".palette-note")?.textContent).toBe("search failed");
  expect(sessionIds(container)).toEqual([]);
});

test("the input stops at the query cap the server refuses with 400", () => {
  const { input } = mount();
  expect(input.maxLength).toBe(256);
});

test("archived badge renders when archived prop returns true for a session", async () => {
  const { search, pending } = scriptedSearch();
  const onOpen = vi.fn();
  const onClose = vi.fn();
  const { container } = render(
    <SearchPalette
      open
      search={search}
      onOpen={onOpen}
      onClose={onClose}
      nowMs={NOW}
      archived={(sessionId) => sessionId === "s1"}
    />,
  );
  const input = container.querySelector("input") as HTMLInputElement;
  type(input, "a");
  act(() => void vi.advanceTimersByTime(150));
  pending[0]?.resolve([
    { session_id: "s1", title: "Archived", role: "user", snippet: "", at: "" },
    { session_id: "s2", title: "Active", role: "user", snippet: "", at: "" },
  ]);
  await act(async () => {
    await Promise.resolve();
  });
  const badges = [...container.querySelectorAll(".hit-badge")];
  expect(badges).toHaveLength(1);
  expect(badges[0]?.textContent).toBe("Archived");
  const hits = [...container.querySelectorAll(".hit")];
  expect(hits[0]?.querySelector(".hit-badge")).not.toBeNull();
  expect(hits[1]?.querySelector(".hit-badge")).toBeNull();
});

test("archived badge does not render when archived prop is not provided", async () => {
  const { input, pending, container } = mount();
  type(input, "a");
  act(() => void vi.advanceTimersByTime(150));
  pending[0]?.resolve([{ session_id: "s1", title: "Session", role: "user", snippet: "", at: "" }]);
  await act(async () => {
    await Promise.resolve();
  });
  expect(container.querySelectorAll(".hit-badge")).toHaveLength(0);
});
