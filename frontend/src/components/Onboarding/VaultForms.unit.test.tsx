/**
 * CreateVaultForm and OpenVaultForm: the suggestion prefill's dirty-field
 * rule, every backend refusal landing on the right field or the form, the stalled-bootstrap and
 * open-existing hints, the select-after-success POST and its own failure path, and the CSP rule
 * that nothing here ever submits natively.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  type CreateVaultInput,
  type CreateVaultResult,
  ShellApiError,
} from "../../clients/app/shellApi";
import { createShellStore, type ShellStore } from "../../state/shell";
import { AppStore } from "../../state/store";
import type { FakeShellApi } from "../../testShell";
import { FAKE_WORKSPACE_ID, fakeShellApi, inspectionFixture } from "../../testShell";
import { CreateVaultForm } from "./CreateVaultForm";
import { OpenVaultForm } from "./OpenVaultForm";

async function flush(): Promise<void> {
  await act(async () => {
    for (let turn = 0; turn < 10; turn += 1) await Promise.resolve();
  });
}

function makeStore(api: FakeShellApi): ShellStore {
  return createShellStore(api, new AppStore());
}

/** jsdom exposes `navigator.platform` through a prototype getter, so this file's own property
 * is removed afterwards rather than restored — there is no own descriptor to put back. */
function setPlatform(value: string): () => void {
  Object.defineProperty(window.navigator, "platform", { value, configurable: true });
  return () => {
    delete (window.navigator as { platform?: string }).platform;
  };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("the suggestion fills only untouched fields", () => {
  it("prefills parent and folder from the name, but never a field the user edited themselves", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Trip notes" } });
    act(() => void vi.advanceTimersByTime(150));
    await flush();
    expect((screen.getByLabelText("Parent folder") as HTMLInputElement).value).toBe(
      "D:\\VIKI\\Vaults",
    );
    expect((screen.getByLabelText("Folder name") as HTMLInputElement).value).toBe("Trip notes");

    fireEvent.change(screen.getByLabelText("Folder name"), { target: { value: "custom" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Trip notes 2" } });
    act(() => void vi.advanceTimersByTime(150));
    await flush();
    expect((screen.getByLabelText("Folder name") as HTMLInputElement).value).toBe("custom");
    expect((screen.getByLabelText("Parent folder") as HTMLInputElement).value).toBe(
      "D:\\VIKI\\Vaults",
    );
  });

  it("dirtying the parent after the suggestion request has been sent still wins over the answer", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Trip notes" } });
    // The debounce fires: vaultSuggestion is sent, but its promise has not resolved yet (fake
    // timers advance the setTimeout callback synchronously; the fetch's own microtask is still
    // pending). Dirtying the parent in this window must still beat the answer when it lands.
    act(() => void vi.advanceTimersByTime(150));
    expect(api.calls.some((call) => call.method === "vaultSuggestion")).toBe(true);
    fireEvent.change(screen.getByLabelText("Parent folder"), { target: { value: "E:\\Mine" } });
    await flush();
    expect((screen.getByLabelText("Parent folder") as HTMLInputElement).value).toBe("E:\\Mine");
  });

  it("a failed suggestion leaves the fields exactly as the user left them", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    api.fail("vaultSuggestion", new ShellApiError(0, "invalid_input"));
    render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Trip notes" } });
    act(() => void vi.advanceTimersByTime(150));
    await flush();
    expect((screen.getByLabelText("Parent folder") as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("Folder name") as HTMLInputElement).value).toBe("");
  });
});

describe("two in-flight suggestion answers cannot race", () => {
  it("an older answer landing after a newer one does not overwrite the newer prefill", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    // A hand-rolled vaultSuggestion that hands back a resolver instead of resolving on its own,
    // so the test controls the landing order of the two requests directly.
    const resolvers: Array<() => void> = [];
    api.vaultSuggestion = (name: string) =>
      new Promise((resolve) => {
        resolvers.push(() =>
          resolve({ parent: "D:\\VIKI\\Vaults", folder: name, path: `D:\\VIKI\\Vaults\\${name}` }),
        );
      });
    render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "V" } });
    act(() => void vi.advanceTimersByTime(150)); // the debounce fires the "V" request
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "VIKI" } });
    fireEvent.blur(screen.getByLabelText("Name")); // blur fires a second, "VIKI" request
    expect(resolvers).toHaveLength(2);

    resolvers[1]?.(); // the newer ("VIKI") request answers first
    await flush();
    expect((screen.getByLabelText("Folder name") as HTMLInputElement).value).toBe("VIKI");

    resolvers[0]?.(); // the older ("V") request answers last
    await flush();
    expect((screen.getByLabelText("Folder name") as HTMLInputElement).value).toBe("VIKI");
  });

  it("a blur that repeats the name already requested fires no redundant request", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Trip notes" } });
    act(() => void vi.advanceTimersByTime(150));
    await flush();
    const afterDebounce = api.calls.filter((call) => call.method === "vaultSuggestion");
    expect(afterDebounce).toHaveLength(1);

    fireEvent.blur(screen.getByLabelText("Name"));
    await flush();
    const afterBlur = api.calls.filter((call) => call.method === "vaultSuggestion");
    expect(afterBlur).toHaveLength(1);
  });
});

