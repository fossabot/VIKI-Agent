import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { textDelta, turnStarted, view } from "../../accept/m2b/fakes";
import type { HandsFreeComposerEvent, HandsFreeState } from "../../voice/handsFree";
import type { UseHandsFree } from "../../voice/useHandsFree";
import { Conversation } from "../Conversation/Conversation";
import { LauncherChat, type LauncherChatProps } from "./LauncherChat";

afterEach(cleanup);

const NOW = Date.parse("2026-10-21T14:02:00.000Z");

/** Gap 2's own fake layout (scrollBehavior.unit.test.tsx's helper): a real browser gives
 * `.dloop-chat-messages` its `max-height`-bounded `scrollHeight`/`clientHeight`; jsdom reports 0
 * for both unless told otherwise. */
function hostMetrics(host: HTMLElement, height: number): void {
  Object.defineProperty(host, "scrollHeight", { value: height, configurable: true });
  Object.defineProperty(host, "clientHeight", { value: 400, configurable: true });
}

function mount(overrides: Partial<LauncherChatProps> = {}) {
  const onSend = vi.fn(async () => true);
  const onStop = vi.fn();
  const onNewConversation = vi.fn();
  const onOpenInMain = vi.fn();
  const { container } = render(
    <LauncherChat
      sessionId={null}
      messages={<p>unused while there is no session</p>}
      running={false}
      onSend={onSend}
      onStop={onStop}
      onNewConversation={onNewConversation}
      onOpenInMain={onOpenInMain}
      {...overrides}
    />,
  );
  return { container, onSend, onStop, onNewConversation, onOpenInMain };
}

const HANDS_FREE_OFF: HandsFreeState = {
  phase: "off",
  noticeOpen: false,
  notice: null,
  turnId: null,
};

/** A minimal `UseHandsFree` fake: its own `vi.fn()` handlers so a test can read whether this
 * chat's voiceControl wiring called them. */
function fakeHandsFree(state: HandsFreeState): UseHandsFree {
  return {
    state,
    bridge: {
      active: state.phase !== "off",
      listening: state.phase === "listening",
      dispatch: vi.fn((_event: HandsFreeComposerEvent) => undefined),
    },
    toggle: vi.fn(),
    confirmNotice: vi.fn(),
    cancelNotice: vi.fn(),
    dismissNotice: vi.fn(),
    interrupt: vi.fn(),
  };
}

test("with no session, LauncherChat shows the empty line instead of the caller's messages, and disables both header actions", () => {
  mount({ messages: <p>should not show</p> });
  expect(screen.getByText("Ask VIKI anything.")).toBeTruthy();
  expect(screen.queryByText("should not show")).toBeNull();
  expect(
    (screen.getByRole("button", { name: "New conversation" }) as HTMLButtonElement).disabled,
  ).toBe(true);
  expect(
    (screen.getByRole("button", { name: "Open in main window" }) as HTMLButtonElement).disabled,
  ).toBe(true);
});

test("with a session, LauncherChat renders the caller's messages, not the empty line, and enables both header actions", () => {
  mount({ sessionId: "sess-1", messages: <p>hello from the transcript</p> });
  expect(screen.getByText("hello from the transcript")).toBeTruthy();
  expect(screen.queryByText("Ask VIKI anything.")).toBeNull();
  expect(
    (screen.getByRole("button", { name: "New conversation" }) as HTMLButtonElement).disabled,
  ).toBe(false);
  expect(
    (screen.getByRole("button", { name: "Open in main window" }) as HTMLButtonElement).disabled,
  ).toBe(false);
});

test("New conversation and Open in main window call their own prop once enabled", () => {
  const { onNewConversation, onOpenInMain } = mount({ sessionId: "sess-1", messages: <p>hi</p> });
  fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
  fireEvent.click(screen.getByRole("button", { name: "Open in main window" }));
  expect(onNewConversation).toHaveBeenCalledTimes(1);
  expect(onOpenInMain).toHaveBeenCalledTimes(1);
});

test("the composer underneath is labelled Message VIKI, not Composer's own default", () => {
  const { container } = mount();
  expect(container.querySelector("textarea")?.getAttribute("aria-label")).toBe("Message VIKI");
});

test("a typed message is sent through the given onSend", () => {
  const { container, onSend } = mount();
  const box = container.querySelector("textarea") as HTMLTextAreaElement;
  fireEvent.change(box, { target: { value: "hello VIKI" } });
  fireEvent.keyDown(box, { key: "Enter" });
  expect(onSend).toHaveBeenCalledWith("hello VIKI");
});

test("while running, the composer shows Stop, which calls the given onStop", () => {
  const { onStop } = mount({ running: true });
  fireEvent.click(screen.getByRole("button", { name: "Stop" }));
  expect(onStop).toHaveBeenCalledTimes(1);
});

