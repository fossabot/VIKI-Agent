import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { PaneGroup, PaneNode, PaneSplit } from "../../state/panes";
import { PaneTree } from "./PaneTree";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function group(id: string): PaneGroup {
  return { kind: "group", id, tabs: [id], active: id };
}

function split(
  id: string,
  dir: PaneSplit["dir"],
  children: readonly PaneNode[],
  sizes: readonly number[],
): PaneSplit {
  return { kind: "split", id, dir, children, sizes };
}

function renderGroup(g: PaneGroup) {
  return <div data-testid={`group-${g.id}`}>{g.id}</div>;
}

/** Captures every `ResizeObserver` constructed, so a test can drive its callback once jsdom's
 * own layout (which never reports a real size) has been stubbed on the node it observes. */
class StubResizeObserver {
  static callbacks: ResizeObserverCallback[] = [];
  constructor(callback: ResizeObserverCallback) {
    StubResizeObserver.callbacks.push(callback);
  }
  observe() {}
  unobserve() {}
  disconnect() {}
}

function fireResize() {
  for (const callback of StubResizeObserver.callbacks) {
    callback([], StubResizeObserver as unknown as ResizeObserver);
  }
}

function stubSize(el: Element, axis: "clientWidth" | "clientHeight", px: number) {
  Object.defineProperty(el, axis, { value: px, configurable: true });
}

function mountMeasured(root: PaneNode, onResize = vi.fn()) {
  vi.stubGlobal("ResizeObserver", StubResizeObserver);
  StubResizeObserver.callbacks = [];
  const view = render(<PaneTree root={root} renderGroup={renderGroup} onResize={onResize} />);
  return { view, onResize };
}

test("a group root renders only the caller's content", () => {
  const view = render(<PaneTree root={group("g1")} renderGroup={renderGroup} onResize={vi.fn()} />);
  expect(view.getByTestId("group-g1")).toBeTruthy();
  expect(view.container.querySelector(".pane-split")).toBeNull();
});

test("order and nesting follow the tree depth-first; a sash waits for its split to be measured", () => {
  const root = split(
    "s1",
    "row",
    [group("g1"), split("s2", "column", [group("g2"), group("g3")], [0.5, 0.5])],
    [0.4, 0.6],
  );
  const { view } = mountMeasured(root);

  expect(view.container.querySelectorAll(".pane-sash")).toHaveLength(0);
  const cellIds = [...view.container.querySelectorAll(".pane-cell")].map((el) => el.id);
  expect(cellIds).toEqual(["g1", "s2", "g2", "g3"]);

  const outer = view.container.querySelector('.pane-split[data-dir="row"]') as Element;
  const inner = view.container.querySelector('.pane-split[data-dir="column"]') as Element;
  expect(outer).toBeTruthy();
  expect(inner).toBeTruthy();

  stubSize(outer, "clientWidth", 1000);
  stubSize(inner, "clientHeight", 600);
  act(() => fireResize());

  expect(view.container.querySelectorAll(".pane-sash")).toHaveLength(2);
  const order = [...view.container.querySelectorAll(".pane-cell, .pane-sash")].map((el) =>
    el.classList.contains("pane-cell") ? el.id : "sash",
  );
  expect(order).toEqual(["g1", "sash", "s2", "g2", "sash", "g3"]);
});

test("a row split's cells carry proportional flex and a width floor", () => {
  const row = split("s1", "row", [group("g1"), group("g2"), group("g3")], [0.2, 0.3, 0.5]);
  const { view } = mountMeasured(row);
  const cells = [...view.container.querySelectorAll(".pane-cell")] as HTMLElement[];
  expect(cells.map((el) => el.style.flex)).toEqual(["0.2 1 0px", "0.3 1 0px", "0.5 1 0px"]);
  for (const cell of cells) {
    expect(cell.style.minWidth).toBe("420px");
    expect(cell.style.minHeight).toBe("");
  }
});

test("a column split's cells carry proportional flex and a height floor", () => {
  const column = split("s1", "column", [group("g1"), group("g2")], [0.7, 0.3]);
  const { view } = mountMeasured(column);
  const cells = [...view.container.querySelectorAll(".pane-cell")] as HTMLElement[];
  expect(cells.map((el) => el.style.flex)).toEqual(["0.7 1 0px", "0.3 1 0px"]);
  for (const cell of cells) {
    expect(cell.style.minHeight).toBe("240px");
    expect(cell.style.minWidth).toBe("");
  }
});

/** A measured 2,000px row split, [0.6, 0.4]: the one sash's floor is 420 on each side, so its
 * value starts at 1,200, inside [420, 1580]. */
function mountPair(onResize = vi.fn()) {
  const root = split("s1", "row", [group("g1"), group("g2")], [0.6, 0.4]);
  const { view } = mountMeasured(root, onResize);
  const splitEl = view.container.querySelector(".pane-split") as Element;
  stubSize(splitEl, "clientWidth", 2000);
  act(() => fireResize());
  return { handle: view.getByRole("separator"), onResize };
}

test("ArrowRight commits the dragged pair in proportion; the keyboard step is unclamped", () => {
  const { handle, onResize } = mountPair();
  expect(handle.getAttribute("aria-valuenow")).toBe("1200");

  fireEvent.keyDown(handle, { key: "ArrowRight" });

  expect(onResize).toHaveBeenCalledTimes(1);
  const [splitId, sizes] = onResize.mock.calls[0] as [string, number[]];
  expect(splitId).toBe("s1");
  expect(sizes[0]).toBeCloseTo(0.605, 3);
  expect(sizes[1]).toBeCloseTo(0.395, 3);
});

test("Home clamps the dragged pane to its 420px floor", () => {
  const { handle, onResize } = mountPair();

  fireEvent.keyDown(handle, { key: "Home" });

  expect(onResize).toHaveBeenCalledTimes(1);
  const [, sizes] = onResize.mock.calls[0] as [string, number[]];
  expect(sizes[0]).toBeCloseTo(420 / 2000, 4);
  expect(sizes[1]).toBeCloseTo(1 - 420 / 2000, 4);
});

test("double click resets the dragged pair to equal halves", () => {
  const { handle, onResize } = mountPair();

  fireEvent.doubleClick(handle);

  expect(onResize).toHaveBeenCalledTimes(1);
  const [, sizes] = onResize.mock.calls[0] as [string, number[]];
  expect(sizes[0]).toBeCloseTo(0.5, 4);
  expect(sizes[1]).toBeCloseTo(0.5, 4);
});
