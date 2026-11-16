import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useEffect, useState } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { PanelHost, type PanelHostProps } from "./PanelHost";

const originalPoint = Object.getOwnPropertyDescriptor(document, "elementFromPoint");
afterEach(() => {
  cleanup();
  document.body.replaceChildren();
  if (originalPoint) Object.defineProperty(document, "elementFromPoint", originalPoint);
  else Reflect.deleteProperty(document, "elementFromPoint");
});

function fixture(
  panels: PanelHostProps["panels"] = {
    sessions: <p>Duplicate title</p>,
    agents: <p>Duplicate title</p>,
    approvals: <p>Approval detail</p>,
    routines: <p>Routine detail</p>,
  },
) {
  const left = document.createElement("div");
  const right = document.createElement("div");
  document.body.append(left, right);
  const hosts = { left, right };
  const view = render(<PanelHost panels={panels} hosts={hosts} />);
  const order = (node: HTMLElement) =>
    [...node.children].map((item) => (item as HTMLElement).dataset.panelId);
  return {
    ...view,
    hosts,
    order,
    update: (next: PanelHostProps) => view.rerender(<PanelHost {...next} />),
  };
}

test("the four current detail panels keep canonical hosts, order and distinct controls", () => {
  const { hosts, order } = fixture();
  expect(order(hosts.left)).toEqual(["sessions", "agents"]);
  expect(order(hosts.right)).toEqual(["approvals", "routines"]);
  expect(screen.queryByRole("heading", { name: "Skills" })).toBeNull();
  expect(screen.getAllByText("Duplicate title")).toHaveLength(2);
  expect(
    (
      screen.getByRole("button", {
        name: "Move Sessions left",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
  expect(
    (
      screen.getByRole("button", {
        name: "Move Routines down",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
});

test("keyboard controls move, reorder and reset without conflating duplicate content labels", () => {
  const { hosts, order } = fixture();
  fireEvent.click(screen.getByRole("button", { name: "Move Agents right" }));
  expect(order(hosts.right)).toEqual(["approvals", "routines", "agents"]);
  fireEvent.click(screen.getByRole("button", { name: "Move Agents up" }));
  expect(order(hosts.right)).toEqual(["approvals", "agents", "routines"]);
  fireEvent.click(screen.getByRole("button", { name: "Move Approvals down" }));
  expect(order(hosts.right)).toEqual(["agents", "approvals", "routines"]);
  fireEvent.click(screen.getByRole("button", { name: "Reset all panels from Agents" }));
  expect(order(hosts.left)).toEqual(["sessions", "agents"]);
  expect(order(hosts.right)).toEqual(["approvals", "routines"]);
});

function retainedRead() {
  let resolve: (value: string) => void = () => {};
  const pending = new Promise<string>((done) => {
    resolve = done;
  });
  const read = vi.fn(() => pending);
  const mounts = vi.fn();
  const exits = vi.fn();
  function Widget() {
    const [text, setText] = useState("Original draft text");
    const [selected, setSelected] = useState(false);
    const [status, setStatus] = useState("Pending read");
    useEffect(() => {
      let live = true;
      mounts();
      void read().then((value) => {
        if (live) setStatus(value);
      });
      return () => {
        live = false;
        exits();
      };
    }, []);
    return (
      <>
        <textarea
          aria-label="Dirty detail"
          value={text}
          onChange={(event) => setText(event.target.value)}
        />
        <input
          aria-label="Selected detail"
          type="checkbox"
          checked={selected}
          onChange={(event) => setSelected(event.target.checked)}
        />
        <details>
          <summary>Expanded detail</summary>
          <p>{status}</p>
        </details>
      </>
    );
  }
  return { Widget, read, mounts, exits, resolve };
}

test("repeated cross-host moves preserve the mounted dirty selected expanded widget and its pending read", async () => {
  const { Widget, read, mounts, exits, resolve } = retainedRead();
  const { hosts, order } = fixture({
    sessions: <Widget />,
    agents: <p>Agents detail</p>,
    skills: <p>Real supplied Skills detail</p>,
  });
  const overview = document.createElement("output");
  overview.textContent = "Knowledge 7 · Skills 2 partial · Approvals 3";
  document.body.prepend(overview);
  const input = screen.getByRole("textbox", { name: "Dirty detail" }) as HTMLTextAreaElement;
  fireEvent.change(input, { target: { value: "Dirty edited draft" } });
  input.focus();
  input.setSelectionRange(2, 8);
  fireEvent.click(screen.getByRole("checkbox", { name: "Selected detail" }));
  const details = screen.getByText("Expanded detail").parentElement as HTMLDetailsElement;
  details.open = true;
  fireEvent.click(screen.getByRole("button", { name: "Move Sessions right" }));
  fireEvent.click(screen.getByRole("button", { name: "Move Sessions up" }));
  fireEvent.click(screen.getByRole("button", { name: "Move Sessions left" }));
  expect(screen.getByRole("textbox", { name: "Dirty detail" })).toBe(input);
  expect(input.value).toBe("Dirty edited draft");
  expect([input.selectionStart, input.selectionEnd]).toEqual([2, 8]);
  expect(document.activeElement).toBe(input);
  expect(
    (screen.getByRole("checkbox", { name: "Selected detail" }) as HTMLInputElement).checked,
  ).toBe(true);
  expect(details.open).toBe(true);
  expect(order(hosts.right)).toEqual(["skills"]);
  expect(overview.textContent).toBe("Knowledge 7 · Skills 2 partial · Approvals 3");
  expect([read.mock.calls.length, mounts.mock.calls.length, exits.mock.calls.length]).toEqual([
    1, 1, 0,
  ]);
  await act(async () => {
    resolve("Resolved exact read");
    await Promise.resolve();
  });
  expect(screen.getByText("Resolved exact read")).toBeTruthy();
  expect(read).toHaveBeenCalledTimes(1);
});

test("an invoked side control becoming disabled leaves focus in the same retained panel", () => {
  const { hosts } = fixture();
  const control = screen.getByRole("button", { name: "Move Sessions right" });
  control.focus();
  fireEvent.click(control);
  expect((control as HTMLButtonElement).disabled).toBe(true);
  expect(hosts.right.contains(document.activeElement)).toBe(true);
  expect(document.activeElement?.closest('[data-panel-id="sessions"]')).toBeTruthy();
});

test("drawer host removal and rebinding retain one read; actual panel removal and unmount clean owned wrappers", () => {
  const { Widget, read, exits } = retainedRead();
  const panels = { sessions: <Widget /> };
  const { hosts, update, unmount } = fixture(panels);
  const input = screen.getByRole("textbox", { name: "Dirty detail" });
  update({ panels, hosts: { left: null, right: hosts.right } });
  expect(hosts.left.children).toHaveLength(0);
  const rebound = document.createElement("div");
  document.body.append(rebound);
  update({ panels, hosts: { left: rebound, right: hosts.right } });
  expect(screen.getByRole("textbox", { name: "Dirty detail" })).toBe(input);
  expect(read).toHaveBeenCalledTimes(1);
  update({
    panels: { agents: <p>Available Agents</p> },
    hosts: { left: rebound, right: hosts.right },
  });
  expect(screen.queryByRole("textbox", { name: "Dirty detail" })).toBeNull();
  expect(exits).toHaveBeenCalledTimes(1);
  unmount();
  expect(rebound.children).toHaveLength(0);
  expect(hosts.right.children).toHaveLength(0);
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
  const hit = vi.fn<() => Element | null>(() => null);
  Object.defineProperty(document, "elementFromPoint", { configurable: true, value: hit });
  return { set, release, hit };
}

test("captured header drag and keyboard placement have the same final order", () => {
  const { hosts, order } = fixture();
  const grip = screen.getByRole("button", { name: "Drag Agents" });
  const { set, release, hit } = capture(grip);
  pointer(grip, "pointerdown");
  hit.mockReturnValue(screen.getByText("Place Agents before Routines"));
  pointer(grip, "pointermove");
  expect(order(hosts.left).filter(Boolean)).toEqual(["sessions", "agents"]);
  expect(screen.getByText("Place Agents before Routines").className).toContain("is-target");
  pointer(grip, "pointerup");
  expect(order(hosts.right)).toEqual(["approvals", "agents", "routines"]);
  expect(set).toHaveBeenCalledTimes(1);
  expect(release).toHaveBeenCalledTimes(1);
  expect(document.activeElement).toBe(grip);
  fireEvent.click(screen.getByRole("button", { name: "Reset all panels from Agents" }));
  fireEvent.click(screen.getByRole("button", { name: "Move Agents right" }));
  fireEvent.click(screen.getByRole("button", { name: "Move Agents up" }));
  expect(order(hosts.right)).toEqual(["approvals", "agents", "routines"]);
});

test.each(["Escape", "pointercancel", "lostpointercapture", "blur", "invalid drop"])(
  "%s cancels capture without moving, remounting or changing the dirty detail",
  (reason) => {
    const { Widget, read, mounts } = retainedRead();
    const { hosts, order } = fixture({ sessions: <Widget />, agents: <p>Agent detail</p> });
    const input = screen.getByRole("textbox", { name: "Dirty detail" }) as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "Unsaved edit" } });
    const grip = screen.getByRole("button", { name: "Drag Sessions" });
    const { release, hit } = capture(grip);
    pointer(grip, "pointerdown");
    hit.mockReturnValue(screen.getByText("Place Sessions at end of right rail"));
    pointer(grip, "pointermove");
    if (reason === "Escape") fireEvent.keyDown(grip, { key: "Escape" });
    else if (reason === "blur") fireEvent(window, new Event("blur"));
    else if (reason === "invalid drop") {
      hit.mockReturnValue(null);
      pointer(grip, "pointerup");
    } else pointer(grip, reason);
    pointer(grip, "pointerup");
    expect(order(hosts.left)).toEqual(["sessions", "agents"]);
    expect(order(hosts.right)).toEqual([]);
    expect(screen.queryByText("Place Sessions at end of right rail")).toBeNull();
    expect(document.activeElement).toBe(grip);
    expect(screen.getByRole("textbox", { name: "Dirty detail" })).toBe(input);
    expect(input.value).toBe("Unsaved edit");
    expect([read.mock.calls.length, mounts.mock.calls.length]).toEqual([1, 1]);
    expect(release).toHaveBeenCalledTimes(1);
  },
);

test("modified and composing Escape, foreign pointers and non-primary starts do not admit movement", () => {
  const { hosts, order } = fixture();
  const grip = screen.getByRole("button", { name: "Drag Agents" });
  const { set, release, hit } = capture(grip);
  pointer(grip, "pointerdown", { isPrimary: false });
  pointer(grip, "pointerdown", { button: 1 });
  expect(set).toHaveBeenCalledTimes(0);
  pointer(grip, "pointerdown");
  for (const modifiers of [
    { ctrlKey: true },
    { altKey: true },
    { metaKey: true },
    { shiftKey: true },
    { isComposing: true },
    { keyCode: 229 },
  ]) {
    expect(fireEvent.keyDown(grip, { key: "Escape", ...modifiers })).toBe(true);
    expect(release).toHaveBeenCalledTimes(0);
  }
  hit.mockReturnValue(screen.getByText("Place Agents before Routines"));
  pointer(grip, "pointerup", { pointerId: 2 });
  pointer(grip, "pointerup", { isPrimary: false });
  expect(order(hosts.left).filter(Boolean)).toEqual(["sessions", "agents"]);
  pointer(grip, "pointerup");
  expect(order(hosts.right)).toEqual(["approvals", "agents", "routines"]);
  expect(release).toHaveBeenCalledTimes(1);
});

test("Reset cancels a held drag and a late release cannot reinstall its target", () => {
  const { hosts, order } = fixture();
  fireEvent.click(screen.getByRole("button", { name: "Move Agents right" }));
  const grip = screen.getByRole("button", { name: "Drag Agents" });
  const { release, hit } = capture(grip);
  pointer(grip, "pointerdown");
  hit.mockReturnValue(screen.getByText("Place Agents before Routines"));
  pointer(grip, "pointermove");
  fireEvent.click(screen.getByRole("button", { name: "Reset all panels from Agents" }));
  pointer(grip, "pointerup");
  expect(order(hosts.left)).toEqual(["sessions", "agents"]);
  expect(order(hosts.right)).toEqual(["approvals", "routines"]);
  expect(release).toHaveBeenCalledTimes(1);
  pointer(grip, "pointerdown");
  hit.mockReturnValue(screen.getByText("Place Agents at end of left rail"));
  pointer(grip, "pointerup");
  expect(order(hosts.left)).toEqual(["sessions", "agents"]);
  expect(release).toHaveBeenCalledTimes(2);
});

test("removing the targeted panel or rebinding a host cancels the captured operation", () => {
  const panels = { sessions: <p>Sessions</p>, agents: <p>Agents</p>, routines: <p>Routines</p> };
  const { hosts, order, update } = fixture(panels);
  const grip = screen.getByRole("button", { name: "Drag Agents" });
  const { release, hit } = capture(grip);
  pointer(grip, "pointerdown");
  hit.mockReturnValue(screen.getByText("Place Agents before Routines"));
  pointer(grip, "pointermove");
  update({ panels: { sessions: panels.sessions, agents: panels.agents }, hosts });
  pointer(grip, "pointerup");
  expect(release).toHaveBeenCalledTimes(1);
  expect(order(hosts.left)).toEqual(["sessions", "agents"]);
  expect(order(hosts.right)).toEqual([]);
  pointer(grip, "pointerdown");
  const rebound = document.createElement("div");
  document.body.append(rebound);
  update({ panels, hosts: { left: hosts.left, right: rebound } });
  pointer(grip, "pointerup");
  expect(release).toHaveBeenCalledTimes(2);
  expect(order(hosts.left)).toEqual(["sessions", "agents"]);
  expect(order(rebound)).toEqual(["routines"]);
});

test("removing the dragged panel and unmounting release capture and remove only owned wrappers", () => {
  const { hosts, update, unmount } = fixture();
  const grip = screen.getByRole("button", { name: "Drag Agents" });
  const { release } = capture(grip);
  pointer(grip, "pointerdown");
  update({ panels: { sessions: <p>Current Sessions</p> }, hosts });
  pointer(grip, "pointerup");
  expect(release).toHaveBeenCalledTimes(1);
  const next = screen.getByRole("button", { name: "Drag Sessions" });
  const second = capture(next);
  pointer(next, "pointerdown");
  unmount();
  expect(second.release).toHaveBeenCalledTimes(1);
  expect(hosts.left.children).toHaveLength(0);
  expect(hosts.right.children).toHaveLength(0);
});

test("a completed drag preserves dirty selection, expansion and the original pending read", async () => {
  const { Widget, read, mounts, exits, resolve } = retainedRead();
  const { hosts } = fixture({ sessions: <Widget />, routines: <p>Routines detail</p> });
  const input = screen.getByRole("textbox", { name: "Dirty detail" }) as HTMLTextAreaElement;
  fireEvent.change(input, { target: { value: "Drag retains text" } });
  input.setSelectionRange(3, 9);
  fireEvent.click(screen.getByRole("checkbox", { name: "Selected detail" }));
  const details = screen.getByText("Expanded detail").parentElement as HTMLDetailsElement;
  details.open = true;
  const grip = screen.getByRole("button", { name: "Drag Sessions" });
  const { hit } = capture(grip);
  pointer(grip, "pointerdown");
  hit.mockReturnValue(screen.getByText("Place Sessions before Routines"));
  pointer(grip, "pointerup");
  expect(hosts.right.contains(input)).toBe(true);
  expect(screen.getByRole("textbox", { name: "Dirty detail" })).toBe(input);
  expect(input.value).toBe("Drag retains text");
  expect([input.selectionStart, input.selectionEnd]).toEqual([3, 9]);
  expect(
    (screen.getByRole("checkbox", { name: "Selected detail" }) as HTMLInputElement).checked,
  ).toBe(true);
  expect(details.open).toBe(true);
  expect([read.mock.calls.length, mounts.mock.calls.length, exits.mock.calls.length]).toEqual([
    1, 1, 0,
  ]);
  await act(async () => {
    resolve("Original retained response");
    await Promise.resolve();
  });
  expect(screen.getByText("Original retained response")).toBeTruthy();
  expect(read).toHaveBeenCalledTimes(1);
});

test("supplied Skills has its own panel heading and region while drag and global Reset controls stay explicit", () => {
  fixture({ sessions: <p>Duplicate title</p>, skills: <p>Duplicate title</p> });
  const heading = screen.getByRole("heading", { name: "Skills" });
  const region = screen.getByRole("region", { name: "Skills" });
  expect(region.contains(heading)).toBe(true);
  expect(region.contains(screen.getByRole("button", { name: "Drag Skills" }))).toBe(true);
  for (const label of ["Sessions", "Skills"]) {
    expect(screen.getByRole("button", { name: `Reset all panels from ${label}` }).textContent).toBe(
      "Reset all panels",
    );
  }
  expect(screen.getAllByText("Duplicate title")).toHaveLength(2);
});
