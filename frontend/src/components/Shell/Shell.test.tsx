import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { flushSync } from "react-dom";
import { afterEach, expect, test, vi } from "vitest";
import {
  FILE_PANE_MIN,
  PANE_MIN,
  type PaneGroup,
  type PaneNode,
  type PaneSplit,
} from "../../state/panes";
import { readCss } from "../../testing/css";
import { Composer } from "../Composer/Composer";
import { CHAT_DRAG_TYPE, FILE_DRAG_TYPE } from "../Panes/paneDrop";
import { Shell, type ShellPanes, type ShellProps } from "./Shell";

afterEach(cleanup);

// jsdom applies no CSS from stylesheets (only inline styles), so the box-sizing metrics
// this component is graded on (see e2e/layout.accept.test.ts, frozen) are checked here
// against the source instead of a computed style.
const css = readCss(import.meta.url, "Shell.css");

function rule(selector: string): string {
  const escaped = selector.replace(/[.:]/g, "\\$&");
  const match = css.match(new RegExp(`${escaped}\\s*\\{[^}]*\\}`));
  if (!match) throw new Error(`no ${selector} rule in Shell.css`);
  return match[0];
}

test("the conversation header's declared height folds in the hairline (border-box)", () => {
  // 52px + the 1px hairline, so the region's real bounding box is 53px.
  expect(rule(".conversation-header")).toMatch(/height:\s*53px/);
});

test("the conversation header's border is its own --hair-header token, not the shared --hair", () => {
  expect(rule(".conversation-header")).toMatch(/border-bottom:\s*1px solid var\(--hair-header\)/);
});

function renderShell(props: Partial<ShellProps> = {}) {
  return render(
    <Shell
      topBar={<div data-region="topbar">top</div>}
      leftRail={<nav data-region="left-rail">sessions</nav>}
      header={<span>header</span>}
      messages={<p>messages</p>}
      composer={
        <div data-region="composer">
          <textarea aria-label="Message" />
        </div>
      }
      rightRail={<aside data-region="right-rail">VIKI</aside>}
      statusBar={<footer data-region="statusbar">status</footer>}
      {...props}
    />,
  );
}

function group(id: string): PaneGroup {
  return { kind: "group", id, tabs: [id], active: id };
}

function fileGroup(id: string): PaneGroup {
  return { kind: "group", id, tabs: ["f1"], active: "f1" };
}

function rowSplit(id: string, children: PaneNode[]): PaneSplit {
  return {
    kind: "split",
    id,
    dir: "row",
    children,
    sizes: children.map(() => 1 / children.length),
  };
}

function stubPanes(
  overrides: Partial<ShellPanes> & Pick<ShellPanes, "root" | "focused">,
): ShellPanes {
  return {
    canSplit: true,
    pane: (groupId) => ({ header: null, messages: null, label: groupId, onActivate: () => {} }),
    onDrop: () => {},
    onResize: () => {},
    ...overrides,
  };
}

const PANE_UUID = "11111111-1111-1111-1111-111111111111";

function fakeTransfer(type: string, data: string): DataTransfer {
  return { types: [type], getData: () => data } as unknown as DataTransfer;
}

// jsdom's DragEvent does not carry clientX/clientY or dataTransfer through fireEvent, so a plain
// MouseEvent stands in, same as paneDrop.unit.test.tsx and this file's other pointer-position tests.
function drag(type: string, clientX: number, clientY: number, dataTransfer: DataTransfer): Event {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX, clientY });
  Object.defineProperty(event, "dataTransfer", { value: dataTransfer, configurable: true });
  return event;
}

test("with no layout prop both rails are docked, which is what every jsdom test sees", () => {
  const { container } = renderShell();
  expect(container.querySelector('[data-region="left-rail"]')).not.toBeNull();
  expect(container.querySelector('[data-region="right-rail"]')).not.toBeNull();
  expect(screen.queryAllByRole("dialog")).toEqual([]);
});

test("header and banners directly precede the actual message scroller", () => {
  const headerAction = vi.fn();
  const noticeAction = vi.fn();
  const { container } = renderShell({
    header: (
      <button type="button" onClick={headerAction}>
        Header action
      </button>
    ),
    banner: (
      <button type="button" onClick={noticeAction}>
        Notice action
      </button>
    ),
  });
  const scroll = container.querySelector(".conversation-scroll");
  const conversation = container.querySelector(".conversation");
  const header = screen.getByRole("button", { name: "Header action" });
  const notice = screen.getByRole("button", { name: "Notice action" });
  expect(Array.from(conversation?.children ?? []).slice(0, 3)).toEqual([
    header.parentElement,
    notice,
    scroll,
  ]);
  expect(scroll?.contains(header)).toBe(false);
  expect(scroll?.contains(notice)).toBe(false);
  expect(screen.getByText("messages").closest(".conversation-messages")).not.toBeNull();
  expect(scroll?.contains(screen.getByRole("textbox", { name: "Message" }))).toBe(false);
  fireEvent.click(header);
  fireEvent.click(notice);
  expect(headerAction).toHaveBeenCalledOnce();
  expect(noticeAction).toHaveBeenCalledOnce();
});

test("the conversation overlay has a final host outside the transcript and composer", () => {
  const { container } = renderShell({ overlay: <button type="button">Overlay action</button> });
  const conversation = container.querySelector(".conversation");
  const overlay = screen.getByRole("button", { name: "Overlay action" });
  expect(conversation?.lastElementChild?.className).toBe("conversation-overlay");
  expect(conversation?.lastElementChild?.contains(overlay)).toBe(true);
  expect(overlay.closest(".conversation-scroll, .composer-slot")).toBeNull();
});

test("a rail the window cannot dock is out of the DOM until its drawer is opened", () => {
  const layout = { leftDocked: true, rightDocked: false };
  const { container, rerender } = renderShell({
    layout,
    rails: { drawer: null, onClose: vi.fn() },
  });
  expect(container.querySelector('[data-region="left-rail"]')).not.toBeNull();
  expect(container.querySelector('[data-region="right-rail"]')).toBeNull();
  expect(screen.queryAllByRole("dialog")).toEqual([]);

  rerender(
    <Shell
      topBar={<div data-region="topbar">top</div>}
      leftRail={<nav data-region="left-rail">sessions</nav>}
      header={<span>header</span>}
      messages={<p>messages</p>}
      composer={
        <div data-region="composer">
          <textarea aria-label="Message" />
        </div>
      }
      rightRail={<aside data-region="right-rail">VIKI</aside>}
      statusBar={<footer data-region="statusbar">status</footer>}
      layout={layout}
      rails={{ drawer: "right", onClose: vi.fn() }}
    />,
  );
  const dialog = screen.getByRole("dialog", { name: "VIKI panel" });
  expect(dialog.querySelector('[data-region="right-rail"]')?.textContent).toBe("VIKI");
});

test("the open drawer is the last thing in the body, so the rest of the shell is inert", () => {
  const { container } = renderShell({
    layout: { leftDocked: false, rightDocked: false },
    rails: { drawer: "left", onClose: vi.fn() },
  });
  const body = container.querySelector(".shell-body") as HTMLElement;
  const dialog = screen.getByRole("dialog", { name: "Sessions and agents" });
  expect(body.lastElementChild?.contains(dialog)).toBe(true);
  // useModalFocus inerts every sibling on the way up: the conversation here, and the top bar and
  // status bar one level further out.
  expect(
    container.querySelector('[data-region="conversation"]')?.closest("[inert]"),
  ).not.toBeNull();
  expect(container.querySelector('[data-region="topbar"]')?.closest("[inert]")).not.toBeNull();
  expect(container.querySelector('[data-region="statusbar"]')?.closest("[inert]")).not.toBeNull();
  expect(dialog.closest("[inert]")).toBeNull();
});

test("each drawer holds its own rail's nodes and only one is open at a time", () => {
  renderShell({
    layout: { leftDocked: false, rightDocked: false },
    rails: { drawer: "left", onClose: vi.fn() },
  });
  const dialogs = screen.getAllByRole("dialog");
  expect(dialogs).toHaveLength(1);
  expect(dialogs[0]?.getAttribute("aria-label")).toBe("Sessions and agents");
  expect(dialogs[0]?.textContent).toContain("sessions");
});

test("a drawer that closes because its rail docked hands focus to the composer", () => {
  // The one close where "focus returns to the toggle" cannot hold: the window grew, so
  // TopBar drops the toggle in the same commit that closes the drawer. Without a fallback,
  // useModalFocus finds its opener disconnected and focus lands on document.body — a keyboard or
  // Narrator user restarts at the top of the document.
  const toggle = document.createElement("button");
  document.body.append(toggle);
  toggle.focus();

  const narrow = { leftDocked: false, rightDocked: false };
  const view = renderShell({ layout: narrow, rails: { drawer: "left", onClose: vi.fn() } });
  expect(screen.getByRole("dialog", { name: "Sessions and agents" })).toBeTruthy();

  toggle.remove(); // the rail docked, so the toggle it belonged to is gone
  view.rerender(
    <Shell
      topBar={<div data-region="topbar">top</div>}
      leftRail={<nav data-region="left-rail">sessions</nav>}
      header={<span>header</span>}
      messages={<p>messages</p>}
      composer={
        <div data-region="composer">
          <textarea aria-label="Message" />
        </div>
      }
      rightRail={<aside data-region="right-rail">VIKI</aside>}
      statusBar={<footer data-region="statusbar">status</footer>}
      layout={{ leftDocked: true, rightDocked: true }}
      rails={{ drawer: null, onClose: vi.fn() }}
    />,
  );

  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(screen.getByLabelText("Message"));
});

