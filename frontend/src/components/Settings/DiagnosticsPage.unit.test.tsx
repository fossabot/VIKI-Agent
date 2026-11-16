import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ShellApiError } from "../../clients/app/shellApi";
import { DATA_FLOWS, NOT_AFFILIATED, PRODUCT_LINE } from "../../productFacts";
import type { ShellSnapshot } from "../../state/shell";
import {
  capabilitiesFixture,
  deferredShellApi,
  fakeShellApi,
  settingsFixture,
} from "../../testShell";
import { resetHelp, useHelpHost, useHelpTarget } from "../Help/helpStore";
import { DiagnosticsPage } from "./DiagnosticsPage";

afterEach(cleanup);
afterEach(resetHelp);

function snapshotFixture(overrides: Partial<ShellSnapshot> = {}): ShellSnapshot {
  return {
    capabilities: capabilitiesFixture(),
    capabilitiesStatus: "ready",
    settings: settingsFixture(),
    settingsStatus: "ready",
    workspaces: null,
    workspacesStatus: "unavailable",
    errors: { capabilities: "", settings: "", workspaces: "" },
    recovery: false,
    checkRunning: null,
    ...overrides,
  };
}

test("the app facts and the document render once the fetch resolves", async () => {
  const api = fakeShellApi({
    diagnostics: { app: { mode: "normal" }, note: "hello" },
  });
  render(<DiagnosticsPage api={api} snapshot={snapshotFixture()} onNavigate={vi.fn()} />);
  expect(screen.getByText("normal")).toBeTruthy();
  await waitFor(() => expect(screen.getByText("hello")).toBeTruthy());
  const report = screen.getByText("View report details").closest("details");
  expect(report?.open).toBe(false);
  fireEvent.click(screen.getByText("View report details"));
  expect(report?.open).toBe(true);
});

test("a failed fetch renders the error text, not a stuck loading state", async () => {
  const api = fakeShellApi();
  api.fail("diagnostics", new ShellApiError(0, "network_error"));
  render(<DiagnosticsPage api={api} snapshot={snapshotFixture()} onNavigate={vi.fn()} />);
  await waitFor(() =>
    expect(screen.getByRole("alert").textContent).toBe("The server could not be reached."),
  );
});

test("the paths note and its link show only while paths are excluded", async () => {
  const api = fakeShellApi();
  const onNavigate = vi.fn();
  const { rerender } = render(
    <DiagnosticsPage
      api={api}
      snapshot={snapshotFixture({
        settings: settingsFixture({ "privacy.diagnostics_include_paths": false }),
      })}
      onNavigate={onNavigate}
    />,
  );
  await waitFor(() => expect(api.calls.some((c) => c.method === "diagnostics")).toBe(true));
  fireEvent.click(screen.getByRole("button", { name: "Open Privacy settings" }));
  expect(onNavigate).toHaveBeenCalledWith({ view: "settings", page: "privacy" });

  rerender(
    <DiagnosticsPage
      api={api}
      snapshot={snapshotFixture({
        settings: settingsFixture({ "privacy.diagnostics_include_paths": true }),
      })}
      onNavigate={onNavigate}
    />,
  );
  expect(screen.queryByRole("button", { name: "Open Privacy settings" })).toBeNull();
});

test("in the recovery host the privacy button is aria-disabled and its click is inert", async () => {
  // create_recovery_app mounts no settings route, so settings stays unread — the exact state
  // that leaves the paths note on screen with a button that, ungated, would navigate a view the
  // recovery host pins away from (see App.tsx).
  const api = fakeShellApi();
  const onNavigate = vi.fn();
  render(
    <DiagnosticsPage
      api={api}
      snapshot={snapshotFixture({ recovery: true, settings: null, settingsStatus: "unavailable" })}
      onNavigate={onNavigate}
    />,
  );
  const button = screen.getByRole("button", { name: "Open Privacy settings" });
  expect(button.getAttribute("aria-disabled")).toBe("true");
  expect(button.getAttribute("title")).toBe("Unavailable in recovery mode.");
  const describedBy = button.getAttribute("aria-describedby");
  expect(describedBy).not.toBeNull();
  expect(document.getElementById(describedBy as string)?.textContent).toBe(
    "Unavailable in recovery mode.",
  );
  fireEvent.click(button);
  expect(onNavigate).not.toHaveBeenCalled();
});

