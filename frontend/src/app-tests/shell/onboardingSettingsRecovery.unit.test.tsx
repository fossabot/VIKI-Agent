/**
 * The shell services as App wires them (hard rules 7 and 8).
 *
 * What the seven view lanes build is pinned beside each component. What is pinned here is only
 * what App itself decides: which view is on screen, what the dispatcher does with each command
 * id, which banners stack in which order, which surfaces the shared scratch hides, what a
 * provider that cannot start refuses (and what it must not), and what a recovery window stops
 * asking for.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import {
  emptyRails,
  type FakeRest,
  failingRest,
  fakeRest,
  fakeViki,
  gate,
  sessionRow,
} from "../../accept/m3b/fakes";
import { RestError } from "../../api";
import type { Rails } from "../../protocol";
import { fakeDurable } from "../../testDurable";
import {
  type CapabilitiesOverrides,
  capabilitiesFixture,
  type FakeShellApi,
  fakeShellApi,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../testShell";

const TOKEN = "a-per-launch-token-that-must-not-leak";

beforeEach(() => {
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  vi.restoreAllMocks();
  // restoreAllMocks does not restore the clock, and the tests below this one use `waitFor`,
  // which polls on a real one.
  vi.useRealTimers();
});

interface MountOptions {
  capabilities?: CapabilitiesOverrides;
  settings?: Record<string, unknown>;
  workspaces?: ReturnType<typeof listingFixture>;
  rails?: Rails;
  sessions?: ReturnType<typeof sessionRow>[];
  /** The REST client this window gets; the default resolves every call. A recovery launch is
   * the one that needs a rejecting one — its two routes are not mounted. */
  rest?: FakeRest;
}

function mount(options: MountOptions = {}) {
  const rest =
    options.rest ??
    fakeRest({
      ...(options.rails ? { rails: options.rails } : {}),
      ...(options.sessions ? { sessions: options.sessions } : {}),
    });
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  // Every draft read this window makes, so "no durable call after recovery" is a fact and not
  // an absence nobody looked for.
  const draftReads: string[] = [];
  const getDraft = durable.getDraft.bind(durable);
  durable.getDraft = async (key: string) => {
    draftReads.push(key);
    return getDraft(key);
  };
  const shell: FakeShellApi = fakeShellApi({
    capabilities: capabilitiesFixture(options.capabilities ?? {}),
    // `onboarding.completed` is true unless a test is about the first launch, so nothing else
    // lands in that view.
    settings: settingsFixture({
      "onboarding.completed": true,
      ...options.settings,
    }),
    ...(options.workspaces ? { workspaces: options.workspaces } : {}),
  });
  const utils = render(<App viki={viki} rest={rest} durable={durable} shell={shell} />);
  return { ...utils, rest, viki, socket, durable, shell, draftReads };
}

/** Let the shell store's three reads settle (they resolve at once on the fake). */
async function settled(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

function menuButton(): HTMLElement {
  return screen.getByRole("button", { name: "App menu" });
}

async function openMenu(): Promise<void> {
  await act(async () => {
    fireEvent.click(menuButton());
  });
}

async function runCommand(name: string | RegExp): Promise<void> {
  await openMenu();
  await act(async () => {
    fireEvent.click(screen.getByRole("menuitem", { name }));
  });
}

/** The onboarding view's create card, which the automatic first-launch entry already shows. */
async function openOnboardingCreate(): Promise<void> {
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: /Create a vault/ }));
  });
}

// --- navigation and the dispatcher -------------------------------------------------------

test("navigation and the dispatcher: one destination per id, and the data ids are four", async () => {
  const { container } = mount({ workspaces: listingFixture([workspaceFixture()]) });
  await settled();
  expect(container.querySelector('[data-region="conversation"]')).not.toBeNull();

  await runCommand(/^Settings\s*Ctrl/);
  expect(container.querySelector('[data-region="settings"]')).not.toBeNull();
  // The chat body is gone, not merely covered: a view replaces the whole shell body.
  expect(container.querySelector('[data-region="conversation"]')).toBeNull();
  expect(container.querySelector('[data-region="view"]')).not.toBeNull();

  await runCommand(/Switch workspace/);
  expect(screen.getByRole("tab", { selected: true }).textContent).toContain("Workspaces");

  await runCommand(/^Diagnostics$/);
  expect(screen.getByRole("tab", { selected: true }).textContent).toContain("Diagnostics");

  // The four data ids are four destinations, each focusing its own panel heading — App focuses
  // nothing itself, so the panel the user asked for keeps the focus.
  for (const [label, heading] of [
    [/Data locations/, "Locations"],
    [/Export diagnostics/, "Export"],
    [/Import settings/, "Import"],
    [/^Reset$/, "Reset"],
  ] as const) {
    await runCommand(label);
    await waitFor(() => {
      expect(document.activeElement?.textContent).toContain(heading);
    });
  }
});