test("a drawer that docks restores focus to the focused pane's composer when both panes have one", () => {
  const toggle = document.createElement("button");
  document.body.append(toggle);
  toggle.focus();
  const panes = stubPanes({
    root: rowSplit("s1", [group("g1"), group("g2")]),
    focused: "g2",
    pane: (groupId) => ({
      header: null,
      messages: null,
      label: groupId,
      composer: <Composer running={false} onSend={async () => true} onStop={() => {}} />,
      onActivate: () => {},
    }),
  });
  const props = { ...railSlots, panes };
  const view = render(
    <Shell
      {...props}
      layout={{ leftDocked: false, rightDocked: true }}
      rails={{ drawer: "left", onClose: vi.fn() }}
    />,
  );
  const inputs = Array.from(view.container.querySelectorAll(".pane textarea"));
  expect(inputs).toHaveLength(2);
  const focusedInput = view.container.querySelector('[data-pane="g2"] textarea');
  expect(focusedInput).not.toBeNull();
  toggle.remove();
  view.rerender(<Shell {...props} rails={{ drawer: null, onClose: vi.fn() }} />);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(Array.from(view.container.querySelectorAll(".pane textarea"))).toEqual(inputs);
  expect(document.activeElement).toBe(focusedInput);
});

test("drawer close leaves another split chat untouched when the focused pane has no composer", () => {
  const toggle = document.createElement("button");
  document.body.append(toggle);
  toggle.focus();
  const activate = vi.fn();
  const panes = stubPanes({
    root: rowSplit("s1", [group("g1"), group("g2")]),
    focused: "g2",
    pane: (groupId) => ({
      header: null,
      messages: null,
      label: groupId,
      composer: groupId === "g2" ? null : <textarea aria-label="Message" />,
      onActivate: () => activate(groupId),
    }),
  });
  const props = { ...railSlots, panes };
  const view = render(
    <Shell
      {...props}
      layout={{ leftDocked: false, rightDocked: true }}
      rails={{ drawer: "left", onClose: vi.fn() }}
    />,
  );
  const otherInput = view.container.querySelector('[data-pane="g1"] textarea');
  expect(otherInput).not.toBeNull();
  toggle.remove();
  view.rerender(<Shell {...props} rails={{ drawer: null, onClose: vi.fn() }} />);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).not.toBe(otherInput);
  expect(activate).not.toHaveBeenCalled();
});

test("the view slot replaces the whole chat body and keeps the two bars", () => {
  const { container } = renderShell({ view: <p>settings</p> });

  expect(container.querySelector('[data-region="view"]')?.textContent).toBe("settings");
  expect(container.querySelector('[data-region="topbar"]')).not.toBeNull();
  expect(container.querySelector('[data-region="statusbar"]')).not.toBeNull();
  // Nothing of the chat is left behind it: no rails, no header, no scroll, no composer, no
  // drawers. e2e/layout.accept.test.ts measures the chat, so its DOM must not gain a sibling
  // that only exists for another view either.
  for (const region of ["left-rail", "right-rail", "conversation", "composer"])
    expect(container.querySelector(`[data-region="${region}"]`)).toBeNull();
  expect(container.querySelector(".shell-body")).toBeNull();
});

test("without the view slot the chat body is byte-for-byte what it always was", () => {
  const ownedIds = (html: string) =>
    html.replace(/(id|aria-controls)="_[^"]+-(left|right)-content"/g, '$1="$2-content"');
  const withoutProp = ownedIds(renderShell().container.innerHTML);
  cleanup();
  const withNull = ownedIds(renderShell({ view: null }).container.innerHTML);

  expect(withNull).toBe(withoutProp);
});

test("an omitted or null center keeps the original chat DOM", () => {
  const ownedIds = (html: string) =>
    html.replace(/(id|aria-controls)="_[^"]+-(left|right)-content"/g, '$1="$2-content"');
  const withoutProp = ownedIds(renderShell().container.innerHTML);
  cleanup();
  expect(ownedIds(renderShell({ center: null }).container.innerHTML)).toBe(withoutProp);
});

test("the center takes the chat width while the chat pane and the draft stay mounted", () => {
  const props = {
    ...railSlots,
    topBar: <div data-region="topbar">top</div>,
    messages: <p>Primary selected content</p>,
    composer: <textarea aria-label="Message" defaultValue="synthetic draft" />,
    statusBar: <footer data-region="statusbar">status</footer>,
  };
  const view = render(<Shell {...props} />);
  const primary = view.container.querySelector<HTMLElement>('[data-region="conversation"]');
  const input = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  const scroll = view.container.querySelector<HTMLElement>(".conversation-scroll");
  const bars = ["topbar", "statusbar"].map((region) =>
    view.container.querySelector(`[data-region="${region}"]`),
  );
  const railNodes = Array.from(view.container.querySelectorAll(".shell-rail-host"));
  input.setSelectionRange(2, 5);
  if (scroll) scroll.scrollTop = 137;
  view.rerender(<Shell {...props} center={<button type="button">Close code preview</button>} />);
  const center = view.container.querySelector<HTMLElement>('[data-region="center-pane"]');
  expect(center?.tagName).toBe("MAIN");
  expect(center?.contains(screen.getByRole("button", { name: "Close code preview" }))).toBe(true);
  expect(center?.nextElementSibling).toBe(railNodes[1]);
  expect(primary?.isConnected).toBe(true);
  expect(primary?.hidden).toBe(true);
  expect(primary?.hasAttribute("inert")).toBe(true);
  expect(screen.queryByRole("textbox", { name: "Message" })).toBeNull();
  expect(view.container.querySelector("textarea")).toBe(input);
  expect(Array.from(view.container.querySelectorAll(".shell-rail-host"))).toEqual(railNodes);
  expect(
    ["topbar", "statusbar"].map((region) =>
      view.container.querySelector(`[data-region="${region}"]`),
    ),
  ).toEqual(bars);
  view.rerender(<Shell {...props} center={null} />);
  expect(view.container.querySelector('[data-region="center-pane"]')).toBeNull();
  expect(screen.getByRole("textbox", { name: "Message" })).toBe(input);
  expect(input.value).toBe("synthetic draft");
  expect([input.selectionStart, input.selectionEnd]).toEqual([2, 5]);
  expect(scroll?.scrollTop).toBe(137);
  expect(primary?.hidden).toBe(false);
  expect(primary?.hasAttribute("inert")).toBe(false);
});

test("a whole-window view still omits all chat and center slots", () => {
  const { container } = renderShell({ view: <p>settings</p>, center: <p>code preview</p> });
  expect(screen.getByText("settings")).toBeTruthy();
  expect(screen.queryByText("code preview")).toBeNull();
  expect(container.querySelector('[data-region="conversation"]')).toBeNull();
});

test.each(["button", "heading"])(
  "a drawer that docks restores focus to the visible center %s instead of the hidden draft",
  (target) => {
    const toggle = document.createElement("button");
    document.body.append(toggle);
    toggle.focus();
    const props = {
      ...railSlots,
      composer: <textarea aria-label="Message" />,
      center:
        target === "button" ? (
          <button type="button">Close code preview</button>
        ) : (
          <>
            <button type="button" hidden>
              Hidden preview action
            </button>
            <h2 tabIndex={-1}>Close code preview</h2>
          </>
        ),
    };
    const view = render(
      <Shell
        {...props}
        layout={{ leftDocked: false, rightDocked: false }}
        rails={{ drawer: "right", onClose: vi.fn() }}
      />,
    );
    expect(screen.getByRole("dialog", { name: "VIKI panel" })).toBeTruthy();
    expect(screen.getByText("Close code preview").closest("[inert]")).not.toBeNull();
    toggle.remove();
    view.rerender(<Shell {...props} rails={{ drawer: null, onClose: vi.fn() }} />);
    const close = screen.getByText("Close code preview");
    expect(close.closest("[inert]")).toBeNull();
    expect(document.activeElement).toBe(close);
  },
);

