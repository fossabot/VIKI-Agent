import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { SkillProposalActions, type SkillProposalActionsProps } from "./SkillProposalActions";

afterEach(cleanup);

function setup(over: Partial<SkillProposalActionsProps> = {}) {
  const onApprove = vi.fn();
  const onEdit = vi.fn();
  const onReject = vi.fn();
  const onWithdraw = vi.fn();
  const onDirty = vi.fn();
  const props: SkillProposalActionsProps = {
    state: "open",
    text: "original",
    enabled: true,
    busy: false,
    onApprove,
    onEdit,
    onReject,
    onWithdraw,
    onDirty,
    leaveEditKey: 0,
    ...over,
  };
  const view = render(<SkillProposalActions {...props} />);
  return { ...view, props, onApprove, onEdit, onReject, onWithdraw, onDirty };
}

const button = (name: string) => screen.getByRole("button", { name }) as HTMLButtonElement;
const idleButtons = ["Approve", "Edit", "Reject", "Withdraw"];

test.each([
  ["open", true, true, true, true],
  ["handed", true, true, true, true],
  ["applied", false, false, false, true],
  ["rejected", false, true, false, true],
  ["stale", false, true, true, true],
  ["source_missing", false, true, true, true],
  ["unknown", false, true, true, true],
] as const)(
  "state %s: approve enabled %s, edit %s, reject %s, withdraw %s",
  (state, approve, edit, reject, withdraw) => {
    setup({ state });
    expect(button("Approve").disabled).toBe(!approve);
    expect(button("Edit").disabled).toBe(!edit);
    expect(button("Reject").disabled).toBe(!reject);
    expect(button("Withdraw").disabled).toBe(!withdraw);
  },
);

test("enabled=false disables all four idle buttons regardless of state", () => {
  setup({ enabled: false, state: "open" });
  for (const name of idleButtons) expect(button(name).disabled).toBe(true);
});

test("busy disables all four idle buttons regardless of state", () => {
  setup({ busy: true, state: "open" });
  for (const name of idleButtons) expect(button(name).disabled).toBe(true);
});

test("Approve calls onApprove directly and stays in idle mode", () => {
  const { onApprove } = setup({ state: "handed" });
  fireEvent.click(button("Approve"));
  expect(onApprove).toHaveBeenCalledOnce();
  for (const name of idleButtons) expect(screen.getByRole("button", { name })).toBeTruthy();
});

test("edit mode starts from text, gates Save changes, and saves the typed value", () => {
  const { onEdit } = setup({ text: "original" });
  fireEvent.click(button("Edit"));
  const textarea = screen.getByLabelText("SKILL.md") as HTMLTextAreaElement;
  expect(textarea.value).toBe("original");
  expect(textarea.rows).toBe(18);
  expect(textarea.getAttribute("spellcheck")).toBe("false");
  expect(button("Save changes").disabled).toBe(true); // unchanged
  fireEvent.change(textarea, { target: { value: "   " } });
  expect(button("Save changes").disabled).toBe(true); // blank
  fireEvent.change(textarea, { target: { value: "edited text" } });
  expect(button("Save changes").disabled).toBe(false);
  fireEvent.click(button("Save changes"));
  expect(onEdit).toHaveBeenCalledWith("edited text");
});

test("Save changes is disabled while busy even with a changed, non-blank draft", () => {
  const { rerender, props } = setup({ text: "original", busy: false });
  fireEvent.click(button("Edit"));
  fireEvent.change(screen.getByLabelText("SKILL.md"), { target: { value: "changed" } });
  expect(button("Save changes").disabled).toBe(false);
  rerender(<SkillProposalActions {...props} busy />);
  expect(button("Save changes").disabled).toBe(true);
  expect(button("Cancel").disabled).toBe(false);
});

test("Cancel from edit mode returns to idle without calling onEdit", () => {
  const { onEdit } = setup();
  fireEvent.click(button("Edit"));
  fireEvent.change(screen.getByLabelText("SKILL.md"), { target: { value: "changed" } });
  fireEvent.click(button("Cancel"));
  expect(onEdit).not.toHaveBeenCalled();
  for (const name of idleButtons) expect(screen.getByRole("button", { name })).toBeTruthy();
});

test("reject mode starts blank, caps the reason at 200 chars, and rejects with the trimmed value", () => {
  const { onReject } = setup();
  fireEvent.click(button("Reject"));
  const input = screen.getByLabelText("Reason (optional)") as HTMLInputElement;
  expect(input.value).toBe("");
  expect(input.maxLength).toBe(200);
  fireEvent.change(input, { target: { value: "  not helpful  " } });
  fireEvent.click(button("Reject"));
  expect(onReject).toHaveBeenCalledWith("not helpful");
});

test("reject with a blank or whitespace-only reason sends null", () => {
  const { onReject } = setup();
  fireEvent.click(button("Reject"));
  fireEvent.click(button("Reject"));
  expect(onReject).toHaveBeenCalledWith(null);
});

test("Cancel from reject mode returns to idle without calling onReject", () => {
  const { onReject } = setup();
  fireEvent.click(button("Reject"));
  fireEvent.change(screen.getByLabelText("Reason (optional)"), { target: { value: "x" } });
  fireEvent.click(button("Cancel"));
  expect(onReject).not.toHaveBeenCalled();
  for (const name of idleButtons) expect(screen.getByRole("button", { name })).toBeTruthy();
});

