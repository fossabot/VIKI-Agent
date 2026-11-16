/**
 * Composer wired to a real SlashMenu: the seam no frozen acceptance test reaches.
 * m2c2/slashMenu.accept.test.tsx mounts SlashMenu standalone; m2c/composer.accept.test.tsx and
 * m2c2/stop.accept.test.tsx mount Composer without `commands`, so `hasCommands` is false and the
 * menu never opens in either. That gap is exactly where three bugs lived — three ways a
 * user's own Enter, Shift+Enter or IME-closing Enter got hijacked by SlashMenu's window listener
 * the moment a real command list made the menu mountable. This file pins the wiring so a
 * regression there fails a test instead of waiting for Composer to be wired into App and a
 * user to notice.
 *
 * Uses `container.querySelector` throughout, not `screen.getByText`, on purpose: with more than
 * one row on screen SlashMenu splits the matched run into its own `.hl` element (see
 * SlashMenu.tsx), which breaks `getByText`'s direct-text-node match —
 * a real tension, not something this file should also trip over.
 */
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { Composer } from "../../components/Composer/Composer";
import type { Command } from "../../protocol";

afterEach(cleanup);

const command = (name: string, skill: boolean, description = ""): Command => ({
  name,
  description,
  argument_hint: "",
  skill,
});

// "re" deliberately matches all three ("research", "retro" as skills; "recap" as a plain
// command) so the Shift+Enter and IME tests below reproduce the multi-row case the findings
// were filed against, not a single-row menu that never exercised SlashMenu's own Enter branch.
const COMMANDS: Command[] = [
  command("research", true, "Deep-dive a topic on the web"),
  command("retro", true, "VIKI's self-improvement loop"),
  command("recap", false, "Generate a one-line session recap now"),
];

function mount(props: Partial<Parameters<typeof Composer>[0]> = {}) {
  const onSend = vi.fn(async () => true);
  const onStop = vi.fn();
  const { container, rerender } = render(
    <Composer running={false} onSend={onSend} onStop={onStop} commands={COMMANDS} {...props} />,
  );
  const box = container.querySelector("textarea") as HTMLTextAreaElement;
  return { container, box, onSend, onStop, rerender };
}

test("a bullet the menu can't match still sends on Enter (M2-c2-R1-01, PD1)", async () => {
  const { container, box, onSend } = mount();
  fireEvent.change(box, { target: { value: "・牛乳" } });
  expect(container.querySelector(".menu-note")).not.toBeNull(); // menu open, zero rows
  fireEvent.keyDown(box, { key: "Enter" });
  expect(onSend).toHaveBeenCalledWith("・牛乳");
  await act(async () => {});
  expect(box.value).toBe("");
});

test("Shift+Enter with the menu open is left for the textarea, never a pick (M2-c2-R1-02)", () => {
  const { container, box, onSend } = mount();
  fireEvent.change(box, { target: { value: "/re" } });
  expect(container.querySelectorAll(".mi").length).toBe(3); // multi-row: SlashMenu's Enter branch is live
  const notCancelled = fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
  expect(notCancelled).toBe(true); // nobody called preventDefault; a real browser inserts the newline
  expect(onSend).not.toHaveBeenCalled();
  expect(box.value).toBe("/re"); // unchanged — picking is the only rewrite, and this wasn't one
  expect(container.querySelector(".menu")).not.toBeNull(); // still open, untouched
});

test("the IME's own Enter leaves the text alone even with the menu open (M2-c2-R1-03)", () => {
  const { container, box, onSend } = mount();
  fireEvent.change(box, { target: { value: "/re" } });
  expect(container.querySelectorAll(".mi").length).toBe(3);
  fireEvent.keyDown(box, { key: "Enter", keyCode: 229 });
  expect(box.value).toBe("/re"); // not rewritten to "/research " (the first row, picked by the bug)
  expect(onSend).not.toHaveBeenCalled();
});

test("picking still rewrites to '/<name> ' and closes the menu", () => {
  const { container, box } = mount();
  fireEvent.change(box, { target: { value: "/retro" } }); // one match: "retro" alone
  expect(container.querySelectorAll(".mi").length).toBe(1);
  fireEvent.keyDown(box, { key: "Enter" });
  expect(box.value).toBe("/retro ");
  expect(container.querySelector(".menu")).toBeNull(); // the trailing space closed it
});

test("Escape closes the menu for that exact text only", () => {
  const { container, box } = mount();
  fireEvent.change(box, { target: { value: "/re" } });
  fireEvent.keyDown(box, { key: "Escape" });
  expect(container.querySelector(".menu")).toBeNull();
  fireEvent.change(box, { target: { value: "/rec" } }); // new text re-evaluates from scratch
  expect(container.querySelector(".menu")).not.toBeNull();
});
