import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { narrowBlock, readCss, ruleIn } from "../../testing/css";
import { LeftRail } from "./LeftRail";

afterEach(cleanup);

// jsdom applies no CSS from stylesheets (only inline styles), so the box-sizing metrics
// this component is graded on are checked here against the source instead.
const css = readCss(import.meta.url, "LeftRail.css");

test("the search box is 38px, matching the design (jsdom applies no CSS)", () => {
  const rule = css.match(/\.rail-search\s*\{[^}]*\}/)?.[0];
  expect(rule).toMatch(/height:\s*38px/);
});

test("the session list carries no extra top margin (it would stack with the Today label's 18px)", () => {
  const rule = css.match(/\.rail-sessions\s*\{[^}]*\}/)?.[0];
  expect(rule).not.toMatch(/margin-top/);
});

test("forced colors distinguish the selected destination from its keyboard focus", () => {
  const forced = narrowBlock(css, "@media (forced-colors: active)");
  expect(ruleIn(forced, '.rail-nav-button[aria-current="page"]')).toMatch(
    /border-color:\s*Highlight/,
  );
  expect(forced).toMatch(
    /\.rail-nav-button:focus-visible,\s*\.rail-workspace:focus-visible\s*\{[^}]*outline-color:\s*Highlight/,
  );
  expect(ruleIn(css, ".rail-nav-button:focus-visible,\n.rail-workspace:focus-visible")).toMatch(
    /outline:\s*2px solid/,
  );
});

test("the search box calls onSearch", () => {
  const onSearch = vi.fn();
  render(<LeftRail onSearch={onSearch} />);

  fireEvent.click(screen.getByText("Search sessions"));
  expect(onSearch).toHaveBeenCalledTimes(1);
});

test("optional navigation keeps the available destinations and current view explicit", () => {
  const onNewConversation = vi.fn();
  const onNavigate = vi.fn();
  const onWorkspace = vi.fn();
  render(
    <LeftRail
      onNewConversation={onNewConversation}
      onNavigate={onNavigate}
      destinations={["chat", "capabilities", "vault", "code"]}
      currentView="capabilities"
      workspace={{ label: "Code lab", detail: "Ordinary workspace" }}
      onWorkspace={onWorkspace}
    >
      <span>Existing sessions</span>
    </LeftRail>,
  );
  expect(screen.queryByText("VIKI Agent")).toBeNull();
  expect(screen.getByRole("button", { name: "Capabilities" }).getAttribute("aria-current")).toBe(
    "page",
  );
  expect(screen.queryByRole("button", { name: "Agents" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "New session" }));
  fireEvent.click(screen.getByRole("button", { name: "Code" }));
  fireEvent.click(screen.getByRole("button", { name: "Choose workspace" }));
  expect(onNewConversation).toHaveBeenCalledTimes(1);
  expect(onNavigate).toHaveBeenCalledExactlyOnceWith("code", undefined);
  expect(onWorkspace).toHaveBeenCalledTimes(1);
  expect(screen.getByText("Code lab")).toBeTruthy();
  expect(screen.getByText("Existing sessions")).toBeTruthy();
});

test("legacy callers keep only search and their children; workspace names remain text", () => {
  const { rerender, container } = render(<LeftRail>Sessions</LeftRail>);
  expect(screen.getAllByRole("button")).toHaveLength(1);
  expect(screen.queryByText("VIKI Agent")).toBeNull();
  rerender(<LeftRail workspace={{ label: "<img src=x>" }}>Sessions</LeftRail>);
  expect(screen.getByText("<img src=x>")).toBeTruthy();
  expect(container.querySelector("img")).toBeNull();
  expect(screen.getAllByRole("button")).toHaveLength(1);
});

