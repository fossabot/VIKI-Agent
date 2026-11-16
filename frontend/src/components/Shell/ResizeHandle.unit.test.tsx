import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { useState } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { ResizeHandle, type ResizeHandleProps } from "./ResizeHandle";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function mount(props: Partial<ResizeHandleProps> = {}) {
  const events: string[] = [];
  let setControlled: (next: number) => void = () => {};
  function Controlled(extra: Partial<ResizeHandleProps>) {
    const [value, setValue] = useState(extra.value ?? 300);
    setControlled = setValue;
    return (
      <ResizeHandle
        label="Resize pane"
        controls="pane"
        orientation="vertical"
        growDirection={1}
        min={200}
        max={500}
        defaultValue={300}
        collapsible
        {...extra}
        value={value}
        onPreview={(next) => {
          events.push(`preview:${next}`);
          setValue(next);
        }}
        onCommit={(next, intent) => events.push(`commit:${next}:${intent}`)}
      />
    );
  }
  const view = render(<Controlled {...props} />);
  return {
    handle: view.getByRole("separator", { name: "Resize pane" }),
    events,
    unmount: view.unmount,
    forceValue: (next: number) => act(() => setControlled(next)),
    change: (extra: Partial<ResizeHandleProps>) =>
      view.rerender(<Controlled {...props} {...extra} />),
  };
}

const axes = [
  ["vertical", 1, "ArrowRight", "ArrowLeft"],
  ["vertical", -1, "ArrowLeft", "ArrowRight"],
  ["horizontal", 1, "ArrowDown", "ArrowUp"],
  ["horizontal", -1, "ArrowUp", "ArrowDown"],
] as const;

test.each(axes)(
  "%s / %i previews and commits 10/50px in physical direction",
  (orientation, growDirection, grow, shrink) => {
    const { handle, events } = mount({ orientation, growDirection });
    expect(handle.tabIndex).toBe(0);
    expect(handle.getAttribute("aria-controls")).toBe("pane");
    expect(handle.getAttribute("aria-orientation")).toBe(orientation);
    expect(handle.getAttribute("aria-valuemin")).toBe("0");
    expect(handle.getAttribute("aria-valuemax")).toBe("500");
    expect(handle.getAttribute("aria-valuetext")).toBe("300 pixels");
    for (const [key, shiftKey, next] of [
      [grow, false, 310],
      [grow, true, 360],
      [shrink, false, 350],
      [shrink, true, 300],
    ] as const) {
      events.length = 0;
      expect(fireEvent.keyDown(handle, { key, shiftKey })).toBe(false);
      expect(events).toEqual([`preview:${next}`, `commit:${next}:resize`]);
      expect(handle.getAttribute("aria-valuenow")).toBe(String(next));
    }
  },
);

test("Home/End obey expanded bounds and unchanged operations publish nothing", () => {
  const { handle, events } = mount();
  for (const [key, next] of [
    ["Home", 200],
    ["End", 500],
  ] as const) {
    events.length = 0;
    fireEvent.keyDown(handle, { key });
    expect(events).toEqual([`preview:${next}`, `commit:${next}:resize`]);
    events.length = 0;
    fireEvent.keyDown(handle, { key });
    expect(events).toEqual([]);
  }
  fireEvent.keyDown(handle, { key: "ArrowRight", shiftKey: true });
  expect(events).toEqual([]);
});

test.each(axes)(
  "%s / %i ignores unrelated axes, modifiers and composing input",
  (orientation, growDirection, grow) => {
    const { handle, events } = mount({ orientation, growDirection });
    for (const input of [
      { key: orientation === "vertical" ? "ArrowDown" : "ArrowRight" },
      { key: "Escape" },
      { key: grow, ctrlKey: true },
      { key: grow, altKey: true },
      { key: grow, metaKey: true },
      { key: grow, isComposing: true },
      { key: grow, keyCode: 229 },
    ]) {
      expect(fireEvent.keyDown(handle, input)).toBe(true);
    }
    expect(events).toEqual([]);
    expect(handle.getAttribute("aria-valuenow")).toBe("300");
  },
);

