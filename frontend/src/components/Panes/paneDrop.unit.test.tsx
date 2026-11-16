import { cleanup, fireEvent, render } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, expect, test, vi } from "vitest";
import type { Zone } from "../../state/panes";
import {
  CHAT_DRAG_TYPE,
  type DropPayload,
  dropZone,
  FILE_DRAG_TYPE,
  PaneDropOverlay,
  parseDrop,
  usePaneDrop,
} from "./paneDrop";

afterEach(cleanup);

const RECT = { left: 0, top: 0, width: 100, height: 100 };
const BOTH = { row: true, column: true };

test.each([
  [10, 50, BOTH, "left"],
  [90, 50, BOTH, "right"],
  [50, 10, BOTH, "top"],
  [50, 90, BOTH, "bottom"],
  [50, 50, BOTH, "center"],
  [24, 50, BOTH, "left"],
  [25, 50, BOTH, "center"],
  [5, 15, BOTH, "left"],
  [15, 5, BOTH, "top"],
  [10, 50, { row: false, column: true }, "center"],
  [50, 10, { row: true, column: false }, "center"],
] as const)("dropZone at (%i, %i) with %j gives %s", (x, y, edges, expected) => {
  expect(dropZone(RECT, x, y, edges)).toBe(expected);
});

test.each([
  { ...RECT, width: 0 },
  { ...RECT, height: 0 },
])("a 0-size rect gives center regardless of position", (rect) => {
  expect(dropZone(rect, 10, 10, BOTH)).toBe("center");
});

const UUID_A = "11111111-1111-1111-1111-111111111111";

function fakeTransfer(type: string | null, data = ""): DataTransfer {
  return { types: type ? [type] : [], getData: () => data } as unknown as DataTransfer;
}

// jsdom has no working DragEvent (clientX/clientY and dataTransfer do not survive
// fireEvent.dragOver's init object), so drag/leave/drop are fired as plain MouseEvents, same as
// this codebase's other pointer-position tests (ResizeHandle.unit.test.tsx), with `dataTransfer`
// attached by hand.
function drag(
  type: string,
  init: {
    dataTransfer?: DataTransfer;
    clientX?: number;
    clientY?: number;
    relatedTarget?: EventTarget | null;
  } = {},
): Event {
  const event = new MouseEvent(type, {
    bubbles: true,
    cancelable: true,
    clientX: init.clientX ?? 0,
    clientY: init.clientY ?? 0,
    relatedTarget: init.relatedTarget ?? null,
  });
  if (init.dataTransfer)
    Object.defineProperty(event, "dataTransfer", { value: init.dataTransfer, configurable: true });
  return event;
}

function Harness({
  edges = () => BOTH,
  onDrop,
}: {
  edges?: (rect: DOMRect, kind: DropPayload["kind"]) => { row: boolean; column: boolean };
  onDrop: (payload: DropPayload, zone: Zone) => void;
}): ReactElement {
  const { zone, onDragOver, onDragLeave, onDrop: handleDrop } = usePaneDrop({ edges, onDrop });
  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: fixture for the real pane section, which carries the same handlers
    <div data-testid="pane" onDragOver={onDragOver} onDragLeave={onDragLeave} onDrop={handleDrop}>
      <PaneDropOverlay zone={zone} />
      <div className="session-tabs" data-testid="strip" />
    </div>
  );
}

function mount(
  onDrop: (payload: DropPayload, zone: Zone) => void,
  edges?: (rect: DOMRect, kind: DropPayload["kind"]) => { row: boolean; column: boolean },
) {
  const view = render(
    edges ? <Harness onDrop={onDrop} edges={edges} /> : <Harness onDrop={onDrop} />,
  );
  const pane = view.getByTestId("pane");
  pane.getBoundingClientRect = () => ({ ...RECT }) as DOMRect;
  return {
    strip: view.getByTestId("strip"),
    pane,
    overlay: () => view.container.querySelector(".pane-drop"),
  };
}

test("a foreign drag type is ignored: no zone, browser default not prevented", () => {
  const { pane, overlay } = mount(vi.fn());
  const notPrevented = fireEvent(
    pane,
    drag("dragover", { dataTransfer: fakeTransfer("text/plain"), clientX: 10, clientY: 50 }),
  );
  expect(notPrevented).toBe(true);
  expect(overlay()).toBeNull();
});