test("outside recovery the privacy button stays enabled and navigates on click", () => {
  const api = fakeShellApi();
  const onNavigate = vi.fn();
  render(
    <DiagnosticsPage
      api={api}
      snapshot={snapshotFixture({
        settings: settingsFixture({ "privacy.diagnostics_include_paths": false }),
      })}
      onNavigate={onNavigate}
    />,
  );
  const button = screen.getByRole("button", { name: "Open Privacy settings" });
  expect(button.getAttribute("aria-disabled")).toBe("false");
  fireEvent.click(button);
  expect(onNavigate).toHaveBeenCalledWith({ view: "settings", page: "privacy" });
});

test("pending reads show their state and disable repeated Reload without moving focus", async () => {
  const api = deferredShellApi();
  render(<DiagnosticsPage api={api} snapshot={snapshotFixture()} onNavigate={vi.fn()} />);
  const button = screen.getByRole("button", { name: "Reload" }) as HTMLButtonElement;
  expect(screen.getByRole("status").textContent).toBe("Loading diagnostics…");
  expect(screen.getByRole("status").closest('[aria-busy="true"]')).toBeNull();
  expect(button.disabled).toBe(true);
  fireEvent.click(button);
  expect(api.pending).toHaveLength(1);
  await act(async () => api.pending[0]?.resolve({ note: "first-read" }));
  expect(button.disabled).toBe(false);
  expect(screen.queryByRole("status")).toBeNull();
  button.focus();
  fireEvent.click(button);
  expect(screen.getByRole("status").textContent).toBe("Reloading diagnostics…");
  expect(screen.getByRole("status").closest('[aria-busy="true"]')).toBeNull();
  expect(button.disabled).toBe(true);
  expect(screen.getByText("first-read")).toBeTruthy();
  fireEvent.click(button);
  expect(api.pending).toHaveLength(2);
  await act(async () => api.pending[1]?.resolve({ note: "reloaded" }));
  expect(screen.getByText("reloaded")).toBeTruthy();
  expect(button.disabled).toBe(false);
  expect(document.activeElement).toBe(button);
});

test("a failed Reload keeps the document already on screen", async () => {
  const api = fakeShellApi({ diagnostics: { note: "first-fetch-marker" } });
  render(<DiagnosticsPage api={api} snapshot={snapshotFixture()} onNavigate={vi.fn()} />);
  await waitFor(() => expect(screen.getByText("first-fetch-marker")).toBeTruthy());

  api.fail("diagnostics", new ShellApiError(0, "network_error"));
  fireEvent.click(screen.getByRole("button", { name: "Reload" }));
  await waitFor(() =>
    expect(screen.getByRole("alert").textContent).toBe("The server could not be reached."),
  );
  // The document from the first, successful fetch is still on screen next to the new error.
  expect(screen.getByText("first-fetch-marker")).toBeTruthy();
  expect(
    screen.getByText("The latest diagnostics read failed. Showing the last successful read."),
  ).toBeTruthy();
  expect((screen.getByRole("button", { name: "Reload" }) as HTMLButtonElement).disabled).toBe(
    false,
  );
});

test("a replacement API clears the previous document and reload error even if its read fails", async () => {
  const oldApi = deferredShellApi();
  const newApi = deferredShellApi();
  const props = { snapshot: snapshotFixture(), onNavigate: vi.fn() };
  const { rerender } = render(<DiagnosticsPage api={oldApi} {...props} />);
  await act(async () => oldApi.pending[0]?.resolve({ note: "old-document" }));
  fireEvent.click(screen.getByRole("button", { name: "Reload" }));
  await act(async () => oldApi.pending[1]?.reject(new ShellApiError(0, "network_error")));
  expect(screen.getByRole("alert")).toBeTruthy();
  rerender(<DiagnosticsPage api={newApi} {...props} />);
  expect(screen.queryByText("old-document")).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByRole("status").textContent).toBe("Loading diagnostics…");
  await act(async () => newApi.pending[0]?.reject(new ShellApiError(0, "network_error")));
  expect(screen.getByRole("alert").textContent).toBe("The server could not be reached.");
  expect(screen.queryByText("old-document")).toBeNull();
  expect(screen.queryByText(/Showing the last successful read/)).toBeNull();
});

