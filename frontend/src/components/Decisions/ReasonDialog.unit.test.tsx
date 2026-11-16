import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { NOTE_MAX } from "./approvalKinds";
import { ReasonDialog } from "./ReasonDialog";

afterEach(() => {
  cleanup();
});

function mount(over: Partial<Parameters<typeof ReasonDialog>[0]> = {}) {
  const props = {
    title: "title a",
    noteLabel: "Reason (optional)",
    confirmLabel: "Reject",
    onConfirm: vi.fn(),
    onCancel: vi.fn(),
    ...over,
  };
  const view = render(<ReasonDialog {...props} />);
  return { ...view, props };
}

test("renders the title, note label and confirm label, with the input capped at NOTE_MAX", () => {
  mount();
  const dialog = screen.getByRole("dialog");
  expect(dialog.getAttribute("aria-modal")).toBe("true");
  expect(screen.getByText("title a")).toBeTruthy();
  const input = screen.getByLabelText("Reason (optional)") as HTMLInputElement;
  expect(input.maxLength).toBe(NOTE_MAX);
  expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "Cancel" })).toBeTruthy();
});

test("starts with focus on the note input", () => {
  mount();
  expect(document.activeElement).toBe(screen.getByLabelText("Reason (optional)"));
});

test("Confirm sends the note trimmed and with internal whitespace collapsed", () => {
  const { props } = mount({
    noteLabel: "What to change (optional)",
    confirmLabel: "Approve with edits",
  });
  fireEvent.change(screen.getByLabelText("What to change (optional)"), {
    target: { value: "  please   shorten   this  " },
  });
  fireEvent.click(screen.getByRole("button", { name: "Approve with edits" }));
  expect(props.onConfirm).toHaveBeenCalledExactlyOnceWith("please shorten this");
  expect(props.onCancel).not.toHaveBeenCalled();
});

test("a blank note confirms as an empty string", () => {
  const { props } = mount();
  fireEvent.click(screen.getByRole("button", { name: "Reject" }));
  expect(props.onConfirm).toHaveBeenCalledExactlyOnceWith("");
});

test("Enter in the input submits like the confirm button", () => {
  const { props } = mount();
  const input = screen.getByLabelText("Reason (optional)");
  fireEvent.change(input, { target: { value: "too noisy" } });
  fireEvent.keyDown(input, { key: "Enter" });
  expect(props.onConfirm).toHaveBeenCalledExactlyOnceWith("too noisy");
});

test("a composing Enter (IME) does not submit", () => {
  const { props } = mount();
  const input = screen.getByLabelText("Reason (optional)");
  fireEvent.change(input, { target: { value: "still composing" } });
  fireEvent.keyDown(input, { key: "Enter", keyCode: 229, isComposing: true });
  expect(props.onConfirm).not.toHaveBeenCalled();
});

test("Cancel calls onCancel, never onConfirm", () => {
  const { props } = mount();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(props.onCancel).toHaveBeenCalledOnce();
  expect(props.onConfirm).not.toHaveBeenCalled();
});

test("Escape dismisses it through useModalFocus, as onCancel", () => {
  const { props } = mount();
  fireEvent.keyDown(window, { key: "Escape" });
  expect(props.onCancel).toHaveBeenCalledOnce();
});