describe("every backend code is a field error with a retry", () => {
  it("Open: invalid_path lands on the path field", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    api.fail("openVault", new ShellApiError(400, "invalid_path"));
    render(
      <OpenVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        renderInspection={() => null}
      />,
    );
    fireEvent.change(screen.getByLabelText("Folder path"), { target: { value: "D:\\x" } });
    fireEvent.click(screen.getByText("Open"));
    await flush();
    expect(screen.getByRole("alert").textContent).toBe("Not a usable path.");
  });

  it("Open: 503 unavailable is a form-level error with a same-id retry that succeeds", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    api.fail("openVault", new ShellApiError(503, "unavailable"));
    render(
      <OpenVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        renderInspection={() => null}
      />,
    );
    fireEvent.change(screen.getByLabelText("Folder path"), { target: { value: "D:\\x" } });
    fireEvent.click(screen.getByText("Open"));
    await flush();
    expect(screen.getByRole("alert").textContent).toContain(
      "The operation could not be completed.",
    );
    const firstAttempt = api.calls.filter((call) => call.method === "openVault");
    expect(firstAttempt).toHaveLength(1);

    fireEvent.click(screen.getByText("Retry"));
    await flush();
    const afterRetry = api.calls.filter((call) => call.method === "openVault");
    expect(afterRetry).toHaveLength(2);
    // The same attempt replays: the id the retry sends is the id the first attempt sent.
    expect(afterRetry[1]?.args[0]).toBe(afterRetry[0]?.args[0]);
    // The retry succeeded (the fake's one scripted failure was already consumed): the result
    // panel, not the alert, is now on screen.
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it.each([
    ["mount_point", "The folder sits on a mounted volume; use a drive-letter path."],
    [
      "cloud_sync",
      "Cloud-synced folders cannot hold a vault: OneDrive, and on a Mac iCloud Drive and the other cloud folders under ~/Library.",
    ],
    ["drive_type", "This drive type (network, optical, …) cannot hold a vault."],
    ["no_identity", "This volume cannot identify folders (FAT32 and the like)."],
  ])("Open: an unsupported_location reason of %s picks its own sentence", async (reason, text) => {
    const api = fakeShellApi();
    const store = makeStore(api);
    api.fail("openVault", new ShellApiError(422, "unsupported_location", { extra: { reason } }));
    render(
      <OpenVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        renderInspection={() => null}
      />,
    );
    fireEvent.change(screen.getByLabelText("Folder path"), { target: { value: "D:\\x" } });
    fireEvent.click(screen.getByText("Open"));
    await flush();
    expect(screen.getByRole("alert").textContent).toBe(text);
  });

  it("Open: mount_point on macOS shows the POSIX sentence, not the drive-letter one", async () => {
    const restore = setPlatform("MacIntel");
    try {
      const api = fakeShellApi();
      const store = makeStore(api);
      api.fail(
        "openVault",
        new ShellApiError(422, "unsupported_location", { extra: { reason: "mount_point" } }),
      );
      render(
        <OpenVaultForm
          api={api}
          store={store}
          onBack={vi.fn()}
          onDone={vi.fn()}
          onGoToWorkspaces={vi.fn()}
          renderInspection={() => null}
        />,
      );
      fireEvent.change(screen.getByLabelText("Folder path"), { target: { value: "/x" } });
      fireEvent.click(screen.getByText("Open"));
      await flush();
      expect(screen.getByRole("alert").textContent).toBe(
        "VIKI cannot tell what kind of volume this folder is on; choose a folder on a local disk.",
      );
    } finally {
      restore();
    }
  });

  it("Open: mount_point on Linux shows the POSIX sentence too", async () => {
    const restore = setPlatform("Linux x86_64");
    try {
      const api = fakeShellApi();
      const store = makeStore(api);
      api.fail(
        "openVault",
        new ShellApiError(422, "unsupported_location", { extra: { reason: "mount_point" } }),
      );
      render(
        <OpenVaultForm
          api={api}
          store={store}
          onBack={vi.fn()}
          onDone={vi.fn()}
          onGoToWorkspaces={vi.fn()}
          renderInspection={() => null}
        />,
      );
      fireEvent.change(screen.getByLabelText("Folder path"), { target: { value: "/x" } });
      fireEvent.click(screen.getByText("Open"));
      await flush();
      expect(screen.getByRole("alert").textContent).toBe(
        "VIKI cannot tell what kind of volume this folder is on; choose a folder on a local disk.",
      );
    } finally {
      restore();
    }
  });

  it("Open: an unsupported_location reason of network shows its own sentence on every OS", async () => {
    for (const platform of [undefined, "MacIntel", "Linux x86_64"]) {
      const restore = platform ? setPlatform(platform) : (): void => {};
      try {
        const api = fakeShellApi();
        const store = makeStore(api);
        api.fail(
          "openVault",
          new ShellApiError(422, "unsupported_location", { extra: { reason: "network" } }),
        );
        render(
          <OpenVaultForm
            api={api}
            store={store}
            onBack={vi.fn()}
            onDone={vi.fn()}
            onGoToWorkspaces={vi.fn()}
            renderInspection={() => null}
          />,
        );
        fireEvent.change(screen.getByLabelText("Folder path"), { target: { value: "/x" } });
        fireEvent.click(screen.getByText("Open"));
        await flush();
        expect(screen.getByRole("alert").textContent).toBe("Network folders cannot hold a vault.");
      } finally {
        restore();
        cleanup();
      }
    }
  });

  it("Open: an unrecognized unsupported_location reason falls back to the generic sentence", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    api.fail(
      "openVault",
      new ShellApiError(422, "unsupported_location", { extra: { reason: "some_future_reason" } }),
    );
    render(
      <OpenVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        renderInspection={() => null}
      />,
    );
    fireEvent.change(screen.getByLabelText("Folder path"), { target: { value: "D:\\x" } });
    fireEvent.click(screen.getByText("Open"));
    await flush();
    expect(screen.getByRole("alert").textContent).toBe("This volume cannot hold a vault.");
  });

  it("Create: invalid_name lands on the name field", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    api.fail("createVault", new ShellApiError(400, "invalid_name"));
    render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "x" } });
    act(() => void vi.advanceTimersByTime(150));
    await flush();
    fireEvent.click(screen.getByText("Create"));
    await flush();
    screen.getByText("Not a usable name (1–256 characters)."); // throws if absent
  });
});

