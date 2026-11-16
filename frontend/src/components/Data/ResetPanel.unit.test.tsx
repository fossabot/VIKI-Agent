/**
 * `ResetPanel` and `ResetDialog` together.
 * `commandFor("data.reset", snapshot)` treats unknown capabilities (`capabilities === null`) as
 * a refusal, exactly like the real gate does, so every test here reads capabilities first —
 * `store.start()` does that once at launch in production; a test drives the same read by hand.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, expect, test } from "vitest";
import { ShellApiError } from "../../clients/app/shellApi";
import { createShellStore } from "../../state/shell";
import { AppStore } from "../../state/store";
import { capabilitiesFixture, deferredShellApi, fakeShellApi } from "../../testShell";
import { ResetPanel } from "./ResetPanel";

afterEach(cleanup);

async function mount(
  api: ReturnType<typeof fakeShellApi> | null,
  edgeProfilePath: string | null = null,
) {
  const headingRef = createRef<HTMLHeadingElement>();
  const store = createShellStore(api, new AppStore());
  const utils = render(
    <ResetPanel
      api={api}
      store={store}
      headingRef={headingRef}
      edgeProfilePath={edgeProfilePath}
    />,
  );
  await act(async () => {
    await store.refreshCapabilities();
  });
  return { ...utils, store };
}

function openButton(): HTMLButtonElement {
  // The panel's own opener and, once the dialog is open, the dialog's own confirm button both
  // read "Reset"; the opener is the first in document order because the dialog does not exist
  // until it is clicked.
  return screen.getAllByRole("button", { name: "Reset" })[0] as HTMLButtonElement;
}

function typeConfirmation(value: string): void {
  const field = screen.getByLabelText("Type RESET to confirm.") as HTMLInputElement;
  fireEvent.change(field, { target: { value } });
}

function clickDialogConfirm(): void {
  const dialog = screen.getByRole("alertdialog");
  fireEvent.click(dialog.querySelector("button.btn.go") as HTMLButtonElement);
}

function prepareReset(): HTMLButtonElement {
  act(() => {
    (screen.getByLabelText("Settings") as HTMLInputElement).click();
  });
  const opener = openButton();
  act(() => {
    opener.focus();
    opener.click();
  });
  act(() => typeConfirmation("RESET"));
  return opener;
}

test("double activation submits once", async () => {
  const api = fakeShellApi();
  const deferred = deferredShellApi();
  api.reset = deferred.reset;
  await mount(api);
  prepareReset();
  const confirm = screen
    .getByRole("alertdialog")
    .querySelector("button.btn.go") as HTMLButtonElement;

  act(() => {
    confirm.click();
    confirm.click();
  });

  expect(deferred.pending).toHaveLength(1);
  const request = deferred.pending[0];
  if (!request) throw new Error("Reset was not submitted");
  expect(request.args[1]).toEqual(["settings"]);
  await act(async () => request.resolve(api.data.reset));
});

test.each([
  ["Cancel", "success"],
  ["Escape", "success"],
  ["Cancel", "error"],
  ["Escape", "error"],
])("pending dismissal does not conceal reset: %s, %s", async (dismissal, outcome) => {
  const api = fakeShellApi();
  const deferred = deferredShellApi();
  api.reset = deferred.reset;
  api.data.reset = { scope_of: "w1", results: { settings: { status: "done", count: 1 } } };
  await mount(api);
  const opener = prepareReset();

  act(() => clickDialogConfirm());
  const dialog = screen.getByRole("alertdialog");
  const cancel = screen.getByRole("button", { name: "Cancel" });
  act(() => {
    if (dismissal === "Cancel") cancel.click();
    else fireEvent.keyDown(window, { key: "Escape" });
  });

  expect(screen.queryByRole("alertdialog")).toBe(dialog);
  expect(cancel.getAttribute("aria-disabled")).toBe("true");
  expect(deferred.pending).toHaveLength(1);
  const request = deferred.pending[0];
  if (!request) throw new Error("Reset was not submitted");
  await act(async () => {
    if (outcome === "success") request.resolve(api.data.reset);
    else request.reject(new ShellApiError(503, "unavailable"));
  });

  expect(screen.queryByRole("alertdialog")).toBeNull();
  if (outcome === "success")
    expect(document.querySelector('td[data-label="Count"]')?.textContent).toBe("1");
  else
    expect(screen.getByRole("alert").textContent).toContain(
      "The operation could not be completed.",
    );
  expect(document.activeElement).toBe(opener);
  expect(deferred.pending).toHaveLength(1);
});

test("each category shows its own scope sentence, and edge_profile is aria-disabled with its path", async () => {
  const api = fakeShellApi();
  await mount(api, "D:\\AppData\\viki-agent\\edge");
  expect(screen.getByText("Resets the app's settings to defaults.")).toBeTruthy();
  expect(
    screen.getByText(
      "Deletes this workspace's drafts (the vault is not changed). Images attached only to these drafts are deleted within a day.",
    ),
  ).toBeTruthy();
  expect(
    screen.getByText(
      "Cannot delete while running; after quitting, delete D:\\AppData\\viki-agent\\edge.",
    ),
  ).toBeTruthy();
  expect(
    screen.getByText(
      "Deletes this workspace's copy of the send history, apart from any turn still on its way. Sent images lose their original names at once, and VIKI's own local copies go within a day; Claude Code keeps its own conversation record and temporary copies. Export the conversations you want to keep first.",
    ),
  ).toBeTruthy();
  const edgeRow = screen.getByText("Browser profile").closest("label") as HTMLElement;
  expect(edgeRow.querySelector("input")?.getAttribute("aria-disabled")).toBe("true");
});

test("Reset is aria-disabled until a category is checked, and opens the typed-confirmation dialog", async () => {
  const api = fakeShellApi();
  await mount(api);
  const opener = openButton();
  expect(opener.getAttribute("aria-disabled")).toBe("true");

  const settingsCheckbox = screen.getByLabelText("Settings") as HTMLInputElement;
  await act(async () => {
    settingsCheckbox.click();
  });
  expect(opener.getAttribute("aria-disabled")).toBe("false");

  await act(async () => {
    opener.click();
  });
  const dialog = screen.getByRole("alertdialog");
  expect(dialog.textContent).toContain("Resets the app's settings to defaults.");
  expect(dialog.textContent).toContain("The vault and Claude Code's folders are not changed.");

  const dialogConfirm = dialog.querySelector("button.btn.go") as HTMLButtonElement;
  expect(dialogConfirm.getAttribute("aria-disabled")).toBe("true");

  await act(async () => {
    typeConfirmation("reset");
  });
  expect(dialogConfirm.getAttribute("aria-disabled")).toBe("true");

  await act(async () => {
    typeConfirmation("RESET");
  });
  expect(dialogConfirm.getAttribute("aria-disabled")).toBe("false");
});

test("results render a reason through the specific row, the shared storage row or the generic fallback", async () => {
  const api = fakeShellApi();
  api.data.reset = {
    scope_of: "w1",
    results: {
      commands: { status: "refused", reason: "in_use", count: 0 },
      scratch: { status: "refused", reason: "busy", count: 0 },
      drafts: { status: "refused", reason: "something_new", count: 0 },
      settings: { status: "done", count: 1 },
    },
  };
  await mount(api);
  await act(async () => {
    (screen.getByLabelText("Settings") as HTMLInputElement).click();
    (screen.getByLabelText("Send history") as HTMLInputElement).click();
    (screen.getByLabelText("Scratch folder") as HTMLInputElement).click();
    (screen.getByLabelText("Drafts") as HTMLInputElement).click();
  });
  await act(async () => {
    openButton().click();
  });
  await act(async () => {
    typeConfirmation("RESET");
  });
  await act(async () => {
    clickDialogConfirm();
    await Promise.resolve();
  });
  expect(screen.getByText("In use while VIKI runs; skipped.")).toBeTruthy();
  expect(screen.getByText("The app store is busy.")).toBeTruthy();
  expect(screen.getByText("Skipped: something_new.")).toBeTruthy();
});

test("a settings:done result re-reads settings and capabilities instead of adopting a document", async () => {
  const api = fakeShellApi();
  api.data.reset = {
    scope_of: "w1",
    results: { settings: { status: "done", count: 1 } },
  };
  const { store } = await mount(api);
  await act(async () => {
    await store.refreshSettings();
  });
  await act(async () => {
    (screen.getByLabelText("Settings") as HTMLInputElement).click();
  });
  await act(async () => {
    openButton().click();
  });
  await act(async () => {
    typeConfirmation("RESET");
  });
  const before = api.calls.filter((c) => c.method === "settings").length;
  await act(async () => {
    clickDialogConfirm();
    await Promise.resolve();
  });
  const after = api.calls.filter((c) => c.method === "settings").length;
  expect(after).toBe(before + 1);
});

test("with storage not ready, every category is aria-disabled with the reason and cannot be checked", async () => {
  const api = fakeShellApi({
    capabilities: capabilitiesFixture({ storage: { state: "unavailable", reason: null } }),
  });
  await mount(api);
  const settingsCheckbox = screen.getByLabelText("Settings") as HTMLInputElement;
  expect(settingsCheckbox.getAttribute("aria-disabled")).toBe("true");
  expect(screen.getByText("The app store is not ready.")).toBeTruthy();
  await act(async () => {
    settingsCheckbox.click();
  });
  expect(settingsCheckbox.checked).toBe(false);
});

test("a failed reset shows the refusal instead of silently closing the dialog", async () => {
  const api = fakeShellApi();
  api.fail("reset", new ShellApiError(503, "unavailable"));
  await mount(api);
  await act(async () => {
    (screen.getByLabelText("Settings") as HTMLInputElement).click();
  });
  await act(async () => {
    openButton().click();
  });
  await act(async () => {
    typeConfirmation("RESET");
  });
  await act(async () => {
    clickDialogConfirm();
    await Promise.resolve();
  });
  expect(screen.queryByRole("alertdialog")).toBeNull();
  expect(screen.getByRole("alert").textContent).toContain("The operation could not be completed.");
});

test("a partial commands reset says how many rows it kept, not just how many it deleted", async () => {
  // The store refuses to delete a turn this launch admitted and has not finished, so the count
  // alone would read as a complete reset of a ledger that still holds rows.
  const api = fakeShellApi();
  api.data.reset = {
    scope_of: "w1",
    results: { commands: { status: "partial", count: 2, kept: 1 } },
  };
  await mount(api);
  await act(async () => {
    (screen.getByLabelText("Send history") as HTMLInputElement).click();
  });
  await act(async () => {
    openButton().click();
  });
  await act(async () => {
    typeConfirmation("RESET");
  });
  await act(async () => {
    clickDialogConfirm();
    await Promise.resolve();
  });
  const kept = document.querySelector('td[data-label="Kept"]');
  expect(kept?.textContent).toBe("1");
  const count = document.querySelector('td[data-label="Count"]');
  expect(count?.textContent).toBe("2");
});

test("the approvals category shows its scope sentence, resets, and reports what it kept", async () => {
  const api = fakeShellApi();
  api.data.reset = {
    scope_of: "w1",
    results: { approvals: { status: "partial", count: 127, kept: 12 } },
  };
  await mount(api);
  expect(
    screen.getByText(
      "Deletes this workspace's recorded permission answers, apart from the ones this launch recorded — reopen VIKI Agent and run this again to clear those too.",
    ),
  ).toBeTruthy();
  await act(async () => {
    (screen.getByLabelText("Approval records") as HTMLInputElement).click();
  });
  await act(async () => {
    openButton().click();
  });
  await act(async () => {
    typeConfirmation("RESET");
  });
  await act(async () => {
    clickDialogConfirm();
    await Promise.resolve();
  });
  const lastReset = api.calls.filter((c) => c.method === "reset").at(-1);
  expect(lastReset?.args[1]).toEqual(["approvals"]);
  const count = document.querySelector('td[data-label="Count"]');
  expect(count?.textContent).toBe("127");
  const kept = document.querySelector('td[data-label="Kept"]');
  expect(kept?.textContent).toBe("12");
});
