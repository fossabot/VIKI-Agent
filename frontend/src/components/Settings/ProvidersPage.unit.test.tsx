import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { CheckResult } from "../../clients/app/shellApi";
import { ShellApiError } from "../../clients/app/shellApi";
import type { ShellSnapshot, ShellStore } from "../../state/shell";
import { capabilitiesFixture, listingFixture, workspaceFixture } from "../../testShell";
import { ProvidersPage } from "./ProvidersPage";

afterEach(cleanup);

/** jsdom exposes `navigator.platform` through a prototype getter, so this file's own property
 * must be restored afterward or a later test silently inherits it (same pattern as
 * vaultWritePlatform.unit.test.tsx). */
function setPlatform(value: string): () => void {
  Object.defineProperty(window.navigator, "platform", { value, configurable: true });
  return () => {
    delete (window.navigator as { platform?: string }).platform;
  };
}

/** `navigator.clipboard` is not a real property in jsdom; each test that stubs it must restore
 * the original afterward, or a later test in this file silently inherits a stale mock (same
 * pattern as ExportPanel.unit.test.tsx). */
function stubClipboard(write: ReturnType<typeof vi.fn>): () => void {
  const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText: write },
    configurable: true,
  });
  return () => {
    if (original) Object.defineProperty(navigator, "clipboard", original);
    else Reflect.deleteProperty(navigator, "clipboard");
  };
}

function snapshotFixture(overrides: Partial<ShellSnapshot> = {}): ShellSnapshot {
  return {
    capabilities: capabilitiesFixture(),
    capabilitiesStatus: "ready",
    settings: null,
    settingsStatus: "unavailable",
    workspaces: null,
    workspacesStatus: "unavailable",
    errors: { capabilities: "", settings: "", workspaces: "" },
    recovery: false,
    checkRunning: null,
    ...overrides,
  };
}

function fakeStore(runCheck: ShellStore["runCheck"]): ShellStore {
  return {
    getSnapshot: () => {
      throw new Error("not used by ProvidersPage");
    },
    subscribe: () => () => {},
    refreshCapabilities: vi.fn(async () => {}),
    refreshSettings: vi.fn(async () => {}),
    refreshWorkspaces: vi.fn(async () => {}),
    patch: vi.fn(async () => {
      throw new Error("not used by ProvidersPage");
    }),
    adoptSettings: vi.fn(),
    runCheck,
    afterMutation: vi.fn(async () => {}),
    start: vi.fn(),
    stop: vi.fn(),
  };
}

/** Mirrors the page's own `localTime`: computed here too so the expectation stays portable
 * across machines instead of pinning one timezone's rendering of a fixed instant. */
function localTime(iso: string): string {
  return new Date(iso).toLocaleString("en-US", { dateStyle: "medium", timeStyle: "short" });
}

/** The button rule ("every button is `btn` plus `go`, `alt` or `txt`, or a `settings-pill`),
 * reused wherever a test checks a specific set of buttons against it — the page-wide button test
 * and, since the Setup section mounts under its own fixtures and the default fixture never
 * reaches it, the Setup-section tests below. */
function isAcceptableButton(button: Element): boolean {
  const classes = button.className.split(/\s+/);
  return (
    (classes.includes("btn") &&
      ["go", "alt", "txt"].some((variant) => classes.includes(variant))) ||
    classes.includes("settings-pill")
  );
}

/** The Setup section's own `<section aria-labelledby="provider-setup-title">`, found by that
 * relationship rather than by its (translated) heading text. */
function setupSection(): Element | null {
  return document.getElementById("provider-setup-title")?.closest("section") ?? null;
}

test("readiness facts render from capabilities", () => {
  const snapshot = snapshotFixture({
    capabilities: capabilitiesFixture({
      provider: { cli: "ready", version: "1.2.3", login: "signed_in", billing: "clear" },
      git: { state: "ready", version: "2.45.0" },
    }),
  });
  render(<ProvidersPage store={fakeStore(vi.fn())} snapshot={snapshot} onOpenUrl={() => {}} />);
  expect(screen.getAllByText("Ready").length).toBe(2);
  expect(screen.getByText("1.2.3")).toBeTruthy();
  expect(screen.getByText("Signed in")).toBeTruthy();
  expect(screen.getByText("Clear")).toBeTruthy();
  expect(screen.getByText("2.45.0")).toBeTruthy();
  // An Installation row names the source (the default fixture's "installed").
  expect(screen.getByText("Your own installation")).toBeTruthy();
});