describe("a transport failure is form-level, not a false field refusal", () => {
  it("Create: a dropped connection (status 0) is form-level, and Retry replays the same id", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    api.fail("createVault", new ShellApiError(0, "network_error"));
    render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Trip" } });
    act(() => void vi.advanceTimersByTime(150));
    await flush();
    fireEvent.click(screen.getByText("Create"));
    await flush();
    // Not pinned to the folder field: no aria-invalid, and the sentence is the transport one,
    // rendered as a form-level alert with a Retry button rather than a field error.
    expect(screen.getByLabelText("Folder name").getAttribute("aria-invalid")).toBe("false");
    expect(screen.getByRole("alert").textContent).toContain("The server could not be reached.");
    const firstAttempt = api.calls.filter((call) => call.method === "createVault");
    expect(firstAttempt).toHaveLength(1);

    fireEvent.click(screen.getByText("Retry"));
    await flush();
    const afterRetry = api.calls.filter((call) => call.method === "createVault");
    expect(afterRetry).toHaveLength(2);
    // The same attempt replays: the id the retry sends is the id the first attempt sent.
    expect(afterRetry[1]?.args[0]).toBe(afterRetry[0]?.args[0]);
    expect(screen.queryByRole("alert")).toBeNull(); // the fake's one scripted failure is consumed
  });

  it("Create: a 500 with an unreadable body is also form-level, not pinned to the folder field", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    api.fail("createVault", new ShellApiError(500, "http_error"));
    render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Trip" } });
    act(() => void vi.advanceTimersByTime(150));
    await flush();
    fireEvent.click(screen.getByText("Create"));
    await flush();
    expect(screen.getByLabelText("Folder name").getAttribute("aria-invalid")).toBe("false");
    expect(screen.getByRole("alert").textContent).toContain("The server refused the request.");
  });

  it("Create: the client's own invalid_input refusal stays a field error (status 0 is not always ambiguous)", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    api.fail("createVault", new ShellApiError(0, "invalid_input"));
    render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Trip" } });
    act(() => void vi.advanceTimersByTime(150));
    await flush();
    fireEvent.click(screen.getByText("Create"));
    await flush();
    expect(screen.getByLabelText("Folder name").getAttribute("aria-invalid")).toBe("true");
    expect(screen.queryByText("Retry")).toBeNull();
  });

  it("Create: pressing Create again with nothing edited also replays the same id, not just Retry", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    api.fail("createVault", new ShellApiError(0, "timeout"));
    render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Trip" } });
    act(() => void vi.advanceTimersByTime(150));
    await flush();
    fireEvent.click(screen.getByText("Create"));
    await flush();
    const firstAttempt = api.calls.filter((call) => call.method === "createVault");
    expect(firstAttempt).toHaveLength(1);

    fireEvent.click(screen.getByText("Create"));
    await flush();
    const afterSecondCreate = api.calls.filter((call) => call.method === "createVault");
    expect(afterSecondCreate).toHaveLength(2);
    expect(afterSecondCreate[1]?.args[0]).toBe(afterSecondCreate[0]?.args[0]);
  });

  it("Create: editing the folder after a transport failure mints a fresh id instead of reusing the stale one", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    api.fail("createVault", new ShellApiError(0, "network_error"));
    render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Trip" } });
    act(() => void vi.advanceTimersByTime(150));
    await flush();
    fireEvent.click(screen.getByText("Create"));
    await flush();
    const firstAttempt = api.calls.filter((call) => call.method === "createVault");
    expect(firstAttempt).toHaveLength(1);

    fireEvent.change(screen.getByLabelText("Folder name"), { target: { value: "Different" } });
    fireEvent.click(screen.getByText("Create"));
    await flush();
    const afterEdit = api.calls.filter((call) => call.method === "createVault");
    expect(afterEdit).toHaveLength(2);
    expect(afterEdit[1]?.args[0]).not.toBe(afterEdit[0]?.args[0]);
  });

  it("Open: a dropped connection (status 0) is form-level, and Retry replays the same id", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    api.fail("openVault", new ShellApiError(0, "network_error"));
    render(
      <OpenVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        renderInspection={() => null}
      />,
    );
    fireEvent.change(screen.getByLabelText("Folder path"), { target: { value: "D:\\x" } });
    fireEvent.click(screen.getByText("Open"));
    await flush();
    expect(screen.getByLabelText("Folder path").getAttribute("aria-invalid")).toBe("false");
    expect(screen.getByRole("alert").textContent).toContain("The server could not be reached.");

    fireEvent.click(screen.getByText("Retry"));
    await flush();
    const afterRetry = api.calls.filter((call) => call.method === "openVault");
    expect(afterRetry).toHaveLength(2);
    expect(afterRetry[1]?.args[0]).toBe(afterRetry[0]?.args[0]);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("Open: pressing Open again with nothing edited also replays the same id, not just Retry", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    api.fail("openVault", new ShellApiError(408, "intake_timeout"));
    render(
      <OpenVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        renderInspection={() => null}
      />,
    );
    fireEvent.change(screen.getByLabelText("Folder path"), { target: { value: "D:\\x" } });
    fireEvent.click(screen.getByText("Open"));
    await flush();
    fireEvent.click(screen.getByText("Open"));
    await flush();
    const calls = api.calls.filter((call) => call.method === "openVault");
    expect(calls).toHaveLength(2);
    expect(calls[1]?.args[0]).toBe(calls[0]?.args[0]);
  });
});

