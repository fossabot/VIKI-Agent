import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { readCss, ruleIn } from "../../testing/css";
import { ConfirmDialog, type ConfirmDialogProps } from "./ConfirmDialog";

afterEach(cleanup);

function setup(props: Partial<ConfirmDialogProps> = {}) {
  const onConfirm = vi.fn();
  const onCancel = vi.fn();
  const view = render(
    <ConfirmDialog
      label="Delete thing"
      body="Delete the thing?"
      confirmLabel="Delete"
      onConfirm={onConfirm}
      onCancel={onCancel}
      {...props}
    />,
  );
  return { onConfirm, onCancel, ...view };
}

test("alertdialog is named and Cancel has focus", () => {
  setup();
  expect(screen.getByRole("alertdialog", { name: "Delete thing" })).toBeTruthy();
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Cancel" }));
});

test("opening reveals the card, then its actions, once", () => {
  const scrollIntoView = vi.fn();
  const previous = Element.prototype.scrollIntoView;
  Element.prototype.scrollIntoView = scrollIntoView;
  try {
    const { rerender, onConfirm, onCancel } = setup();
    const dialog = screen.getByRole("alertdialog");
    const revealed = [dialog, dialog.querySelector(".confirm-dialog-actions")];
    expect(scrollIntoView.mock.contexts).toEqual(revealed);
    expect(scrollIntoView.mock.calls).toEqual([[{ block: "nearest" }], [{ block: "nearest" }]]);
    // A later render (an error arriving, busy changing) must not move the page again.
    rerender(
      <ConfirmDialog
        label="Delete thing"
        body="Delete the thing?"
        confirmLabel="Delete"
        error="It failed"
        busy
        onConfirm={onConfirm}
        onCancel={onCancel}
      />,
    );
    expect(screen.getByRole("alertdialog")).toBe(dialog);
    expect(scrollIntoView).toHaveBeenCalledTimes(2);
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Cancel" }));
  } finally {
    Element.prototype.scrollIntoView = previous;
  }
});

test("the card and its actions carry the margins their scroll into view lands on", () => {
  const css = readCss(import.meta.url, "./ConfirmDialog.css");
  const card = ruleIn(css, ".confirm-dialog");
  expect(card).toMatch(/scroll-margin-block:\s*64px 24px;/);
  // The actions' margin is the card's 12px padding and 1px border under them, so their scroll
  // ends on the card's bottom edge and cannot push the top of a card that fits out of view.
  expect(card).toMatch(/padding:\s*12px 16px;/);
  expect(card).toMatch(/border:\s*1px solid/);
  expect(ruleIn(css, ".confirm-dialog-actions")).toMatch(/scroll-margin-bottom:\s*13px;/);
});

test("Esc and Cancel call onCancel", () => {
  const { onCancel } = setup();
  fireEvent.keyDown(window, { key: "Escape" });
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(onCancel).toHaveBeenCalledTimes(2);
});

test("Confirm calls onConfirm", () => {
  const { onConfirm } = setup();
  fireEvent.click(screen.getByRole("button", { name: "Delete" }));
  expect(onConfirm).toHaveBeenCalledTimes(1);
});

// `detail` is how many clicks the system counts in a row: 1 for a single click, 2 for the second
// click of a double-click wherever its first click landed, 0 for Enter or Space on the button.

test("the second click of a double-click never confirms: it began before the card was there", () => {
  const { onConfirm, onCancel } = setup();
  const confirm = screen.getByRole("button", { name: "Delete" });
  fireEvent.click(confirm, { detail: 2 });
  fireEvent.click(confirm, { detail: 3 });
  expect(onConfirm).not.toHaveBeenCalled();
  expect(onCancel).not.toHaveBeenCalled();
});

test("a single click and a keyboard activation both still confirm", () => {
  const { onConfirm } = setup();
  const confirm = screen.getByRole("button", { name: "Delete" });
  fireEvent.click(confirm, { detail: 1 });
  expect(onConfirm).toHaveBeenCalledTimes(1);
  fireEvent.click(confirm, { detail: 0 });
  expect(onConfirm).toHaveBeenCalledTimes(2);
});