test("the Claude card's Last checked renders as a formatted <time> element", () => {
  const lastCheckedAt = "2026-09-17T00:00:00.000Z";
  const snapshot = snapshotFixture({
    capabilities: capabilitiesFixture({ provider: { last_checked_at: lastCheckedAt } }),
  });
  render(<ProvidersPage store={fakeStore(vi.fn())} snapshot={snapshot} onOpenUrl={() => {}} />);
  const card = screen.getByRole("heading", { name: "Claude Code" }).closest(".settings-card");
  const timeEl = card?.querySelector("time") ?? null;
  expect(timeEl).not.toBeNull();
  expect(timeEl?.getAttribute("dateTime")).toBe(lastCheckedAt);
  expect(timeEl?.textContent).toBe(localTime(lastCheckedAt));
});

test("Last checked shows an em dash when the provider has never been checked", () => {
  const snapshot = snapshotFixture({
    capabilities: capabilitiesFixture({ provider: { last_checked_at: null } }),
  });
  render(<ProvidersPage store={fakeStore(vi.fn())} snapshot={snapshot} onOpenUrl={() => {}} />);
  const card = screen.getByRole("heading", { name: "Claude Code" }).closest(".settings-card");
  expect(card?.querySelector("time")).toBeNull();
  expect(card?.textContent).toContain("—");
});