describe("a stalled bootstrap can be cancelled from the form", () => {
  it("cancel_bootstrap offers cancel (asserting cancelled) and a retry", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    api.fail(
      "createVault",
      new ShellApiError(409, "destination_not_empty", {
        hint: "cancel_bootstrap",
        extra: { workspace_id: FAKE_WORKSPACE_ID },
      }),
    );
    render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Trip" } });
    act(() => void vi.advanceTimersByTime(150));
    await flush();
    fireEvent.click(screen.getByText("Create"));
    await flush();
    screen.getByText("A previous creation is still in progress."); // throws if absent

    fireEvent.click(screen.getByText("Cancel"));
    await flush();
    const cancelCall = api.calls.find((call) => call.method === "cancelBootstrap");
    expect(cancelCall?.args[1]).toBe(FAKE_WORKSPACE_ID);
  });

  it("a failed cancel shows the refusal without clearing the stalled state", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    api.fail(
      "createVault",
      new ShellApiError(409, "destination_not_empty", {
        hint: "cancel_bootstrap",
        extra: { workspace_id: FAKE_WORKSPACE_ID },
      }),
    );
    api.fail("cancelBootstrap", new ShellApiError(409, "not_app_created"));
    render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Trip" } });
    act(() => void vi.advanceTimersByTime(150));
    await flush();
    fireEvent.click(screen.getByText("Create"));
    await flush();
    fireEvent.click(screen.getByText("Cancel"));
    await flush();
    screen.getByText("The folder holds files this app did not create; nothing was removed."); // throws if absent
    screen.getByText("A previous creation is still in progress."); // still shown, throws if absent
  });
});