test("Enter remembers the actual size and restores under current bounds", () => {
  const { handle, events, change } = mount({ value: 360 });
  fireEvent.keyDown(handle, { key: "Enter" });
  expect(handle.getAttribute("aria-valuenow")).toBe("0");
  expect(handle.getAttribute("aria-valuetext")).toBe("Collapsed");
  change({ max: 250 });
  fireEvent.keyDown(handle, { key: "Enter" });
  expect(events).toEqual(["preview:0", "commit:0:collapse", "preview:250", "commit:250:restore"]);
  expect(handle.getAttribute("aria-valuenow")).toBe("250");
});

test("first mount collapsed restores its rounded default", () => {
  const { handle, events } = mount({ value: 0, defaultValue: 333.6 });
  fireEvent.keyDown(handle, { key: "Enter" });
  expect(events).toEqual(["preview:334", "commit:334:restore"]);
});

test("double click resets a collapsed pane to the bounded default once", () => {
  const { handle, events } = mount({ value: 0, defaultValue: 700 });
  fireEvent.doubleClick(handle);
  expect(events).toEqual(["preview:500", "commit:500:reset"]);
  expect(handle.getAttribute("aria-valuetext")).toBe("500 pixels");
  events.length = 0;
  fireEvent.doubleClick(handle);
  expect(events).toEqual([]);
});

test("resetAtEqual commits reset again at equal value; without it the second double-click is a no-op", () => {
  const { handle, events } = mount({ value: 0, defaultValue: 700, resetAtEqual: true });
  fireEvent.doubleClick(handle);
  expect(events).toEqual(["preview:500", "commit:500:reset"]);
  events.length = 0;
  fireEvent.doubleClick(handle);
  expect(events).toEqual(["commit:500:reset"]);
});

test("non-collapsible Enter resets to the bounded default and exposes the expanded minimum", () => {
  const { handle, events } = mount({ collapsible: false, value: 350 });
  expect(handle.getAttribute("aria-valuemin")).toBe("200");
  expect(fireEvent.keyDown(handle, { key: "Enter" })).toBe(false);
  expect(events).toEqual(["preview:300", "commit:300:reset"]);
});

test("non-collapsible Enter already at the default commits nothing without resetAtEqual", () => {
  const { handle, events } = mount({ collapsible: false });
  expect(fireEvent.keyDown(handle, { key: "Enter" })).toBe(false);
  expect(events).toEqual([]);
});

test("non-collapsible Enter with resetAtEqual commits reset again at an equal value", () => {
  const { handle, events } = mount({ collapsible: false, resetAtEqual: true });
  expect(fireEvent.keyDown(handle, { key: "Enter" })).toBe(false);
  expect(events).toEqual(["commit:300:reset"]);
});

function pointer(handle: HTMLElement, type: string, init: PointerEventInit = {}) {
  const event = new MouseEvent(type, {
    bubbles: true,
    cancelable: true,
    button: 0,
    clientX: 100,
    clientY: 100,
    ...init,
  });
  Object.defineProperties(event, {
    pointerId: { value: init.pointerId ?? 1 },
    isPrimary: { value: init.isPrimary ?? true },
  });
  return fireEvent(handle, event);
}

function capture(handle: HTMLElement) {
  let held: number | null = null;
  const set = vi.fn((id: number) => {
    held = id;
  });
  const release = vi.fn((id: number) => {
    held = null;
    pointer(handle, "lostpointercapture", { pointerId: id });
  });
  Object.assign(handle, {
    setPointerCapture: set,
    releasePointerCapture: release,
    hasPointerCapture: (id: number) => held === id,
  });
  return { set, release };
}

test.each([0, 300])("pointer focus keeps the frame in place at pane width %i", (value) => {
  const { handle, events } = mount({ value });
  const { release } = capture(handle);
  const focus = vi.spyOn(handle, "focus");
  pointer(handle, "pointerdown");
  expect(focus).toHaveBeenCalledWith({ preventScroll: true });
  expect(document.activeElement).toBe(handle);
  pointer(handle, "pointerup");
  expect(events).toEqual([]);
  expect(release).toHaveBeenCalledOnce();
});

