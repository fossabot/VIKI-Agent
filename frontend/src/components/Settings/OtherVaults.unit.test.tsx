import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { Capabilities, WorkspacesListing } from "../../clients/app/shellApi";
import { ShellApiError } from "../../clients/app/shellApi";
import type { ShellSnapshot, ShellStore } from "../../state/shell";
import {
  capabilitiesFixture,
  fakeShellApi,
  listingFixture,
  workspaceFixture,
} from "../../testShell";
import { OtherVaults } from "./OtherVaults";

afterEach(cleanup);

function snapshotFixture(
  capabilities: Capabilities,
  workspaces: WorkspacesListing | null,
  overrides: Partial<ShellSnapshot> = {},
): ShellSnapshot {
  return {
    capabilities,
    capabilitiesStatus: "ready",
    settings: null,
    settingsStatus: "unavailable",
    workspaces,
    workspacesStatus: workspaces === null ? "unavailable" : "ready",
    errors: { capabilities: "", settings: "", workspaces: "" },
    recovery: false,
    checkRunning: null,
    ...overrides,
  };
}

function fakeStore(): ShellStore {
  return {
    getSnapshot: () => {
      throw new Error("not used by OtherVaults");
    },
    subscribe: () => () => {},
    refreshCapabilities: vi.fn(async () => {}),
    refreshSettings: vi.fn(async () => {}),
    refreshWorkspaces: vi.fn(async () => {}),
    patch: vi.fn(async () => {
      throw new Error("not used by OtherVaults");
    }),
    adoptSettings: vi.fn(),
    runCheck: vi.fn(async () => {
      throw new Error("not used by OtherVaults");
    }),
    afterMutation: vi.fn(async () => {}),
    start: vi.fn(),
    stop: vi.fn(),
  };
}

test("Use this vault shows the restart sentence", async () => {
  const workspace = workspaceFixture({ name: "My notes" });
  const listing = listingFixture([workspace]);
  const capabilities = capabilitiesFixture({ workspace: { chat_only: true } });
  const api = fakeShellApi({ workspaces: listing });
  render(
    <OtherVaults api={api} store={fakeStore()} snapshot={snapshotFixture(capabilities, listing)} />,
  );
  expect(screen.getByText("Other vaults:")).toBeTruthy();
  const row = screen.getByText("My notes").closest(".other-vault-row") as HTMLElement;
  fireEvent.click(within(row).getByRole("button", { name: "Use this vault" }));
  await waitFor(() => expect(screen.getByText("Switches on the next launch.")).toBeTruthy());
  const call = api.calls.find((c) => c.method === "selectWorkspace");
  expect(call?.args[1]).toBe(workspace.workspace_id);
  expect(screen.getByText("· Switching vaults applies after a restart.")).toBeTruthy();
});

test("an unavailable other vault shows the not-found chip", () => {
  const workspace = workspaceFixture({ name: "Old vault", root_available: false });
  const listing = listingFixture([workspace]);
  const capabilities = capabilitiesFixture({ workspace: { chat_only: false } });
  render(
    <OtherVaults
      api={fakeShellApi()}
      store={fakeStore()}
      snapshot={snapshotFixture(capabilities, listing)}
    />,
  );
  const row = screen.getByText("Old vault").closest(".other-vault-row") as HTMLElement;
  expect(within(row).getByText("Not found")).toBeTruthy();
});

test("a pending vault shows the pending chip and a cancel action, not Use this vault", async () => {
  const workspace = workspaceFixture({ name: "New vault", bootstrap: "pending" });
  const listing = listingFixture([workspace]);
  const capabilities = capabilitiesFixture({ workspace: { chat_only: false } });
  const api = fakeShellApi({ workspaces: listing });
  const store = fakeStore();
  render(<OtherVaults api={api} store={store} snapshot={snapshotFixture(capabilities, listing)} />);
  const row = screen.getByText("New vault").closest(".other-vault-row") as HTMLElement;
  expect(within(row).getByText("Creation is still in progress")).toBeTruthy();
  expect(within(row).queryByRole("button", { name: "Use this vault" })).toBeNull();
  fireEvent.click(within(row).getByRole("button", { name: "Cancel" }));
  await waitFor(() => expect(api.calls.some((c) => c.method === "cancelBootstrap")).toBe(true));
  await waitFor(() =>
    expect(store.afterMutation).toHaveBeenCalledWith(["capabilities", "workspaces"]),
  );
});

test("a failed cancel shows the refusal instead of failing silently", async () => {
  const workspace = workspaceFixture({ name: "New vault", bootstrap: "pending" });
  const listing = listingFixture([workspace]);
  const capabilities = capabilitiesFixture({ workspace: { chat_only: false } });
  const api = fakeShellApi({ workspaces: listing });
  api.fail("cancelBootstrap", new ShellApiError(503, "unavailable"));
  render(
    <OtherVaults api={api} store={fakeStore()} snapshot={snapshotFixture(capabilities, listing)} />,
  );
  const row = screen.getByText("New vault").closest(".other-vault-row") as HTMLElement;
  fireEvent.click(within(row).getByRole("button", { name: "Cancel" }));
  await waitFor(() =>
    expect(screen.getByRole("alert").textContent).toBe("The operation could not be completed."),
  );
  // The pending chip is still there: the row was never quietly cleared.
  expect(within(row).getByText("Creation is still in progress")).toBeTruthy();
});

