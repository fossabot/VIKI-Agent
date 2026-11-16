import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ShellSnapshot } from "../../state/shell";
import { capabilitiesFixture } from "../../testShell";
import { ReadinessBanner } from "./ReadinessBanner";

afterEach(cleanup);

function snapshotWith(overrides: Parameters<typeof capabilitiesFixture>[0]): ShellSnapshot {
  return {
    capabilities: capabilitiesFixture(overrides),
    capabilitiesStatus: "ready",
    settings: null,
    settingsStatus: "unavailable",
    workspaces: null,
    workspacesStatus: "unavailable",
    errors: { capabilities: "", settings: "", workspaces: "" },
    recovery: false,
    checkRunning: null,
  };
}

describe("one reason at a time, in order", () => {
  it("renders null when capabilities are unknown", () => {
    const unknown: ShellSnapshot = { ...snapshotWith({}), capabilities: null };
    const { container } = render(<ReadinessBanner snapshot={unknown} onCommand={vi.fn()} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders null when the provider can already start", () => {
    const ready = snapshotWith({ provider: { can_start: true } });
    const { container } = render(<ReadinessBanner snapshot={ready} onCommand={vi.fn()} />);
    expect(container.firstChild).toBeNull();
  });

  it("config_dir_override wins over every other problem", () => {
    const snapshot = snapshotWith({
      provider: {
        can_start: false,
        cli: "config_dir_override",
        billing: "blocked",
        login: "signed_out",
      },
      storage: { state: "unavailable" },
    });
    render(<ReadinessBanner snapshot={snapshot} onCommand={vi.fn()} />);
    screen.getByText("CLAUDE_CONFIG_DIR is set; sessions cannot start until it is removed."); // throws if absent
  });

  it("unusable (batch shim) is next", () => {
    const snapshot = snapshotWith({ provider: { can_start: false, cli: "unusable" } });
    render(<ReadinessBanner snapshot={snapshot} onCommand={vi.fn()} />);
    screen.getByText(
      "The configured Claude Code path is a batch wrapper, not claude.exe; sessions cannot start.",
    ); // throws if absent
  });

  it("missing is next", () => {
    const snapshot = snapshotWith({ provider: { can_start: false, cli: "missing" } });
    render(<ReadinessBanner snapshot={snapshot} onCommand={vi.fn()} />);
    screen.getByText("Claude Code was not found; sessions cannot start."); // throws if absent
  });

  it("billing blocked is next", () => {
    const snapshot = snapshotWith({ provider: { can_start: false, billing: "blocked" } });
    render(<ReadinessBanner snapshot={snapshot} onCommand={vi.fn()} />);
    screen.getByText("Billing is blocked for this account; sessions cannot start."); // throws if absent
  });

  it("storage is next", () => {
    const snapshot = snapshotWith({
      provider: { can_start: false },
      storage: { state: "unavailable" },
    });
    render(<ReadinessBanner snapshot={snapshot} onCommand={vi.fn()} />);
    screen.getByText("The app store is not ready; sessions cannot start."); // throws if absent
  });

  it("storage wins over a stale signed-out latch, because can_start has no login term", () => {
    // The old version of this case asserted `login: "signed_out"` against an otherwise-healthy
    // fixture (cli/billing/storage all left at their ready defaults) -- a `can_start: false` the
    // backend can never emit, since its formula (src/viki_agent/app/diagnostics.py) omits login entirely. This is
    // the reachable shape instead: cli ready, billing clear, login latched signed_out from an
    // earlier failed start in this launch, and storage actually the thing that is broken -- the
    // storage sentence is the only true cause, so it must be what renders.
    const snapshot = snapshotWith({
      provider: { can_start: false, login: "signed_out" },
      storage: { state: "unavailable" },
    });
    render(<ReadinessBanner snapshot={snapshot} onCommand={vi.fn()} />);
    screen.getByText("The app store is not ready; sessions cannot start."); // throws if absent
    expect(screen.queryByText("Not signed in to Claude Code; sessions cannot start.")).toBeNull();
  });

  it("carries id=readiness-reason and a Set up provider button that dispatches provider.setup", () => {
    const snapshot = snapshotWith({ provider: { can_start: false, cli: "missing" } });
    const onCommand = vi.fn();
    render(<ReadinessBanner snapshot={snapshot} onCommand={onCommand} />);
    expect(document.getElementById("readiness-reason")).not.toBeNull();
    fireEvent.click(screen.getByText("Set up provider"));
    expect(onCommand).toHaveBeenCalledWith("provider.setup");
  });
});