test("Ctrl+, opens settings, Escape returns to the chat and focuses the app menu", async () => {
  const { container } = mount();
  await settled();
  // Escape is page Back, and Back hands focus to the page's opener: the app menu here.
  menuButton().focus();

  await act(async () => {
    fireEvent.keyDown(window, { key: ",", ctrlKey: true });
  });
  expect(container.querySelector('[data-region="settings"]')).not.toBeNull();

  await act(async () => {
    fireEvent.keyDown(window, { key: "Escape" });
  });
  expect(container.querySelector('[data-region="conversation"]')).not.toBeNull();
  expect(document.activeElement).toBe(menuButton());
});

test("Escape yields to the onboarding view's own handler while a form is open", async () => {
  const { container } = mount({ settings: { "onboarding.completed": false } });
  await settled();
  await openOnboardingCreate();
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBeTruthy();

  await act(async () => {
    fireEvent.keyDown(window, { key: "Escape" });
  });

  // Still in onboarding: the view took its own Escape back to the three cards. App's listener
  // was installed at mount, long before this view existed, so this is a guard, not an ordering
  // assumption about who listens first.
  expect(container.querySelector('[data-region="conversation"]')).toBeNull();
});

test("Escape is the app menu's while the menu is open", async () => {
  const { container } = mount();
  await settled();
  await runCommand(/^Settings\s*Ctrl/);
  await openMenu();

  await act(async () => {
    fireEvent.keyDown(window, { key: "Escape" });
  });

  expect(container.querySelector('[data-region="settings"]')).not.toBeNull();
});

test("Ctrl+K opens the palette from a view, and a composing keystroke is not a command", async () => {
  const { container } = mount();
  await settled();
  await runCommand(/^Settings\s*Ctrl/);

  await act(async () => {
    fireEvent.keyDown(window, { key: ",", ctrlKey: true, isComposing: true });
  });
  expect(container.querySelector('[data-region="settings"]')).not.toBeNull();

  await act(async () => {
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
  });
  expect(screen.getByRole("dialog", { name: "Search sessions" })).toBeTruthy();
});

test("the view survives a reload; a stored recovery or onboarding reads as the chat", async () => {
  window.sessionStorage.setItem(
    "viki.view",
    JSON.stringify({ view: "data", page: "privacy", panel: "reset", onboardingMode: "create" }),
  );
  const first = mount();
  await settled();
  expect(first.container.querySelector(".data-view")).not.toBeNull();
  cleanup();

  window.sessionStorage.setItem(
    "viki.view",
    JSON.stringify({ view: "onboarding", page: "general", panel: "locations" }),
  );
  const second = mount({ workspaces: listingFixture([workspaceFixture()]) });
  await settled();
  expect(second.container.querySelector('[data-region="conversation"]')).not.toBeNull();
});

// --- the chat view's own shell additions ----------------------------------------------------