test.each(["resolve", "reject"] as const)("a replaced API ignores a late %s", async (outcome) => {
  const oldApi = deferredShellApi();
  const newApi = deferredShellApi();
  const props = { snapshot: snapshotFixture(), onNavigate: vi.fn() };
  const { rerender } = render(<DiagnosticsPage api={oldApi} {...props} />);
  rerender(<DiagnosticsPage api={newApi} {...props} />);
  await act(async () => newApi.pending[0]?.resolve({ note: "current-document" }));
  await act(async () =>
    oldApi.pending[0]?.[outcome](
      outcome === "resolve" ? { note: "late-document" } : new ShellApiError(0, "network_error"),
    ),
  );
  expect(screen.getByText("current-document")).toBeTruthy();
  expect(screen.queryByText("late-document")).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
});

test("About offers bundled license downloads and the existing What's new help page", () => {
  function HelpHost() {
    useHelpHost();
    return null;
  }
  render(
    <>
      <HelpHost />
      <DiagnosticsPage api={null} snapshot={snapshotFixture()} onNavigate={vi.fn()} />
    </>,
  );
  const { result: target } = renderHook(useHelpTarget);
  expect(screen.getByText("About VIKI Agent")).toBeTruthy();
  expect(screen.getByRole("heading", { name: "Diagnostics report" })).toBeTruthy();
  const license = screen.getByRole("link", { name: "VIKI Agent license" });
  expect(license.getAttribute("href")).toBe("/LICENSE.txt");
  expect(license.getAttribute("download")).toBe("VIKI-Agent-LICENSE.txt");
  const notices = screen.getByRole("link", { name: "Third-party notices" });
  expect(notices.getAttribute("href")).toBe("/THIRD_PARTY_NOTICES.txt");
  expect(notices.getAttribute("download")).toBe("THIRD_PARTY_NOTICES.txt");
  expect(screen.queryByText("Licences and release notes appear in a future update.")).toBeNull();
  const button = screen.getByRole("button", { name: "What's new" }) as HTMLButtonElement;
  expect(button.disabled).toBe(false);
  fireEvent.click(button);
  expect(target.current).toEqual({ kind: "whats-new" });
});

test("About disables What's new when no help host is mounted", () => {
  render(<DiagnosticsPage api={null} snapshot={snapshotFixture()} onNavigate={vi.fn()} />);
  const { result: target } = renderHook(useHelpTarget);
  const button = screen.getByRole("button", { name: "What's new" }) as HTMLButtonElement;
  expect(button.disabled).toBe(true);
  expect(button.getAttribute("title")).toBe("Help is unavailable in this window.");
  fireEvent.click(button);
  expect(target.current).toBeNull();
});

test("an absent API settles as unavailable and cannot reload", async () => {
  const api = deferredShellApi();
  const props = { snapshot: snapshotFixture(), onNavigate: vi.fn() };
  const { rerender } = render(<DiagnosticsPage api={null} {...props} />);
  expect(screen.getByRole("status").textContent).toBe(
    "Diagnostics are unavailable in this window.",
  );
  rerender(<DiagnosticsPage api={api} {...props} />);
  rerender(<DiagnosticsPage api={null} {...props} />);
  expect(screen.getByRole("status").textContent).toBe(
    "Diagnostics are unavailable in this window.",
  );
  const button = screen.getByRole("button", { name: "Reload" }) as HTMLButtonElement;
  expect(button.disabled).toBe(true);
  fireEvent.click(button);
  await act(async () => api.pending[0]?.resolve({ note: "removed-api-document" }));
  expect(api.pending).toHaveLength(1);
  expect(screen.queryByText("removed-api-document")).toBeNull();
});

test("Last approvals check section shows off message when undefined", () => {
  render(
    <DiagnosticsPage
      api={null}
      snapshot={snapshotFixture()}
      onNavigate={vi.fn()}
      approvalsRead={undefined}
    />,
  );
  expect(screen.getByText("Off").getAttribute("data-tone")).toBeNull();
  expect(
    screen.getByText(
      "Approvals have not been checked in this window. A vault must be open and its approvals available.",
    ),
  ).toBeTruthy();
});

test("Last approvals check section shows off message when status is off", () => {
  render(
    <DiagnosticsPage
      api={null}
      snapshot={snapshotFixture()}
      onNavigate={vi.fn()}
      approvalsRead={{
        status: "off",
        readAt: null,
        workspaceId: null,
        attention: null,
      }}
    />,
  );
  expect(screen.getByText("Off").getAttribute("data-tone")).toBeNull();
  expect(
    screen.getByText(
      "Approvals have not been checked in this window. A vault must be open and its approvals available.",
    ),
  ).toBeTruthy();
});

