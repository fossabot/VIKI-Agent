/**
 * `DataView`: the four panels render together, `nav.panel` moves DOM focus to
 * that panel's own heading (or, with no panel named, to the view's own `<h1>`), and
 * "Back to chat" asks `onNavigate` for chat.
 */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { ResetResult } from "../../clients/app/shellApi";
import type { DataPanel } from "../../commands";
import { createShellStore } from "../../state/shell";
import { AppStore } from "../../state/store";
import type { VaultKnowledgeApi } from "../../state/vaultKnowledgeApi";
import { fakeShellApi } from "../../testShell";
// biome-ignore lint/suspicious/noShadowRestrictedNames: same fixed name as DataView.tsx itself.
import { DataView } from "./DataView";

afterEach(cleanup);

function mount(panel?: DataPanel, onReset?: (result: ResetResult) => void) {
  const api = fakeShellApi();
  const store = createShellStore(api, new AppStore());
  const onNavigate = vi.fn();
  const renderPathField = () => <div />;
  const renderConflict = () => <div />;
  const utils = render(
    <DataView
      store={store}
      api={api}
      {...(panel !== undefined ? { panel } : {})}
      onNavigate={onNavigate}
      renderPathField={renderPathField}
      renderConflict={renderConflict}
      {...(onReset ? { onReset } : {})}
    />,
  );
  return { ...utils, onNavigate, store, api };
}

test.each(["done", "partial"] as const)(
  "reports the actual %s reset scope so saved Bot groups can be invalidated",
  async (status) => {
    const onReset = vi.fn();
    const { store, api } = mount("reset", onReset);
    api.data.reset = {
      scope_of: "11111111-1111-4111-8111-111111111111",
      results: { commands: { status, count: 4, kept: status === "partial" ? 1 : 0 } },
    };
    await act(async () => store.refreshCapabilities());
    fireEvent.click(screen.getByLabelText("Send history"));
    fireEvent.click(screen.getAllByRole("button", { name: "Reset" })[0] as HTMLElement);
    fireEvent.change(screen.getByLabelText("Type RESET to confirm."), {
      target: { value: "RESET" },
    });
    await act(async () => {
      fireEvent.click(
        screen.getByRole("alertdialog").querySelector("button.btn.go") as HTMLElement,
      );
    });
    expect(onReset).toHaveBeenCalledExactlyOnceWith(api.data.reset);
  },
);

async function flush(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
  });
}

test("all four panels render together, not as a tab switch", async () => {
  mount("locations");
  await flush();
  expect(screen.getByRole("heading", { name: "Locations" })).toBeTruthy();
  expect(screen.getByRole("heading", { name: "Export" })).toBeTruthy();
  expect(screen.getByRole("heading", { name: "Import" })).toBeTruthy();
  expect(screen.getByRole("heading", { name: "Reset" })).toBeTruthy();
});

test("the Backup panel appears only when a knowledge client is given", async () => {
  const { unmount } = mount("locations");
  await flush();
  expect(screen.queryByRole("heading", { name: "Backup" })).toBeNull();
  unmount();
  const api = fakeShellApi();
  render(
    <DataView
      store={createShellStore(api, new AppStore())}
      api={api}
      onNavigate={vi.fn()}
      renderPathField={() => <div />}
      renderConflict={() => <div />}
      knowledgeApi={{} as VaultKnowledgeApi}
    />,
  );
  await flush();
  expect(screen.getByRole("heading", { name: "Backup" })).toBeTruthy();
});

test("nav.panel moves focus to that panel's own heading on entry", async () => {
  mount("export");
  await flush();
  expect(document.activeElement?.textContent).toBe("Export");
});

test("with no panel named, the view's own h1 gets entry focus on mount", async () => {
  mount();
  await flush();
  expect(document.activeElement?.tagName).toBe("H1");
  expect(document.activeElement?.textContent).toBe("Data");
});

test("the back-to-chat button asks the integrator to navigate, never navigates itself", async () => {
  const { onNavigate } = mount("locations");
  await flush();
  await act(async () => {
    screen.getByText("Back to chat").click();
  });
  expect(onNavigate).toHaveBeenCalledWith({ view: "chat" });
});

test("a supplied Back replaces the header button without changing panel entry focus", async () => {
  const api = fakeShellApi();
  const onNavigate = vi.fn();
  render(
    <DataView
      store={createShellStore(api, new AppStore())}
      api={api}
      panel="export"
      onNavigate={onNavigate}
      renderPathField={() => <div />}
      renderConflict={() => <div />}
      renderBack={(onBack) => (
        <button type="button" onClick={onBack}>
          Back
        </button>
      )}
    />,
  );
  await flush();
  const header = screen.getByRole("heading", { name: "Data", level: 1 }).closest("header");
  expect(header).not.toBeNull();
  expect(within(header as HTMLElement).getAllByRole("button")).toHaveLength(1);
  expect(screen.queryByRole("button", { name: "Back to chat" })).toBeNull();
  expect(document.activeElement).toBe(screen.getByRole("heading", { name: "Export" }));
  expect(onNavigate).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Back" }));
  expect(onNavigate).toHaveBeenCalledTimes(1);
  expect(onNavigate).toHaveBeenCalledWith({ view: "chat" });
});