test("dragging over a nested tab strip clears the zone and leaves it to the strip", () => {
  const { pane, strip, overlay } = mount(vi.fn());
  fireEvent(
    pane,
    drag("dragover", {
      dataTransfer: fakeTransfer(CHAT_DRAG_TYPE, UUID_A),
      clientX: 90,
      clientY: 50,
    }),
  );
  expect(overlay()?.getAttribute("data-zone")).toBe("right");
  const notPrevented = fireEvent(
    strip,
    drag("dragover", {
      dataTransfer: fakeTransfer(CHAT_DRAG_TYPE, UUID_A),
      clientX: 90,
      clientY: 50,
    }),
  );
  expect(notPrevented).toBe(true);
  expect(overlay()).toBeNull();
});

test("edges() decides which axes are live, using the element's own rect", () => {
  const { pane, overlay } = mount(vi.fn(), () => ({ row: false, column: true }));
  // Row disabled: x=90 (which would be "right" with row live) is ignored; y=10 is a top hit.
  fireEvent(
    pane,
    drag("dragover", {
      dataTransfer: fakeTransfer(CHAT_DRAG_TYPE, UUID_A),
      clientX: 90,
      clientY: 10,
    }),
  );
  expect(overlay()?.getAttribute("data-zone")).toBe("top");
});

test("a drop with an id that is not a canonical UUID calls nothing, and clears the zone", () => {
  const onDrop = vi.fn();
  const { pane, overlay } = mount(onDrop);
  fireEvent(
    pane,
    drag("dragover", {
      dataTransfer: fakeTransfer(CHAT_DRAG_TYPE, "NOT-A-UUID"),
      clientX: 90,
      clientY: 50,
    }),
  );
  fireEvent(pane, drag("drop", { dataTransfer: fakeTransfer(CHAT_DRAG_TYPE, "NOT-A-UUID") }));
  expect(onDrop).not.toHaveBeenCalled();
  expect(overlay()).toBeNull();
});

test("a drop with a canonical UUID calls onDrop with the hovered zone, once", () => {
  const onDrop = vi.fn();
  const { pane, overlay } = mount(onDrop);
  fireEvent(
    pane,
    drag("dragover", {
      dataTransfer: fakeTransfer(CHAT_DRAG_TYPE, UUID_A),
      clientX: 90,
      clientY: 50,
    }),
  );
  fireEvent(pane, drag("drop", { dataTransfer: fakeTransfer(CHAT_DRAG_TYPE, UUID_A) }));
  expect(onDrop).toHaveBeenCalledTimes(1);
  expect(onDrop).toHaveBeenCalledWith({ kind: "chat", id: UUID_A }, "right");
  expect(overlay()).toBeNull();
});

test("leaving the pane clears the zone; leaving to a child inside it does not", () => {
  const { pane, strip, overlay } = mount(vi.fn());
  fireEvent(
    pane,
    drag("dragover", {
      dataTransfer: fakeTransfer(CHAT_DRAG_TYPE, UUID_A),
      clientX: 90,
      clientY: 50,
    }),
  );
  expect(overlay()).not.toBeNull();
  fireEvent(pane, drag("dragleave", { relatedTarget: strip }));
  expect(overlay()).not.toBeNull();
  fireEvent(pane, drag("dragleave", { relatedTarget: document.body }));
  expect(overlay()).toBeNull();
});

test("a window dragend clears the zone even without a drop", () => {
  const { pane, overlay } = mount(vi.fn());
  fireEvent(
    pane,
    drag("dragover", {
      dataTransfer: fakeTransfer(CHAT_DRAG_TYPE, UUID_A),
      clientX: 90,
      clientY: 50,
    }),
  );
  expect(overlay()).not.toBeNull();
  fireEvent(window, new Event("dragend"));
  expect(overlay()).toBeNull();
});

// -- parseDrop --------------------------------------------------------------------------------

function fileTransfer(data: string): DataTransfer {
  return fakeTransfer(FILE_DRAG_TYPE, data);
}

test("parseDrop reads a valid chat payload", () => {
  expect(parseDrop(fakeTransfer(CHAT_DRAG_TYPE, UUID_A))).toEqual({ kind: "chat", id: UUID_A });
});

test("parseDrop rejects a chat payload that is not a canonical UUID", () => {
  expect(parseDrop(fakeTransfer(CHAT_DRAG_TYPE, "NOT-A-UUID"))).toBeNull();
});

test("parseDrop reads a file tab's id dragged as a tab, f1 to f64", () => {
  expect(parseDrop(fakeTransfer(CHAT_DRAG_TYPE, "f1"))).toEqual({ kind: "fileTab", id: "f1" });
  expect(parseDrop(fakeTransfer(CHAT_DRAG_TYPE, "f64"))).toEqual({ kind: "fileTab", id: "f64" });
});

