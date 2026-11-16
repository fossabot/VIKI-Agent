/** WorkspaceList's own tests: tick/cap/blocked gating, the state chip and hint per row, the row
 * menu's remove/open wiring, the details panel and remove-confirm flow, the summary line, and the
 * Trust… focus hand-off (no frozen acceptance test covers any of this).
 *
 * `@radix-ui/react-dropdown-menu` is mocked below with a minimal open-state-gated stand-in.
 * `WorkspaceRowMenu` already has its own test file against the
 * real Radix component; what this file needs to prove is which items WorkspaceList asks it to
 * render for a given row, not Radix's own floating-ui/portal mechanics — exercising the real
 * popper here, across this file's many rows and reopenings, intermittently (not every run) drove a
 * worker-RPC stack overflow in manual runs, never once this mock replaced it. Not a logic bug in
 * WorkspaceList itself: every failing/passing combination was checked by hand first.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import type { Key } from "../../i18n/index";
import { fakeShellApi, workspaceFixture } from "../../testShell";
import { MAX_TICKS, tickable, WorkspaceList, type WorkspaceListProps } from "./WorkspaceList";

vi.mock("@radix-ui/react-dropdown-menu", async () => {
  const React = await import("react");
  const Ctx = React.createContext({ open: false, close: () => {} });
  type DivProps = React.HTMLAttributes<HTMLDivElement>;
  function Root({
    open,
    onOpenChange,
    children,
  }: {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    children?: React.ReactNode;
  }) {
    return React.createElement(
      Ctx.Provider,
      { value: { open, close: () => onOpenChange(false) } },
      children,
    );
  }
  function Trigger(props: React.ButtonHTMLAttributes<HTMLButtonElement>) {
    return React.createElement("button", { type: "button", ...props });
  }
  function Portal({ children }: { children?: React.ReactNode }) {
    return React.createElement(React.Fragment, null, children);
  }
  function Content({
    align: _align,
    sideOffset: _sideOffset,
    ...props
  }: DivProps & { align?: string; sideOffset?: number }) {
    const { open } = React.useContext(Ctx);
    return open ? React.createElement("div", { role: "menu", ...props }) : null;
  }
  function Item({
    onSelect,
    disabled,
    ...props
  }: DivProps & { onSelect?: () => void; disabled?: boolean }) {
    const { close } = React.useContext(Ctx);
    return React.createElement("div", {
      role: "menuitem",
      ...(disabled ? { "aria-disabled": "true" } : {}),
      ...props,
      onClick: () => {
        if (disabled) return;
        onSelect?.();
        close();
      },
    });
  }
  function Separator(props: DivProps) {
    return React.createElement("div", props);
  }
  return { Root, Trigger, Portal, Content, Item, Separator };
});

afterEach(() => {
  cleanup();
});

function codeRow(overrides: Partial<WorkspaceJson> = {}): WorkspaceJson {
  return workspaceFixture({
    workspace_id: "row-a",
    name: "Row A",
    kind: "code",
    policy: "ordinary",
    root: "C:\\code\\row-a",
    ...overrides,
  });
}

function baseProps(overrides: Partial<WorkspaceListProps> = {}): WorkspaceListProps {
  return {
    rows: [],
    vaultName: "VIKI",
    ticked: new Set(),
    tickBlockedReason: null,
    onTick: vi.fn(),
    inUseIds: new Set(),
    detailsId: null,
    onDetails: vi.fn(),
    onTrust: vi.fn(),
    focusTrustId: null,
    onFocusDone: vi.fn(),
    api: null,
    allWorkspaces: [],
    onPolicyChanged: vi.fn(),
    onRemove: vi.fn(async () => null),
    ...overrides,
  };
}

test("tickable needs a trusted, present, persistent code folder", () => {
  expect(tickable(codeRow())).toBe(true);
  expect(tickable(codeRow({ kind: "vault" }))).toBe(false);
  expect(tickable(codeRow({ policy: "existing_readonly" }))).toBe(false);
  expect(tickable(codeRow({ root_available: false }))).toBe(false);
  expect(tickable(codeRow({ persistent: false }))).toBe(false);
});

test("Details and trust in the row menu calls onDetails", () => {
  const onDetails = vi.fn();
  render(<WorkspaceList {...baseProps({ rows: [codeRow()], onDetails })} />);
  fireEvent.click(screen.getByRole("button", { name: "More actions for Row A" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Details and trust" }));
  expect(onDetails).toHaveBeenCalledWith("row-a");
});

test("the row menu offers no Remove for a protected row", () => {
  render(<WorkspaceList {...baseProps({ rows: [codeRow({ policy: "protected" })] })} />);
  fireEvent.click(screen.getByRole("button", { name: "More actions for Row A" }));
  expect(screen.queryByRole("menuitem", { name: "Remove from list…" })).toBeNull();
});

test("the row menu offers no Remove for an in-use row, even when found and trusted", () => {
  render(<WorkspaceList {...baseProps({ rows: [codeRow()], inUseIds: new Set(["row-a"]) })} />);
  fireEvent.click(screen.getByRole("button", { name: "More actions for Row A" }));
  expect(screen.queryByRole("menuitem", { name: "Remove from list…" })).toBeNull();
});

test("the row menu offers Remove for an ordinary, not-in-use row", () => {
  render(<WorkspaceList {...baseProps({ rows: [codeRow()] })} />);
  fireEvent.click(screen.getByRole("button", { name: "More actions for Row A" }));
  expect(screen.getByRole("menuitem", { name: "Remove from list…" })).toBeTruthy();
});

test("Open in Code is disabled in the row menu when the row is missing", () => {
  const onOpenInCode = vi.fn();
  render(
    <WorkspaceList {...baseProps({ rows: [codeRow({ root_available: false })], onOpenInCode })} />,
  );
  fireEvent.click(screen.getByRole("button", { name: "More actions for Row A" }));
  const item = screen.getByRole("menuitem", { name: "Open in Code" });
  expect(item.getAttribute("aria-disabled")).toBe("true");
  fireEvent.click(item);
  expect(onOpenInCode).not.toHaveBeenCalled();
});

test("Remove from list… opens RemoveConfirm, and a null resolution closes it", async () => {
  const onRemove = vi.fn(async () => null);
  render(<WorkspaceList {...baseProps({ rows: [codeRow()], onRemove })} />);
  fireEvent.click(screen.getByRole("button", { name: "More actions for Row A" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Remove from list…" }));
  expect(screen.getByRole("alertdialog")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "Remove" }));
  expect(onRemove).toHaveBeenCalledWith("row-a");
  await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
});

test("a remove error keeps the dialog open and shows the message", async () => {
  const onRemove = vi.fn(async () => "The operation could not be completed.");
  render(<WorkspaceList {...baseProps({ rows: [codeRow()], onRemove })} />);
  fireEvent.click(screen.getByRole("button", { name: "More actions for Row A" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Remove from list…" }));
  fireEvent.click(screen.getByRole("button", { name: "Remove" }));
  await waitFor(() =>
    expect(screen.getByText("The operation could not be completed.")).toBeTruthy(),
  );
  expect(screen.getByRole("alertdialog")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
});

test("Cancel closes RemoveConfirm without calling onRemove", () => {
  const onRemove = vi.fn();
  render(<WorkspaceList {...baseProps({ rows: [codeRow()], onRemove })} />);
  fireEvent.click(screen.getByRole("button", { name: "More actions for Row A" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Remove from list…" }));
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.queryByRole("alertdialog")).toBeNull();
  expect(onRemove).not.toHaveBeenCalled();
});

test("a tick sends the sorted ids", () => {
  const onTick = vi.fn();
  const rows = [
    codeRow({ workspace_id: "row-b", name: "Row B" }),
    codeRow({ workspace_id: "row-a", name: "Row A" }),
  ];
  render(<WorkspaceList {...baseProps({ rows, ticked: new Set(["row-b"]), onTick })} />);
  fireEvent.click(screen.getByRole("checkbox", { name: "Row A" }));
  expect(onTick).toHaveBeenCalledWith(["row-a", "row-b"]);
});

test("unticking drops it from the sent ids", () => {
  const onTick = vi.fn();
  const rows = [
    codeRow({ workspace_id: "row-a" }),
    codeRow({ workspace_id: "row-b", name: "Row B" }),
  ];
  render(<WorkspaceList {...baseProps({ rows, ticked: new Set(["row-a", "row-b"]), onTick })} />);
  fireEvent.click(screen.getByRole("checkbox", { name: "Row A" }));
  expect(onTick).toHaveBeenCalledWith(["row-b"]);
});

test("a tick ignores a stale id that no longer matches a tickable row", () => {
  const onTick = vi.fn();
  const rows = [
    codeRow({ workspace_id: "row-a" }),
    codeRow({ workspace_id: "row-b", name: "Row B" }),
  ];
  // "row-gone" is ticked in storage but is not among the rows this list knows about.
  render(<WorkspaceList {...baseProps({ rows, ticked: new Set(["row-gone"]), onTick })} />);
  fireEvent.click(screen.getByRole("checkbox", { name: "Row A" }));
  expect(onTick).toHaveBeenCalledWith(["row-a"]);
});

test("an untrusted row's checkbox is disabled, and a click is a no-op", () => {
  const onTick = vi.fn();
  const rows = [codeRow({ policy: "existing_readonly" })];
  render(<WorkspaceList {...baseProps({ rows, onTick })} />);
  const checkbox = screen.getByRole("checkbox", { name: "Row A" });
  expect(checkbox.getAttribute("aria-disabled")).toBe("true");
  fireEvent.click(checkbox);
  expect(onTick).not.toHaveBeenCalled();
});

test("a missing row's checkbox is disabled with the not-found reason", () => {
  const rows = [codeRow({ root_available: false })];
  render(<WorkspaceList {...baseProps({ rows })} />);
  const checkbox = screen.getByRole("checkbox", { name: "Row A" });
  expect(checkbox.getAttribute("aria-disabled")).toBe("true");
  expect(checkbox.getAttribute("title")).toBe(
    "This folder is gone from disk. Remove it, or put the folder back.",
  );
});

test("at the cap, an unticked row is disabled with the cap reason but a ticked one stays enabled", () => {
  const capped = Array.from({ length: MAX_TICKS }, (_, index) =>
    codeRow({ workspace_id: `row-${index}`, name: `Row ${index}` }),
  );
  const rows = [...capped, codeRow({ workspace_id: "row-extra", name: "Row Extra" })];
  const ticked = new Set(capped.map((row) => row.workspace_id));
  render(<WorkspaceList {...baseProps({ rows, ticked })} />);

  const extra = screen.getByRole("checkbox", { name: "Row Extra" });
  expect(extra.getAttribute("aria-disabled")).toBe("true");
  expect(extra.getAttribute("title")).toBe("Up to 7 folders. Untick one first.");

  const first = screen.getByRole("checkbox", { name: "Row 0" }) as HTMLInputElement;
  expect(first.getAttribute("aria-disabled")).toBe("false");
  expect(first.checked).toBe(true);
});

test("tickBlockedReason disables every row's checkbox, ticked or not", () => {
  const rows = [
    codeRow({ workspace_id: "row-a" }),
    codeRow({ workspace_id: "row-b", name: "Row B" }),
  ];
  render(
    <WorkspaceList
      {...baseProps({
        rows,
        ticked: new Set(["row-a"]),
        tickBlockedReason: "error.read_only" as Key,
      })}
    />,
  );
  const a = screen.getByRole("checkbox", { name: "Row A" }) as HTMLInputElement;
  const b = screen.getByRole("checkbox", { name: "Row B" });
  expect(a.getAttribute("aria-disabled")).toBe("true");
  expect(b.getAttribute("aria-disabled")).toBe("true");
  expect(a.checked).toBe(true);
  expect(a.getAttribute("title")).toBe("Settings are read-only; see the recovery guidance.");
});

test.each([
  ["protected" as const, "Protected"],
  ["existing_readonly" as const, "Not trusted"],
  ["ordinary" as const, "Trusted"],
])("the %s chip reads %s", (policy, text) => {
  render(<WorkspaceList {...baseProps({ rows: [codeRow({ policy })] })} />);
  expect(screen.getByText(text)).toBeTruthy();
});

test("an unavailable row's chip reads Not found even when its policy is ordinary", () => {
  render(<WorkspaceList {...baseProps({ rows: [codeRow({ root_available: false })] })} />);
  expect(screen.getByText("Not found")).toBeTruthy();
});

test("an untrusted row's Trust… hint calls onTrust, and disappears once its panel is open", () => {
  const onTrust = vi.fn();
  const rows = [codeRow({ policy: "existing_readonly" })];
  const { container, rerender } = render(<WorkspaceList {...baseProps({ rows, onTrust })} />);
  expect(container.querySelector(".ws-list-hint")?.textContent).toContain(
    "Trust this folder to let chats work in it.",
  );
  fireEvent.click(screen.getByRole("button", { name: "Trust…" }));
  expect(onTrust).toHaveBeenCalledWith("row-a");

  rerender(<WorkspaceList {...baseProps({ rows, onTrust, detailsId: "row-a" })} />);
  expect(container.querySelector(".ws-list-hint")).toBeNull();
});

test("a missing row offers a Remove hint unless it is in use", () => {
  const rows = [codeRow({ root_available: false })];
  const { container, rerender } = render(<WorkspaceList {...baseProps({ rows })} />);
  expect(container.querySelector(".ws-list-hint")?.textContent).toContain(
    "This folder is gone from disk. Remove it, or put the folder back.",
  );
  expect(screen.getByRole("button", { name: "Remove" })).toBeTruthy();

  rerender(<WorkspaceList {...baseProps({ rows, inUseIds: new Set(["row-a"]) })} />);
  expect(screen.queryByRole("button", { name: "Remove" })).toBeNull();
});

test("PolicyPanel mounts under the row only once detailsId matches it", () => {
  const rows = [codeRow()];
  const api = fakeShellApi();
  const { rerender } = render(<WorkspaceList {...baseProps({ rows, allWorkspaces: rows, api })} />);
  expect(api.calls.some((call) => call.method === "policy")).toBe(false);

  rerender(
    <WorkspaceList {...baseProps({ rows, allWorkspaces: rows, api, detailsId: "row-a" })} />,
  );
  expect(api.calls.some((call) => call.method === "policy")).toBe(true);
});

test("focusTrustId moves focus to the panel's Trust this folder button, then calls onFocusDone", async () => {
  const onFocusDone = vi.fn();
  const rows = [codeRow({ policy: "existing_readonly" })];
  render(
    <WorkspaceList
      {...baseProps({
        rows,
        allWorkspaces: rows,
        api: fakeShellApi(),
        detailsId: "row-a",
        focusTrustId: "row-a",
        onFocusDone,
      })}
    />,
  );
  await waitFor(() =>
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Trust this folder" })),
  );
  expect(onFocusDone).toHaveBeenCalledTimes(1);
});

test("the summary counts only ticked, tickable rows and uses the singular form for one", () => {
  const rows = [
    codeRow({ workspace_id: "row-a" }),
    codeRow({ workspace_id: "row-b", name: "Row B", policy: "existing_readonly" }),
  ];
  render(
    <WorkspaceList
      {...baseProps({ rows, vaultName: "VIKI", ticked: new Set(["row-a", "row-b"]) })}
    />,
  );
  expect(screen.getByRole("status").textContent).toBe(
    "New chats: VIKI + 1 workspace · up to 7 ticked code folders · applies to chats you start from now on; open chats keep their folders.",
  );
});

test("the summary uses the plural form for zero and for more than one", () => {
  const rows = [
    codeRow({ workspace_id: "row-a" }),
    codeRow({ workspace_id: "row-b", name: "Row B" }),
  ];
  const { rerender } = render(<WorkspaceList {...baseProps({ rows, vaultName: "VIKI" })} />);
  expect(screen.getByRole("status").textContent).toContain("VIKI + 0 workspaces");

  rerender(
    <WorkspaceList
      {...baseProps({ rows, vaultName: "VIKI", ticked: new Set(["row-a", "row-b"]) })}
    />,
  );
  expect(screen.getByRole("status").textContent).toContain("VIKI + 2 workspaces");
});

test("no rows shows the empty message instead of the list", () => {
  render(<WorkspaceList {...baseProps({ rows: [] })} />);
  expect(
    screen.getByText("No workspaces yet. Add folders to let chats work in them."),
  ).toBeTruthy();
  expect(screen.queryByRole("status")).toBeNull();
});