test("closing a drawer to open the center leaves hidden chat inert", () => {
  const props = { ...railSlots, composer: <textarea aria-label="Message" /> };
  const layout = { leftDocked: true, rightDocked: false };
  const view = render(
    <Shell {...props} layout={layout} rails={{ drawer: "right", onClose: vi.fn() }} />,
  );
  view.rerender(
    <Shell
      {...props}
      layout={layout}
      rails={{ drawer: null, onClose: vi.fn() }}
      center={<button type="button">Close code preview</button>}
    />,
  );
  const chat = view.container.querySelector<HTMLElement>('[data-region="conversation"]');
  expect(chat?.hidden).toBe(true);
  expect(chat?.hasAttribute("inert")).toBe(true);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.getByRole("button", { name: "Close code preview" }).closest("[inert]")).toBeNull();
  view.rerender(
    <Shell
      {...props}
      layout={layout}
      rails={{ drawer: "right", onClose: vi.fn() }}
      center={<button type="button">Close code preview</button>}
    />,
  );
  view.rerender(
    <Shell
      {...props}
      layout={layout}
      rails={{ drawer: "right", onClose: vi.fn() }}
      center={null}
    />,
  );
  expect(chat?.hidden).toBe(false);
  expect(chat?.hasAttribute("inert")).toBe(true);
  expect(screen.getByRole("dialog", { name: "VIKI panel" })).toBeTruthy();
  view.rerender(
    <Shell {...props} layout={layout} rails={{ drawer: null, onClose: vi.fn() }} center={null} />,
  );
  expect(chat?.hidden).toBe(false);
  expect(chat?.hasAttribute("inert")).toBe(false);
});

test("docked separators own stable unique hosts without changing caller rail IDs", () => {
  const first = renderShell({ leftRail: <nav id="caller-left" data-region="left-rail" /> });
  const firstHandles = first.container.querySelectorAll('[role="separator"]');
  expect(firstHandles).toHaveLength(2);
  const ids = Array.from(firstHandles).map((handle) => handle.getAttribute("aria-controls"));
  for (const [index, side] of ["left", "right"].entries()) {
    const host = first.container.querySelector<HTMLElement>(`[data-resize-rail="${side}"]`);
    expect(host?.style.width).toBe(side === "left" ? "272px" : "320px");
    expect(host?.querySelectorAll(`[data-region="${side}-rail"]`)).toHaveLength(1);
    expect(host?.querySelector(".shell-rail-content")?.id).toBe(ids[index]);
  }
  expect(first.container.querySelector('[data-region="left-rail"]')?.id).toBe("caller-left");
  fireEvent.keyDown(firstHandles[0] as HTMLElement, { key: "ArrowRight" });
  expect(Array.from(firstHandles).map((handle) => handle.getAttribute("aria-controls"))).toEqual(
    ids,
  );
  const second = renderShell();
  const secondIds = Array.from(second.container.querySelectorAll('[role="separator"]')).map(
    (handle) => handle.getAttribute("aria-controls"),
  );
  expect(new Set([...ids, ...secondIds]).size).toBe(4);
});

test.each(["left", "right"])(
  "%s rail keyboard bounds, collapse and double-click reset keep controls reachable",
  (side) => {
    const { container } = renderShell({
      leftRail: (
        <nav data-region="left-rail">
          <button type="button">Left item</button>
        </nav>
      ),
      rightRail: (
        <aside data-region="right-rail">
          <button type="button">Right item</button>
        </aside>
      ),
    });
    expect(screen.queryByRole("button", { name: /Reset .* rail width/ })).toBeNull();
    const handle = screen.getByRole("separator", { name: `Resize ${side} rail` });
    const content = container.querySelector<HTMLElement>(
      `[id="${handle.getAttribute("aria-controls")}"]`,
    );
    fireEvent.keyDown(handle, { key: "End" });
    expect(handle.getAttribute("aria-valuenow")).toBe(side === "left" ? "480" : "560");
    fireEvent.keyDown(handle, { key: "Enter" });
    expect(content?.hidden).toBe(true);
    expect(content?.hasAttribute("inert")).toBe(true);
    expect(
      screen.queryByRole("button", { name: `${side === "left" ? "Left" : "Right"} item` }),
    ).toBeNull();
    expect(handle.closest("[hidden], [inert]")).toBeNull();
    fireEvent.keyDown(handle, { key: "Enter" });
    expect(content?.hidden).toBe(false);
    expect(handle.getAttribute("aria-valuenow")).toBe(side === "left" ? "480" : "560");
    fireEvent.doubleClick(handle);
    expect(handle.getAttribute("aria-valuenow")).toBe(side === "left" ? "272" : "320");
  },
);

test("drawers and whole-window views have no docked resize controls", () => {
  renderShell({
    layout: { leftDocked: false, rightDocked: false },
    rails: { drawer: "left", onClose: vi.fn() },
  });
  expect(screen.queryByRole("separator")).toBeNull();
  cleanup();
  renderShell({ view: <p>settings</p> });
  expect(screen.queryByRole("separator")).toBeNull();
});

test("Code view keeps both rails and 536px of center under maximum preferences", () => {
  withSizedShell(
    {
      view: <h1 tabIndex={-1}>Code</h1>,
      viewSidebar: <nav data-region="left-rail">sessions</nav>,
      rightRail: <aside data-region="right-rail">VIKI</aside>,
      railWidths: { left: 480, right: 560 },
    },
    (view, resize) => {
      resize(1012);
      const left = view.container.querySelector<HTMLElement>('[data-resize-rail="left"]');
      const right = view.container.querySelector<HTMLElement>('[data-resize-rail="right"]');
      expect(left).not.toBeNull();
      expect(right).not.toBeNull();
      expect(Number.parseInt(left?.style.width ?? "0", 10)).toBeGreaterThan(0);
      expect(Number.parseInt(right?.style.width ?? "0", 10)).toBeGreaterThan(0);
      expect(
        1012 -
          Number.parseInt(left?.style.width ?? "0", 10) -
          Number.parseInt(right?.style.width ?? "0", 10),
      ).toBeGreaterThanOrEqual(536);
      expect(view.container.querySelectorAll('[data-region="right-rail"]')).toHaveLength(1);
    },
  );
});

test("Code view right drawer closes with focus on its heading after the toggle leaves", () => {
  const toggle = document.createElement("button");
  document.body.append(toggle);
  toggle.focus();
  const props = {
    ...railSlots,
    view: <h1 tabIndex={-1}>Code</h1>,
    viewSidebar: <nav data-region="left-rail">sessions</nav>,
    rightRail: <aside data-region="right-rail">VIKI</aside>,
  };
  const view = render(
    <Shell
      {...props}
      layout={{ leftDocked: false, rightDocked: false }}
      rails={{ drawer: "right", onClose: vi.fn() }}
    />,
  );
  expect(screen.getByRole("dialog", { name: "VIKI panel" })).toBeTruthy();
  toggle.remove();
  view.rerender(<Shell {...props} rails={{ drawer: null, onClose: vi.fn() }} />);
  expect(document.activeElement).toBe(screen.getByRole("heading", { name: "Code" }));
});

test("rail hosts add no separator layout space and the roster has local scrolling bounds", () => {
  expect(rule(".shell-rail-host")).toMatch(/flex:\s*none/);
  expect(rule(".shell-rail-host")).toMatch(/position:\s*relative/);
  expect(rule(".shell-rail-content")).toMatch(/min-height:\s*0/);
  const left = readCss(import.meta.url, "../LeftRail/AgentsSection.css");
  const roster = left.match(/\.rail-agents\s*\{[^}]*\}/)?.[0];
  expect(roster).toMatch(/max-height:\s*40%/);
  expect(roster).toMatch(/overflow-y:\s*auto/);
  expect(roster).not.toMatch(/vh/);
});

function withSizedShell(
  props: Partial<ShellProps>,
  check: (
    view: ReturnType<typeof renderShell>,
    resize: (width: number, height?: number) => void,
    observers: {
      callback: ResizeObserverCallback;
      observe: ReturnType<typeof vi.fn>;
      disconnect: ReturnType<typeof vi.fn>;
    }[],
  ) => void,
) {
  const observers: {
    callback: ResizeObserverCallback;
    observe: ReturnType<typeof vi.fn>;
    disconnect: ReturnType<typeof vi.fn>;
  }[] = [];
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe = vi.fn();
      disconnect = vi.fn();
      constructor(callback: ResizeObserverCallback) {
        observers.push({ callback, observe: this.observe, disconnect: this.disconnect });
      }
    },
  );
  try {
    const view = renderShell(props);
    const frame = view.container.querySelector(".shell-body") as HTMLElement;
    // 2000px is taller than any pane-fit floor this file tests; only a test that passes its own
    // `height` cares about the frame's vertical size.
    const resize = (width: number, height = 2000) => {
      Object.defineProperty(frame, "clientWidth", { configurable: true, value: width });
      Object.defineProperty(frame, "clientHeight", { configurable: true, value: height });
      act(() => {
        for (const observer of observers) observer.callback([], {} as ResizeObserver);
      });
    };
    check(view, resize, observers);
  } finally {
    cleanup();
    vi.unstubAllGlobals();
  }
}

function railPointer(handle: HTMLElement, type: string, clientX: number) {
  const event = new MouseEvent(type, { bubbles: true, button: 0, clientX });
  Object.defineProperties(event, { pointerId: { value: 1 }, isPrimary: { value: true } });
  fireEvent(handle, event);
}