test("the idle Withdraw button opens a confirmation", () => {
  setup();
  fireEvent.click(button("Withdraw"));
  expect(screen.getByText("Withdraw this proposal? The skill file does not change.")).toBeTruthy();
});

test("confirming withdraw calls onWithdraw", () => {
  const { onWithdraw } = setup();
  fireEvent.click(button("Withdraw"));
  fireEvent.click(button("Withdraw"));
  expect(onWithdraw).toHaveBeenCalledOnce();
});

test("Keep it returns to idle without calling onWithdraw", () => {
  const { onWithdraw } = setup();
  fireEvent.click(button("Withdraw"));
  fireEvent.click(button("Keep it"));
  expect(onWithdraw).not.toHaveBeenCalled();
  for (const name of idleButtons) expect(screen.getByRole("button", { name })).toBeTruthy();
});

test("busy disables both withdraw-mode buttons", () => {
  const { rerender, props } = setup();
  fireEvent.click(button("Withdraw"));
  rerender(<SkillProposalActions {...props} busy />);
  expect(button("Withdraw").disabled).toBe(true);
  expect(button("Keep it").disabled).toBe(true);
});

test("a text change while editing discards the draft and returns to idle", () => {
  const { rerender, props, onEdit } = setup({ text: "v1" });
  fireEvent.click(button("Edit"));
  fireEvent.change(screen.getByLabelText("SKILL.md"), { target: { value: "typed" } });
  rerender(<SkillProposalActions {...props} text="v2" />);
  for (const name of idleButtons) expect(screen.getByRole("button", { name })).toBeTruthy();
  expect(screen.queryByLabelText("SKILL.md")).toBeNull();
  expect(onEdit).not.toHaveBeenCalled();
});

test("a state change while rejecting discards the draft and returns to idle", () => {
  const { rerender, props, onReject } = setup({ state: "open" });
  fireEvent.click(button("Reject"));
  rerender(<SkillProposalActions {...props} state="stale" />);
  for (const name of idleButtons) expect(screen.getByRole("button", { name })).toBeTruthy();
  expect(screen.queryByLabelText("Reason (optional)")).toBeNull();
  expect(onReject).not.toHaveBeenCalled();
});

test("a state change while confirming withdraw returns to idle", () => {
  const { rerender, props } = setup({ state: "open" });
  fireEvent.click(button("Withdraw"));
  rerender(<SkillProposalActions {...props} state="applied" />);
  expect(screen.queryByText("Withdraw this proposal? The skill file does not change.")).toBeNull();
  expect(screen.getByRole("button", { name: "Approve" })).toBeTruthy();
});

test("busy or enabled changing alone does not reset an open mode", () => {
  const { rerender, props } = setup({ state: "open", text: "v1" });
  fireEvent.click(button("Withdraw"));
  rerender(<SkillProposalActions {...props} busy enabled={false} />);
  expect(screen.getByText("Withdraw this proposal? The skill file does not change.")).toBeTruthy();
});

test("onDirty reports true once the draft differs from text, false once it matches again", () => {
  const { onDirty } = setup({ text: "original" });
  fireEvent.click(button("Edit"));
  fireEvent.change(screen.getByLabelText("SKILL.md"), { target: { value: "changed" } });
  expect(onDirty).toHaveBeenLastCalledWith(true);
  fireEvent.change(screen.getByLabelText("SKILL.md"), { target: { value: "original" } });
  expect(onDirty).toHaveBeenLastCalledWith(false);
});

test("Cancel after a dirty edit resets the draft and reports dirty false", () => {
  const { onDirty } = setup({ text: "original" });
  fireEvent.click(button("Edit"));
  fireEvent.change(screen.getByLabelText("SKILL.md"), { target: { value: "changed" } });
  expect(onDirty).toHaveBeenLastCalledWith(true);
  fireEvent.click(button("Cancel"));
  expect(onDirty).toHaveBeenLastCalledWith(false);
});

test("bumping leaveEditKey leaves edit mode without saving, keeping the draft for the next Edit", () => {
  const { rerender, props, onEdit } = setup({ text: "original" });
  fireEvent.click(button("Edit"));
  fireEvent.change(screen.getByLabelText("SKILL.md"), { target: { value: "typed" } });
  rerender(<SkillProposalActions {...props} leaveEditKey={1} />);
  for (const name of idleButtons) expect(screen.getByRole("button", { name })).toBeTruthy();
  expect(screen.queryByLabelText("SKILL.md")).toBeNull();
  expect(onEdit).not.toHaveBeenCalled();
  fireEvent.click(button("Edit"));
  expect((screen.getByLabelText("SKILL.md") as HTMLTextAreaElement).value).toBe("typed");
});

test("bumping leaveEditKey still reports the kept draft as dirty, not idle mode as clean", () => {
  const { rerender, props, onDirty } = setup({ text: "original" });
  fireEvent.click(button("Edit"));
  fireEvent.change(screen.getByLabelText("SKILL.md"), { target: { value: "typed" } });
  expect(onDirty).toHaveBeenLastCalledWith(true);
  rerender(<SkillProposalActions {...props} leaveEditKey={1} />);
  expect(onDirty).toHaveBeenLastCalledWith(true);
});