test.each(axes)(
  "%s / %i commits final pointer coordinates after preview-only moves",
  (orientation, growDirection) => {
    const { handle, events, change } = mount({ orientation, growDirection });
    const { set, release } = capture(handle);
    const axis = orientation === "vertical" ? "clientX" : "clientY";
    expect(pointer(handle, "pointerdown")).toBe(false);
    expect(document.activeElement).toBe(handle);
    expect(set).toHaveBeenCalledWith(1);
    expect(handle.hasAttribute("data-dragging")).toBe(true);
    pointer(handle, "pointermove", { [axis]: 120 });
    expect(events).toEqual([`preview:${300 + 20 * growDirection}`]);
    change({});
    for (const key of ["ArrowRight", "Home", "End", "Enter"]) fireEvent.keyDown(handle, { key });
    pointer(handle, "pointerup", { [axis]: 140 });
    const final = 300 + 40 * growDirection;
    expect(events).toEqual([
      `preview:${300 + 20 * growDirection}`,
      `preview:${final}`,
      `commit:${final}:resize`,
    ]);
    expect(handle.getAttribute("aria-valuenow")).toBe(String(final));
    expect(release).toHaveBeenCalledWith(1);
    expect(release).toHaveBeenCalledTimes(1);
    // Cleared on a real commit too, not only on cancel/unmount (below) — a dialog's
    // useModalFocus must stop stepping aside for Escape the instant this drag ends.
    expect(handle.hasAttribute("data-dragging")).toBe(false);
  },
);

test("other buttons, pointers and non-primary samples cannot start or finish a drag", () => {
  const { handle, events } = mount();
  const { set, release } = capture(handle);
  for (const init of [{ button: 1 }, { button: 2 }, { isPrimary: false }])
    pointer(handle, "pointerdown", init);
  expect(set).not.toHaveBeenCalled();
  pointer(handle, "pointerdown");
  for (const init of [{ pointerId: 2 }, { isPrimary: false }]) {
    pointer(handle, "pointermove", { clientX: 150, ...init });
    pointer(handle, "pointerup", { clientX: 150, ...init });
  }
  pointer(handle, "pointercancel", { pointerId: 2 });
  expect(events).toEqual([]);
  expect(release).not.toHaveBeenCalled();
  pointer(handle, "pointerup", { clientX: 130 });
  expect(events).toEqual(["preview:330", "commit:330:resize"]);
});

test("a stationary drag publishes nothing; moves and final coordinates use current bounds", () => {
  const { handle, events } = mount();
  const { release } = capture(handle);
  pointer(handle, "pointerdown");
  pointer(handle, "pointerup");
  expect(events).toEqual([]);
  pointer(handle, "pointerdown");
  pointer(handle, "pointermove", { clientX: 1000 });
  pointer(handle, "pointerup", { clientX: 0 });
  expect(events).toEqual(["preview:500", "preview:200", "commit:200:resize"]);
  expect(release).toHaveBeenCalledTimes(2);
});

test.each(["Escape", "pointercancel", "lostpointercapture", "blur", "unmount"])(
  "%s releases the drag without committing and removes its listeners",
  (terminal) => {
    const { handle, events, unmount } = mount();
    const { release } = capture(handle);
    const add = vi.spyOn(window, "addEventListener");
    const remove = vi.spyOn(window, "removeEventListener");
    pointer(handle, "pointerdown");
    expect(handle.hasAttribute("data-dragging")).toBe(true);
    pointer(handle, "pointermove", { clientX: 140 });
    if (terminal === "Escape") fireEvent.keyDown(window, { key: "Escape" });
    else if (terminal === "blur") fireEvent(window, new Event("blur"));
    else if (terminal === "unmount") unmount();
    else pointer(handle, terminal);
    // Cleared the instant this drag ends, by whichever means — a dialog's
    // useModalFocus (which treats a dragging handle like an open menu) must stop stepping aside
    // for Escape, including the "unmount" case, once nothing is dragging any more.
    expect(handle.hasAttribute("data-dragging")).toBe(false);
    expect(events).toEqual(
      terminal === "unmount" ? ["preview:340"] : ["preview:340", "preview:300"],
    );
    expect(release).toHaveBeenCalledWith(1);
    expect(release).toHaveBeenCalledTimes(1);
    for (const [type, listener] of add.mock.calls) {
      expect(
        remove.mock.calls.some(
          ([removedType, removedListener]) => removedType === type && removedListener === listener,
        ),
      ).toBe(true);
    }
    events.length = 0;
    pointer(handle, "pointerup", { clientX: 180 });
    fireEvent.keyDown(window, { key: "Escape" });
    fireEvent(window, new Event("blur"));
    expect(events).toEqual([]);
  },
);