test("local frame shortage shrinks right first and restores retained preferred widths", () => {
  withSizedShell({}, (_view, resize) => {
    const left = screen.getByRole("separator", { name: "Resize left rail" });
    const right = screen.getByRole("separator", { name: "Resize right rail" });
    for (const [frame, expectedLeft, expectedRight, leftMaximum, rightMaximum] of [
      [880, 272, 288, 272, 288],
      [800, 240, 240, 240, 240],
      [700, 220, 160, 220, 160],
      [500, 180, 0, 180, 0],
      [0, 0, 0, 0, 0],
      [1440, 272, 320, 480, 560],
    ] as const) {
      resize(frame);
      expect(left.getAttribute("aria-valuenow")).toBe(String(expectedLeft));
      expect(right.getAttribute("aria-valuenow")).toBe(String(expectedRight));
      expect(left.getAttribute("aria-valuemax")).toBe(String(leftMaximum));
      expect(right.getAttribute("aria-valuemax")).toBe(String(rightMaximum));
    }
    fireEvent.keyDown(right, { key: "Enter" });
    resize(800);
    resize(1440);
    expect(right.getAttribute("aria-valuenow")).toBe("0");
    fireEvent.keyDown(right, { key: "Enter" });
    expect(right.getAttribute("aria-valuenow")).toBe("320");
  });
});

test("passive-bound cancellation does not save rendered87 over desired320", () => {
  withSizedShell({}, (_view, resize) => {
    const right = screen.getByRole("separator", { name: "Resize right rail" });
    let held = false;
    const release = vi.fn(() => {
      held = false;
    });
    Object.assign(right, {
      setPointerCapture: () => {
        held = true;
      },
      hasPointerCapture: () => held,
      releasePointerCapture: release,
    });
    resize(1440);
    railPointer(right, "pointerdown", 100);
    railPointer(right, "pointermove", 80);
    expect(right.getAttribute("aria-valuenow")).toBe("340");
    resize(627);
    expect(right.getAttribute("aria-valuenow")).toBe("87");
    expect(release).toHaveBeenCalledTimes(1);
    resize(1440);
    railPointer(right, "pointerup", 60);
    expect(right.getAttribute("aria-valuenow")).toBe("320");
  });
});

test("frame observer disconnects and ignores a late callback after unmount", () => {
  withSizedShell({}, (view, _resize, observers) => {
    const frame = view.container.querySelector(".shell-body") as HTMLElement;
    const frameObservers = observers.filter((item) =>
      item.observe.mock.calls.some(([target]) => target === frame),
    );
    expect(frameObservers).toHaveLength(1);
    const observer = frameObservers[0];
    if (!observer) throw new Error("Missing frame observer");
    view.unmount();
    const read = vi.fn(() => 400);
    Object.defineProperty(frame, "clientWidth", { configurable: true, get: read });
    act(() => observer.callback([], {} as ResizeObserver));
    expect(read).not.toHaveBeenCalled();
    expect(observer.disconnect).toHaveBeenCalledTimes(1);
  });
});

const railSlots = {
  topBar: null,
  leftRail: <nav>sessions</nav>,
  header: null,
  messages: null,
  composer: null,
  rightRail: <aside>VIKI</aside>,
  statusBar: null,
};

function rail(side: "left" | "right") {
  return screen.getByRole("separator", { name: `Resize ${side} rail` });
}
function captureRail(handle: HTMLElement) {
  let held = false;
  const release = vi.fn(() => {
    held = false;
  });
  Object.assign(handle, {
    setPointerCapture: () => {
      held = true;
    },
    hasPointerCapture: () => held,
    releasePointerCapture: release,
  });
  return release;
}

test("optional widths default and clamp without publishing mount or prop adoption", () => {
  const commit = vi.fn();
  withSizedShell({ onRailWidthCommit: commit }, (view, resize) => {
    resize(1440);
    for (const [left, right, expectedLeft, expectedRight] of [
      [null, null, 272, 320],
      [999, 1, 480, 240],
      [1, 999, 220, 560],
    ] as const) {
      view.rerender(
        <Shell {...railSlots} railWidths={{ left, right }} onRailWidthCommit={commit} />,
      );
      expect(rail("left").getAttribute("aria-valuenow")).toBe(String(expectedLeft));
      expect(rail("right").getAttribute("aria-valuenow")).toBe(String(expectedRight));
      expect(commit).not.toHaveBeenCalled();
    }
  });
});

test("resize and restore publish nominal preference; collapse, reflow and rollback do not", () => {
  const commit = vi.fn();
  withSizedShell(
    { railWidths: { left: 300, right: 400 }, onRailWidthCommit: commit },
    (_view, resize) => {
      resize(1440);
      const right = rail("right");
      fireEvent.keyDown(right, { key: "ArrowLeft" });
      expect(commit).toHaveBeenLastCalledWith("right", 410);
      commit.mockClear();
      fireEvent.keyDown(right, { key: "Enter" });
      resize(700);
      expect(commit).not.toHaveBeenCalled();
      fireEvent.keyDown(right, { key: "Enter" });
      expect(right.getAttribute("aria-valuenow")).toBe("160");
      expect(commit).toHaveBeenLastCalledWith("right", 410);
      commit.mockClear();
      fireEvent.doubleClick(right);
      expect(right.getAttribute("aria-valuenow")).toBe("160");
      expect(commit).not.toHaveBeenCalled();
      fireEvent.keyDown(right, { key: "Enter" });
      fireEvent.doubleClick(right);
      expect(right.getAttribute("aria-valuenow")).toBe("160");
      expect(commit.mock.calls).toEqual([["right", null]]);
      resize(1440);
      captureRail(right);
      railPointer(right, "pointerdown", 100);
      railPointer(right, "pointermove", 80);
      commit.mockClear();
      fireEvent.keyDown(window, { key: "Escape" });
      expect(commit).not.toHaveBeenCalled();
    },
  );
});

test.each([
  [80, 340],
  [100, 320],
] as const)(
  "incoming raw right width %i to %i cancels equal-display capture without publication or focus loss",
  (movedX, incoming) => {
    const commit = vi.fn();
    withSizedShell({ onRailWidthCommit: commit }, (view, resize) => {
      resize(1440);
      const right = rail("right");
      const controls = right.getAttribute("aria-controls");
      const release = captureRail(right);
      railPointer(right, "pointerdown", 100);
      railPointer(right, "pointermove", movedX);
      view.rerender(
        <Shell
          {...railSlots}
          railWidths={{ left: null, right: incoming }}
          onRailWidthCommit={commit}
        />,
      );
      expect(release).toHaveBeenCalledTimes(1);
      // Capture mocks require the public event normally emitted by native release.
      railPointer(right, "lostpointercapture", movedX);
      railPointer(right, "pointerup", 60);
      expect(right).toBe(rail("right"));
      expect(document.activeElement).toBe(right);
      expect(right.getAttribute("aria-controls")).toBe(controls);
      expect(right.getAttribute("aria-valuenow")).toBe(String(incoming));
      expect(commit).not.toHaveBeenCalled();
    });
  },
);

test("new objects and callbacks preserve the other rail preview and use the current callback", () => {
  const oldCommit = vi.fn();
  const commit = vi.fn();
  withSizedShell({ onRailWidthCommit: oldCommit }, (view, resize) => {
    resize(1440);
    const right = rail("right");
    const release = captureRail(right);
    railPointer(right, "pointerdown", 100);
    railPointer(right, "pointermove", 80);
    view.rerender(
      <Shell {...railSlots} railWidths={{ left: null, right: null }} onRailWidthCommit={commit} />,
    );
    expect(release).not.toHaveBeenCalled();
    view.rerender(
      <Shell {...railSlots} railWidths={{ left: 300, right: null }} onRailWidthCommit={commit} />,
    );
    expect(rail("left").getAttribute("aria-valuenow")).toBe("300");
    expect(right.getAttribute("aria-valuenow")).toBe("340");
    expect(release).not.toHaveBeenCalled();
    expect(commit).not.toHaveBeenCalled();
    railPointer(right, "pointerup", 60);
    expect(commit).toHaveBeenCalledTimes(1);
    expect(commit).toHaveBeenLastCalledWith("right", 360);
    expect(oldCommit).not.toHaveBeenCalled();
  });
});

function sizedComposer(height = "") {
  const view = renderShell();
  const slot = view.container.querySelector<HTMLElement>(".composer-slot");
  if (!slot) throw new Error("Missing composer slot");
  slot.style.setProperty("--composer-min-height", "80px");
  slot.style.setProperty("--composer-max-height", "360px");
  slot.style.height = height;
  Object.defineProperty(slot, "offsetHeight", {
    configurable: true,
    get: () => Number.parseFloat(slot.style.height) || 80,
  });
  act(() => window.dispatchEvent(new Event("resize")));
  return { ...view, slot, handle: screen.getByRole("separator", { name: "Resize composer" }) };
}

function composerPointer(handle: HTMLElement, type: string, clientY: number) {
  const event = new MouseEvent(type, { bubbles: true, button: 0, clientY });
  Object.defineProperties(event, { pointerId: { value: 1 }, isPrimary: { value: true } });
  fireEvent(handle, event);
}