describe("open_existing only hands the Open form a full destination", () => {
  it("offers the switch, with the full parent\\folder path, when both fields are set", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    const onOpenExisting = vi.fn();
    api.fail(
      "createVault",
      new ShellApiError(409, "destination_not_empty", { hint: "open_existing" }),
    );
    render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={onOpenExisting}
      />,
    );
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Trip" } });
    act(() => void vi.advanceTimersByTime(150));
    await flush();
    // The suggestion fills both parent and folder from the name; both are non-empty now.
    expect((screen.getByLabelText("Parent folder") as HTMLInputElement).value).toBe(
      "D:\\VIKI\\Vaults",
    );
    expect((screen.getByLabelText("Folder name") as HTMLInputElement).value).toBe("Trip");
    fireEvent.click(screen.getByText("Create"));
    await flush();
    fireEvent.click(screen.getByText("Open as an existing folder"));
    expect(onOpenExisting).toHaveBeenCalledWith("D:\\VIKI\\Vaults\\Trip");
  });

  it("falls through to the plain field error when the folder is blank (no full destination to hand off)", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    const onOpenExisting = vi.fn();
    api.fail(
      "createVault",
      new ShellApiError(409, "destination_not_empty", { hint: "open_existing" }),
    );
    render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={onOpenExisting}
      />,
    );
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Trip" } });
    act(() => void vi.advanceTimersByTime(150));
    await flush();
    fireEvent.change(screen.getByLabelText("Folder name"), { target: { value: "" } });
    fireEvent.click(screen.getByText("Create"));
    await flush();
    expect(screen.queryByText("Open as an existing folder")).toBeNull();
    screen.getByText("The folder already has content."); // throws if absent
    expect(onOpenExisting).not.toHaveBeenCalled();
  });
});

describe("opening a folder shows it registered read-only", () => {
  it("renders the inspection through renderInspection and says read-only", async () => {
    const api = fakeShellApi({ inspection: inspectionFixture() });
    const store = makeStore(api);
    const renderInspection = vi.fn(() => <div data-testid="inspection">shown</div>);
    render(
      <OpenVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        renderInspection={renderInspection}
      />,
    );
    fireEvent.change(screen.getByLabelText("Folder path"), { target: { value: "D:\\x" } });
    fireEvent.click(screen.getByText("Open"));
    await flush();
    screen.getByText("This folder is registered as read-only."); // throws if absent
    screen.getByTestId("inspection"); // throws if absent
    expect(renderInspection).toHaveBeenCalledWith(inspectionFixture(), FAKE_WORKSPACE_ID);
  });
});