test("the second click of a double-click does not cancel either, and leaves focus on Cancel", () => {
  const { onCancel } = setup();
  const cancel = screen.getByRole("button", { name: "Cancel" });
  const confirm = screen.getByRole("button", { name: "Delete" });
  // fireEvent returns false when the event's default was prevented: the press takes no focus.
  expect(fireEvent.mouseDown(confirm, { detail: 2 })).toBe(false);
  expect(fireEvent.mouseDown(confirm, { detail: 1 })).toBe(true);
  fireEvent.click(cancel, { detail: 2 });
  expect(onCancel).not.toHaveBeenCalled();
  expect(document.activeElement).toBe(cancel);
  fireEvent.click(cancel, { detail: 1 });
  expect(onCancel).toHaveBeenCalledTimes(1);
});

test("busy disables Confirm without calling", () => {
  const { onConfirm } = setup({ busy: true });
  const button = screen.getByRole("button", { name: "Delete" });
  expect(button.getAttribute("aria-disabled")).toBe("true");
  fireEvent.click(button);
  expect(onConfirm).not.toHaveBeenCalled();
});

test("irreversible line only when set", () => {
  const { unmount } = setup();
  expect(document.querySelector(".confirm-dialog-irreversible")).toBeNull();
  unmount();
  setup({ irreversible: true });
  expect(document.querySelector(".confirm-dialog-irreversible")?.textContent).toBe(
    "This can't be undone.",
  );
});

test("error renders in role=alert", () => {
  const { unmount } = setup();
  expect(screen.queryByRole("alert")).toBeNull();
  unmount();
  setup({ error: "It failed" });
  expect(screen.getByRole("alert").textContent).toBe("It failed");
});

test("trash icon only when destructive", () => {
  const { container, unmount } = setup();
  expect(container.querySelector('svg[data-icon="trash"]')).toBeNull();
  unmount();
  const view = setup({ destructive: true });
  expect(view.container.querySelector('svg[data-icon="trash"]')).not.toBeNull();
});

test("className is merged", () => {
  setup({ className: "extra" });
  const dialog = screen.getByRole("alertdialog");
  expect(dialog.classList.contains("confirm-dialog")).toBe(true);
  expect(dialog.classList.contains("extra")).toBe(true);
});

test("sections describe body and each nonempty list with text nodes", () => {
  setup({
    sections: [
      { heading: "Changes", items: ["First effect", "<second effect>"] },
      { heading: "Empty", items: [] },
      { heading: "Stays", items: ["One thing"] },
    ],
  });
  const dialog = screen.getByRole("alertdialog");
  const ids = (dialog.getAttribute("aria-describedby") ?? "").split(" ");
  expect(ids).toHaveLength(3);
  expect(document.getElementById(ids[0] ?? "")?.textContent).toBe("Delete the thing?");
  expect(
    [...dialog.querySelectorAll(".confirm-dialog-heading")].map((item) => item.textContent),
  ).toEqual(["Changes", "Stays"]);
  const lists = [...dialog.querySelectorAll(".confirm-dialog-list")];
  expect(
    lists.map((list) => [...list.querySelectorAll("li")].map((item) => item.textContent)),
  ).toEqual([["First effect", "<second effect>"], ["One thing"]]);
  expect(ids.slice(1)).toEqual(lists.map((list) => list.id));
  expect(dialog.querySelector("second effect")).toBeNull();
});

test("without sections keeps body markup and no description ids", () => {
  setup();
  const dialog = screen.getByRole("alertdialog");
  expect(dialog.getAttribute("aria-describedby")).toBeNull();
  expect(dialog.firstElementChild?.outerHTML).toBe("<p>Delete the thing?</p>");
});

test("error line carries its own size, not Settings.css's", () => {
  const css = readCss(import.meta.url, "./ConfirmDialog.css");
  const rule = ruleIn(css, '.confirm-dialog [role="alert"]');
  expect(rule).toMatch(/font-size:\s*12px/);
  expect(rule).toMatch(/line-height:\s*1\.5/);
  expect(rule).toMatch(/color:\s*var\(--err\)/);
});