test("Last approvals check section shows reading message when status is reading", () => {
  render(
    <DiagnosticsPage
      api={null}
      snapshot={snapshotFixture()}
      onNavigate={vi.fn()}
      approvalsRead={{
        status: "reading",
        readAt: null,
        workspaceId: null,
        attention: null,
      }}
    />,
  );
  expect(screen.getByText("Reading").getAttribute("data-tone")).toBe("attention");
  expect(screen.getByText("Reading…")).toBeTruthy();
});

test("Last approvals check section shows last read info when status is ready", () => {
  const readAt = "2026-10-09T12:00:00Z";
  render(
    <DiagnosticsPage
      api={null}
      snapshot={snapshotFixture()}
      onNavigate={vi.fn()}
      approvalsRead={{
        status: "ready",
        readAt,
        workspaceId: "workspace-123",
        attention: 5,
      }}
    />,
  );
  // Check that the section title and its pill exist
  expect(screen.getByRole("heading", { name: "Last approvals check" })).toBeTruthy();
  expect(screen.getByText("OK").getAttribute("data-tone")).toBe("ready");
  // Verify the section contains all expected content
  const section = document.querySelector('[aria-labelledby="approvals-read-title"]');
  expect(section?.textContent).toContain("Last read");
  expect(section?.textContent).toContain("5");
  expect(section?.textContent).toContain("need attention");
  expect(section?.textContent).toContain("workspace");
  expect(section?.textContent).toContain("workspace-123");
  // The displayed time is locale-formatted; only its machine-readable dateTime is pinned here,
  // and the formatted text is computed with the same call so this stays portable across machines.
  const timeEl = document.querySelector("time");
  expect(timeEl?.tagName).toBe("TIME");
  expect(timeEl?.getAttribute("dateTime")).toBe(readAt);
  expect(timeEl?.textContent).toBe(
    new Date(readAt).toLocaleString("en-US", { dateStyle: "medium", timeStyle: "short" }),
  );
});

test("Last approvals check section shows failed message when status is failed", () => {
  const readAt = "2026-10-09T11:00:00Z";
  render(
    <DiagnosticsPage
      api={null}
      snapshot={snapshotFixture()}
      onNavigate={vi.fn()}
      approvalsRead={{
        status: "failed",
        readAt,
        workspaceId: "workspace-456",
        attention: 3,
      }}
    />,
  );
  expect(screen.getByText("Failed").getAttribute("data-tone")).toBe("error");
  // Check that the failed message appears
  expect(
    screen.getByText("The latest read failed; this is the last successful read."),
  ).toBeTruthy();
  // Verify the time is still shown, keyed off its machine-readable attribute
  const timeEl = document.querySelector("time");
  expect(timeEl?.tagName).toBe("TIME");
  expect(timeEl?.getAttribute("dateTime")).toBe(readAt);
});

test("Last approvals check section shows only the first-failure message when nothing has ever been read", () => {
  render(
    <DiagnosticsPage
      api={null}
      snapshot={snapshotFixture()}
      onNavigate={vi.fn()}
      approvalsRead={{
        status: "failed",
        readAt: null,
        workspaceId: null,
        attention: null,
      }}
    />,
  );
  expect(screen.getByText("Failed").getAttribute("data-tone")).toBe("error");
  expect(screen.getByText("Could not check approvals. No successful check yet.")).toBeTruthy();
  const section = document.querySelector('[aria-labelledby="approvals-read-title"]');
  expect(section?.textContent).not.toContain("Last read");
  expect(section?.textContent).not.toContain("last successful read");
});

test("Last approvals check section handles null attention", () => {
  const readAt = "2026-10-09T12:00:00Z";
  render(
    <DiagnosticsPage
      api={null}
      snapshot={snapshotFixture()}
      onNavigate={vi.fn()}
      approvalsRead={{
        status: "ready",
        readAt,
        workspaceId: "workspace-123",
        attention: null,
      }}
    />,
  );
  const section = document.querySelector('[aria-labelledby="approvals-read-title"]');
  // Time and workspace should be shown
  expect(document.querySelector("time")?.getAttribute("dateTime")).toBe(readAt);
  expect(section?.textContent).toContain("workspace-123");
  // But attention should not be mentioned
  expect(section?.textContent).not.toContain("need attention");
});

