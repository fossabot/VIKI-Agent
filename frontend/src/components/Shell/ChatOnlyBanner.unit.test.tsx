import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { WorkspacesListing } from "../../clients/app/shellApi";
import type { ShellSnapshot } from "../../state/shell";
import {
  capabilitiesFixture,
  FAKE_WORKSPACE_ID,
  listingFixture,
  workspaceFixture,
} from "../../testShell";
import { ChatOnlyBanner } from "./ChatOnlyBanner";

afterEach(cleanup);

function snapshotWith(
  overrides: Parameters<typeof capabilitiesFixture>[0],
  workspaces: WorkspacesListing | null = null,
): ShellSnapshot {
  return {
    capabilities: capabilitiesFixture(overrides),
    capabilitiesStatus: "ready",
    settings: null,
    settingsStatus: "unavailable",
    workspaces,
    workspacesStatus: workspaces === null ? "unavailable" : "ready",
    errors: { capabilities: "", settings: "", workspaces: "" },
    recovery: false,
    checkRunning: null,
  };
}

describe("ChatOnlyBanner", () => {
  it("renders null unless capabilities.workspace.chat_only is known true", () => {
    const notChatOnly = snapshotWith({ workspace: { chat_only: false } });
    const { container } = render(<ChatOnlyBanner snapshot={notChatOnly} onCommand={vi.fn()} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders null when capabilities are unknown", () => {
    const unknown: ShellSnapshot = { ...snapshotWith({}), capabilities: null };
    const { container } = render(<ChatOnlyBanner snapshot={unknown} onCommand={vi.fn()} />);
    expect(container.firstChild).toBeNull();
  });

  it("plain variant: two buttons, create and open", () => {
    const snapshot = snapshotWith({ workspace: { chat_only: true, restart_required: false } });
    const onCommand = vi.fn();
    render(<ChatOnlyBanner snapshot={snapshot} onCommand={onCommand} />);
    screen.getByText(
      "No vault is connected. This conversation runs in a shared temporary working folder.",
    ); // throws if absent
    fireEvent.click(screen.getByText("Create a vault"));
    expect(onCommand).toHaveBeenCalledWith("workspace.create");
    fireEvent.click(screen.getByText("Open a folder"));
    expect(onCommand).toHaveBeenCalledWith("workspace.open");
    expect(screen.queryByText("Switch workspace")).toBeNull();
  });

  it("registered variant: one button, workspace.select, when nothing is selected", () => {
    // workspace.select needs at least one registration to be enabled (commands.ts): the
    // registered variant only ever shows once a create/open registered exactly this workspace.
    const snapshot = snapshotWith(
      { workspace: { chat_only: true, restart_required: true } },
      listingFixture([workspaceFixture()]),
    );
    const onCommand = vi.fn();
    render(<ChatOnlyBanner snapshot={snapshot} onCommand={onCommand} />);
    screen.getByText(
      "A vault is registered and becomes active on the next launch. This conversation continues in the shared temporary folder.",
    ); // throws if absent
    expect(screen.queryByText("Create a vault")).toBeNull();
    expect(screen.queryByText("Open a folder")).toBeNull();
    fireEvent.click(screen.getByText("Switch workspace"));
    expect(onCommand).toHaveBeenCalledWith("workspace.select");
  });

  it("registered variant only once the listing confirms the selected vault is usable", () => {
    const snapshot = snapshotWith(
      { workspace: { chat_only: true, restart_required: true, selected_id: FAKE_WORKSPACE_ID } },
      listingFixture([workspaceFixture({ root_available: true, bootstrap: "none" })]),
    );
    render(<ChatOnlyBanner snapshot={snapshot} onCommand={vi.fn()} />);
    screen.getByText(
      "A vault is registered and becomes active on the next launch. This conversation continues in the shared temporary folder.",
    ); // throws if absent
  });

  it("names the real cause and restores Create/Open when the selected vault's root is gone", () => {
    const snapshot = snapshotWith(
      { workspace: { chat_only: true, restart_required: true, selected_id: FAKE_WORKSPACE_ID } },
      listingFixture([workspaceFixture({ root_available: false })]),
    );
    const onCommand = vi.fn();
    render(<ChatOnlyBanner snapshot={snapshot} onCommand={onCommand} />);
    screen.getByText("The folder is not where it was registered."); // throws if absent
    expect(screen.queryByText(/becomes active on the next launch/)).toBeNull();
    fireEvent.click(screen.getByText("Create a vault"));
    expect(onCommand).toHaveBeenCalledWith("workspace.create");
    fireEvent.click(screen.getByText("Open a folder"));
    expect(onCommand).toHaveBeenCalledWith("workspace.open");
    fireEvent.click(screen.getByText("Switch workspace"));
    expect(onCommand).toHaveBeenCalledWith("workspace.select");
  });

  it("names a stalled create when the selected vault's bootstrap never finished", () => {
    const snapshot = snapshotWith(
      { workspace: { chat_only: true, restart_required: true, selected_id: FAKE_WORKSPACE_ID } },
      listingFixture([workspaceFixture({ bootstrap: "pending" })]),
    );
    render(<ChatOnlyBanner snapshot={snapshot} onCommand={vi.fn()} />);
    screen.getByText("Creation is still in progress"); // throws if absent
  });

  it("an unread listing cannot disprove the registered sentence, so it stays", () => {
    const snapshot = snapshotWith({
      workspace: { chat_only: true, restart_required: true, selected_id: FAKE_WORKSPACE_ID },
    }); // workspaces: null (the listing has not been read this launch)
    render(<ChatOnlyBanner snapshot={snapshot} onCommand={vi.fn()} />);
    screen.getByText(
      "A vault is registered and becomes active on the next launch. This conversation continues in the shared temporary folder.",
    ); // throws if absent
  });

  it("an untrusted selected code folder is never activated: names the trust and restores actions", () => {
    const snapshot = snapshotWith(
      { workspace: { chat_only: true, restart_required: true, selected_id: FAKE_WORKSPACE_ID } },
      listingFixture([
        workspaceFixture({ kind: "code", policy: "existing_readonly", bootstrap: "none" }),
      ]),
    );
    render(<ChatOnlyBanner snapshot={snapshot} onCommand={vi.fn()} />);
    screen.getByText("Trust this folder first."); // throws if absent
    screen.getByText("Create a vault");
    screen.getByText("Open a folder");
    screen.getByText("Switch workspace");
    expect(screen.queryByText(/is registered and becomes active/)).toBeNull();
  });

  it("a trusted selected code folder gets the code sentence, not the vault one", () => {
    const snapshot = snapshotWith(
      { workspace: { chat_only: true, restart_required: true, selected_id: FAKE_WORKSPACE_ID } },
      listingFixture([workspaceFixture({ kind: "code", policy: "ordinary", bootstrap: "none" })]),
    );
    render(<ChatOnlyBanner snapshot={snapshot} onCommand={vi.fn()} />);
    screen.getByText(
      "A code folder is selected and becomes active on the next launch. This conversation continues in the shared temporary folder.",
    ); // throws if absent
    expect(screen.queryByText(/A vault is registered/)).toBeNull();
  });

  it("a disabled action is focusable, aria-disabled, and its click is a no-op", () => {
    const snapshot = snapshotWith({
      workspace: { chat_only: true, restart_required: false },
      storage: { state: "unavailable" },
    });
    const onCommand = vi.fn();
    render(<ChatOnlyBanner snapshot={snapshot} onCommand={onCommand} />);
    const createButton = screen.getByText("Create a vault").closest("button");
    expect(createButton?.getAttribute("aria-disabled")).toBe("true");
    expect(createButton?.tabIndex).toBe(0);
    fireEvent.click(createButton as HTMLButtonElement);
    expect(onCommand).not.toHaveBeenCalled();
  });
});
