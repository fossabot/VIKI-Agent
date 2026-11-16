/**
 * Loading.tsx: useHeld's four timing cases (under fake timers, StrictMode),
 * Loading's own text and its `announce` switch, and BusyLabel's accessible name, which stays
 * the same busy or not.
 * Loading.css.unit.test.ts pins the stylesheet; this file pins behaviour only.
 */

import { act, cleanup, render } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { BusyLabel, Check, Loading, MIN_VISIBLE_MS, SHOW_DELAY_MS, useHeld } from "./Loading";

afterEach(cleanup);

function Probe({ pending }: { pending: boolean }) {
  const held = useHeld(pending);
  return <div data-testid="held">{String(held)}</div>;
}

function probe(pending: boolean) {
  return (
    <StrictMode>
      <Probe pending={pending} />
    </StrictMode>
  );
}

function heldOf(container: HTMLElement): boolean {
  return container.querySelector('[data-testid="held"]')?.textContent === "true";
}

describe("useHeld", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  test("the hold window is SHOW_DELAY_MS + MIN_VISIBLE_MS (300 + 400 = 700)", () => {
    expect(SHOW_DELAY_MS).toBe(300);
    expect(MIN_VISIBLE_MS).toBe(400);
  });

  test("true at once while pending, and a 100 ms wait ends at once (never reached the delay)", () => {
    const { container, rerender } = render(probe(true));
    expect(heldOf(container)).toBe(true);
    act(() => vi.advanceTimersByTime(100));
    rerender(probe(false));
    expect(heldOf(container)).toBe(false);
  });

  test("a 350 ms wait stays held until 700 ms, not until pending itself drops", () => {
    const { container, rerender } = render(probe(true));
    act(() => vi.advanceTimersByTime(350));
    rerender(probe(false));
    // A buggy `useHeld` that just returns `pending` would already be false here: this is the
    // assertion that catches it.
    expect(heldOf(container)).toBe(true);
    act(() => vi.advanceTimersByTime(349));
    expect(heldOf(container)).toBe(true);
    act(() => vi.advanceTimersByTime(1));
    expect(heldOf(container)).toBe(false);
  });

  test("a 2 s wait ends at once (already well past the hold window)", () => {
    const { container, rerender } = render(probe(true));
    act(() => vi.advanceTimersByTime(2000));
    rerender(probe(false));
    expect(heldOf(container)).toBe(false);
  });

  test("a new wait starting mid-hold keeps the first start, not a reset deadline", () => {
    const { container, rerender } = render(probe(true)); // t=0: wait begins
    act(() => vi.advanceTimersByTime(350));
    rerender(probe(false)); // t=350: ends; held until t=700 unless renewed
    act(() => vi.advanceTimersByTime(50));
    rerender(probe(true)); // t=400: a new wait, still inside the first hold
    act(() => vi.advanceTimersByTime(50));
    rerender(probe(false)); // t=450: ends again
    expect(heldOf(container)).toBe(true);
    act(() => vi.advanceTimersByTime(249)); // t=699: still before the ORIGINAL deadline
    expect(heldOf(container)).toBe(true);
    act(() => vi.advanceTimersByTime(1)); // t=700: the first start's deadline, not t=1100
    expect(heldOf(container)).toBe(false);
  });

  test("an idle render never re-sets state, even while Date.now() keeps advancing", () => {
    // Fake timers hold Date.now() still between renders, which hides this bug: a real clock
    // (or a component slow enough to take over 1ms to render) never reads the same millisecond
    // twice, which is exactly what this spy stands in for.
    vi.useRealTimers();
    let now = 1000;
    const dateNow = vi.spyOn(Date, "now").mockImplementation(() => {
      now += 5;
      return now;
    });
    let renders = 0;
    function Probe() {
      renders++;
      useHeld(false);
      return null;
    }
    try {
      expect(() => {
        const { rerender } = render(<Probe />);
        rerender(<Probe />);
        rerender(<Probe />);
        rerender(<Probe />);
      }).not.toThrow();
      // Exactly one render per render this test caused: no render-phase update chained on top.
      expect(renders).toBe(4);
    } finally {
      dateNow.mockRestore();
    }
  });
});