describe("a new vault is selected for the next launch", () => {
  it("POSTs select with the new id after create, then shows the restart sentence", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Trip" } });
    act(() => void vi.advanceTimersByTime(150));
    await flush();
    fireEvent.click(screen.getByText("Create"));
    await flush();

    const methods = api.calls.map((call) => call.method);
    const createIndex = methods.indexOf("createVault");
    const selectIndex = methods.indexOf("selectWorkspace");
    expect(createIndex).toBeGreaterThanOrEqual(0);
    expect(selectIndex).toBeGreaterThan(createIndex);
    const selectCall = api.calls[selectIndex];
    expect(selectCall?.args[1]).toBe(FAKE_WORKSPACE_ID);
    screen.getByText("Switches on the next launch."); // throws if absent
    screen.getByText("Back to chat"); // throws if absent
  });
});

describe("a failed select leaves the vault registered and says how to select it", () => {
  it("shows the registered sentence, the error, and a Workspaces button", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    api.fail("selectWorkspace", new ShellApiError(503, "unavailable"));
    const onGoToWorkspaces = vi.fn();
    render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={onGoToWorkspaces}
        onOpenExisting={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Trip" } });
    act(() => void vi.advanceTimersByTime(150));
    await flush();
    fireEvent.click(screen.getByText("Create"));
    await flush();

    screen.getByText(
      "The vault is registered. To use it on the next launch, select it from the Workspaces page.",
    ); // throws if absent
    screen.getByText("The operation could not be completed."); // the select's own error, throws if absent
    fireEvent.click(screen.getByText("Go to the Workspaces page"));
    expect(onGoToWorkspaces).toHaveBeenCalledTimes(1);
  });

  it("Open: shows the same registered sentence and the select's own error", async () => {
    const api = fakeShellApi({ inspection: inspectionFixture() });
    const store = makeStore(api);
    api.fail("selectWorkspace", new ShellApiError(503, "unavailable"));
    render(
      <OpenVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        renderInspection={() => null}
      />,
    );
    fireEvent.change(screen.getByLabelText("Folder path"), { target: { value: "D:\\x" } });
    fireEvent.click(screen.getByText("Open"));
    await flush();
    screen.getByText("This folder is registered as read-only."); // throws if absent
    screen.getByText("The operation could not be completed."); // throws if absent
  });
});

describe("onCreated fires exactly once per successful create", () => {
  it("calls onCreated once with the create answer, and a rerender does not call it again", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    const onCreated = vi.fn();
    const { rerender } = render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={vi.fn()}
        onCreated={onCreated}
      />,
    );
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Trip" } });
    act(() => void vi.advanceTimersByTime(150));
    await flush();
    fireEvent.click(screen.getByText("Create"));
    await flush();

    expect(onCreated).toHaveBeenCalledTimes(1);
    const result = onCreated.mock.calls[0]?.[0] as CreateVaultResult;
    expect(result.created_files).toEqual(["Welcome.md"]);
    expect(result.workspace.workspace_id).toBe(FAKE_WORKSPACE_ID);

    rerender(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={vi.fn()}
        onCreated={onCreated}
      />,
    );
    await flush();
    expect(onCreated).toHaveBeenCalledTimes(1);
  });

  it("never calls onCreated when the create itself is refused", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    api.fail("createVault", new ShellApiError(409, "destination_not_empty"));
    const onCreated = vi.fn();
    render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={vi.fn()}
        onCreated={onCreated}
      />,
    );
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Trip" } });
    act(() => void vi.advanceTimersByTime(150));
    await flush();
    fireEvent.click(screen.getByText("Create"));
    await flush();

    expect(onCreated).not.toHaveBeenCalled();
  });

  it("still fires exactly once when the follow-up select fails, alongside the registered sentence", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    api.fail("selectWorkspace", new ShellApiError(503, "unavailable"));
    const onCreated = vi.fn();
    render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={vi.fn()}
        onCreated={onCreated}
      />,
    );
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Trip" } });
    act(() => void vi.advanceTimersByTime(150));
    await flush();
    fireEvent.click(screen.getByText("Create"));
    await flush();

    expect(onCreated).toHaveBeenCalledTimes(1);
    screen.getByText(
      "The vault is registered. To use it on the next launch, select it from the Workspaces page.",
    ); // throws if absent
  });
});