test("Last approvals check section handles null workspaceId", () => {
  const readAt = "2026-10-09T12:00:00Z";
  render(
    <DiagnosticsPage
      api={null}
      snapshot={snapshotFixture()}
      onNavigate={vi.fn()}
      approvalsRead={{
        status: "ready",
        readAt,
        workspaceId: null,
        attention: 2,
      }}
    />,
  );
  const section = document.querySelector('[aria-labelledby="approvals-read-title"]');
  // Time and attention should be shown
  expect(document.querySelector("time")?.getAttribute("dateTime")).toBe(readAt);
  expect(section?.textContent).toContain("2");
  expect(section?.textContent).toContain("need attention");
  // But neither "workspace" label nor any workspace ID should appear
  expect(section?.textContent).not.toContain("workspace");
});

test("the About facts list Version before Mode before Launch when a version is present", () => {
  render(
    <DiagnosticsPage
      api={null}
      snapshot={snapshotFixture({
        capabilities: capabilitiesFixture({ app: { version: "1.2.3" } }),
      })}
      onNavigate={vi.fn()}
    />,
  );
  const about = screen.getByRole("region", { name: "About VIKI Agent" });
  const labels = Array.from(about.querySelectorAll(".settings-facts dt")).map(
    (dt) => dt.textContent,
  );
  expect(labels).toEqual(["Version", "Mode", "Launch"]);
});

test("the About region shows the product line and independence notice, even with capabilities null", () => {
  render(
    <DiagnosticsPage
      api={null}
      snapshot={snapshotFixture({ capabilities: null, capabilitiesStatus: "unavailable" })}
      onNavigate={vi.fn()}
    />,
  );
  const about = screen.getByRole("region", { name: "About VIKI Agent" });
  expect(about.textContent).toContain(PRODUCT_LINE);
  expect(about.textContent).toContain(NOT_AFFILIATED);
  // Capabilities are null, so the Version/Mode/Launch facts list is absent.
  expect(about.querySelector(".settings-facts")).toBeNull();
});

test('the "Where your data goes" region lists every DATA_FLOWS label and fact, in order', () => {
  render(<DiagnosticsPage api={null} snapshot={snapshotFixture()} onNavigate={vi.fn()} />);
  const region = screen.getByRole("region", { name: "Where your data goes" });
  const rows = Array.from(region.querySelectorAll(".settings-facts > div")).map((row) => ({
    label: row.querySelector("dt")?.textContent,
    fact: row.querySelector("dd")?.textContent,
  }));
  expect(rows).toEqual(DATA_FLOWS.map(({ label, fact }) => ({ label, fact })));
});

test("an unparsable read time falls back to showing the raw string", () => {
  render(
    <DiagnosticsPage
      api={null}
      snapshot={snapshotFixture()}
      onNavigate={vi.fn()}
      approvalsRead={{
        status: "ready",
        readAt: "not-a-real-timestamp",
        workspaceId: null,
        attention: null,
      }}
    />,
  );
  const timeEl = document.querySelector("time");
  expect(timeEl?.getAttribute("dateTime")).toBe("not-a-real-timestamp");
  expect(timeEl?.textContent).toBe("not-a-real-timestamp");
});

test("About and Last approvals check are separate named regions; About's region holds no approvals text", () => {
  render(
    <DiagnosticsPage
      api={null}
      snapshot={snapshotFixture()}
      onNavigate={vi.fn()}
      approvalsRead={{
        status: "ready",
        readAt: "2026-10-09T12:00:00Z",
        workspaceId: "workspace-123",
        attention: 5,
      }}
    />,
  );
  const about = screen.getByRole("region", { name: "About VIKI Agent" });
  const approvals = screen.getByRole("region", { name: "Last approvals check" });
  expect(about).not.toBe(approvals);
  expect(about.textContent).not.toContain("Last read");
  expect(about.textContent).not.toContain("workspace-123");
  expect(approvals.textContent).toContain("Last read");
});

test("every rendered button carries the shared btn class and a variant, or is a settings-pill", () => {
  render(<DiagnosticsPage api={null} snapshot={snapshotFixture()} onNavigate={vi.fn()} />);
  const buttons = screen.getAllByRole("button");
  expect(
    buttons.length > 0 &&
      buttons.every((button) => {
        const classes = button.className.split(/\s+/);
        return (
          (classes.includes("btn") &&
            ["go", "alt", "txt"].some((variant) => classes.includes(variant))) ||
          classes.includes("settings-pill")
        );
      }),
  ).toBe(true);
});
