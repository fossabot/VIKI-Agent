/** Acceptance (frozen): the Ctrl+K search palette's debounce, staleness and keys. */
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { SearchPalette } from "../../components/Search/SearchPalette";
import type { SearchHit, SearchResult } from "../../protocol";
import { localIso } from "./fakes";

const NOW = new Date(2026, 8, 12, 18, 0).getTime();

const hits: SearchHit[] = [
  { session_id: "s1", title: "Linter", role: "title", snippet: "", at: "" },
  {
    session_id: "s2",
    title: "Milk",
    role: "user",
    snippet: "Buy milk",
    at: localIso(2026, 9, 12, 9, 5),
  },
  {
    session_id: "s3",
    title: "Eggs",
    role: "assistant",
    snippet: "And eggs",
    at: localIso(2026, 9, 1, 9, 5),
  },
];

/** The observation a completed, unbudgeted scan reports; the frozen assertions read the hits. */
function ready(hits: SearchHit[]): SearchResult {
  return { hits, state: "ready", reason: "", scanned: 1, total: 1, request_id: "" };
}

interface Pending {
  q: string;
  resolve: (hits: SearchHit[]) => void;
  reject: (error: unknown) => void;
}

function scriptedSearch() {
  const pending: Pending[] = [];
  const search = (q: string) =>
    new Promise<SearchResult>((resolve, reject) => {
      pending.push({ q, resolve: (hits) => resolve(ready(hits)), reject });
    });
  return { search, pending };
}

async function settle(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function mount(overrides: Partial<Parameters<typeof SearchPalette>[0]> = {}) {
  const { search, pending } = scriptedSearch();
  const onOpen = vi.fn();
  const onClose = vi.fn();
  const utils = render(
    <SearchPalette
      open
      search={search}
      onOpen={onOpen}
      onClose={onClose}
      nowMs={NOW}
      {...overrides}
    />,
  );
  const input = utils.container.querySelector("input") as HTMLInputElement;
  return { ...utils, pending, onOpen, onClose, input };
}

function type(input: HTMLInputElement, value: string): void {
  fireEvent.change(input, { target: { value } });
}

test("closed renders nothing; open focuses the input inside a dialog", () => {
  const { container, unmount } = render(
    <SearchPalette
      open={false}
      search={async () => ready([])}
      onOpen={vi.fn()}
      onClose={vi.fn()}
      nowMs={NOW}
    />,
  );
  expect(container.querySelector('[role="dialog"]')).toBeNull();
  unmount();
  const { input } = mount();
  expect(document.activeElement).toBe(input);
  expect(input.getAttribute("placeholder")).toBe("Search sessions");
  expect(document.querySelector('[role="dialog"]')?.getAttribute("aria-label")).toBe(
    "Search sessions",
  );
});

test("typing searches once after the debounce, with the final text; blank searches nothing", async () => {
  const { input, pending } = mount();
  type(input, "mi");
  act(() => void vi.advanceTimersByTime(100));
  type(input, "milk");
  act(() => void vi.advanceTimersByTime(149));
  expect(pending).toHaveLength(0);
  act(() => void vi.advanceTimersByTime(1));
  expect(pending.map((p) => p.q)).toEqual(["milk"]);
  type(input, "   ");
  act(() => void vi.advanceTimersByTime(150));
  expect(pending).toHaveLength(1);
});

test("hits render title, role chip, snippet and time; a stale response is dropped", async () => {
  const { input, pending, container } = mount();
  type(input, "milk");
  act(() => void vi.advanceTimersByTime(150));
  type(input, "eggs");
  act(() => void vi.advanceTimersByTime(150));
  expect(pending.map((p) => p.q)).toEqual(["milk", "eggs"]);
  pending[1]?.resolve(hits);
  await settle();
  const rows = [...container.querySelectorAll<HTMLElement>(".hit")];
  expect(rows.map((r) => r.getAttribute("data-session-id"))).toEqual(["s1", "s2", "s3"]);
  expect(rows.map((r) => r.querySelector(".chip")?.textContent ?? null)).toEqual([
    null,
    "you",
    "VIKI",
  ]);
  expect(rows[1]?.querySelector(".hit-snippet")?.textContent).toBe("Buy milk");
  expect(rows.map((r) => r.querySelector(".hit-at")?.textContent ?? null)).toEqual([
    null,
    "09:05",
    "09-01",
  ]);
  pending[0]?.resolve([
    { session_id: "stale", title: "Stale", role: "title", snippet: "", at: "" },
  ]);
  await settle();
  expect(
    [...container.querySelectorAll(".hit")].map((r) => r.getAttribute("data-session-id")),
  ).toEqual(["s1", "s2", "s3"]);
});

test("Enter opens the first hit, arrows move the highlight, Escape closes", async () => {
  const { input, pending, container, onOpen, onClose } = mount();
  type(input, "x");
  act(() => void vi.advanceTimersByTime(150));
  pending[0]?.resolve(hits);
  await settle();
  fireEvent.keyDown(input, { key: "ArrowDown" });
  fireEvent.keyDown(input, { key: "ArrowDown" });
  expect(container.querySelector(".hit-active")?.getAttribute("data-session-id")).toBe("s2");
  fireEvent.keyDown(input, { key: "ArrowUp" });
  fireEvent.keyDown(input, { key: "ArrowUp" });
  expect(container.querySelector(".hit-active")?.getAttribute("data-session-id")).toBe("s3"); // wraps
  fireEvent.keyDown(input, { key: "Enter" });
  expect(onOpen).toHaveBeenLastCalledWith("s3");
  expect(onClose).toHaveBeenCalledTimes(1);
  fireEvent.keyDown(input, { key: "Escape" });
  expect(onClose).toHaveBeenCalledTimes(2);
  expect(onOpen).toHaveBeenCalledTimes(1);
});

test("with no highlight Enter opens the first hit; clicking a hit opens it too", async () => {
  const { input, pending, container, onOpen, onClose } = mount();
  type(input, "x");
  act(() => void vi.advanceTimersByTime(150));
  pending[0]?.resolve(hits);
  await settle();
  fireEvent.keyDown(input, { key: "Enter" });
  expect(onOpen).toHaveBeenLastCalledWith("s1");
  fireEvent.click(container.querySelector('[data-session-id="s2"]') as HTMLElement);
  expect(onOpen).toHaveBeenLastCalledWith("s2");
  expect(onClose).toHaveBeenCalledTimes(2);
});

test("a failed search says so and keeps the input; no matches says so", async () => {
  const { input, pending, container } = mount();
  type(input, "boom");
  act(() => void vi.advanceTimersByTime(150));
  pending[0]?.reject(new Error("500"));
  await settle();
  expect(container.querySelector(".palette-note")?.textContent).toBe("search failed");
  expect(input.value).toBe("boom");
  type(input, "none");
  act(() => void vi.advanceTimersByTime(150));
  pending[1]?.resolve([]);
  await settle();
  expect(container.querySelector(".palette-note")?.textContent).toBe("no matches");
  expect(container.querySelectorAll(".hit")).toHaveLength(0);
});

test("a custom debounce is honoured", () => {
  const { input, pending } = mount({ debounceMs: 20 });
  type(input, "q");
  act(() => void vi.advanceTimersByTime(19));
  expect(pending).toHaveLength(0);
  act(() => void vi.advanceTimersByTime(1));
  expect(pending).toHaveLength(1);
});