describe("the example path and open_existing join follow the platform", () => {
  it("Create: a /home/you parent joins open_existing's path with /", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    const onOpenExisting = vi.fn();
    api.fail(
      "createVault",
      new ShellApiError(409, "destination_not_empty", { hint: "open_existing" }),
    );
    render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={onOpenExisting}
      />,
    );
    fireEvent.change(screen.getByLabelText("Parent folder"), {
      target: { value: "/home/you/VIKI/Vaults" },
    });
    fireEvent.change(screen.getByLabelText("Folder name"), { target: { value: "Trip" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Trip" } });
    fireEvent.click(screen.getByText("Create"));
    await flush();
    fireEvent.click(screen.getByText("Open as an existing folder"));
    expect(onOpenExisting).toHaveBeenCalledWith("/home/you/VIKI/Vaults/Trip");
  });

  it("Create: a Windows parent still joins open_existing's path with \\", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    const onOpenExisting = vi.fn();
    api.fail(
      "createVault",
      new ShellApiError(409, "destination_not_empty", { hint: "open_existing" }),
    );
    render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={onOpenExisting}
      />,
    );
    fireEvent.change(screen.getByLabelText("Parent folder"), {
      target: { value: "D:\\VIKI\\Vaults" },
    });
    fireEvent.change(screen.getByLabelText("Folder name"), { target: { value: "Trip" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Trip" } });
    fireEvent.click(screen.getByText("Create"));
    await flush();
    fireEvent.click(screen.getByText("Open as an existing folder"));
    expect(onOpenExisting).toHaveBeenCalledWith("D:\\VIKI\\Vaults\\Trip");
  });

  it("shows the mac example path on macOS", async () => {
    const restore = setPlatform("MacIntel");
    try {
      const api = fakeShellApi();
      const store = makeStore(api);
      render(
        <CreateVaultForm
          api={api}
          store={store}
          onBack={vi.fn()}
          onDone={vi.fn()}
          onGoToWorkspaces={vi.fn()}
          onOpenExisting={vi.fn()}
        />,
      );
      screen.getByText("/Users/you/VIKI/Vaults/My notes"); // throws if absent
    } finally {
      restore();
    }
  });

  it("shows the linux example path on Linux, in the Open form too", async () => {
    const restore = setPlatform("Linux x86_64");
    try {
      const api = fakeShellApi();
      const store = makeStore(api);
      render(
        <OpenVaultForm
          api={api}
          store={store}
          onBack={vi.fn()}
          onDone={vi.fn()}
          onGoToWorkspaces={vi.fn()}
          renderInspection={() => null}
        />,
      );
      screen.getByText("/home/you/VIKI/Vaults/My notes"); // throws if absent
    } finally {
      restore();
    }
  });

  it("shows the Windows example path on the default jsdom platform", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={vi.fn()}
      />,
    );
    screen.getByText("D:\\VIKI\\Vaults\\My notes"); // throws if absent
  });
});

describe("nothing submits natively", () => {
  // The record allows a `<form onSubmit={e => e.preventDefault()}>` whose buttons stay
  // `type="button"` (never `type="submit"`); the actual submit action always runs through an
  // explicit handler (a button's onClick, or PathForm's own Enter handling), never through the
  // form's own submit event. What is pinned here: every button is `type="button"`, and the
  // form's own submit event (Enter in a field the app did not itself intercept, a stray
  // `requestSubmit()`) is cancelled rather than left to navigate the page.
  it("CreateVaultForm and OpenVaultForm's <form> never actually submits", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    const { container: createContainer } = render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={vi.fn()}
      />,
    );
    const createForm = createContainer.querySelector("form");
    expect(createForm).not.toBeNull();
    for (const button of createContainer.querySelectorAll("button"))
      expect(button.getAttribute("type")).toBe("button");
    expect(fireEvent.submit(createForm as HTMLFormElement)).toBe(false);

    const { container: openContainer } = render(
      <OpenVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        renderInspection={() => null}
      />,
    );
    const openForm = openContainer.querySelector("form");
    expect(openForm).not.toBeNull();
    for (const button of openContainer.querySelectorAll("button"))
      expect(button.getAttribute("type")).toBe("button");
    expect(fireEvent.submit(openForm as HTMLFormElement)).toBe(false);
  });
});

