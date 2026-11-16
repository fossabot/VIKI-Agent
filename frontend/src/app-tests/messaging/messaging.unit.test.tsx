import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki } from "../../accept/m3b/fakes";
import type { MessagingApi } from "../../clients/messaging/messagingApi";
import type { ProfileApi } from "../../clients/profiles/profileApi";
import { fakeDurable } from "../../testDurable";
import { fakeShellApi, settingsFixture } from "../../testShell";

beforeEach(() => {
  sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  sessionStorage.clear();
  vi.restoreAllMocks();
});

const messagingStatus = {
  mattermost: { configured: false, state: "not_configured", restart_required: false, reason: null },
  slack: { configured: false, state: "not_configured", restart_required: false, reason: null },
  discord: { configured: false, state: "not_configured", restart_required: false, reason: null },
} as const;
const messaging: MessagingApi = {
  get: async () => messagingStatus,
  enable: vi.fn(),
  disable: vi.fn(),
  disableMattermost: vi.fn(),
  config: async () => ({
    mattermost: { source: "none", values: {} },
    slack: { source: "none", values: {} },
    discord: { source: "none", values: {} },
  }),
  save: vi.fn(async () => messagingStatus),
  remove: vi.fn(async () => messagingStatus),
};
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

async function settled() {
  await act(async () => {
    for (let index = 0; index < 5; index++) await Promise.resolve();
  });
}
function mount(api?: MessagingApi, profileApi?: ProfileApi) {
  const { viki, socket } = fakeViki();
  const view = render(
    <App
      viki={viki}
      rest={fakeRest()}
      durable={fakeDurable()}
      shell={fakeShellApi({ settings: settingsFixture({ "onboarding.completed": true }) })}
      {...(api ? { messagingApi: api } : {})}
      {...(profileApi ? { profileApi } : {})}
    />,
  );
  return { socket, ...view };
}
const sidebar = () => within(screen.getByRole("navigation", { name: "Navigation and sessions" }));

test("the production-injected Messaging destination is discoverable even without bot setup", async () => {
  mount(messaging, profiles);
  await settled();
  const button = sidebar().getByRole("button", { name: "Messaging" });
  button.focus();
  await act(async () => void fireEvent.click(button));
  expect(screen.getByRole("heading", { name: "Messaging" })).toBeTruthy();
  expect(
    within(screen.getByRole("region", { name: "Mattermost bot" })).getByText("Not configured"),
  ).toBeTruthy();
  expect(sidebar().getByRole("button", { name: "Messaging" }).getAttribute("aria-current")).toBe(
    "page",
  );
  await act(
    async () => void fireEvent.click(screen.getByRole("button", { name: "Open Capabilities" })),
  );
  expect(screen.getByRole("region", { name: "Capabilities" })).toBeTruthy();
  await act(async () => void fireEvent.click(screen.getByRole("button", { name: "Back" })));
  expect(screen.getByRole("heading", { name: "Messaging" })).toBeTruthy();
  await act(async () => void fireEvent.click(screen.getByRole("button", { name: "Back" })));
  expect(screen.queryByRole("heading", { name: "Messaging" })).toBeNull();
  expect(document.activeElement).toBe(sidebar().getByRole("button", { name: "Messaging" }));
});

// App wires MessagingView's onOpenUrl to the real viki.actions.openUrl, which sends
// an "open.url" command over the client — nothing short of mounting the real App proves that.
test("Open setup guide opens the provider's guide URL through the viki action", async () => {
  const { socket } = mount(messaging, profiles);
  act(() => socket.open());
  await settled();
  const button = sidebar().getByRole("button", { name: "Messaging" });
  await act(async () => void fireEvent.click(button));
  const mattermost = within(screen.getByRole("region", { name: "Mattermost bot" }));
  await act(
    async () => void fireEvent.click(mattermost.getByRole("button", { name: "Mattermost bot" })),
  );
  await act(
    async () => void fireEvent.click(mattermost.getByRole("button", { name: "Open setup guide" })),
  );
  expect(socket.sent).toContainEqual({
    type: "open.url",
    url: "https://docs.mattermost.com/developers/integrate/reference/bot-accounts",
  });
});

test("Messaging restores only with its injected client and adds no dead destination", async () => {
  sessionStorage.setItem(
    "viki.view",
    JSON.stringify({ view: "messaging", page: "general", panel: "locations" }),
  );
  const first = mount(messaging);
  await settled();
  expect(screen.getByRole("heading", { name: "Messaging" })).toBeTruthy();
  first.unmount();
  mount();
  await settled();
  expect(sidebar().queryByRole("button", { name: "Messaging" })).toBeNull();
  expect(screen.queryByRole("heading", { name: "Messaging" })).toBeNull();
});