test("creation actions remain focusable with a readiness reason and refuse clicks until enabled", () => {
  const onNewConversation = vi.fn();
  const { rerender } = render(
    <LeftRail
      onNewConversation={onNewConversation}
      newSession={{ disabled: true, describedBy: "readiness-reason" }}
    />,
  );
  const button = screen.getByRole("button", { name: "New session" }) as HTMLButtonElement;
  expect(button.disabled).toBe(false);
  expect(button.getAttribute("aria-disabled")).toBe("true");
  expect(button.getAttribute("aria-describedby")).toBe("readiness-reason");
  button.focus();
  expect(document.activeElement).toBe(button);
  fireEvent.click(button);
  expect(onNewConversation).not.toHaveBeenCalled();

  rerender(
    <LeftRail
      onNewConversation={onNewConversation}
      newSession={{ disabled: false, describedBy: "readiness-reason" }}
    />,
  );
  const enabled = screen.getByRole("button", { name: "New session" });
  expect(enabled.hasAttribute("aria-disabled")).toBe(false);
  expect(enabled.hasAttribute("aria-describedby")).toBe(false);
  fireEvent.click(enabled);
  expect(onNewConversation).toHaveBeenCalledOnce();
});

test("navigation, session creation and workspace management forward their exact drawer origin", () => {
  const onNavigate = vi.fn();
  const onNewConversation = vi.fn();
  const onWorkspace = vi.fn();
  render(
    <div role="dialog" aria-modal="true" aria-label="Sidebar drawer">
      <LeftRail
        onNavigate={onNavigate}
        onNewConversation={onNewConversation}
        onWorkspace={onWorkspace}
      />
    </div>,
  );
  const drawer = screen.getByRole("dialog", { name: "Sidebar drawer" });
  fireEvent.click(screen.getByRole("button", { name: "Capabilities" }));
  fireEvent.click(screen.getByRole("button", { name: "New session" }));
  fireEvent.click(screen.getByRole("button", { name: "Choose workspace" }));
  expect(onNavigate).toHaveBeenCalledExactlyOnceWith("capabilities", drawer);
  expect(onNewConversation).toHaveBeenCalledExactlyOnceWith(drawer);
  expect(onWorkspace).toHaveBeenCalledExactlyOnceWith(drawer);
});

test("the Decision destination renders its label and reports aria-current when selected", () => {
  const onNavigate = vi.fn();
  render(
    <LeftRail onNavigate={onNavigate} destinations={["chat", "decision"]} currentView="decision" />,
  );
  const decision = screen.getByRole("button", { name: "Decision" });
  expect(decision.getAttribute("aria-current")).toBe("page");
  fireEvent.click(decision);
  expect(onNavigate).toHaveBeenCalledExactlyOnceWith("decision", undefined);
});

test("a waiting destination keeps its place but stays disabled until it can be used", () => {
  const onNavigate = vi.fn();
  render(
    <LeftRail
      onNavigate={onNavigate}
      destinations={["chat", "bots", "vault"]}
      waiting={["bots"]}
    />,
  );
  const names = screen.getAllByRole("button").map((button) => button.textContent);
  expect(names.slice(0, 3)).toEqual(["Chat", "Bots", "Vault"]);
  const bots = screen.getByRole("button", { name: "Bots" }) as HTMLButtonElement;
  expect(bots.disabled).toBe(true);
  fireEvent.click(bots);
  expect(onNavigate).not.toHaveBeenCalled();
  const vault = screen.getByRole("button", { name: "Vault" }) as HTMLButtonElement;
  expect(vault.disabled).toBe(false);
  fireEvent.click(vault);
  expect(onNavigate).toHaveBeenCalledExactlyOnceWith("vault", undefined);
});

test("a disabled destination looks like a refused creation action", () => {
  expect(ruleIn(css, '.rail-nav-button[aria-disabled="true"],\n.rail-nav-button:disabled')).toMatch(
    /opacity:\s*0\.5/,
  );
});

test("only the available Routines destination exposes its positive attention count", () => {
  const { rerender } = render(
    <LeftRail onNavigate={vi.fn()} destinations={["chat", "routines"]} routinesAttention={2} />,
  );
  const routines = screen.getByRole("button", { name: "Routines, 2 need you" });
  expect(routines.querySelector('[aria-hidden="true"].rail-count')?.textContent).toBe("2");
  rerender(
    <LeftRail onNavigate={vi.fn()} destinations={["chat", "routines"]} routinesAttention={0} />,
  );
  expect(screen.getByRole("button", { name: "Routines" }).querySelector(".rail-count")).toBeNull();
  rerender(<LeftRail onNavigate={vi.fn()} destinations={["chat"]} routinesAttention={2} />);
  expect(screen.queryByRole("button", { name: /Routines/ })).toBeNull();
});