test.each(["constructor", "__proto__", "f0", "f65", "f1 ", " f1", "F1", "f01", "f", ""])(
  "parseDrop refuses the tab id %j, neither a canonical UUID nor exactly a file tab id",
  (id) => {
    expect(parseDrop(fakeTransfer(CHAT_DRAG_TYPE, id))).toBeNull();
  },
);

test("parseDrop reads a valid file payload", () => {
  const data = JSON.stringify({ workspace_id: "ws1", ref: "src/a.ts" });
  expect(parseDrop(fileTransfer(data))).toEqual({
    kind: "file",
    workspaceId: "ws1",
    ref: "src/a.ts",
  });
});

test.each([
  ["malformed JSON", "{not json"],
  ["a JSON array", "[]"],
  ["a missing key", JSON.stringify({ workspace_id: "ws1" })],
  ["an extra key", JSON.stringify({ workspace_id: "ws1", ref: "a.ts", extra: 1 })],
  ["an empty workspace id", JSON.stringify({ workspace_id: "", ref: "a.ts" })],
  ["an over-length workspace id", JSON.stringify({ workspace_id: "w".repeat(65), ref: "a.ts" })],
  ["a ref starting with /", JSON.stringify({ workspace_id: "ws1", ref: "/a.ts" })],
  ["a ref starting with \\", JSON.stringify({ workspace_id: "ws1", ref: "\\a.ts" })],
  ["a non-string ref", JSON.stringify({ workspace_id: "ws1", ref: 1 })],
] as const)("parseDrop fails closed on %s", (_label, data) => {
  expect(parseDrop(fileTransfer(data))).toBeNull();
});

test("parseDrop returns null when neither drag type is present", () => {
  expect(parseDrop(fakeTransfer("text/plain", "x"))).toBeNull();
});

// -- per-kind floors ----------------------------------------------------------------------------

test("edges() is called with the dragged kind: chat for CHAT_DRAG_TYPE, file for FILE_DRAG_TYPE", () => {
  const edges = vi.fn(() => BOTH);
  const { pane } = mount(vi.fn(), edges);
  fireEvent(
    pane,
    drag("dragover", {
      dataTransfer: fakeTransfer(CHAT_DRAG_TYPE, UUID_A),
      clientX: 50,
      clientY: 50,
    }),
  );
  expect(edges).toHaveBeenLastCalledWith(RECT, "chat");
  fireEvent(
    pane,
    drag("dragover", {
      dataTransfer: fileTransfer(JSON.stringify({ workspace_id: "ws1", ref: "a.ts" })),
      clientX: 50,
      clientY: 50,
    }),
  );
  expect(edges).toHaveBeenLastCalledWith(RECT, "file");
});

test("a file drop calls onDrop with the file payload and the hovered zone, once", () => {
  const onDrop = vi.fn();
  const { pane, overlay } = mount(onDrop);
  const data = JSON.stringify({ workspace_id: "ws1", ref: "src/a.ts" });
  fireEvent(pane, drag("dragover", { dataTransfer: fileTransfer(data), clientX: 90, clientY: 50 }));
  fireEvent(pane, drag("drop", { dataTransfer: fileTransfer(data) }));
  expect(onDrop).toHaveBeenCalledTimes(1);
  expect(onDrop).toHaveBeenCalledWith(
    { kind: "file", workspaceId: "ws1", ref: "src/a.ts" },
    "right",
  );
  expect(overlay()).toBeNull();
});

test("a file tab's drop calls onDrop with its tab payload and the hovered zone, once", () => {
  const onDrop = vi.fn();
  const { pane } = mount(onDrop);
  const transfer = fakeTransfer(CHAT_DRAG_TYPE, "f3");
  fireEvent(pane, drag("dragover", { dataTransfer: transfer, clientX: 90, clientY: 50 }));
  fireEvent(pane, drag("drop", { dataTransfer: transfer }));
  expect(onDrop).toHaveBeenCalledTimes(1);
  expect(onDrop).toHaveBeenCalledWith({ kind: "fileTab", id: "f3" }, "right");
});

test("a file drop with an invalid payload calls nothing, and clears the zone", () => {
  const onDrop = vi.fn();
  const { pane, overlay } = mount(onDrop);
  const bad = JSON.stringify({ workspace_id: "", ref: "a.ts" });
  fireEvent(pane, drag("dragover", { dataTransfer: fileTransfer(bad), clientX: 90, clientY: 50 }));
  fireEvent(pane, drag("drop", { dataTransfer: fileTransfer(bad) }));
  expect(onDrop).not.toHaveBeenCalled();
  expect(overlay()).toBeNull();
});
