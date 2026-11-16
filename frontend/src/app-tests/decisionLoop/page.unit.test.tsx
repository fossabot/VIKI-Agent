/**
 * The Decision page as App wires it: `decisionApi` adds the rail's
 * Decision destination right after Chat and a restorable "decision" view that renders
 * DecisionLoopPage with the settings document's effective values, its writability, the shell
 * store's patch and App's own openers. An empty chat carries no brief and the right rail's Up
 * next no pick.
 */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki } from "../../accept/m3b/fakes";
import type { SettingsView } from "../../clients/app/shellApi";
import type { ContextApi } from "../../clients/conversations/contextApi";
import { createFakeDecisionLoopApi, decisionLoopExamples } from "../../clients/decisionLoop/fake";
import type { DecisionLoopApi } from "../../clients/decisionLoop/types";
import type { ProfileApi } from "../../clients/profiles/profileApi";
import { fakeDurable } from "../../testDurable";
import { type FakeShellApi, fakeShellApi, settingsFixture } from "../../testShell";

beforeEach(() => {
  sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  sessionStorage.clear();
  vi.restoreAllMocks();
});

const pending = () => new Promise<never>(() => {});
const profiles: ProfileApi = {
  list: async () => [],
  get: pending,
  create: pending,
  revise: pending,
  archive: pending,
  duplicate: pending,
  compare: pending,
};
const context: ContextApi = { get: pending };

async function settled() {
  await act(async () => {
    for (let index = 0; index < 5; index++) await Promise.resolve();
  });
}
function readySettings(overrides: Partial<SettingsView> = {}): SettingsView {
  return settingsFixture({ "onboarding.completed": true }, overrides);
}
function mount(
  options: { decisionApi?: DecisionLoopApi; shell?: FakeShellApi; contextApi?: ContextApi } = {},
) {
  const { viki, socket } = fakeViki();
  const shell = options.shell ?? fakeShellApi({ settings: readySettings() });
  const view = render(
    <App
      viki={viki}
      rest={fakeRest()}
      durable={fakeDurable()}
      shell={shell}
      profileApi={profiles}
      {...(options.decisionApi ? { decisionApi: options.decisionApi } : {})}
      {...(options.contextApi ? { contextApi: options.contextApi } : {})}
    />,
  );
  act(() => socket.open());
  return { socket, shell, ...view };
}
const sidebar = () => within(screen.getByRole("navigation", { name: "Navigation and sessions" }));
const destinations = () =>
  [...document.querySelectorAll(".rail-navigation .rail-nav-button:not(.rail-new)")].map(
    (button) => button.textContent,
  );
const pageHeading = () => screen.queryByRole("heading", { level: 1, name: "Decision" });
const click = async (element: Element) => act(async () => void fireEvent.click(element));

async function openDecision(): Promise<void> {
  await click(sidebar().getByRole("button", { name: "Decision" }));
  await settled();
}
/** Expands the page's row named `name` and answers its section. */
async function expandRow(name: string): Promise<HTMLElement> {
  const row = screen.getByRole("region", { name });
  await click(within(row).getByRole("button", { name }));
  return row;
}

test("the rail offers Decision right after Chat, and only with a decisionApi", async () => {
  const first = mount({ decisionApi: createFakeDecisionLoopApi() });
  await settled();
  expect(destinations()).toEqual(["Chat", "Decision", "Capabilities", "Agents"]);
  first.unmount();
  mount();
  await settled();
  expect(destinations()).toEqual(["Chat", "Capabilities", "Agents"]);
});

test("the rail opens the Decision page, which reads the page view, and Back leaves it", async () => {
  const api = createFakeDecisionLoopApi();
  mount({ decisionApi: api });
  await settled();
  expect(pageHeading()).toBeNull();
  await openDecision();
  expect(pageHeading()).toBeTruthy();
  expect(sidebar().getByRole("button", { name: "Decision" }).getAttribute("aria-current")).toBe(
    "page",
  );
  expect(api.requests.map((request) => request.method)).toContain("page");
  expect(screen.getByRole("region", { name: "Proactive VIKI" })).toBeTruthy();
  await click(screen.getByRole("button", { name: "Back" }));
  expect(pageHeading()).toBeNull();
});

