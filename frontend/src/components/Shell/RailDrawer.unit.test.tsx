/**
 * The rail drawer as a modal: what it renders,
 * what it contains, and the four ways it closes. The focus and inert behaviour comes from
 * useModalFocus, so what is pinned here is that this component hands the hook the right panel,
 * the right initial focus and the right dismissal — and that Escape still belongs to the IME and
 * to a row menu open inside the drawer.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { type ReactNode, useState } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { RailDrawer } from "./RailDrawer";

afterEach(cleanup);

/** The drawer as App drives it: a toggle that opens it, background content beside it. */
function Harness({ side = "left", children }: { side?: "left" | "right"; children?: ReactNode }) {
  const [open, setOpen] = useState(false);
  const label = side === "left" ? "Sessions and agents" : "VIKI panel";
  return (
    <div>
      <button type="button" onClick={() => setOpen(true)}>
        {label}
      </button>
      <p>the conversation</p>
      <RailDrawer side={side} label={label} open={open} onClose={() => setOpen(false)}>
        {children ?? (
          <button type="button" className="sess">
            Weekly review draft
          </button>
        )}
      </RailDrawer>
    </div>
  );
}

function openDrawer(label = "Sessions and agents"): HTMLElement {
  fireEvent.click(screen.getByRole("button", { name: label }));
  return screen.getByRole("dialog", { name: label });
}

test("a closed drawer is not in the DOM at all", () => {
  const { container } = render(
    <RailDrawer side="left" label="Sessions and agents" open={false} onClose={vi.fn()}>
      <button type="button">Weekly review draft</button>
    </RailDrawer>,
  );
  expect(container.innerHTML).toBe("");
  expect(screen.queryByRole("dialog")).toBeNull();
});

test("an open drawer is a labelled modal dialog holding the rail, focused on Close", () => {
  render(<Harness />);
  const dialog = openDrawer();
  expect(dialog.getAttribute("aria-modal")).toBe("true");
  expect(dialog.className).toContain("rail-drawer--left");
  expect(dialog.textContent).toContain("Weekly review draft");
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Close" }));
});

test("the background is inert while the drawer is open and live again after it closes", () => {
  render(<Harness />);
  const toggle = screen.getByRole("button", { name: "Sessions and agents" });
  openDrawer();
  expect(toggle.hasAttribute("inert")).toBe(true);
  expect(screen.getByText("the conversation").hasAttribute("inert")).toBe(true);

  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(toggle.hasAttribute("inert")).toBe(false);
  expect(screen.getByText("the conversation").hasAttribute("inert")).toBe(false);
});

test("Tab and Shift+Tab cycle inside the panel and never reach the shell behind it", () => {
  render(<Harness />);
  openDrawer();
  const close = screen.getByRole("button", { name: "Close" });
  const row = screen.getByRole("button", { name: "Weekly review draft" });

  fireEvent.keyDown(window, { key: "Tab" });
  expect(document.activeElement).toBe(row);
  fireEvent.keyDown(window, { key: "Tab" });
  expect(document.activeElement).toBe(close);
  fireEvent.keyDown(window, { key: "Tab", shiftKey: true });
  expect(document.activeElement).toBe(row);
});

test("Escape closes the drawer and gives focus back to the toggle that opened it", () => {
  render(<Harness />);
  const toggle = screen.getByRole("button", { name: "Sessions and agents" });
  toggle.focus();
  openDrawer();

  fireEvent.keyDown(window, { key: "Escape" });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(toggle);
});

test("an Escape that belongs to the IME does not close the drawer", () => {
  render(<Harness />);
  openDrawer();

  fireEvent.keyDown(window, { key: "Escape", isComposing: true });
  expect(screen.queryByRole("dialog")).not.toBeNull();
  fireEvent.keyDown(window, { key: "Escape", keyCode: 229 });
  expect(screen.queryByRole("dialog")).not.toBeNull();

  fireEvent.keyDown(window, { key: "Escape" });
  expect(screen.queryByRole("dialog")).toBeNull();
});

test("a row menu open inside the drawer owns the first Escape; the next one closes the drawer", () => {
  function RowMenu() {
    const [menu, setMenu] = useState(true);
    return menu ? (
      <div role="menu">
        <button type="button" role="menuitem" onClick={() => setMenu(false)}>
          Fork
        </button>
      </div>
    ) : (
      <button type="button">Weekly review draft</button>
    );
  }
  render(
    <Harness>
      <RowMenu />
    </Harness>,
  );
  openDrawer();

  fireEvent.keyDown(window, { key: "Escape" });
  expect(screen.queryByRole("dialog")).not.toBeNull();

  fireEvent.click(screen.getByRole("menuitem", { name: "Fork" }));
  fireEvent.keyDown(window, { key: "Escape" });
  expect(screen.queryByRole("dialog")).toBeNull();
});

test("a mousedown on the backdrop closes; one that starts on the panel does not", () => {
  const { container } = render(<Harness />);
  const dialog = openDrawer();
  const backdrop = container.querySelector(".rail-drawer-backdrop") as HTMLElement;

  fireEvent.mouseDown(dialog);
  expect(screen.queryByRole("dialog")).not.toBeNull();

  fireEvent.mouseDown(backdrop);
  expect(screen.queryByRole("dialog")).toBeNull();
});

test("the right drawer is the VIKI panel, on the right, with its own label", () => {
  render(
    <Harness side="right">
      <span>Needs you</span>
    </Harness>,
  );
  const dialog = openDrawer("VIKI panel");
  expect(dialog.className).toContain("rail-drawer--right");
  expect(dialog.textContent).toContain("Needs you");
});