test("composer double-click reset clears a resized height", () => {
  const { slot, handle, container } = sizedComposer();
  const controls = container.querySelector(".composer-resize-controls");
  expect(controls?.contains(handle)).toBe(true);
  expect(controls?.parentElement).toBe(slot);
  expect(controls?.getAttribute("title")).toBe(
    "Drag to resize. Double-click or press Enter to reset.",
  );
  fireEvent.keyDown(handle, { key: "ArrowUp" });
  expect(slot.style.height).toBe("90px");
  fireEvent.doubleClick(handle);
  expect(slot.style.height).toBe("");
  expect(handle.getAttribute("aria-valuenow")).toBe("80");
});

test("composer Enter reset clears a resized height", () => {
  const { slot, handle, container } = sizedComposer();
  const controls = container.querySelector(".composer-resize-controls");
  expect(controls?.getAttribute("title")).toBe(
    "Drag to resize. Double-click or press Enter to reset.",
  );
  fireEvent.keyDown(handle, { key: "ArrowUp" });
  expect(slot.style.height).toBe("90px");
  handle.focus();
  fireEvent.keyDown(handle, { key: "Enter" });
  expect(slot.style.height).toBe("");
  expect(handle.getAttribute("aria-valuenow")).toBe("80");
});

test("composer separator controls the actual slot using current local bounds and keyboard sizes", () => {
  const { slot, handle } = sizedComposer();
  expect(slot.style.height).toBe("");
  expect(handle.getAttribute("aria-controls")).toBe(slot.id);
  expect(handle.getAttribute("aria-orientation")).toBe("horizontal");
  expect(handle.getAttribute("aria-valuemin")).toBe("80");
  expect(handle.getAttribute("aria-valuemax")).toBe("360");
  for (const [key, shiftKey, height] of [
    ["ArrowUp", false, 90],
    ["ArrowUp", true, 140],
    ["End", false, 360],
    ["Home", false, 80],
  ] as const) {
    fireEvent.keyDown(handle, { key, shiftKey });
    expect(slot.style.height).toBe(`${height}px`);
    expect(handle.getAttribute("aria-valuenow")).toBe(String(height));
  }
  fireEvent.doubleClick(handle);
  expect(slot.style.height).toBe("");
});

test("composer pointer commits its final coordinate and Escape restores the previous explicit mode", () => {
  const { slot, handle } = sizedComposer();
  const release = captureRail(handle);
  composerPointer(handle, "pointerdown", 100);
  composerPointer(handle, "pointermove", 80);
  expect(slot.style.height).toBe("100px");
  composerPointer(handle, "pointerup", 60);
  expect(slot.style.height).toBe("120px");
  composerPointer(handle, "pointerdown", 100);
  composerPointer(handle, "pointermove", 80);
  fireEvent.keyDown(window, { key: "Escape" });
  expect(slot.style.height).toBe("120px");
  expect(release).toHaveBeenCalledTimes(2);
});

test.each(["", "180px"])(
  "composer rollback retains prior inline mode %s without freezing natural height",
  (height) => {
    const { slot, handle } = sizedComposer(height);
    const release = captureRail(handle);
    composerPointer(handle, "pointerdown", 100);
    composerPointer(handle, "pointermove", 80);
    composerPointer(handle, "pointerup", 100);
    expect(slot.style.height).toBe(height);
    composerPointer(handle, "pointerdown", 100);
    composerPointer(handle, "pointerup", 100);
    expect(slot.style.height).toBe(height);
    composerPointer(handle, "pointerdown", 100);
    composerPointer(handle, "pointermove", 80);
    composerPointer(handle, "pointercancel", 80);
    composerPointer(handle, "pointerup", 40);
    expect(slot.style.height).toBe(height);
    expect(release).toHaveBeenCalledTimes(3);
  },
);

test("Reset recomputes the actual Composer budget at unchanged slot offset without moving the draft caret", () => {
  const send = vi.fn(async () => true);
  const stop = vi.fn();
  const view = renderShell({
    composer: <Composer value="synthetic draft" running={false} onSend={send} onStop={stop} />,
  });
  const slot = view.container.querySelector<HTMLElement>(".composer-slot");
  const conversation = view.container.querySelector<HTMLElement>(".conversation");
  const wrap = view.container.querySelector<HTMLElement>(".composer-wrap");
  const pill = view.container.querySelector<HTMLElement>('[data-region="composer"]');
  const input = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  if (!slot || !conversation || !wrap || !pill) throw new Error("Missing actual Composer nodes");
  slot.style.padding = "10px 16px 14px";
  Object.defineProperty(conversation, "clientHeight", { configurable: true, value: 600 });
  // Hold offset fixed so Reset cannot depend on a ResizeObserver notification.
  Object.defineProperty(slot, "offsetHeight", { configurable: true, value: 240 });
  Object.defineProperty(input, "scrollHeight", { configurable: true, value: 300 });
  for (const node of [wrap, pill])
    Object.defineProperty(node, "offsetHeight", {
      configurable: true,
      get: () => (Number.parseFloat(input.style.height) || 54) + 2,
    });
  act(() => {
    window.dispatchEvent(new Event("resize"));
    window.dispatchEvent(new Event("resize"));
  });
  expect(slot.style.getPropertyValue("--composer-min-height")).toBe("80px");
  expect(slot.style.getPropertyValue("--composer-max-height")).toBe("360px");
  slot.style.height = "240px";
  act(() => window.dispatchEvent(new Event("resize")));
  expect(input.style.height).toBe("214px");
  input.setSelectionRange(2, 5);
  // The measured value (240, from the fixed offsetHeight stub) already equals the bounded
  // default: double-click must still commit "reset" to clear the pinned inline height.
  const handle = screen.getByRole("separator", { name: "Resize composer" });
  fireEvent.doubleClick(handle);
  expect(slot.offsetHeight).toBe(240);
  expect(slot.style.height).toBe("");
  expect(input.style.height).toBe("300px");
  captureRail(handle);
  composerPointer(handle, "pointerdown", 100);
  composerPointer(handle, "pointermove", 110);
  act(() => window.dispatchEvent(new Event("resize")));
  expect(input.style.height).toBe("204px");
  fireEvent.keyDown(window, { key: "Escape" });
  expect(slot.style.height).toBe("");
  expect(input.style.height).toBe("300px");
  expect(input.value).toBe("synthetic draft");
  expect([input.selectionStart, input.selectionEnd]).toEqual([2, 5]);
  expect(document.activeElement).toBe(handle);
  expect(send).not.toHaveBeenCalled();
  expect(stop).not.toHaveBeenCalled();
});

function observedComposer(
  check: (data: {
    sized: ReturnType<typeof sizedComposer>;
    reads: {
      callback: ResizeObserverCallback;
      targets: Set<Element>;
      disconnect: ReturnType<typeof vi.fn>;
    }[];
    bounds: {
      callback: MutationCallback;
      targets: Set<Node>;
      disconnect: ReturnType<typeof vi.fn>;
    }[];
  }) => void,
) {
  const reads: {
    callback: ResizeObserverCallback;
    targets: Set<Element>;
    disconnect: ReturnType<typeof vi.fn>;
  }[] = [];
  const bounds: {
    callback: MutationCallback;
    targets: Set<Node>;
    disconnect: ReturnType<typeof vi.fn>;
  }[] = [];
  vi.stubGlobal(
    "ResizeObserver",
    class {
      targets = new Set<Element>();
      observe = (target: Element) => {
        this.targets.add(target);
      };
      disconnect = vi.fn();
      constructor(callback: ResizeObserverCallback) {
        reads.push({ callback, targets: this.targets, disconnect: this.disconnect });
      }
    },
  );
  vi.stubGlobal(
    "MutationObserver",
    class {
      targets = new Set<Node>();
      observe = (target: Node) => {
        this.targets.add(target);
      };
      disconnect = vi.fn();
      constructor(callback: MutationCallback) {
        bounds.push({ callback, targets: this.targets, disconnect: this.disconnect });
      }
    },
  );
  try {
    check({ sized: sizedComposer(), reads, bounds });
  } finally {
    cleanup();
    vi.unstubAllGlobals();
  }
}

test("fractional slot notifications cancel a captured resize; identical sizes are inert and style bounds remain current", () => {
  observedComposer(({ sized: { slot, handle }, reads, bounds }) => {
    const read = reads.find((item) => item.targets.has(slot));
    const bound = bounds.find((item) => item.targets.has(slot));
    if (!read || !bound) throw new Error("Missing current slot observers");
    Object.defineProperty(slot, "clientWidth", { configurable: true, value: 300 });
    const notify = (width: number) =>
      act(() =>
        read.callback(
          [
            {
              target: slot,
              contentRect: new DOMRect(0, 0, width, 80),
              borderBoxSize: [],
              contentBoxSize: [],
              devicePixelContentBoxSize: [],
            },
          ],
          {} as ResizeObserver,
        ),
      );
    const input = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
    input.value = "draft";
    input.setSelectionRange(1, 3);
    input.focus();
    notify(300.25);
    expect(document.activeElement).toBe(input);
    const release = captureRail(handle);
    composerPointer(handle, "pointerdown", 100);
    composerPointer(handle, "pointermove", 80);
    slot.style.setProperty("--composer-min-height", "120px");
    slot.style.setProperty("--composer-max-height", "240px");
    notify(300.5);
    expect(release).toHaveBeenCalledTimes(1);
    expect(slot.style.height).toBe("");
    expect(handle.getAttribute("aria-valuemin")).toBe("120");
    expect(handle.getAttribute("aria-valuemax")).toBe("240");
    expect(document.activeElement).toBe(handle);
    expect(input.value).toBe("draft");
    expect([input.selectionStart, input.selectionEnd]).toEqual([1, 3]);
    composerPointer(handle, "pointerup", 40);
    expect(slot.style.height).toBe("");
    slot.style.setProperty("--composer-max-height", "180px");
    notify(300.5);
    expect(handle.getAttribute("aria-valuemax")).toBe("240");
    act(() => bound.callback([], {} as MutationObserver));
    expect(handle.getAttribute("aria-valuemax")).toBe("180");
    expect(Array.from(read.targets)).toEqual([slot]);
    expect(Array.from(bound.targets)).toEqual([slot]);
  });
});

