/**
 * Acceptance (frozen): the composer while a turn runs. Stop is the user's brake on a process
 * acting in their vault, so it must be visible and reachable the moment a turn starts —
 * never hidden behind a menu or a disabled control.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { Composer } from "../../components/Composer/Composer";

afterEach(cleanup);

function mount(running: boolean) {
  const onSend = vi.fn();
  const onStop = vi.fn();
  const { container, rerender } = render(
    <Composer running={running} onSend={onSend} onStop={onStop} />,
  );
  const box = container.querySelector("textarea") as HTMLTextAreaElement;
  return { container, box, onSend, onStop, rerender };
}

test("idle shows the round Send button and no Stop", () => {
  const { container } = mount(false);
  expect(screen.getByRole("button", { name: "Send" })).toBeTruthy();
  expect(container.querySelector(".composer-stop")).toBeNull();
});

test("a running turn replaces Send with the Stop pill, which names its shortcut", () => {
  const { container } = mount(true);
  const stop = container.querySelector(".composer-stop") as HTMLButtonElement;
  expect(stop).not.toBeNull();
  expect(stop.textContent).toContain("Stop");
  expect(stop.querySelector(".kbd")?.textContent).toBe("Esc");
  expect(stop.disabled).toBe(false);
  // The mock's running composer has no send button at all; a disabled one would read as a
  // dead control next to a live one.
  expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
});

test("clicking Stop asks once, the same call Esc makes", () => {
  const { container, onStop } = mount(true);
  const stop = container.querySelector(".composer-stop") as HTMLElement;
  fireEvent.click(stop);
  expect(onStop).toHaveBeenCalledTimes(1);
});

test("the box stays usable while a turn runs: the draft survives and Enter still sends nothing", () => {
  const { box, onSend, onStop, rerender } = mount(false);
  fireEvent.change(box, { target: { value: "next question" } });
  rerender(<Composer running={true} onSend={onSend} onStop={onStop} />);
  fireEvent.keyDown(box, { key: "Enter" });
  expect(onSend).not.toHaveBeenCalled();
  expect(box.value).toBe("next question");
  expect(box.disabled).toBe(false);
});

test("the turn ending brings Send back and takes Stop away", () => {
  const { container, onSend, onStop, rerender } = mount(true);
  rerender(<Composer running={false} onSend={onSend} onStop={onStop} />);
  expect(container.querySelector(".composer-stop")).toBeNull();
  expect(screen.getByRole("button", { name: "Send" })).toBeTruthy();
});