describe("Loading", () => {
  test("keeps its words as a direct text child beside the hidden spinner", () => {
    const { getByText, container } = render(<Loading>Loading…</Loading>);
    const el = getByText("Loading…");
    expect(el.tagName).toBe("P");
    expect(el.className).toContain("loading");
    expect(container.querySelector(".spinner")).not.toBeNull();
  });

  test("announces by default (role=status); announce=false sets no role", () => {
    const { container, rerender } = render(<Loading>Reading…</Loading>);
    expect(container.querySelector("p.loading")?.getAttribute("role")).toBe("status");
    rerender(<Loading announce={false}>Reading…</Loading>);
    expect(container.querySelector("p.loading")?.getAttribute("role")).toBeNull();
  });

  test("inline renders a span instead of a p, keeping the same words", () => {
    const { getByText } = render(<Loading inline>Reading…</Loading>);
    expect(getByText("Reading…").tagName).toBe("SPAN");
  });
});

describe("Check", () => {
  // The kit's tick uses the "check-mark" class, not the bare "check" class, which would collide
  // with Workbench/Checks.css's own ".check" (a <li> row style, unrelated).
  test("renders an svg whose class is exactly check-mark", () => {
    const { container } = render(<Check />);
    expect(container.querySelector("svg")?.getAttribute("class")).toBe("check-mark");
  });
});

describe("BusyLabel", () => {
  test("keeps the button's accessible name unchanged whether busy or not", () => {
    const { getByRole, rerender } = render(
      <button type="button">
        <BusyLabel busy={false}>Save</BusyLabel>
      </button>,
    );
    expect(getByRole("button", { name: "Save" }).tagName).toBe("BUTTON");
    rerender(
      <button type="button">
        <BusyLabel busy={true}>Save</BusyLabel>
      </button>,
    );
    expect(getByRole("button", { name: "Save" }).tagName).toBe("BUTTON");
  });

  test("busy grows a spinner-late busy-spinner in front of the unchanged label", () => {
    const { container, getByText } = render(
      <button type="button">
        <BusyLabel busy={true}>Save</BusyLabel>
      </button>,
    );
    expect(container.querySelector(".spinner.spinner-late.busy-spinner")).not.toBeNull();
    expect(getByText("Save")).not.toBeNull();
  });

  test("not busy renders no spinner at all", () => {
    const { container } = render(
      <button type="button">
        <BusyLabel busy={false}>Save</BusyLabel>
      </button>,
    );
    expect(container.querySelector(".spinner")).toBeNull();
  });
});

describe("BusyLabel failed", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  function probe(busy: boolean, failed: boolean) {
    return (
      <button type="button">
        <BusyLabel busy={busy} failed={failed}>
          Save
        </BusyLabel>
      </button>
    );
  }

  test("failed turning true at 400 ms, inside the hold window, drops the spinner at once", () => {
    const { container, rerender } = render(probe(true, false));
    act(() => vi.advanceTimersByTime(400));
    rerender(probe(false, true));
    expect(container.querySelector(".busy-spinner")).toBeNull();
  });

  test("the same 400 ms wait without failed still shows the spinner until 700 ms (existing behaviour)", () => {
    const { container, rerender } = render(probe(true, false));
    act(() => vi.advanceTimersByTime(400));
    rerender(probe(false, false));
    expect(container.querySelector(".busy-spinner")).not.toBeNull();
    act(() => vi.advanceTimersByTime(299));
    expect(container.querySelector(".busy-spinner")).not.toBeNull();
    act(() => vi.advanceTimersByTime(1));
    expect(container.querySelector(".busy-spinner")).toBeNull();
  });
});