// With a real Conversation as `messages` (as App.tsx's renderMessagesFor
// hands it in), the messages box is the scroll host Conversation's own auto-follow expects.
test("a new reply auto-scrolls the messages box to the bottom when it was already there", () => {
  const onSend = vi.fn(async () => true);
  const first = view([turnStarted, textDelta("one")]);
  const { container, rerender } = render(
    <LauncherChat
      sessionId="sess-1"
      messages={<Conversation view={first} nowMs={NOW} onOpenUrl={vi.fn()} />}
      running={false}
      onSend={onSend}
      onStop={vi.fn()}
      onNewConversation={vi.fn()}
      onOpenInMain={vi.fn()}
    />,
  );
  const host = container.querySelector(".dloop-chat-messages") as HTMLElement;
  hostMetrics(host, 1000);
  rerender(
    <LauncherChat
      sessionId="sess-1"
      messages={<Conversation view={first} nowMs={NOW} onOpenUrl={vi.fn()} />}
      running={false}
      onSend={onSend}
      onStop={vi.fn()}
      onNewConversation={vi.fn()}
      onOpenInMain={vi.fn()}
    />,
  );
  expect(host.scrollTop).toBe(1000 - 400);

  // A reply lands, growing the content the way VIKI's answer does in the panel.
  Object.defineProperty(host, "scrollHeight", { value: 1900, configurable: true });
  rerender(
    <LauncherChat
      sessionId="sess-1"
      messages={
        <Conversation view={view([textDelta(" two")], first)} nowMs={NOW} onOpenUrl={vi.fn()} />
      }
      running={false}
      onSend={onSend}
      onStop={vi.fn()}
      onNewConversation={vi.fn()}
      onOpenInMain={vi.fn()}
    />,
  );
  expect(host.scrollTop).toBe(1900 - 400);
});

test("a new reply leaves the messages box alone when the user had scrolled up", () => {
  const onSend = vi.fn(async () => true);
  const first = view([turnStarted, textDelta("one")]);
  const { container, rerender } = render(
    <LauncherChat
      sessionId="sess-1"
      messages={<Conversation view={first} nowMs={NOW} onOpenUrl={vi.fn()} />}
      running={false}
      onSend={onSend}
      onStop={vi.fn()}
      onNewConversation={vi.fn()}
      onOpenInMain={vi.fn()}
    />,
  );
  const host = container.querySelector(".dloop-chat-messages") as HTMLElement;
  hostMetrics(host, 1000);
  rerender(
    <LauncherChat
      sessionId="sess-1"
      messages={<Conversation view={first} nowMs={NOW} onOpenUrl={vi.fn()} />}
      running={false}
      onSend={onSend}
      onStop={vi.fn()}
      onNewConversation={vi.fn()}
      onOpenInMain={vi.fn()}
    />,
  );
  host.scrollTop = 100;
  host.dispatchEvent(new Event("scroll"));
  Object.defineProperty(host, "scrollHeight", { value: 1900, configurable: true });

  rerender(
    <LauncherChat
      sessionId="sess-1"
      messages={
        <Conversation view={view([textDelta(" two")], first)} nowMs={NOW} onOpenUrl={vi.fn()} />
      }
      running={false}
      onSend={onSend}
      onStop={vi.fn()}
      onNewConversation={vi.fn()}
      onOpenInMain={vi.fn()}
    />,
  );
  expect(host.scrollTop).toBe(100);
});

// The composer's voice controls, wired only when the caller gives a handsFree loop.
test("without handsFree, the composer shows no Hands-free button", () => {
  const { container } = mount();
  expect(container.querySelector(".hands-free-voice")).toBeNull();
  expect(screen.queryByRole("button", { name: "Hands-free" })).toBeNull();
});

test("with handsFree and voiceShortcut, the composer's Hands-free button carries the shortcut and its tip", () => {
  mount({
    handsFree: fakeHandsFree(HANDS_FREE_OFF),
    voiceShortcut: { aria: "Alt+T", label: "Alt+T" },
  });
  const button = screen.getByRole("button", { name: "Hands-free" });
  expect(button.getAttribute("aria-keyshortcuts")).toBe("Alt+T");
  expect(screen.getByRole("tooltip").textContent).toBe("Start voice conversation (Alt+T)");
});

test("clicking the composed Hands-free button calls handsFree's own toggle", () => {
  const handsFree = fakeHandsFree(HANDS_FREE_OFF);
  mount({ handsFree });
  fireEvent.click(screen.getByRole("button", { name: "Hands-free" }));
  expect(handsFree.toggle).toHaveBeenCalledTimes(1);
});

test("with handsFree listening, the strip between the messages box and the composer shows Listening and End", () => {
  const { container } = mount({
    handsFree: fakeHandsFree({ ...HANDS_FREE_OFF, phase: "listening" }),
  });
  const strip = container.querySelector(".hands-free-strip");
  expect(strip?.textContent).toContain("Listening");
  expect(strip?.querySelector(".hands-free-end")).toBeTruthy();
});

test("with handsFree's first-use notice open, the chat offers Start hands-free", () => {
  mount({ handsFree: fakeHandsFree({ ...HANDS_FREE_OFF, noticeOpen: true }) });
  expect(screen.getByRole("button", { name: "Start hands-free" })).toBeTruthy();
});
