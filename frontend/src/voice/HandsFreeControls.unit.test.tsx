/**
 * HandsFreeControls: the toggle, its first-use notice and the strip, which also
 * shows the one-line notice the loop leaves behind when Enter is refused or the mode exits
 * mid-loop (without it the toggle looked dead with nothing on screen explaining why).
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { HandsFreeNotice, HandsFreeStrip, HandsFreeToggle } from "./HandsFreeControls";
import type {
  HandsFreeComposerEvent,
  HandsFreeState,
  HandsFreeNotice as NoticeReason,
} from "./handsFree";
import type { UseHandsFree } from "./useHandsFree";

afterEach(() => {
  cleanup();
});

const OFF: HandsFreeState = { phase: "off", noticeOpen: false, notice: null, turnId: null };

function hf(
  state: HandsFreeState,
  handlers: Partial<
    Pick<
      UseHandsFree,
      | "toggle"
      | "confirmNotice"
      | "cancelNotice"
      | "dismissNotice"
      | "interrupt"
      | "note"
      | "preparing"
    >
  > = {},
): UseHandsFree {
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
    ...handlers,
  };
}

test("the toggle names itself and tracks the phase with aria-pressed", () => {
  const toggle = vi.fn();
  const { rerender } = render(<HandsFreeToggle handsFree={hf(OFF, { toggle })} />);
  const button = () => screen.getByRole("button", { name: "Hands-free" });
  expect(button().getAttribute("aria-pressed")).toBe("false");
  fireEvent.click(button());
  expect(toggle).toHaveBeenCalledTimes(1);
  rerender(<HandsFreeToggle handsFree={hf({ ...OFF, phase: "listening" }, { toggle })} />);
  expect(button().getAttribute("aria-pressed")).toBe("true");
});

const SHORTCUT = { aria: "Alt+T", label: "Alt+T" };

const TIP_CASES: ReadonlyArray<{
  name: string;
  phase: HandsFreeState["phase"];
  shortcut?: { aria: string; label: string };
  tip: string;
  ariaKeyshortcuts: string | null;
}> = [
  {
    name: "off, no shortcut",
    phase: "off",
    tip: "Start voice conversation",
    ariaKeyshortcuts: null,
  },
  {
    name: "listening, no shortcut",
    phase: "listening",
    tip: "End voice conversation (Esc)",
    ariaKeyshortcuts: null,
  },
  {
    name: "off, with shortcut",
    phase: "off",
    shortcut: SHORTCUT,
    tip: "Start voice conversation (Alt+T)",
    ariaKeyshortcuts: "Alt+T",
  },
  {
    name: "listening, with shortcut",
    phase: "listening",
    shortcut: SHORTCUT,
    tip: "End voice conversation (Esc or Alt+T)",
    ariaKeyshortcuts: "Alt+T",
  },
];

test.each(TIP_CASES)(
  "the toggle's tip and aria-keyshortcuts, $name",
  ({ phase, shortcut, tip, ariaKeyshortcuts }) => {
    const { container } = render(
      <HandsFreeToggle handsFree={hf({ ...OFF, phase })} {...(shortcut ? { shortcut } : {})} />,
    );
    expect(
      screen.getByRole("button", { name: "Hands-free" }).getAttribute("aria-keyshortcuts"),
    ).toBe(ariaKeyshortcuts);
    expect(container.querySelector(".hands-free-tip")?.textContent).toBe(tip);
  },
);

test("the tip puts the shortcut ahead of the engine suffix", () => {
  render(
    <HandsFreeToggle
      handsFree={hf({ ...OFF, phase: "listening" })}
      shortcut={SHORTCUT}
      engine="natural voice"
    />,
  );
  expect(screen.getByRole("tooltip").textContent).toBe(
    "End voice conversation (Esc or Alt+T) — natural voice",
  );
});

test("the first-use notice renders nothing when it is not open", () => {
  render(<HandsFreeNotice handsFree={hf(OFF)} />);
  expect(screen.queryByRole("group")).toBeNull();
});

test("the first-use notice focuses its confirm on open, and Confirm/Cancel call back", () => {
  const confirmNotice = vi.fn();
  const cancelNotice = vi.fn();
  render(
    <HandsFreeNotice
      handsFree={hf({ ...OFF, noticeOpen: true }, { confirmNotice, cancelNotice })}
    />,
  );
  const confirm = screen.getByRole("button", { name: "Start hands-free" });
  expect(document.activeElement).toBe(confirm);
  fireEvent.click(confirm);
  expect(confirmNotice).toHaveBeenCalledTimes(1);
  const cancel = screen.getByRole("button", { name: "Cancel" });
  // Not a bare UA button: a styled class, same fix ReadAloud.tsx's own notice took.
  expect(cancel.className).toContain("hands-free-notice-cancel");
  fireEvent.click(cancel);
  expect(cancelNotice).toHaveBeenCalledTimes(1);
});

test("the strip names each phase, and only Speaking offers the interrupt", () => {
  const interrupt = vi.fn();
  const toggle = vi.fn();
  const { container, rerender } = render(
    <HandsFreeStrip handsFree={hf({ ...OFF, phase: "listening" }, { interrupt, toggle })} />,
  );
  expect(screen.getByRole("status").textContent).toContain("Listening");
  expect(container.querySelector(".hands-free-interrupt")).toBeNull();
  rerender(<HandsFreeStrip handsFree={hf({ ...OFF, phase: "speaking" }, { interrupt, toggle })} />);
  expect(screen.getByRole("status").textContent).toContain("Speaking");
  fireEvent.click(container.querySelector(".hands-free-interrupt") as HTMLButtonElement);
  expect(interrupt).toHaveBeenCalledTimes(1);
  fireEvent.click(container.querySelector(".hands-free-end") as HTMLButtonElement);
  expect(toggle).toHaveBeenCalledTimes(1);
});

test("the strip says Getting the voice ready and offers the interrupt", () => {
  const interrupt = vi.fn();
  const { container } = render(
    <HandsFreeStrip
      handsFree={hf({ ...OFF, phase: "thinking" }, { interrupt, preparing: true })}
    />,
  );
  expect(screen.getByRole("status").textContent).toContain("Getting the voice ready");
  fireEvent.click(container.querySelector(".hands-free-interrupt") as HTMLButtonElement);
  expect(interrupt).toHaveBeenCalledTimes(1);
});

test("thinking without a voice being prepared keeps Thinking and no interrupt", () => {
  const { container } = render(<HandsFreeStrip handsFree={hf({ ...OFF, phase: "thinking" })} />);
  expect(screen.getByRole("status").textContent).toContain("Thinking");
  expect(container.querySelector(".hands-free-interrupt")).toBeNull();
});

test("the strip shows the note while on", () => {
  const { container } = render(
    <HandsFreeStrip handsFree={hf({ ...OFF, phase: "listening" }, { note: "voice_fallback" })} />,
  );
  expect(container.querySelector(".hands-free-note")?.textContent).toBe(
    "The chosen voice did not answer, so the system voice read this reply.",
  );
});

test('the strip shows "AI voice" in listening and speaking, but not off with a notice', () => {
  const { container, rerender } = render(
    <HandsFreeStrip handsFree={hf({ ...OFF, phase: "listening" })} />,
  );
  const chip = container.querySelector(".chip.dim");
  expect(chip?.tagName).toBe("SPAN");
  expect(chip?.textContent).toBe("AI voice");
  rerender(<HandsFreeStrip handsFree={hf({ ...OFF, phase: "speaking" })} />);
  expect(container.querySelector(".chip.dim")?.textContent).toBe("AI voice");
  rerender(<HandsFreeStrip handsFree={hf({ ...OFF, notice: "nothing_heard" })} />);
  expect(container.querySelector(".chip.dim")).toBeNull();
});

test("off with no notice renders nothing", () => {
  render(<HandsFreeStrip handsFree={hf(OFF)} />);
  expect(screen.queryByRole("status")).toBeNull();
});

const NOTICE_TEXT: Record<NoticeReason, string> = {
  dictation_unavailable: "Dictation is not ready, so hands-free did not start.",
  no_voice: "No local English voice is installed, so hands-free did not start.",
  nothing_heard: "Hands-free heard nothing, so it stopped.",
  send_blocked: "The message could not be sent, so hands-free stopped.",
  turn_failed: "The reply failed, so hands-free stopped.",
  turn_stopped: "The reply was stopped, so hands-free stopped.",
  speech_failed: "Reading the reply aloud failed, so hands-free stopped.",
  window_hidden: "Hands-free stopped because this window was covered.",
  session_switched: "Hands-free stopped because you switched chats.",
};

test.each(Object.entries(NOTICE_TEXT) as [NoticeReason, string][])(
  "a %s notice shows its text, and Dismiss calls dismissNotice",
  (reason, text) => {
    const dismissNotice = vi.fn();
    render(<HandsFreeStrip handsFree={hf({ ...OFF, notice: reason }, { dismissNotice })} />);
    expect(screen.getByRole("status").textContent).toContain(text);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(dismissNotice).toHaveBeenCalledTimes(1);
  },
);