describe("the starter template choice reaches createVault", () => {
  it("viki is the default: hides the portable checkbox, shows the preview, and sends template viki with portable_metadata true", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={vi.fn()}
      />,
    );
    expect(screen.queryByLabelText("Write portable metadata")).toBeNull();
    screen.getByText(
      "AGENTS.md and CLAUDE.md: the persona and the vault's rules. The assistant is called VIKI and replies in English.",
    ); // throws if absent — the preview already shows the (default) names

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Trip" } });
    act(() => void vi.advanceTimersByTime(150));
    await flush();
    fireEvent.click(screen.getByText("Create"));
    await flush();

    const call = api.calls.find((item) => item.method === "createVault");
    const input = call?.args[1] as CreateVaultInput;
    expect(input.template).toBe("viki");
    expect(input.portable_metadata).toBe(true);
    // A vault has no language of its own: no field asks for one and none is sent.
    expect(screen.queryByLabelText("Vault language")).toBeNull();
    expect("language" in input).toBe(false);
  });

  it("Blank folder shows the checkbox again and sends template blank, with portable_metadata following it", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByLabelText("Blank folder"));
    fireEvent.click(screen.getByLabelText("Write portable metadata")); // throws if absent

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Trip" } });
    act(() => void vi.advanceTimersByTime(150));
    await flush();
    fireEvent.click(screen.getByText("Create"));
    await flush();

    const call = api.calls.find((item) => item.method === "createVault");
    const input = call?.args[1] as CreateVaultInput;
    expect(input.template).toBe("blank");
    expect(input.portable_metadata).toBe(true);
  });

  // There is no "changing the template after a failed attempt mints a fresh command id" case:
  // it could only pass because jsdom lets a click change a disabled radio; no browser does. A
  // network_error locks the template radios (see retryLocked below), so that click can never
  // happen for real. The id-minting mechanism itself is already covered by "editing the folder
  // after a transport failure mints a fresh id..." above (a field that really does stay
  // editable), and what Retry actually resends while locked is covered by the lock describe
  // block below.
});

describe("a Retry locks the template and portable choices to what it reserved", () => {
  it("disables the radios and checkbox, shows why, and Retry resends the first attempt's template", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    api.fail("createVault", new ShellApiError(0, "network_error"));
    render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByLabelText("Blank folder"));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Trip" } });
    act(() => void vi.advanceTimersByTime(150));
    await flush();
    fireEvent.click(screen.getByText("Create"));
    await flush();

    const viki = screen.getByLabelText("Start with VIKI (recommended)") as HTMLInputElement;
    const blank = screen.getByLabelText("Blank folder") as HTMLInputElement;
    const checkbox = screen.getByLabelText("Write portable metadata") as HTMLInputElement;
    expect(viki.disabled).toBe(true);
    expect(blank.disabled).toBe(true);
    expect(checkbox.disabled).toBe(true);
    // A form-level retryable refusal: no real Cancel button sits on this screen, and
    // Back is no way to choose again either (a same-folder retry would just get open_existing),
    // so the lock line names only the control that is actually there.
    const reason = screen.getByText(
      "Retry continues this setup when its starter files and setup details still match.",
    );
    expect(checkbox.getAttribute("aria-describedby")).toBe(reason.id);
    expect(screen.queryByText(/Cancel to choose again/)).toBeNull();

    fireEvent.click(screen.getByText("Retry"));
    await flush();
    const calls = api.calls.filter((call) => call.method === "createVault");
    expect(calls).toHaveLength(2);
    const secondInput = calls[1]?.args[1] as CreateVaultInput;
    expect(secondInput.template).toBe("blank");
  });

  it("a stalled_cancel refusal keeps the Cancel sentence too, and its Cancel button really cancels", async () => {
    const api = fakeShellApi();
    const store = makeStore(api);
    api.fail(
      "createVault",
      new ShellApiError(409, "destination_not_empty", {
        hint: "cancel_bootstrap",
        extra: { workspace_id: FAKE_WORKSPACE_ID },
      }),
    );
    render(
      <CreateVaultForm
        api={api}
        store={store}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onGoToWorkspaces={vi.fn()}
        onOpenExisting={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Trip" } });
    act(() => void vi.advanceTimersByTime(150));
    await flush();
    fireEvent.click(screen.getByText("Create"));
    await flush();

    const viki = screen.getByLabelText("Start with VIKI (recommended)") as HTMLInputElement;
    expect(viki.disabled).toBe(true);
    screen.getByText(
      "Cancel this setup to choose again. If the starter files or setup details changed, start a new setup.",
    );

    fireEvent.click(screen.getByText("Cancel"));
    await flush();
    const cancelCall = api.calls.find((call) => call.method === "cancelBootstrap");
    expect(cancelCall?.args[1]).toBe(FAKE_WORKSPACE_ID);
  });
});