test("the banners stack in order: reconnect, vault, chat-only, readiness", async () => {
  const { container, socket } = mount({
    capabilities: { provider: { can_start: false, cli: "missing" } },
  });
  act(() => socket.open());
  await settled();

  const banners = [...container.querySelectorAll(".chat-only-banner, .readiness-banner")];
  expect(banners.map((node) => node.className)).toEqual(["chat-only-banner", "readiness-banner"]);
  // Both sit in the banner slot, above the scroll and never in the composer slot (the two
  // frozen relative-index assertions in m4b/m4d depend on that).
  const scroll = container.querySelector(".conversation-scroll");
  for (const banner of banners)
    expect(
      banner.compareDocumentPosition(scroll as Node) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
});

test("vault surfaces follow the shared scratch, and an unknown vault keeps them", async () => {
  const rails: Rails = {
    ...emptyRails(),
    gates: [gate({ id: "H1", item: "Recall skill" })],
    counts: { knowledge: 4, sources: 2, inbox: 1 },
  };
  const scratch = mount({ rails });
  await settled();
  await waitFor(() => {
    expect(scratch.container.querySelector(".rail-section")).not.toBeNull();
  });
  expect(scratch.container.querySelector(".tile")).toBeNull();
  expect(scratch.container.textContent).not.toContain("Needs you");
  cleanup();

  const vault = mount({
    rails,
    capabilities: { workspace: { shared_scratch: false, chat_only: false } },
  });
  await settled();
  await waitFor(() => {
    expect(vault.container.querySelector(".tile")).not.toBeNull();
  });
});

test("a provider that cannot start blocks sessions, not drafts", async () => {
  const { container, durable, socket, viki } = mount({
    capabilities: { provider: { can_start: false, cli: "missing" } },
  });
  act(() => socket.open());
  await settled();

  const button = screen.getByRole("button", { name: "New session" });
  expect(button.getAttribute("aria-disabled")).toBe("true");
  expect(button.getAttribute("aria-describedby")).toBe("readiness-reason");
  const before = socket.sent.length;
  await act(async () => {
    fireEvent.click(button);
  });
  await act(async () => {
    fireEvent.keyDown(window, { key: "n", altKey: true });
  });
  expect(socket.sent.length).toBe(before);
  expect(Object.keys(viki.store.getSnapshot().sessions)).toEqual([]);

  // The draft still saves, and the refusal is the banner's own sentence — not an error card.
  const box = screen.getByLabelText<HTMLTextAreaElement>("Message");
  await act(async () => {
    fireEvent.change(box, { target: { value: "A draft to send later" } });
  });
  await act(async () => {
    fireEvent.keyDown(box, { key: "Enter" });
  });
  expect(box.value).toBe("A draft to send later");
  expect(container.querySelector(".composer-refusal")?.textContent).toBe(
    container.querySelector("#readiness-reason span")?.textContent,
  );
  expect(container.querySelector(".error-card")).toBeNull();
  await waitFor(() => {
    expect(durable.saves.some((save) => save.text === "A draft to send later")).toBe(true);
  });
});

test("the user's label is their setting, and an empty one keeps You", async () => {
  const named = mount({
    settings: { "identity.display_name": "Synth User" },
    sessions: [sessionRow({ session_id: "s-1", title: "vault" })],
  });
  act(() => named.socket.open());
  await settled();
  await openSessionWithTurn(named);
  await waitFor(() => {
    expect(named.container.querySelector(".user-at")?.textContent).toContain("Synth User");
  });
  cleanup();

  const anonymous = mount({
    settings: { "identity.display_name": "" },
    sessions: [sessionRow({ session_id: "s-1", title: "vault" })],
  });
  act(() => anonymous.socket.open());
  await settled();
  await openSessionWithTurn(anonymous);
  await waitFor(() => {
    expect(anonymous.container.querySelector(".user-at")?.textContent).toContain("You");
  });
});

test("the sidebar Chat destination knows where the user is", async () => {
  mount();
  await settled();
  const chat = within(
    screen.getByRole("navigation", { name: "Navigation and sessions" }),
  ).getByRole("button", {
    name: "Chat",
  });
  expect(chat.getAttribute("aria-current")).toBe("page");
  expect(chat.tabIndex).toBe(0);

  await runCommand(/^Settings\s*Ctrl/);
  const away = within(
    screen.getByRole("navigation", { name: "Navigation and sessions" }),
  ).getByRole("button", {
    name: "Chat",
  });
  expect(away.getAttribute("aria-current")).toBeNull();
  expect(away.tabIndex).toBe(0);
});

// --- onboarding, recovery, and what never reaches the page -------------------------------

test("onboarding shows once, and Set up later does not bring it back", async () => {
  const { container, shell } = mount({ settings: { "onboarding.completed": false } });
  await settled();
  expect(screen.getByRole("button", { name: /Set up later/ })).toBeTruthy();

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: /Set up later/ }));
  });
  await settled();
  expect(container.querySelector('[data-region="conversation"]')).not.toBeNull();
  // The PATCH is the evidence for this half: it is what keeps the next LAUNCH out of the view.
  // What keeps THIS launch out after a re-read is `onboardingDecided`, and that guard is only
  // provable while `onboarding.completed` is still false — the test below.
  expect(shell.data.settings.effective["onboarding.completed"]).toBe(true);
});

test("a reconnect's re-read does not drag the user back into onboarding", async () => {
  // New session out of the three cards changes none of the three facts — no PATCH, no
  // workspace — so this is the state the ref exists for, and the one "Set up later" above cannot
  // exercise. (Escape does not leave them: on a first launch their page Back is disabled.)
  const { container, socket } = mount({ settings: { "onboarding.completed": false } });
  await settled();
  expect(screen.getByRole("button", { name: /Set up later/ })).toBeTruthy();

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "New session" }));
  });
  expect(container.querySelector('[data-region="conversation"]')).not.toBeNull();

  // The real trigger, not a hand-made one: the shell store re-reads capabilities on every
  // transition to "open" (state/shell.ts), which is what a dropped and recovered socket does.
  act(() => socket.open());
  await settled();
  expect(container.querySelector('[data-region="conversation"]')).not.toBeNull();
  expect(container.querySelector(".onboarding-card")).toBeNull();
});

test("a registered workspace means no automatic onboarding", async () => {
  const { container } = mount({
    settings: { "onboarding.completed": false },
    workspaces: listingFixture([workspaceFixture()]),
  });
  await settled();
  expect(container.querySelector('[data-region="conversation"]')).not.toBeNull();
});

