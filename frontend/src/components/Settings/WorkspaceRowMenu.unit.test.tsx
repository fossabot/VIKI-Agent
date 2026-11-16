/** WorkspaceRowMenu and RemoveConfirm's own tests (no frozen acceptance test owns them yet). */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { RemoveConfirm, WorkspaceRowMenu } from "./WorkspaceRowMenu";

afterEach(cleanup);

const NAME = "My notes";
const TRIGGER_NAME = "More actions for My notes";

function openMenu(extra: Partial<Parameters<typeof WorkspaceRowMenu>[0]> = {}) {
  const onDetails = vi.fn();
  render(<WorkspaceRowMenu name={NAME} onDetails={onDetails} {...extra} />);
  fireEvent.click(screen.getByRole("button", { name: TRIGGER_NAME }));
  return onDetails;
}

test("the trigger's accessible name carries the row's name, and shows only a hidden icon", () => {
  render(<WorkspaceRowMenu name={NAME} onDetails={vi.fn()} />);
  const trigger = screen.getByRole("button", { name: TRIGGER_NAME });
  expect(trigger.querySelectorAll('svg[aria-hidden="true"]')).toHaveLength(1);
  expect(trigger.textContent).toBe("");
});

test("items follow the handlers", () => {
  const onDetails = vi.fn();
  const { rerender } = render(<WorkspaceRowMenu name={NAME} onDetails={onDetails} />);
  fireEvent.click(screen.getByRole("button", { name: TRIGGER_NAME }));
  expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual([
    "Details and trust",
  ]);
  expect(document.querySelector(".ws-row-menu-sep")).toBeNull();

  const onOpenInCode = vi.fn();
  rerender(<WorkspaceRowMenu name={NAME} onDetails={onDetails} onOpenInCode={onOpenInCode} />);
  expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual([
    "Details and trust",
    "Open in Code",
  ]);
  expect(document.querySelector(".ws-row-menu-sep")).toBeNull();

  const onRemove = vi.fn();
  rerender(<WorkspaceRowMenu name={NAME} onDetails={onDetails} onRemove={onRemove} />);
  expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual([
    "Details and trust",
    "Remove from list…",
  ]);
  expect(document.querySelector(".ws-row-menu-sep")).not.toBeNull();
});

test("Open in Code is disabled by openDisabled and a click on it is a no-op", () => {
  const onOpenInCode = vi.fn();
  openMenu({ onOpenInCode, openDisabled: true });
  const item = screen.getByRole("menuitem", { name: "Open in Code" });
  expect(item.getAttribute("aria-disabled")).toBe("true");
  fireEvent.click(item);
  expect(onOpenInCode).not.toHaveBeenCalled();
});

test("each item calls its own handler, and only that one", () => {
  const onDetails = vi.fn();
  const onOpenInCode = vi.fn();
  const onRemove = vi.fn();
  render(
    <WorkspaceRowMenu
      name={NAME}
      onDetails={onDetails}
      onOpenInCode={onOpenInCode}
      onRemove={onRemove}
    />,
  );

  fireEvent.click(screen.getByRole("button", { name: TRIGGER_NAME }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Remove from list…" }));
  expect(onRemove).toHaveBeenCalledTimes(1);
  expect(onDetails).not.toHaveBeenCalled();
  expect(onOpenInCode).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: TRIGGER_NAME }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Open in Code" }));
  expect(onOpenInCode).toHaveBeenCalledTimes(1);
  expect(onDetails).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: TRIGGER_NAME }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Details and trust" }));
  expect(onDetails).toHaveBeenCalledTimes(1);
});

function renderConfirm(extra: Partial<Parameters<typeof RemoveConfirm>[0]> = {}) {
  const onConfirm = vi.fn();
  const onCancel = vi.fn();
  render(
    <RemoveConfirm
      name={NAME}
      busy={false}
      error={null}
      onConfirm={onConfirm}
      onCancel={onCancel}
      {...extra}
    />,
  );
  return { onConfirm, onCancel };
}

test("RemoveConfirm is an alertdialog labeled with the row's name and says the confirm sentence", () => {
  renderConfirm();
  const dialog = screen.getByRole("alertdialog", { name: "Remove My notes" });
  expect(dialog.textContent).toContain(
    "Remove My notes from VIKI Agent? Nothing on disk changes, and you can add the folder again later.",
  );
});

test("focus starts on Cancel", () => {
  renderConfirm();
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Cancel" }));
});

test("Remove is aria-disabled while busy, and a click on it is a no-op", () => {
  const { onConfirm } = renderConfirm({ busy: true });
  const removeButton = screen.getByRole("button", { name: "Remove" });
  expect(removeButton.getAttribute("aria-disabled")).toBe("true");
  fireEvent.click(removeButton);
  expect(onConfirm).not.toHaveBeenCalled();
});

test("Remove calls onConfirm once it is not busy", () => {
  const { onConfirm } = renderConfirm();
  fireEvent.click(screen.getByRole("button", { name: "Remove" }));
  expect(onConfirm).toHaveBeenCalledTimes(1);
});

test("Cancel calls onCancel", () => {
  const { onCancel } = renderConfirm();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(onCancel).toHaveBeenCalledTimes(1);
});

test("Escape cancels", () => {
  const { onCancel } = renderConfirm();
  fireEvent.keyDown(window, { key: "Escape" });
  expect(onCancel).toHaveBeenCalledTimes(1);
});

test("a non-null error renders in settings-error as role alert", () => {
  renderConfirm({ error: "The operation could not be completed." });
  const alert = screen.getByRole("alert");
  expect(alert.className).toBe("settings-error");
  expect(alert.textContent).toBe("The operation could not be completed.");
});

test("a null error renders no alert", () => {
  renderConfirm();
  expect(screen.queryByRole("alert")).toBeNull();
});
