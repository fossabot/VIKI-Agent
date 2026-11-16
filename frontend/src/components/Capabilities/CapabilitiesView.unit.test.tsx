import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { InventoryApi } from "../../clients/conversations/inventoryApi";
import type { NativeAssetsApi } from "../../clients/profiles/nativeAssetsApi";
import type { ProfileApi } from "../../clients/profiles/profileApi";
import type { ProfileLaunchApi } from "../../clients/profiles/profileLaunchApi";
import { readCss, ruleIn } from "../../testing/css";
import { CapabilitiesView } from "./CapabilitiesView";

const css = readCss(import.meta.url, "CapabilitiesView.css");

vi.mock("./NativeCapabilities", () => ({ NativeCapabilities: () => <p>Native list</p> }));
vi.mock("./InventoryCapabilities", () => ({
  InventoryCapabilities: ({ category }: { category: string }) => <p>Inventory {category}</p>,
}));
vi.mock("./ConnectorSelection", () => ({
  ConnectorSelection: ({
    workspaceId,
    onManageProfiles,
  }: {
    workspaceId?: string;
    onManageProfiles?: () => void;
  }) => (
    <div>
      <input aria-label="Connector draft" defaultValue="saved" />
      <span>{workspaceId}</span>
      <button type="button" onClick={onManageProfiles}>
        Manage profiles
      </button>
    </div>
  ),
}));
afterEach(cleanup);
const props = {
  nativeApi: {} as NativeAssetsApi,
  inventoryApi: {} as InventoryApi,
  profileApi: {} as ProfileApi,
  launchApi: {} as ProfileLaunchApi,
  chats: [],
  workspaceId: "workspace",
};

test("the eyebrow precedes a page-head holding the title and intro", () => {
  render(<CapabilitiesView {...props} />);
  const head = document.querySelector("header.page-head") as HTMLElement;
  expect(head.previousElementSibling?.className).toBe("capabilities-eyebrow");
  expect(head.querySelector("h1.page-title")?.textContent).toBe("Shape what comes next.");
  expect(head.querySelector("p.page-intro")).not.toBeNull();
});

test("the eyebrow is a block with 8px under it, so the page title does not sit flush on the label", () => {
  const rule = ruleIn(css, ".capabilities-eyebrow");
  expect(rule).toMatch(/display:\s*block/);
  expect(rule).toMatch(/margin:\s*0 0 8px/);
});

test("five category panels mount only when visited", () => {
  render(<CapabilitiesView {...props} />);
  expect(screen.getAllByRole("tab").map((node) => node.textContent)).toEqual([
    "Skills",
    "Subagents",
    "Tools",
    "Connectors",
    "Plugins",
  ]);
  expect(screen.getByText("Native list")).toBeTruthy();
  expect(screen.queryByRole("textbox")).toBeNull();
  fireEvent.click(screen.getByRole("tab", { name: "Tools" }));
  expect(screen.getByText("Inventory tools")).toBeTruthy();
  fireEvent.click(screen.getByRole("tab", { name: "Plugins" }));
  expect(screen.getByText("Inventory plugins")).toBeTruthy();
  fireEvent.click(screen.getByRole("tab", { name: "Connectors" }));
  expect(screen.getByText("Inventory connectors")).toBeTruthy();
  expect(screen.getByRole("textbox", { name: "Connector draft" })).toBeTruthy();
});

test("tab switches retain connector drafts and management callback", () => {
  const onManageProfiles = vi.fn();
  render(<CapabilitiesView {...props} onManageProfiles={onManageProfiles} />);
  fireEvent.click(screen.getByRole("tab", { name: "Connectors" }));
  const input = screen.getByRole("textbox", { name: "Connector draft" }) as HTMLInputElement;
  fireEvent.change(input, { target: { value: "local edits" } });
  fireEvent.click(screen.getByRole("tab", { name: "Skills" }));
  expect(screen.queryByRole("textbox", { name: "Connector draft" })).toBeNull();
  fireEvent.click(screen.getByRole("tab", { name: "Connectors" }));
  expect(screen.getByRole("textbox", { name: "Connector draft" })).toBe(input);
  expect(input.value).toBe("local edits");
  fireEvent.click(screen.getByRole("button", { name: "Manage profiles" }));
  expect(onManageProfiles).toHaveBeenCalledOnce();
});

test("keyboard wraps tabs, tracks focus and panel association", () => {
  render(<CapabilitiesView {...props} />);
  const skills = screen.getByRole("tab", { name: "Skills" });
  skills.focus();
  fireEvent.keyDown(skills, { key: "ArrowLeft" });
  const plugins = screen.getByRole("tab", { name: "Plugins" });
  expect(document.activeElement).toBe(plugins);
  expect(plugins.getAttribute("aria-selected")).toBe("true");
  expect(screen.getByRole("tabpanel").id).toBe(plugins.getAttribute("aria-controls"));
  fireEvent.keyDown(plugins, { key: "Home" });
  expect(document.activeElement).toBe(skills);
  fireEvent.keyDown(skills, { key: "End" });
  expect(document.activeElement).toBe(plugins);
  fireEvent.keyDown(plugins, { key: "ArrowRight" });
  expect(document.activeElement).toBe(skills);
});

test("missing services give explicit unknown availability", () => {
  render(<CapabilitiesView chats={[]} />);
  expect(
    screen.getByText("Native skill discovery is unavailable in this environment."),
  ).toBeTruthy();
  fireEvent.click(screen.getByRole("tab", { name: "Connectors" }));
  expect(
    screen.getByText("App-profile connector selection is unavailable in this environment."),
  ).toBeTruthy();
  expect(screen.getByText("Session inventory is unavailable in this environment.")).toBeTruthy();
});