const configurations: [Partial<ResizeHandleProps>, number][] = [
  [{ min: 350 }, 350],
  [{ max: 250 }, 250],
  [{ controls: "other-pane" }, 300],
  [{ orientation: "horizontal" }, 300],
  [{ growDirection: -1 }, 300],
  [{ value: 420 }, 300],
];
test.each(configurations)("configuration %j rolls back under current bounds", (next, rollback) => {
  const { handle, events, change, forceValue } = mount();
  const { release } = capture(handle);
  pointer(handle, "pointerdown");
  pointer(handle, "pointermove", { clientX: 140 });
  if (next.value !== undefined) forceValue(next.value);
  else change(next);
  expect(events).toEqual(["preview:340", `preview:${rollback}`]);
  expect(release).toHaveBeenCalledTimes(1);
  pointer(handle, "pointerup", { clientX: 180 });
  expect(events).toHaveLength(2);
});

test.each(axes)(
  "%s / %i keeps a collapsed stationary or returning drag at zero",
  (orientation, growDirection) => {
    const { handle, events } = mount({ value: 0, orientation, growDirection });
    const { release } = capture(handle);
    const axis = orientation === "vertical" ? "clientX" : "clientY";
    pointer(handle, "pointerdown");
    pointer(handle, "pointerup");
    expect(events).toEqual([]);
    pointer(handle, "pointerdown");
    pointer(handle, "pointermove", { [axis]: 100 - 20 * growDirection });
    pointer(handle, "pointerup", { [axis]: 100 - 40 * growDirection });
    expect(events).toEqual([]);
    pointer(handle, "pointerdown");
    pointer(handle, "pointermove", { [axis]: 100 + 20 * growDirection });
    pointer(handle, "pointerup");
    expect(events).toEqual(["preview:200", "preview:0"]);
    expect(handle.getAttribute("aria-valuenow")).toBe("0");
    events.length = 0;
    pointer(handle, "pointerdown");
    pointer(handle, "pointerup", { [axis]: 100 + 20 * growDirection });
    expect(events).toEqual(["preview:200", "commit:200:resize"]);
    expect(release).toHaveBeenCalledTimes(4);
  },
);

test.each(["Escape", "pointercancel", "lostpointercapture", "blur"])(
  "%s restores a collapsed start without publishing",
  (terminal) => {
    const { handle, events } = mount({ value: 0 });
    const { release } = capture(handle);
    pointer(handle, "pointerdown");
    pointer(handle, "pointermove", { clientX: 140 });
    if (terminal === "Escape") fireEvent.keyDown(window, { key: "Escape" });
    else if (terminal === "blur") fireEvent(window, new Event("blur"));
    else pointer(handle, terminal);
    expect(events).toEqual(["preview:200", "preview:0"]);
    expect(handle.getAttribute("aria-valuetext")).toBe("Collapsed");
    expect(release).toHaveBeenCalledTimes(1);
    pointer(handle, "pointerup", { clientX: 200 });
    expect(events).toHaveLength(2);
  },
);

test("a bounds change cancels a collapsed-start drag back to zero", () => {
  const { handle, events, change } = mount({ value: 0 });
  const { release } = capture(handle);
  pointer(handle, "pointerdown");
  pointer(handle, "pointermove", { clientX: 140 });
  act(() => change({ min: 250, max: 300 }));
  expect(events).toEqual(["preview:200", "preview:0"]);
  expect(release).toHaveBeenCalledTimes(1);
  pointer(handle, "pointerup", { clientX: 200 });
  expect(events).toHaveLength(2);
});

test.each([
  { isComposing: true },
  { keyCode: 229 },
  { ctrlKey: true },
  { altKey: true },
  { metaKey: true },
])("captured Escape leaves %j unconsumed and the drag active", (input) => {
  const { handle, events } = mount();
  const { release } = capture(handle);
  pointer(handle, "pointerdown");
  pointer(handle, "pointermove", { clientX: 140 });
  expect(fireEvent.keyDown(window, { key: "Escape", ...input })).toBe(true);
  expect(events).toEqual(["preview:340"]);
  expect(release).not.toHaveBeenCalled();
  pointer(handle, "pointermove", { clientX: 160 });
  expect(fireEvent.keyDown(window, { key: "Escape" })).toBe(false);
  expect(events).toEqual(["preview:340", "preview:360", "preview:300"]);
  expect(release).toHaveBeenCalledTimes(1);
});