test("current slot observation rebinds after whole-window navigation and old callbacks stay inert", () => {
  observedComposer(({ sized: { slot, handle, rerender }, reads, bounds }) => {
    const read = reads.find((item) => item.targets.has(slot));
    const bound = bounds.find((item) => item.targets.has(slot));
    if (!read || !bound) throw new Error("Missing original slot observers");
    const controls = handle.getAttribute("aria-controls");
    const release = captureRail(handle);
    composerPointer(handle, "pointerdown", 100);
    composerPointer(handle, "pointermove", 80);
    rerender(<Shell {...railSlots} view={<p>settings</p>} />);
    expect(read.disconnect).toHaveBeenCalledOnce();
    expect(bound.disconnect).toHaveBeenCalledOnce();
    expect(release).toHaveBeenCalledOnce();
    expect(screen.queryByRole("separator", { name: "Resize composer" })).toBeNull();
    rerender(
      <Shell
        {...railSlots}
        composer={
          <div data-region="composer">
            <textarea aria-label="Message" />
          </div>
        }
      />,
    );
    const next = document.querySelector<HTMLElement>(".composer-slot");
    if (!next || next === slot) throw new Error("Missing replacement composer slot");
    next.style.setProperty("--composer-min-height", "80px");
    next.style.setProperty("--composer-max-height", "360px");
    Object.defineProperty(next, "offsetHeight", { configurable: true, value: 80 });
    act(() => window.dispatchEvent(new Event("resize")));
    const current = screen.getByRole("separator", { name: "Resize composer" });
    expect(current.getAttribute("aria-controls")).not.toBe(controls);
    expect(current.getAttribute("aria-controls")).toBe(next.id);
    slot.style.setProperty("--composer-max-height", "99px");
    act(() => {
      read.callback(
        [
          {
            target: slot,
            contentRect: new DOMRect(0, 0, 310, 90),
            borderBoxSize: [],
            contentBoxSize: [],
            devicePixelContentBoxSize: [],
          },
        ],
        {} as ResizeObserver,
      );
      bound.callback([], {} as MutationObserver);
    });
    expect(current.getAttribute("aria-valuemax")).toBe("360");
    const currentBounds = bounds.find((item) => item.targets.has(next));
    if (!currentBounds) throw new Error("Missing replacement slot observer");
    next.style.removeProperty("--composer-max-height");
    act(() => currentBounds.callback([], {} as MutationObserver));
    expect(screen.queryByRole("separator", { name: "Resize composer" })).toBeNull();
  });
});

test("a one-group panes keeps today's region order, plus data-pane", () => {
  const ownedIds = (html: string) =>
    html.replace(/(id|aria-controls)="_[^"]+-(left|right)-content"/g, '$1="$2-content"');
  const withoutPanes = ownedIds(renderShell().container.innerHTML);
  cleanup();
  const { container } = renderShell({ panes: stubPanes({ root: group("g1"), focused: "g1" }) });
  const conversation = container.querySelector('[data-region="conversation"]');
  expect(conversation?.getAttribute("data-pane")).toBe("g1");
  expect(ownedIds(container.innerHTML).replace(' data-pane="g1"', "")).toBe(withoutPanes);
});

test(
  "a split root gives one section and composer slot per chat group, banner before " +
    "the tree, and a focused Message box fallback",
  () => {
    const panes = stubPanes({ root: rowSplit("s1", [group("g1"), group("g2")]), focused: "g1" });
    const { container } = renderShell({
      panes,
      banner: <p>Notice</p>,
      composer: (
        <div data-region="composer">
          <textarea aria-label="Message" />
        </div>
      ),
    });
    const main = container.querySelector(".conversation-panes");
    if (!main) throw new Error("Missing multi-pane main");
    const sections = container.querySelectorAll(".pane");
    expect(Array.from(sections).map((section) => section.getAttribute("data-pane"))).toEqual([
      "g1",
      "g2",
    ]);
    expect(container.querySelectorAll(".composer-slot")).toHaveLength(2);
    expect(container.querySelector('.pane[data-focused="true"] .composer-slot')).not.toBeNull();
    expect(screen.getByRole("textbox", { name: "Message" })).toBeTruthy();
    const children = Array.from(main.children);
    const bannerIndex = children.findIndex((child) => child.textContent === "Notice");
    const treeIndex = children.findIndex((child) => child.classList.contains("pane-split"));
    expect(bannerIndex).toBeGreaterThanOrEqual(0);
    expect(treeIndex).toBeGreaterThan(bannerIndex);
    expect(container.querySelectorAll('[data-region="conversation-header"]')).toHaveLength(0);
  },
);

test("center preview keeps a fitted pane tree and composer mounted while reporting it hidden", () => {
  const panes = stubPanes({ root: rowSplit("s1", [group("g1"), group("g2")]), focused: "g1" });
  const onPaneModeChange = vi.fn();
  const props = {
    ...railSlots,
    panes,
    onPaneModeChange,
    composer: <textarea aria-label="Message" />,
  };
  const view = render(<Shell {...props} />);
  const main = view.container.querySelector<HTMLElement>(".conversation-panes");
  const tree = main?.querySelector(".pane-split");
  const slot = main?.querySelector(".composer-slot");
  const input = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  expect(main).not.toBeNull();
  expect(tree).not.toBeNull();
  expect(slot).not.toBeNull();
  input.value = "draft";
  expect(onPaneModeChange).toHaveBeenLastCalledWith(true);

  view.rerender(<Shell {...props} center={<button type="button">File preview</button>} />);
  expect(main?.isConnected).toBe(true);
  expect(view.container.querySelector(".conversation-panes")).toBe(main);
  expect(main?.querySelector(".pane-split")).toBe(tree);
  expect(main?.querySelector(".composer-slot")).toBe(slot);
  expect(main?.querySelector("textarea")).toBe(input);
  expect(input.value).toBe("draft");
  expect(main?.hidden).toBe(true);
  expect(main?.hasAttribute("inert")).toBe(true);
  expect(onPaneModeChange).toHaveBeenLastCalledWith(false);

  view.rerender(<Shell {...props} />);
  expect(view.container.querySelector(".conversation-panes")).toBe(main);
  expect(main?.querySelector(".pane-split")).toBe(tree);
  expect(main?.querySelector(".composer-slot")).toBe(slot);
  expect(input.value).toBe("draft");
  expect(main?.hidden).toBe(false);
  expect(onPaneModeChange).toHaveBeenLastCalledWith(true);
});

test("pointerdown on another section calls its onActivate with the activating event", () => {
  const onActivateG1 = vi.fn();
  const onActivateG2 = vi.fn();
  const panes = stubPanes({
    root: rowSplit("s1", [group("g1"), group("g2")]),
    focused: "g1",
    pane: (groupId) => ({
      header: null,
      messages: <p>{groupId} messages</p>,
      label: groupId,
      onActivate: groupId === "g1" ? onActivateG1 : onActivateG2,
    }),
  });
  const { container } = renderShell({ panes });
  const g2 = container.querySelector('[data-pane="g2"]');
  if (!g2) throw new Error("Missing g2 section");
  fireEvent.pointerDown(g2);
  expect(onActivateG2).toHaveBeenCalledTimes(1);
  expect(onActivateG2.mock.calls[0]?.[0]?.type).toBe("pointerdown");
  expect(onActivateG1).not.toHaveBeenCalled();
});

function SplitComposerHarness() {
  const [focused, setFocused] = useState("g1");
  const panes = stubPanes({
    root: rowSplit("s1", [group("g1"), group("g2")]),
    focused,
    pane: (groupId) => ({
      header: null,
      messages: null,
      label: groupId,
      composer: (
        <Composer
          editorKey={groupId}
          label={`Message ${groupId}`}
          running={false}
          onSend={async () => true}
          onStop={() => {}}
        />
      ),
      onActivate: () => flushSync(() => setFocused(groupId)),
    }),
  });
  return <Shell {...railSlots} panes={panes} />;
}