test("the Decision view survives a reload, and comes back only with its client", async () => {
  const first = mount({ decisionApi: createFakeDecisionLoopApi() });
  await settled();
  await openDecision();
  first.unmount();
  const second = mount({ decisionApi: createFakeDecisionLoopApi() });
  await settled();
  expect(pageHeading()).toBeTruthy();
  expect(sidebar().getByRole("button", { name: "Decision" }).getAttribute("aria-current")).toBe(
    "page",
  );
  second.unmount();
  mount();
  await settled();
  expect(pageHeading()).toBeNull();
  expect(sidebar().queryByRole("button", { name: "Decision" })).toBeNull();
});

test("an empty chat shows no brief, at app open and in a new session", async () => {
  const { socket } = mount({ decisionApi: createFakeDecisionLoopApi() });
  await settled();
  expect(screen.getByText("New conversation")).toBeTruthy();
  expect(screen.queryByRole("region", { name: "Brief" })).toBeNull();
  await click(sidebar().getByRole("button", { name: "New session" }));
  await settled();
  expect(socket.sent.some((frame) => frame.type === "session.open")).toBe(true);
  expect(screen.getByText("New conversation")).toBeTruthy();
  expect(screen.queryByRole("region", { name: "Brief" })).toBeNull();
});

test("the right rail's Up next holds no pick", async () => {
  mount({ decisionApi: createFakeDecisionLoopApi() });
  await settled();
  const rail = document.querySelector<HTMLElement>('[data-region="right-rail"]');
  if (!rail) throw new Error("no right rail");
  const upNext = rail.querySelector(".rail-up-next");
  expect(upNext).toBeTruthy();
  expect(upNext?.children).toHaveLength(0);
  expect(within(rail).queryByText(/pick/i)).toBeNull();
});

test("the page saves through the shell store, and only while settings are writable", async () => {
  const off = { page: { ...decisionLoopExamples().page, enabled: false } };
  const writable = mount({ decisionApi: createFakeDecisionLoopApi(off) });
  await settled();
  await openDecision();
  await click(screen.getByRole("button", { name: "Turn proactive VIKI on" }));
  await settled();
  const patches = writable.shell.calls.filter((call) => call.method === "patchSettings");
  expect(patches.map((call) => call.args[2])).toEqual([{ "decision.enabled": true }]);
  writable.unmount();

  sessionStorage.clear();
  const readOnly = fakeShellApi({
    settings: readySettings({ read_only: true, reason: "newer_version" }),
  });
  mount({ decisionApi: createFakeDecisionLoopApi(off), shell: readOnly });
  await settled();
  await openDecision();
  const turnOn = screen.getByRole("button", { name: "Turn proactive VIKI on" });
  expect((turnOn as HTMLButtonElement).disabled).toBe(true);
  await click(turnOn);
  expect(readOnly.calls.some((call) => call.method === "patchSettings")).toBe(false);
});

test("the page reads the effective settings and opens a URL and Context through App", async () => {
  const effective = { ...readySettings().effective, "decision.never_send": '["Private/"]' };
  const shell = fakeShellApi({ settings: readySettings({ effective }) });
  const { socket } = mount({
    decisionApi: createFakeDecisionLoopApi(),
    shell,
    contextApi: context,
  });
  await settled();
  await openDecision();
  const privacy = await expandRow("What leaves this computer");
  expect(within(privacy).getByText("Private/")).toBeTruthy();
  const model = await expandRow("Decision model");
  await click(within(model).getByRole("button", { name: "Get a key" }));
  expect(socket.sent).toContainEqual({ type: "open.url", url: "https://typesafe.ai" });
  await click(within(privacy).getByRole("button", { name: "See the last state sent" }));
  expect(screen.getByRole("heading", { level: 1, name: "Context & loadout" })).toBeTruthy();
  expect(pageHeading()).toBeNull();
});

test("a running chat's Open shows that chat in the chat view", async () => {
  const mission = decisionLoopExamples().page.missions.running[0];
  if (!mission) throw new Error("the page example has no running mission");
  const { socket } = mount({ decisionApi: createFakeDecisionLoopApi() });
  await settled();
  await openDecision();
  const doing = await expandRow("What VIKI may do");
  await click(within(doing).getByRole("button", { name: "Open" }));
  await settled();
  expect(pageHeading()).toBeNull();
  expect(
    socket.sent.some(
      (frame) => frame.type === "session.open" && frame.session_id === mission.session_id,
    ),
  ).toBe(true);
  expect(sessionStorage.getItem("viki.selected")).toBe(mission.session_id);
});
