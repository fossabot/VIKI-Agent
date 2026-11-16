/**
 * Tests for `RecoveryView`.
 */
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { createShellStore } from "../../state/shell";
import { AppStore } from "../../state/store";
import { capabilitiesFixture, fakeShellApi } from "../../testShell";
import { RecoveryView } from "./RecoveryView";

afterEach(cleanup);

async function flush(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
  });
}

function mount(reason: string | null) {
  const api = fakeShellApi({
    capabilities: capabilitiesFixture({
      app: { mode: "recovery" },
      storage: { state: "unavailable", reason },
    }),
  });
  const store = createShellStore(api, new AppStore());
  const utils = render(
    <RecoveryView store={store}>
      <div data-testid="diagnostics-body">diagnostics + checks here</div>
    </RecoveryView>,
  );
  return { ...utils, store };
}

test("the storage refusal code is explained by its own catalog row", async () => {
  const { store } = mount("corrupt");
  await act(async () => {
    await store.refreshCapabilities();
  });
  expect(screen.getByRole("alert").textContent).toBe("The app store is damaged.");
});

test("insecure_permissions — the read-only reason server.py:470 drives — has its own sentence", async () => {
  const { store } = mount("insecure_permissions");
  await act(async () => {
    await store.refreshCapabilities();
  });
  expect(screen.getByRole("alert").textContent).toBe(
    "The app data folder's permissions are too open.",
  );
});

test("a null reason renders no alert", async () => {
  const { store } = mount(null);
  await act(async () => {
    await store.refreshCapabilities();
  });
  expect(screen.queryByRole("alert")).toBeNull();
});

test("the diagnostics body (and its checks) render through children; no export control exists", async () => {
  mount("busy");
  await flush();
  expect(screen.getByTestId("diagnostics-body")).toBeTruthy();
  expect(screen.getByText(/Exports are unavailable/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: /export/i })).toBeNull();
});

test("safe next steps preserve the complete stopped-app data copy", () => {
  mount("corrupt");
  expect(screen.getAllByRole("listitem").map((item) => item.textContent)).toEqual([
    "Close VIKI Agent before copying or changing app data. Copy the complete data folder, including SQLite journals and retained state.json.",
    "Do not delete SQLite journals to bypass an error. Resolve disk space or permission problems and close other tools holding the database before retrying.",
    "For data from a newer app version, use that version. For damaged data, restore a complete known-good backup made while VIKI Agent was closed. Keep the failed copy for inspection.",
  ]);
});

test("the heading receives focus on mount without stealing it on later updates", async () => {
  const { store, rerender } = mount("busy");
  expect(document.activeElement).toBe(screen.getByRole("heading", { level: 1 }));
  rerender(
    <RecoveryView store={store}>
      <button type="button">Reload diagnostics</button>
    </RecoveryView>,
  );
  const button = screen.getByRole("button", { name: "Reload diagnostics" });
  button.focus();
  await act(async () => store.refreshCapabilities());
  rerender(
    <RecoveryView store={store}>
      <button type="button">Diagnostics updated</button>
    </RecoveryView>,
  );
  expect(document.activeElement).toBe(button);
});