test("both real composers retain their textarea nodes, drafts and independent resize controls across pane focus", () => {
  const { container } = render(<SplitComposerHarness />);
  const inputs = ["g1", "g2"].map(
    (id) => screen.getByRole("textbox", { name: `Message ${id}` }) as HTMLTextAreaElement,
  );
  const slots = inputs.map((input, index) => {
    const slot = input.closest<HTMLElement>(".composer-slot");
    const conversation = input.closest<HTMLElement>(".conversation");
    const wrap = input.closest<HTMLElement>(".composer-wrap");
    const pill = input.closest<HTMLElement>('[data-region="composer"]');
    if (!slot || !conversation || !wrap || !pill) throw new Error("Missing actual Composer nodes");
    slot.style.padding = "10px 16px 14px";
    Object.defineProperty(conversation, "clientHeight", {
      configurable: true,
      value: index === 0 ? 600 : 500,
    });
    Object.defineProperty(slot, "offsetHeight", {
      configurable: true,
      get: () => Number.parseFloat(slot.style.height) || 80,
    });
    Object.defineProperty(input, "scrollHeight", { configurable: true, value: 54 });
    for (const node of [wrap, pill])
      Object.defineProperty(node, "offsetHeight", {
        configurable: true,
        get: () => (Number.parseFloat(input.style.height) || 54) + 2,
      });
    fireEvent.change(input, { target: { value: `draft ${index + 1}` } });
    input.setSelectionRange(1, 4);
    return slot;
  });
  act(() => {
    window.dispatchEvent(new Event("resize"));
    window.dispatchEvent(new Event("resize"));
  });
  const handles = slots.map((slot) => {
    const handle = slot.querySelector<HTMLElement>('[role="separator"]');
    if (!handle) throw new Error("Missing composer resize handle");
    expect(handle.getAttribute("aria-controls")).toBe(slot.id);
    expect(handle.getAttribute("aria-valuemin")).toBe("80");
    return handle;
  });
  expect(new Set(slots.map((slot) => slot.id)).size).toBe(2);
  expect(handles.map((handle) => handle.getAttribute("aria-valuemax"))).toEqual(["360", "300"]);
  const [firstInput, secondInput] = inputs;
  const [firstSlot, secondSlot] = slots;
  const [firstHandle, secondHandle] = handles;
  if (!firstInput || !secondInput || !firstSlot || !secondSlot || !firstHandle || !secondHandle)
    throw new Error("Missing split composer pair");

  act(() => secondInput.focus());
  expect(container.querySelector('[data-pane="g2"]')?.getAttribute("data-focused")).toBe("true");
  act(() => firstHandle.focus());
  fireEvent.keyDown(firstHandle, { key: "ArrowUp", shiftKey: true });
  expect(firstSlot.style.height).toBe("130px");
  expect(secondSlot.style.height).toBe("");
  captureRail(secondHandle);
  composerPointer(secondHandle, "pointerdown", 100);
  composerPointer(secondHandle, "pointermove", 80);
  composerPointer(secondHandle, "pointerup", 60);
  expect(firstSlot.style.height).toBe("130px");
  expect(secondSlot.style.height).toBe("120px");
  expect(container.querySelector('[data-pane="g2"]')?.getAttribute("data-focused")).toBe("true");
  fireEvent.doubleClick(firstHandle);
  expect(firstSlot.style.height).toBe("");
  expect(secondSlot.style.height).toBe("120px");
  expect(handles.map((handle) => handle.getAttribute("aria-controls"))).toEqual(
    slots.map((slot) => slot.id),
  );
  for (const [index, input] of inputs.entries()) {
    expect(screen.getByRole("textbox", { name: `Message g${index + 1}` })).toBe(input);
    expect(input.value).toBe(`draft ${index + 1}`);
    expect([input.selectionStart, input.selectionEnd]).toEqual([1, 4]);
  }
});

test("each chat pane renders its composer while file panes omit it even when supplied", () => {
  const panes = stubPanes({
    root: rowSplit("s1", [group("g1"), group("g2"), fileGroup("g3")]),
    focused: "g1",
    pane: (groupId) => ({
      header: null,
      messages: null,
      label: groupId,
      composer: <textarea aria-label={`Message ${groupId}`} />,
      onActivate: () => {},
    }),
  });
  const { container } = renderShell({ panes });
  expect(screen.getByRole("textbox", { name: "Message g1" })).toBeTruthy();
  expect(screen.getByRole("textbox", { name: "Message g2" })).toBeTruthy();
  expect(screen.queryByRole("textbox", { name: "Message g3" })).toBeNull();
  expect(screen.queryByRole("textbox", { name: "Message" })).toBeNull();
  expect(container.querySelector('[data-pane="g3"] .composer-slot')).toBeNull();
  expect(container.querySelectorAll(".composer-slot")).toHaveLength(2);
  expect(screen.queryByRole("button", { name: /Write in/ })).toBeNull();
});

test.each([group("g1"), fileGroup("g1")])(
  "a lone pane uses its own chat composer and omits composers for files: $tabs",
  (root) => {
    const panes = stubPanes({
      root,
      focused: root.id,
      pane: () => ({
        header: null,
        messages: null,
        label: "Own pane",
        composer: <textarea aria-label="Own message" />,
        onActivate: () => {},
      }),
    });
    const { container } = renderShell({ panes });
    expect(screen.queryByRole("textbox", { name: "Message" })).toBeNull();
    expect(screen.queryAllByRole("textbox", { name: "Own message" })).toHaveLength(
      root.active === "f1" ? 0 : 1,
    );
    expect(container.querySelectorAll(".composer-slot")).toHaveLength(root.active === "f1" ? 0 : 1);
  },
);

test("an explicit null pane composer suppresses the focused fallback", () => {
  const panes = stubPanes({
    root: rowSplit("s1", [group("g1"), group("g2")]),
    focused: "g1",
    pane: (groupId) => ({
      header: null,
      messages: null,
      label: groupId,
      composer: null,
      onActivate: () => {},
    }),
  });
  renderShell({ panes });
  expect(screen.queryByRole("textbox")).toBeNull();
});

test("a measured frame too narrow renders single mode", () => {
  const panes = stubPanes({ root: rowSplit("s1", [group("g1"), group("g2")]), focused: "g1" });
  withSizedShell({ panes }, (view, resize) => {
    // Two PANE_MIN groups side by side need 840x240; narrower or shorter than that falls back.
    resize(800);
    expect(view.container.querySelector(".conversation-panes")).toBeNull();
    expect(
      view.container.querySelector('[data-region="conversation"]')?.getAttribute("data-pane"),
    ).toBe("g1");
    resize(1600, 200);
    expect(view.container.querySelector(".conversation-panes")).toBeNull();
    resize(1600);
    expect(view.container.querySelector(".conversation-panes")).not.toBeNull();
  });
});

test("a compact layout keeps one pane even with a wide measured frame", () => {
  const panes = stubPanes({ root: rowSplit("s1", [group("g1"), group("g2")]), focused: "g1" });
  withSizedShell({ panes, layout: { leftDocked: false, rightDocked: false } }, (view, resize) => {
    resize(1600);
    expect(view.container.querySelector(".conversation-panes")).toBeNull();
  });
});

test("two chat panes fit at 1280 with both maximum-width rails still visible", () => {
  const onPaneModeChange = vi.fn();
  const panes = stubPanes({ root: rowSplit("s1", [group("g1"), group("g2")]), focused: "g1" });
  withSizedShell(
    { panes, railWidths: { left: 480, right: 560 }, onPaneModeChange },
    (view, resize) => {
      resize(1280);
      expect(view.container.querySelectorAll("section.pane")).toHaveLength(2);
      const left = view.container.querySelector<HTMLElement>('[data-resize-rail="left"]');
      const right = view.container.querySelector<HTMLElement>('[data-resize-rail="right"]');
      expect(left?.style.width).toBe("220px");
      expect(right?.style.width).toBe("219px");
      expect(onPaneModeChange).toHaveBeenLastCalledWith(true);
    },
  );
});

test("a vertical split reserves only its measured center width", () => {
  const panes = stubPanes({
    root: { ...rowSplit("s1", [group("g1"), group("g2")]), dir: "column" },
    focused: "g1",
  });
  withSizedShell({ panes, railWidths: { left: 480, right: 560 } }, (view, resize) => {
    resize(1280);
    expect(view.container.querySelectorAll("section.pane")).toHaveLength(2);
    const left = view.container.querySelector<HTMLElement>('[data-resize-rail="left"]');
    const right = view.container.querySelector<HTMLElement>('[data-resize-rail="right"]');
    expect(left?.style.width).toBe("480px");
    expect(right?.style.width).toBe("379px");
  });
});

test("oversized four-row layout folds with both rails visible and reports single mode", () => {
  const onPaneModeChange = vi.fn();
  const panes = stubPanes({
    root: rowSplit("s1", [group("g1"), group("g2"), group("g3"), group("g4")]),
    focused: "g1",
  });
  withSizedShell(
    { panes, railWidths: { left: 480, right: 560 }, onPaneModeChange },
    (view, resize) => {
      resize(1440);
      expect(view.container.querySelector(".conversation-panes")).toBeNull();
      expect(view.container.querySelector('[data-region="conversation"]')).not.toBeNull();
      const left = view.container.querySelector<HTMLElement>('[data-resize-rail="left"]');
      const right = view.container.querySelector<HTMLElement>('[data-resize-rail="right"]');
      expect(Number.parseInt(left?.style.width ?? "0", 10)).toBeGreaterThanOrEqual(200);
      expect(Number.parseInt(right?.style.width ?? "0", 10)).toBeGreaterThanOrEqual(200);
      expect(onPaneModeChange).toHaveBeenLastCalledWith(false);
    },
  );
});