test("removing collapse capability cancels the drag within current expanded bounds", () => {
  const { handle, events, change } = mount({ value: 0 });
  const { release } = capture(handle);
  pointer(handle, "pointerdown");
  pointer(handle, "pointermove", { clientX: 140 });
  act(() => change({ collapsible: false }));
  expect(events).toEqual(["preview:200", "preview:200"]);
  expect(handle.getAttribute("aria-valuemin")).toBe("200");
  expect(handle.getAttribute("aria-valuenow")).toBe("200");
  expect(release).toHaveBeenCalledTimes(1);
  fireEvent.keyDown(window, { key: "Escape" });
  pointer(handle, "pointerup", { clientX: 180 });
  expect(events).toHaveLength(2);
});

function withRootScale(scale: string, check: () => void) {
  const style = document.documentElement.style;
  const previous = style.getPropertyValue("--font-scale");
  const priority = style.getPropertyPriority("--font-scale");
  style.setProperty("--font-scale", scale);
  try {
    check();
  } finally {
    if (previous) style.setProperty("--font-scale", previous, priority);
    else style.removeProperty("--font-scale");
  }
}

test.each(axes)(
  "%s / %i converts zoomed viewport movement and final coordinates to local pixels",
  (orientation, growDirection) => {
    withRootScale("1.5", () => {
      const { handle, events } = mount({ orientation, growDirection });
      const { release } = capture(handle);
      const axis = orientation === "vertical" ? "clientX" : "clientY";
      pointer(handle, "pointerdown");
      pointer(handle, "pointermove", { [axis]: 136 });
      expect(events).toEqual([`preview:${300 + 24 * growDirection}`]);
      pointer(handle, "pointerup", { [axis]: 154 });
      const final = 300 + 36 * growDirection;
      expect(events).toEqual([
        `preview:${300 + 24 * growDirection}`,
        `preview:${final}`,
        `commit:${final}:resize`,
      ]);
      expect(handle.getAttribute("aria-valuenow")).toBe(String(final));
      expect(release).toHaveBeenCalledTimes(1);
    });
  },
);

test.each(axes)(
  "%s / %i retains local 10/50px keyboard steps at zoom",
  (orientation, growDirection, grow) => {
    withRootScale("1.5", () => {
      const { handle, events } = mount({ orientation, growDirection });
      fireEvent.keyDown(handle, { key: grow });
      fireEvent.keyDown(handle, { key: grow, shiftKey: true });
      expect(events).toEqual([
        "preview:310",
        "commit:310:resize",
        "preview:360",
        "commit:360:resize",
      ]);
    });
  },
);

test.each(["pointermove", "pointerup"])(
  "%s after a root scale change rolls back and releases without publication",
  (terminal) => {
    withRootScale("1", () => {
      const { handle, events } = mount();
      const { release } = capture(handle);
      pointer(handle, "pointerdown");
      pointer(handle, "pointermove", { clientX: 140 });
      document.documentElement.style.setProperty("--font-scale", "1.5");
      pointer(handle, terminal, { clientX: 160 });
      expect(events).toEqual(["preview:340", "preview:300"]);
      expect(handle.getAttribute("aria-valuenow")).toBe("300");
      expect(release).toHaveBeenCalledTimes(1);
      pointer(handle, "pointerup", { clientX: 180 });
      fireEvent.keyDown(window, { key: "Escape" });
      expect(events).toHaveLength(2);
    });
  },
);

test.each(["", "0", "-2", "Infinity", "invalid"])(
  "root scale %j falls back to unscaled pointer coordinates",
  (scale) => {
    withRootScale(scale, () => {
      const { handle, events } = mount();
      capture(handle);
      pointer(handle, "pointerdown");
      pointer(handle, "pointerup", { clientX: 136 });
      expect(events).toEqual(["preview:336", "commit:336:resize"]);
      expect(handle.getAttribute("aria-valuenow")).toBe("336");
    });
  },
);
