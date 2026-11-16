/**
 * Acceptance (frozen): the composer's input mechanics, and above all the IME contract —
 * the user writes Japanese, so an Enter that closes a candidate window must never send.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { Composer } from "../../components/Composer/Composer";

afterEach(cleanup);

function mount(props: Partial<Parameters<typeof Composer>[0]> = {}) {
  const onSend = vi.fn();
  const onStop = vi.fn();
  const { container, rerender } = render(
    <Composer running={false} onSend={onSend} onStop={onStop} {...props} />,
  );
  const box = container.querySelector("textarea") as HTMLTextAreaElement;
  return { container, box, onSend, onStop, rerender };
}

function type(box: HTMLTextAreaElement, text: string): void {
  fireEvent.change(box, { target: { value: text } });
}

test("Enter retains input until its local handoff resolves, then clears unchanged text", async () => {
  let resolve!: (value: boolean) => void;
  const pending = new Promise<boolean>((done) => {
    resolve = done;
  });
  const onSend = vi.fn(() => pending);
  const { box } = mount({ onSend });
  type(box, "hi");
  fireEvent.keyDown(box, { key: "Enter" });
  expect(onSend).toHaveBeenCalledWith("hi");
  expect(box.value).toBe("hi");
  await act(async () => {
    resolve(true);
    await pending;
  });
  expect(box.value).toBe("");
});

test("the Enter that closes an IME candidate window does not send", () => {
  const { box, onSend } = mount();
  type(box, "牛乳");
  fireEvent.keyDown(box, { key: "Enter", keyCode: 229 });
  fireEvent.keyDown(box, { key: "Process", keyCode: 229 });
  expect(onSend).not.toHaveBeenCalled();
  // isComposing on its own is enough, even when the browser reports a normal keyCode.
  fireEvent.keyDown(box, { key: "Enter", isComposing: true });
  expect(onSend).not.toHaveBeenCalled();
  fireEvent.keyDown(box, { key: "Enter" });
  expect(onSend).toHaveBeenCalledWith("牛乳");
});

test("Shift+Enter writes a newline instead of sending", () => {
  const { box, onSend } = mount();
  type(box, "one");
  fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
  expect(onSend).not.toHaveBeenCalled();
});

test("a bulleted Japanese list is sent byte for byte", () => {
  const { box, onSend } = mount();
  type(box, "・牛乳\n・卵");
  fireEvent.keyDown(box, { key: "Enter" });
  expect(onSend).toHaveBeenCalledWith("・牛乳\n・卵");
});

test("an empty or blank box sends nothing", () => {
  const { box, onSend } = mount();
  fireEvent.keyDown(box, { key: "Enter" });
  type(box, "   \n  ");
  fireEvent.keyDown(box, { key: "Enter" });
  expect(onSend).not.toHaveBeenCalled();
});

test("while a turn runs the box keeps the draft and sends nothing", () => {
  const { box, onSend, rerender, onStop } = mount();
  type(box, "next question");
  rerender(<Composer running={true} onSend={onSend} onStop={onStop} />);
  fireEvent.keyDown(box, { key: "Enter" });
  expect(onSend).not.toHaveBeenCalled();
  expect(box.value).toBe("next question");
  /* AMENDMENT (2026-09-13). This line read
     `expect(screen.getByRole("button", { name: "Send" }).disabled).toBe(true)`. It was written
     before the running composer's design settled: it shows the Stop pill
     where the round Send button sits and no Send at all — a disabled control next to a live one
     is not the design. The behaviour this test is about is unchanged: Enter sends
     nothing and the draft survives. What Send and Stop look like while running is now pinned by
     src/accept/m2c2/stop.accept.test.tsx. */
  expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
});

test("Esc stops a running turn, but never while composing and never when idle", () => {
  const { box, onStop, onSend, rerender } = mount();
  fireEvent.keyDown(box, { key: "Escape" });
  expect(onStop).not.toHaveBeenCalled(); // nothing is running
  rerender(<Composer running={true} onSend={onSend} onStop={onStop} />);
  fireEvent.keyDown(box, { key: "Escape", isComposing: true });
  expect(onStop).not.toHaveBeenCalled();
  fireEvent.keyDown(box, { key: "Escape" });
  expect(onStop).toHaveBeenCalledTimes(1);
});

test("ArrowUp recalls the last prompt only into an empty box", () => {
  const { box } = mount({ lastPrompt: "the last one" });
  fireEvent.keyDown(box, { key: "ArrowUp" });
  expect(box.value).toBe("the last one");
  type(box, "something else");
  fireEvent.keyDown(box, { key: "ArrowUp" });
  expect(box.value).toBe("something else");
});

test("ArrowUp while composing leaves the box alone", () => {
  const { box } = mount({ lastPrompt: "the last one" });
  fireEvent.keyDown(box, { key: "ArrowUp", isComposing: true });
  expect(box.value).toBe("");
});

test("a new draft replaces the box; the same one does not fight the owner's typing", () => {
  const { box, onSend, onStop, rerender } = mount();
  rerender(<Composer running={false} onSend={onSend} onStop={onStop} draft="About H1" />);
  expect(box.value).toBe("About H1");
  type(box, "About H1: should we ship it?");
  rerender(<Composer running={false} onSend={onSend} onStop={onStop} draft="About H1" />);
  expect(box.value).toBe("About H1: should we ship it?");
});

test("the box grows with its content and stops at 40vh", () => {
  const { box } = mount();
  Object.defineProperty(box, "scrollHeight", { value: 200, configurable: true });
  type(box, "a\nb\nc\nd\ne");
  expect(box.style.height).toBe("200px");
  Object.defineProperty(box, "scrollHeight", { value: 10_000, configurable: true });
  type(box, "very long");
  expect(box.style.height).toBe(`${Math.round(window.innerHeight * 0.4)}px`);
});

test("the mock's chrome is still there: the placeholder, the mic and the send button", () => {
  const { box, container } = mount();
  expect(box.getAttribute("placeholder")).toBe("Talk to VIKI… / for skills");
  expect(container.querySelector(".composer-mic")).not.toBeNull();
  expect(screen.getByRole("button", { name: "Send" })).toBeTruthy();
});