test("running a check shows its status, version, duration and a formatted checked-at time", async () => {
  const result: CheckResult = {
    check: "git",
    status: "ok",
    result: { version: "9.9.9" },
    checked_at: "2026-09-18T06:00:00.000Z",
    duration_ms: 42,
  };
  const runCheck = vi.fn(async () => result);
  render(
    <ProvidersPage store={fakeStore(runCheck)} snapshot={snapshotFixture()} onOpenUrl={() => {}} />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Check Git" }));
  expect(runCheck).toHaveBeenCalledWith("git");
  await waitFor(() => expect(screen.getByText("42 ms")).toBeTruthy());
  const card = screen.getByRole("heading", { name: "Git" }).closest(".settings-card");
  // Status and Version sit before Duration, in the same facts grid: the second one in the
  // card, the first being the git readiness facts rendered whether or not a check has run.
  const resultFacts = card?.querySelectorAll(".settings-facts")[1] ?? null;
  expect(Array.from(resultFacts?.querySelectorAll("dt") ?? []).map((dt) => dt.textContent)).toEqual(
    ["Status", "Version", "Duration", "Checked at"],
  );
  expect(resultFacts?.querySelector("dd.mono")?.textContent).toBe("9.9.9");
  expect(screen.getByText("OK")).toBeTruthy();
  const timeEl = card?.querySelector("time") ?? null;
  expect(timeEl?.getAttribute("dateTime")).toBe(result.checked_at);
  expect(timeEl?.textContent).toBe(localTime(result.checked_at));
});

test("a 409 check_in_progress is rendered through the catalog", async () => {
  const runCheck = vi.fn(async () => {
    throw new ShellApiError(409, "check_in_progress");
  });
  render(
    <ProvidersPage store={fakeStore(runCheck)} snapshot={snapshotFixture()} onOpenUrl={() => {}} />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Check Git" }));
  await waitFor(() =>
    expect(screen.getByRole("alert").textContent).toBe("Another check is running."),
  );
});

test("the provider_cli button is aria-disabled unless the cli is ready, with a described reason", () => {
  const snapshot = snapshotFixture({
    capabilities: capabilitiesFixture({ provider: { cli: "missing" } }),
  });
  render(<ProvidersPage store={fakeStore(vi.fn())} snapshot={snapshot} onOpenUrl={() => {}} />);
  const button = screen.getByRole("button", { name: "Check Claude Code" });
  expect(button.getAttribute("aria-disabled")).toBe("true");
  const describedBy = button.getAttribute("aria-describedby");
  expect(describedBy).not.toBeNull();
  expect(document.getElementById(describedBy as string)?.textContent).toBe(
    "Claude Code is not ready to be checked.",
  );
});

test("a disabled check button's click is a no-op", () => {
  const runCheck = vi.fn();
  const snapshot = snapshotFixture({
    capabilities: capabilitiesFixture({ provider: { cli: "missing" } }),
  });
  render(<ProvidersPage store={fakeStore(runCheck)} snapshot={snapshot} onOpenUrl={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Check Claude Code" }));
  expect(runCheck).not.toHaveBeenCalled();
});

test("the page is aria-busy while a check runs", () => {
  const snapshot = snapshotFixture({ checkRunning: "git" });
  const { container } = render(
    <ProvidersPage store={fakeStore(vi.fn())} snapshot={snapshot} onOpenUrl={() => {}} />,
  );
  expect(container.querySelector("[aria-busy]")?.getAttribute("aria-busy")).toBe("true");
});

test("installation instructions appear when the provider cannot start, and spawn nothing", () => {
  const restorePlatform = setPlatform("Win32");
  const snapshot = snapshotFixture({
    capabilities: capabilitiesFixture({
      provider: { cli: "missing", can_start: false, problems: ["not_found"] },
    }),
  });
  const runCheck = vi.fn();
  const { container } = render(
    <ProvidersPage store={fakeStore(runCheck)} snapshot={snapshot} onOpenUrl={vi.fn()} />,
  );
  expect(
    screen.getByText("Install Claude Code with Anthropic's installer, then restart VIKI Agent."),
  ).toBeTruthy();
  expect(screen.getByText(/does not run this for you/)).toBeTruthy();
  // The setup section's own buttons only copy text or open the guide link: the two check
  // buttons, two Windows install commands' Copy buttons, the guide button and four MCP setup
  // buttons, none of them the store's runCheck.
  expect(container.querySelectorAll("button").length).toBe(9);
  expect(runCheck).not.toHaveBeenCalled();
  restorePlatform();
});

test("a ready provider shows no setup section", () => {
  const snapshot = snapshotFixture({
    capabilities: capabilitiesFixture({ provider: { cli: "ready", can_start: true } }),
  });
  render(<ProvidersPage store={fakeStore(vi.fn())} snapshot={snapshot} onOpenUrl={() => {}} />);
  expect(screen.queryByText(/does not run this for you/)).toBeNull();
});

test("can_start false for a non-provider reason (storage) shows no empty setup block", () => {
  // cli ready, login signed in, billing clear: setupLines() is empty even though can_start is
  // false for some other readiness fact this page does not own (src/viki_agent/app/diagnostics.py).
  const snapshot = snapshotFixture({
    capabilities: capabilitiesFixture({
      provider: {
        cli: "ready",
        login: "signed_in",
        billing: "clear",
        can_start: false,
      },
    }),
  });
  const { container } = render(
    <ProvidersPage store={fakeStore(vi.fn())} snapshot={snapshot} onOpenUrl={() => {}} />,
  );
  expect(container.querySelector("#provider-setup-title")).toBeNull();
  const claudeCard = screen.getByRole("heading", { name: "Claude Code" }).closest(".settings-card");
  expect(claudeCard?.querySelector("pre")).toBeNull();
  expect(screen.getByRole("region", { name: "Add an MCP server" })).toBeTruthy();
});

test("every button is btn plus go, alt or txt, or a settings-pill", () => {
  render(
    <ProvidersPage store={fakeStore(vi.fn())} snapshot={snapshotFixture()} onOpenUrl={() => {}} />,
  );
  const buttons = screen.getAllByRole("button");
  expect(buttons.length > 0 && buttons.every(isAcceptableButton)).toBe(true);
});

test("each provider is a card with a title, a pill, a Check button and a facts grid", () => {
  const snapshot = snapshotFixture({
    capabilities: capabilitiesFixture({
      provider: { cli: "ready", version: "1.2.3" },
      git: { state: "missing", version: "" },
    }),
  });
  const { container } = render(
    <ProvidersPage store={fakeStore(vi.fn())} snapshot={snapshot} onOpenUrl={() => {}} />,
  );
  expect(container.querySelectorAll(".settings-card").length).toBe(3);

  const claudeCard = screen.getByRole("heading", { name: "Claude Code" }).closest(".settings-card");
  expect(claudeCard?.querySelector(".settings-pill")?.getAttribute("data-tone")).toBe("ready");
  expect(claudeCard?.querySelector(".settings-pill")?.textContent).toBe("Ready");
  expect(claudeCard?.querySelector("button")?.textContent).toBe("Check Claude Code");
  expect(claudeCard?.querySelector(".settings-facts")).not.toBeNull();

  const gitCard = screen.getByRole("heading", { name: "Git" }).closest(".settings-card");
  expect(gitCard?.querySelector(".settings-pill")?.getAttribute("data-tone")).toBe("error");
  expect(gitCard?.querySelector(".settings-pill")?.textContent).toBe("Not found");
  expect(gitCard?.querySelector("button")?.textContent).toBe("Check Git");
  expect(gitCard?.querySelector(".settings-facts")).not.toBeNull();
});

test("a missing CLI on Windows shows the PowerShell and WinGet install commands, each copyable, and a guide button", () => {
  const restorePlatform = setPlatform("Win32");
  const write = vi.fn().mockResolvedValue(undefined);
  const restoreClipboard = stubClipboard(write);
  const onOpenUrl = vi.fn();
  const runCheck = vi.fn();
  const snapshot = snapshotFixture({
    capabilities: capabilitiesFixture({ provider: { cli: "missing", can_start: false } }),
  });
  render(<ProvidersPage store={fakeStore(runCheck)} snapshot={snapshot} onOpenUrl={onOpenUrl} />);

  expect(screen.getByText("irm https://claude.ai/install.ps1 | iex")).toBeTruthy();
  expect(screen.getByText("winget install Anthropic.ClaudeCode")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "Copy the PowerShell command" }));
  expect(write).toHaveBeenCalledWith("irm https://claude.ai/install.ps1 | iex");

  fireEvent.click(screen.getByRole("button", { name: "Anthropic's setup guide" }));
  expect(onOpenUrl).toHaveBeenCalledWith("https://code.claude.com/docs/en/setup");
  // Copying a command and opening the guide never touch the store.
  expect(runCheck).not.toHaveBeenCalled();

  // The page-wide button rule covers the page's default fixture, which never mounts the Setup
  // section — hold its
  // own Copy and guide buttons to the same rule here instead.
  const setupButtons = Array.from(setupSection()?.querySelectorAll("button") ?? []);
  expect(setupButtons.length).toBe(3);
  expect(setupButtons.every(isAcceptableButton)).toBe(true);

  restoreClipboard();
  restorePlatform();
});

test("a missing CLI on macOS shows the curl and Homebrew install commands, not WinGet's", () => {
  const restorePlatform = setPlatform("MacIntel");
  const write = vi.fn().mockResolvedValue(undefined);
  const restoreClipboard = stubClipboard(write);
  const snapshot = snapshotFixture({
    capabilities: capabilitiesFixture({ provider: { cli: "missing", can_start: false } }),
  });
  render(<ProvidersPage store={fakeStore(vi.fn())} snapshot={snapshot} onOpenUrl={() => {}} />);

  expect(screen.getByText("curl -fsSL https://claude.ai/install.sh | bash")).toBeTruthy();
  expect(screen.getByText("brew install --cask claude-code")).toBeTruthy();
  expect(screen.queryByText("winget install Anthropic.ClaudeCode")).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Copy the Homebrew command" }));
  expect(write).toHaveBeenCalledWith("brew install --cask claude-code");

  restoreClipboard();
  restorePlatform();
});

test("a missing CLI on Linux shows only the curl install command", () => {
  const restorePlatform = setPlatform("Linux x86_64");
  const snapshot = snapshotFixture({
    capabilities: capabilitiesFixture({ provider: { cli: "missing", can_start: false } }),
  });
  const { container } = render(
    <ProvidersPage store={fakeStore(vi.fn())} snapshot={snapshot} onOpenUrl={() => {}} />,
  );

  expect(screen.getByText("curl -fsSL https://claude.ai/install.sh | bash")).toBeTruthy();
  expect(container.querySelectorAll(".providers-command").length).toBe(1);

  restorePlatform();
});

test("an older CLI shows update steps (claude update, then WinGet) and a package-manager note", () => {
  const restorePlatform = setPlatform("Win32");
  const snapshot = snapshotFixture({
    capabilities: capabilitiesFixture({
      provider: {
        cli: "ready",
        version: "2.1.200",
        version_status: "older",
        tested: ["2.1.281", "2.1.282"],
      },
    }),
  });
  render(<ProvidersPage store={fakeStore(vi.fn())} snapshot={snapshot} onOpenUrl={() => {}} />);

  expect(
    screen.getByText("Update Claude Code to 2.1.281 or newer, then choose Check Claude Code:"),
  ).toBeTruthy();
  expect(screen.getByText("claude update")).toBeTruthy();
  expect(screen.getByText("winget upgrade Anthropic.ClaudeCode")).toBeTruthy();
  expect(
    screen.getByText(
      "Installed it with WinGet, Homebrew or a Linux package manager? Upgrade it there instead.",
    ),
  ).toBeTruthy();

  // The same button rule, scoped to the Setup section (claude update, WinGet upgrade, guide).
  const setupButtons = Array.from(setupSection()?.querySelectorAll("button") ?? []);
  expect(setupButtons.length).toBe(3);
  expect(setupButtons.every(isAcceptableButton)).toBe(true);

  restorePlatform();
});

test("a newer CLI version adds only the newer note beside the version, with no setup section", () => {
  const snapshot = snapshotFixture({
    capabilities: capabilitiesFixture({
      provider: {
        version: "2.1.300",
        version_status: "newer",
        tested: ["2.1.281", "2.1.282"],
        source: "bundled",
      },
    }),
  });
  render(<ProvidersPage store={fakeStore(vi.fn())} snapshot={snapshot} onOpenUrl={() => {}} />);

  expect(screen.getByText("2.1.300")).toBeTruthy();
  expect(screen.getByText("· Newer than tested (2.1.281 to 2.1.282)")).toBeTruthy();
  expect(screen.getByText("SDK copy (source checkout)")).toBeTruthy();
  expect(screen.queryByText(/does not run this for you/)).toBeNull();
});

test("the Installation row shows an em dash when the source is empty", () => {
  const snapshot = snapshotFixture({
    capabilities: capabilitiesFixture({ provider: { source: "" } }),
  });
  render(<ProvidersPage store={fakeStore(vi.fn())} snapshot={snapshot} onOpenUrl={() => {}} />);
  const card = screen.getByRole("heading", { name: "Claude Code" }).closest(".settings-card");
  expect(card?.textContent).toContain("Installation");
  expect(screen.getByText("—")).toBeTruthy();
});

test("the MCP card shows the folder new chats start in", () => {
  const write = vi.fn().mockResolvedValue(undefined);
  const restoreClipboard = stubClipboard(write);
  const workspace = workspaceFixture({ name: "My notes", root: "D:\\vaults\\notes" });
  const snapshot = snapshotFixture({
    capabilities: capabilitiesFixture({
      workspace: {
        chat_only: false,
        active: {
          id: workspace.workspace_id,
          kind: "vault",
          name: workspace.name,
          policy: workspace.policy,
          layout: "unknown",
          source: workspace.source,
          aliased: false,
          root_available: true,
        },
      },
    }),
    workspaces: listingFixture([workspace]),
    workspacesStatus: "ready",
  });
  render(<ProvidersPage store={fakeStore(vi.fn())} snapshot={snapshot} onOpenUrl={() => {}} />);
  const region = screen.getByRole("region", { name: "Where to put .mcp.json" });
  expect(within(region).getByText("D:\\vaults\\notes")).toBeTruthy();
  fireEvent.click(within(region).getByRole("button", { name: "Copy folder path" }));
  expect(write).toHaveBeenCalledWith("D:\\vaults\\notes");
  restoreClipboard();
});

test("the MCP card says chat-only when no vault is active", () => {
  render(
    <ProvidersPage store={fakeStore(vi.fn())} snapshot={snapshotFixture()} onOpenUrl={() => {}} />,
  );
  const region = screen.getByRole("region", { name: "Where to put .mcp.json" });
  expect(within(region).getByText(/In chat-only mode/)).toBeTruthy();
});