test("a failed select shows the refusal instead of failing silently", async () => {
  const workspace = workspaceFixture({ name: "My notes" });
  const listing = listingFixture([workspace]);
  const capabilities = capabilitiesFixture({ workspace: { chat_only: true } });
  const api = fakeShellApi({ workspaces: listing });
  api.fail("selectWorkspace", new ShellApiError(503, "unavailable"));
  render(
    <OtherVaults api={api} store={fakeStore()} snapshot={snapshotFixture(capabilities, listing)} />,
  );
  const row = screen.getByText("My notes").closest(".other-vault-row") as HTMLElement;
  fireEvent.click(within(row).getByRole("button", { name: "Use this vault" }));
  await waitFor(() =>
    expect(screen.getByRole("alert").textContent).toBe("The operation could not be completed."),
  );
});

test("the Chat only row's Use this vault posts a null workspace id", async () => {
  // A code-kind registration only, so `workspace.select` is enabled (it gates on any
  // registration existing) while `otherVaults` itself stays empty — isolating the Chat only row.
  const listing = listingFixture([workspaceFixture({ kind: "code", name: "Project" })]);
  const capabilities = capabilitiesFixture({ workspace: { active: null, chat_only: false } });
  const api = fakeShellApi({ workspaces: listing });
  render(
    <OtherVaults api={api} store={fakeStore()} snapshot={snapshotFixture(capabilities, listing)} />,
  );
  expect(screen.getByText("Chat only (no folder)")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Use this vault" }));
  await waitFor(() =>
    expect(api.calls.some((c) => c.method === "selectWorkspace" && c.args[1] === null)).toBe(true),
  );
  // The fake's own active is still its default scratch, so posting null is a no-op selection —
  // the same "already in use" branch the scratch row hits in WorkspacesPage.
  await waitFor(() => expect(screen.getByText("This workspace is already in use.")).toBeTruthy());
});

test("the Chat only row is omitted once the active workspace is already scratch", () => {
  const listing = listingFixture([workspaceFixture()]);
  const capabilities = capabilitiesFixture({ workspace: { chat_only: true } });
  render(
    <OtherVaults
      api={fakeShellApi()}
      store={fakeStore()}
      snapshot={snapshotFixture(capabilities, listing)}
    />,
  );
  expect(screen.getByText("My notes")).toBeTruthy();
  expect(screen.queryByText("Chat only (no folder)")).toBeNull();
});

test("renders nothing when there are no other vaults and the active workspace is already scratch", () => {
  const listing = listingFixture([]);
  const capabilities = capabilitiesFixture({ workspace: { chat_only: true } });
  const { container } = render(
    <OtherVaults
      api={fakeShellApi()}
      store={fakeStore()}
      snapshot={snapshotFixture(capabilities, listing)}
    />,
  );
  expect(container.firstChild).toBeNull();
});

test("Use this vault is disabled with a reason in a recovery window", () => {
  const listing = listingFixture([]);
  const capabilities = capabilitiesFixture({ workspace: { active: null, chat_only: false } });
  const api = fakeShellApi();
  render(
    <OtherVaults
      api={api}
      store={fakeStore()}
      snapshot={snapshotFixture(capabilities, listing, { recovery: true })}
    />,
  );
  const button = screen.getByRole("button", { name: "Use this vault" });
  expect(button.getAttribute("aria-disabled")).toBe("true");
  expect(button.getAttribute("title")).toBe("Unavailable in recovery mode.");
  const describedBy = button.getAttribute("aria-describedby");
  expect(describedBy).not.toBeNull();
  expect(document.getElementById(describedBy as string)?.textContent).toBe(
    "Unavailable in recovery mode.",
  );
  fireEvent.click(button);
  expect(api.calls.some((c) => c.method === "selectWorkspace")).toBe(false);
});

test("the line has one space after the label and a middot between items, not doubled before the restart note", () => {
  // No active workspace at all: every vault-kind row counts as "other", and chat-only also
  // shows (its active kind is not "scratch") — one case exercises both separators at once.
  const workspace = workspaceFixture({ name: "My notes" });
  const listing = listingFixture([workspace]);
  const capabilities = capabilitiesFixture({ workspace: { active: null, chat_only: false } });
  render(
    <OtherVaults
      api={fakeShellApi()}
      store={fakeStore()}
      snapshot={snapshotFixture(capabilities, listing)}
    />,
  );
  const line = screen.getByText("Other vaults:").closest("p");
  expect(line?.textContent).toBe(
    "Other vaults: My notesUse this vault · Chat only (no folder)Use this vault" +
      " · Switching vaults applies after a restart.",
  );
});