test("recovery is the only view and stops what cannot succeed", async () => {
  // The rest store's only repeat source is `setInterval(pollBoth, 60_000)`, so a missing
  // `restStore.stop()` is invisible in a test that takes milliseconds: the interval is faked
  // (and only the interval — Date, timeouts and microtasks stay real) so the clock can be moved
  // past two ticks. `afterEach` puts the real one back.
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  const { container, rest, viki, socket, draftReads } = mount({
    capabilities: { app: { mode: "recovery" }, storage: { state: "unavailable", reason: "busy" } },
  });
  act(() => socket.open());
  await settled();

  expect(container.querySelector(".recovery-view")).not.toBeNull();
  expect(container.querySelector('[data-region="conversation"]')).toBeNull();
  expect(viki.store.getSnapshot().status).toBe("closed");

  const restCalls = rest.calls.length;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(120_000);
  });
  await act(async () => {
    fireEvent.keyDown(window, { key: ",", ctrlKey: true });
  });
  await settled();
  // Two interval ticks have passed: without `restStore.stop()` this window would have asked
  // /api/sessions and /api/rails four more times, for routes a recovery app does not mount.
  expect(rest.calls.length).toBe(restCalls);
  // One draft read, the mount-time one — it leaves before capabilities report recovery, so the
  // `durableSuspended` guard is not what prevents it, and a recovery window renders no rail and
  // no composer, so nothing can change the editor key afterwards. The absolute count is what
  // this test can honestly prove; a delta between two moments that cannot differ is not.
  expect(draftReads.length).toBe(1);
  // Settings is refused in recovery, so the view never moves.
  expect(container.querySelector(".recovery-view")).not.toBeNull();
});

test("a recovery window reports no network and no git it never asked about", async () => {
  // `create_recovery_app` mounts neither /api/sessions nor /api/rails, and both reads leave at
  // mount, before capabilities can say so: the 404s are already recorded when the recovery
  // effect calls `restStore.stop()`, and nothing clears them for the rest of the launch.
  const rest = failingRest(new RestError(404, ""));
  const { container } = mount({
    rest,
    capabilities: {
      app: { mode: "recovery" },
      storage: { state: "unavailable", reason: "corrupt" },
      // What `_workspace_json(None)` answers in recovery (src/viki_agent/app/diagnostics.py): no active workspace,
      // so the vault surfaces are not hidden and the git chip would draw.
      workspace: { active: null, shared_scratch: false },
    },
  });
  await settled();

  expect(rest.calls).toEqual(["sessions", "rails"]);
  expect(container.querySelector(".recovery-view")).not.toBeNull();
  // Not offline, and retrying nothing: the status bar says neither.
  expect(container.textContent).not.toContain("offline");
  // And no "Git · status unknown" about a window with no vault that never read the rails.
  expect(container.querySelector(".git-chip")).toBeNull();
});

test("the token stays out of the page and the log", async () => {
  // The token has to be somewhere this window can actually reach, or the assertions below hold
  // for a string nothing ever saw. These are the two carriers a real launch uses (main.tsx ->
  // readToken): the launch URL's fragment, and the sessionStorage it is moved into.
  window.location.hash = `#token=${encodeURIComponent(TOKEN)}`;
  window.sessionStorage.setItem("viki.token", TOKEN);
  const log = vi.spyOn(console, "log").mockImplementation(() => {});
  const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
  const error = vi.spyOn(console, "error").mockImplementation(() => {});
  try {
    const { container } = mount();
    await settled();
    // Every view this lane added, because a leak would be one view's markup, not the shell's.
    for (const row of [/^Settings\s*Ctrl/, /^Diagnostics$/, /Data locations/]) {
      await runCommand(row);
      await settled();
      expect(container.innerHTML).not.toContain(TOKEN);
      expect(document.documentElement.innerHTML).not.toContain(TOKEN);
    }
    for (const spy of [log, warn, error])
      for (const call of spy.mock.calls) expect(JSON.stringify(call)).not.toContain(TOKEN);
  } finally {
    window.location.hash = "";
  }
});

/** Open the one scripted row and give it a user turn, so `.user-at` exists to read a label off.
 * The turn arrives as the server's own frame, which is the only thing that makes one. */
async function openSessionWithTurn(mounted: ReturnType<typeof mount>): Promise<void> {
  const row = await waitFor(() => {
    const found = mounted.container.querySelector<HTMLElement>('[data-session-id="s-1"]');
    if (!found) throw new Error("no row yet");
    return found;
  });
  await act(async () => {
    fireEvent.click(row);
  });
  act(() =>
    mounted.socket.receive({
      v: 2,
      generation: "11111111-1111-4111-8111-111111111111",
      request_id: "",
      type: "user.message",
      session_id: "s-1",
      seq: 1,
      data: { text: "Hello", command: "", at: "2026-09-17T09:00:00+09:00" },
    }),
  );
}