test(
  "at a 1300px frame with both rails docked a drag near the right edge of " +
    'the narrower lone pane still shows .pane-drop[data-zone="right"], and a drop calls ' +
    'onDrop("g1", id, "right")',
  () => {
    const onDrop = vi.fn();
    withSizedShell(
      { panes: stubPanes({ root: group("g1"), focused: "g1", onDrop }) },
      (view, resize) => {
        resize(1300);
        const main = view.container.querySelector('[data-region="conversation"]') as HTMLElement;
        // The first split shrinks both rails enough to leave two 420px panes plus the hairline.
        main.getBoundingClientRect = () =>
          ({ left: 0, top: 0, width: 708, height: 800 }) as DOMRect;
        expect(main.getAttribute("data-row-room")).toBe("840");
        const transfer = fakeTransfer(CHAT_DRAG_TYPE, PANE_UUID);
        fireEvent(main, drag("dragover", 690, 400, transfer));
        expect(view.container.querySelector(".pane-drop")?.getAttribute("data-zone")).toBe("right");
        fireEvent(main, drag("drop", 690, 400, transfer));
        expect(onDrop).toHaveBeenCalledTimes(1);
        expect(onDrop).toHaveBeenCalledWith("g1", { kind: "chat", id: PANE_UUID }, "right");
      },
    );
  },
);

test("under 840 of row room the lone pane's right edge offers no split, even with a wide box", () => {
  const onDrop = vi.fn();
  withSizedShell(
    { panes: stubPanes({ root: group("g1"), focused: "g1", onDrop }) },
    (view, resize) => {
      // 1240 cannot keep 840px of center and 400px of usable docked rails.
      resize(1240);
      const main = view.container.querySelector('[data-region="conversation"]') as HTMLElement;
      main.getBoundingClientRect = () => ({ left: 0, top: 0, width: 1000, height: 800 }) as DOMRect;
      const transfer = fakeTransfer(CHAT_DRAG_TYPE, PANE_UUID);
      fireEvent(main, drag("dragover", 900, 400, transfer));
      expect(view.container.querySelector(".pane-drop")?.getAttribute("data-zone")).toBe("center");
      fireEvent(main, drag("drop", 900, 400, transfer));
      expect(onDrop).toHaveBeenCalledWith("g1", { kind: "chat", id: PANE_UUID }, "center");
    },
  );
});

test("canSplit false gives centre", () => {
  const onDrop = vi.fn();
  const { container } = renderShell({
    panes: stubPanes({ root: group("g1"), focused: "g1", canSplit: false, onDrop }),
  });
  const main = container.querySelector('[data-region="conversation"]') as HTMLElement;
  main.getBoundingClientRect = () => ({ left: 0, top: 0, width: 1000, height: 800 }) as DOMRect;
  const transfer = fakeTransfer(CHAT_DRAG_TYPE, PANE_UUID);
  fireEvent(main, drag("dragover", 900, 400, transfer));
  expect(container.querySelector(".pane-drop")?.getAttribute("data-zone")).toBe("center");
  fireEvent(main, drag("drop", 900, 400, transfer));
  expect(onDrop).toHaveBeenCalledWith("g1", { kind: "chat", id: PANE_UUID }, "center");
});

test("a file dropped on the lone chat pane's right edge needs the chat floor beside the file floor", () => {
  // A first split keeps both rails usable. One pixel below that rail budget, neither file nor
  // chat drop offers a hidden split; at the budget, both leave the required center width.
  const onDrop = vi.fn();
  const workspaceId = "ws1";
  const ref = "src/a.ts";
  withSizedShell(
    { panes: stubPanes({ root: group("g1"), focused: "g1", onDrop }) },
    (view, resize) => {
      const main = view.container.querySelector('[data-region="conversation"]') as HTMLElement;
      main.getBoundingClientRect = () => ({ left: 0, top: 0, width: 600, height: 800 }) as DOMRect;
      const data = JSON.stringify({ workspace_id: workspaceId, ref });
      const transfer = fakeTransfer(FILE_DRAG_TYPE, data);
      const zone = () => view.container.querySelector(".pane-drop")?.getAttribute("data-zone");
      resize(1240);
      fireEvent(main, drag("dragover", 590, 400, transfer));
      expect(zone()).toBe("center");
      resize(1241);
      fireEvent(main, drag("dragover", 590, 400, transfer));
      expect(zone()).toBe("right");
      fireEvent(main, drag("dragover", 590, 400, fakeTransfer(CHAT_DRAG_TYPE, PANE_UUID)));
      expect(zone()).toBe("right");
      fireEvent(main, drag("dragover", 590, 400, transfer));
      fireEvent(main, drag("drop", 590, 400, transfer));
      expect(onDrop).toHaveBeenCalledTimes(1);
      expect(onDrop).toHaveBeenCalledWith("g1", { kind: "file", workspaceId, ref }, "right");
    },
  );
});

test("a file dropped on a pane's edge in a split layout calls onDrop with the file payload", () => {
  const onDrop = vi.fn();
  const workspaceId = "ws1";
  const ref = "src/a.ts";
  const panes = stubPanes({
    root: rowSplit("s1", [group("g1"), group("g2")]),
    focused: "g1",
    onDrop,
  });
  const { container } = renderShell({ panes });
  const g2 = container.querySelector('[data-pane="g2"]') as HTMLElement;
  g2.getBoundingClientRect = () => ({ left: 0, top: 0, width: 1000, height: 800 }) as DOMRect;
  const data = JSON.stringify({ workspace_id: workspaceId, ref });
  const transfer = fakeTransfer(FILE_DRAG_TYPE, data);
  fireEvent(g2, drag("dragover", 900, 400, transfer));
  expect(g2.querySelector(".pane-drop")?.getAttribute("data-zone")).toBe("right");
  fireEvent(g2, drag("drop", 900, 400, transfer));
  expect(onDrop).toHaveBeenCalledTimes(1);
  expect(onDrop).toHaveBeenCalledWith("g2", { kind: "file", workspaceId, ref }, "right");
});

test("a pane's edge offers a split only where the pane keeps its own floor beside the dragged kind's", () => {
  const file: PaneGroup = { kind: "group", id: "g3", tabs: ["f1"], active: "f1" };
  const panes = stubPanes({
    root: rowSplit("s1", [group("g1"), group("g2"), file]),
    focused: "g1",
  });
  const { container } = renderShell({ panes });
  const chatTransfer = fakeTransfer(CHAT_DRAG_TYPE, PANE_UUID);
  const fileData = JSON.stringify({ workspace_id: "ws1", ref: "src/a.ts" });
  const fileTransfer = fakeTransfer(FILE_DRAG_TYPE, fileData);
  /** The zone a drag of `transfer` near `edge` of pane `id`, sized `width` by `height`, shows. */
  const zoneAt = (
    id: string,
    width: number,
    height: number,
    edge: "right" | "bottom",
    transfer: DataTransfer,
  ) => {
    const section = container.querySelector(`[data-pane="${id}"]`) as HTMLElement;
    section.getBoundingClientRect = () => ({ left: 0, top: 0, width, height }) as DOMRect;
    const [x, y] = edge === "right" ? [width - 1, height / 2] : [width / 2, height - 1];
    fireEvent(section, drag("dragover", x, y, transfer));
    return section.querySelector(".pane-drop")?.getAttribute("data-zone");
  };
  const chatAndFile = PANE_MIN.width + FILE_PANE_MIN.width; // 740
  const chatAndFileTall = PANE_MIN.height + FILE_PANE_MIN.height; // 400
  // A chat pane takes a file beside it at 420 + 320, a chat only at 420 + 420.
  expect(zoneAt("g2", chatAndFile - 1, 2000, "right", fileTransfer)).toBe("center");
  expect(zoneAt("g2", chatAndFile, 2000, "right", fileTransfer)).toBe("right");
  expect(zoneAt("g2", chatAndFile, 2000, "right", chatTransfer)).toBe("center");
  expect(zoneAt("g2", 2 * PANE_MIN.width, 2000, "right", chatTransfer)).toBe("right");
  expect(zoneAt("g2", 2000, chatAndFileTall - 1, "bottom", fileTransfer)).toBe("center");
  expect(zoneAt("g2", 2000, chatAndFileTall, "bottom", fileTransfer)).toBe("bottom");
  // A file pane takes a file at two file floors, a chat at 320 + 420.
  expect(zoneAt("g3", 2 * FILE_PANE_MIN.width, 2000, "right", fileTransfer)).toBe("right");
  expect(zoneAt("g3", chatAndFile - 1, 2000, "right", chatTransfer)).toBe("center");
  expect(zoneAt("g3", chatAndFile, 2000, "right", chatTransfer)).toBe("right");
  expect(zoneAt("g3", 2000, chatAndFileTall, "bottom", chatTransfer)).toBe("bottom");
});
