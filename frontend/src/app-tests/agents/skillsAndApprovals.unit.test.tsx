import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import {
  emptyRails,
  type FakeRest,
  fakeRest,
  fakeViki,
  GENERATION,
  gate,
  sessionRow,
} from "../../accept/m3b/fakes";
import type { ShellApi } from "../../clients/app/shellApi";
import type {
  NativeAssetsApi,
  NativeAssetView,
  NativeCatalog,
  NativeSource,
} from "../../clients/profiles/nativeAssetsApi";
import type { ProfileApi } from "../../clients/profiles/profileApi";
import type { NeedsYouProps } from "../../components/RightRail/NeedsYou";
import type { Envelope } from "../../protocol";
import type { DecisionInbox, DecisionsApi, VaultProposalItem } from "../../state/decisionsApi";
import type { ShellStore } from "../../state/shell";
import { fakeDurable } from "../../testDurable";
import {
  capabilitiesFixture,
  FAKE_WORKSPACE_ID,
  fakeShellApi,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../testShell";

const observedStores = vi.hoisted(() => [] as ShellStore[]);
vi.mock("../../state/shell", async () => {
  const actual = await vi.importActual<typeof import("../../state/shell")>("../../state/shell");
  return {
    ...actual,
    createShellStore: (...args: Parameters<typeof actual.createShellStore>) => {
      const store = actual.createShellStore(...args);
      observedStores.push(store);
      return store;
    },
  };
});
// Every Needs you render, with the active workspace that App rendered it for.
const renderedNeeds = vi.hoisted(
  () => [] as { workspace: string | undefined; needs: NeedsYouProps["needs"] }[],
);
vi.mock("../../components/RightRail/NeedsYou", async () => {
  const actual = await vi.importActual<typeof import("../../components/RightRail/NeedsYou")>(
    "../../components/RightRail/NeedsYou",
  );
  return {
    ...actual,
    NeedsYou: (props: NeedsYouProps) => {
      const capabilities = observedStores.at(-1)?.getSnapshot().capabilities;
      renderedNeeds.push({ workspace: capabilities?.workspace.active?.id, needs: props.needs });
      return <actual.NeedsYou {...props} />;
    },
  };
});

beforeEach(() => {
  observedStores.length = 0;
  renderedNeeds.length = 0;
  window.sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
async function settled() {
  await act(async () => {
    for (let n = 0; n < 5; n++) await Promise.resolve();
  });
}
function nativeApi(): NativeAssetsApi {
  const assets: NativeAssetView[] = (["agent", "skill"] as const).map((kind) => ({
    source_id: "fixture",
    source_scope: "workspace",
    provider: "claude",
    relative_id: `${kind}/SKILL.md`,
    kind,
    name: kind === "agent" ? "SKILL" : "Meeting notes skill",
    description: "<b>Author's description</b>",
    model: null,
    tools: [],
    digest: "a".repeat(64),
    observed_at: "2026-10-01T00:00:00.000Z",
    supported_fields: [],
    unsupported_fields: [],
    launch_supported: false,
  }));
  const catalog: NativeCatalog = {
    source_id: "fixture",
    provider: "claude",
    source_scope: "workspace",
    assets,
    partial: false,
    partial_reasons: [],
    scanned_entries: 2,
    scanned_bytes: 20,
    omitted: 0,
  };
  return {
    sources: vi.fn(
      async (): Promise<NativeSource[]> => [
        { source_id: "fixture", provider: "claude", scope: "workspace" },
      ],
    ),
    catalog: vi.fn(async () => catalog),
  };
}
function profileApi(): ProfileApi {
  return {
    list: vi.fn(async () => []),
    get: vi.fn(),
    create: vi.fn(),
    revise: vi.fn(),
    archive: vi.fn(),
    duplicate: vi.fn(),
    compare: vi.fn(),
  };
}
async function mount({
  native = nativeApi(),
  profile = profileApi(),
  shell,
  decisions,
  client = fakeViki(),
  rest = fakeRest(),
}: {
  native?: NativeAssetsApi | null;
  profile?: ProfileApi | null;
  shell?: ShellApi;
  decisions?: DecisionsApi;
  client?: ReturnType<typeof fakeViki>;
  rest?: FakeRest;
} = {}) {
  const props = {
    viki: client.viki,
    rest,
    durable: fakeDurable(),
    ...(native ? { nativeAssetsApi: native } : {}),
    ...(profile ? { profileApi: profile } : {}),
    ...(shell ? { shell } : {}),
    ...(decisions ? { decisions } : {}),
  };
  const view = render(<App {...props} />);
  await settled();
  return { ...view, props, client };
}

test("compact Skills uses one reader in the pinned summary and opens the actual skill filter", async () => {
  const native = nativeApi();
  await mount({ native });
  const count = screen.getByText("Skills returned: 1");
  expect(count.closest(".right-rail-summary")).toBeTruthy();
  expect(count.closest("details")).toBeNull();
  expect(screen.getByText("Catalog coverage: complete")).toBeTruthy();
  expect(native.sources).toHaveBeenCalledTimes(1);
  expect(native.catalog).toHaveBeenCalledTimes(1);
  expect(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" }))
      .getAllByRole("button")
      .filter(
        (button) =>
          button.classList.contains("rail-nav-button") && !button.classList.contains("rail-new"),
      )
      .map((tab) => tab.textContent),
  ).toEqual(["Chat", "Capabilities", "Agents"]);
  const opener = screen.getByRole("button", { name: "Open Skills" });
  opener.focus();
  fireEvent.click(opener);
  await settled();
  expect(screen.getByRole("tab", { name: "Subagents" }).getAttribute("aria-selected")).toBe("true");
  expect(
    within(screen.getByRole("group", { name: "Asset kind" }))
      .getByRole("button", { name: "Skills" })
      .getAttribute("aria-pressed"),
  ).toBe("true");
  expect(screen.getByText("Meeting notes skill")).toBeTruthy();
  expect(screen.queryByText("SKILL")).toBeNull();
  expect(screen.getByText("<b>Author's description</b>")).toBeTruthy();
  expect(
    screen.getByText("<b>Author's description</b>").closest(".native-assets")?.querySelector("b"),
  ).toBeNull();
  expect(native.sources).toHaveBeenCalledTimes(2);
  expect(native.catalog).toHaveBeenCalledTimes(2);
});

test("manual native filters survive App renders; an explicit Skills request deliberately remounts", async () => {
  const native = nativeApi();
  const { rerender, props } = await mount({ native });
  fireEvent.click(screen.getByRole("button", { name: "Open Skills" }));
  await settled();
  const kinds = screen.getByRole("group", { name: "Asset kind" });
  fireEvent.click(within(kinds).getByRole("button", { name: "Subagents" }));
  fireEvent.change(screen.getByLabelText("Search native assets"), { target: { value: "SKILL" } });
  rerender(<App {...props} />);
  await settled();
  expect((screen.getByLabelText("Search native assets") as HTMLInputElement).value).toBe("SKILL");
  expect(
    within(kinds).getByRole("button", { name: "Subagents" }).getAttribute("aria-pressed"),
  ).toBe("true");
  expect(screen.getByText("SKILL")).toBeTruthy();
  expect(native.sources).toHaveBeenCalledTimes(2);
  const entry = screen.getByRole("button", { name: "Browse Skills" });
  fireEvent.click(entry);
  await settled();
  expect((screen.getByLabelText("Search native assets") as HTMLInputElement).value).toBe("");
  expect(
    within(screen.getByRole("group", { name: "Asset kind" }))
      .getByRole("button", { name: "Skills" })
      .getAttribute("aria-pressed"),
  ).toBe("true");
  expect(native.sources).toHaveBeenCalledTimes(3);
});

test("Skills admits leaving the actual dirty profile once and decline retains its text and focus", async () => {
  const native = nativeApi();
  const profile = profileApi();
  await mount({ native, profile });
  fireEvent.click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Agents" },
    ),
  );
  await settled();
  fireEvent.click(screen.getByRole("button", { name: "New profile" }));
  const name = screen.getByLabelText("Name") as HTMLInputElement;
  fireEvent.change(name, { target: { value: "Unsaved name" } });
  name.focus();
  const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
  const skills = screen.getByRole("button", { name: "Skills" });
  fireEvent.click(skills);
  await settled();
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("tab", { name: "Profiles" }).getAttribute("aria-selected")).toBe("true");
  expect(name.value).toBe("Unsaved name");
  expect(document.activeElement).toBe(name);
  expect(native.sources).toHaveBeenCalledTimes(1);
  expect(profile.create).not.toHaveBeenCalled();
  confirm.mockClear();
  fireEvent.click(skills);
  await settled();
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("tab", { name: "Subagents" }).getAttribute("aria-selected")).toBe("true");
  expect(screen.queryByLabelText("Name")).toBeNull();
  expect(profile.create).not.toHaveBeenCalled();
});

test.each(["native", "profile"] as const)(
  "missing %s client is unavailable without a zero claim or an enabled entry",
  async (missing) => {
    await mount(missing === "native" ? { native: null } : { profile: null });
    if (missing === "native") {
      expect(screen.getByText("Skills catalog unavailable.")).toBeTruthy();
      expect(screen.queryByText("Skills returned: 0")).toBeNull();
      expect(screen.queryByRole("button", { name: "Open Skills" })).toBeNull();
    } else {
      expect(screen.getByText("Skills returned: 1")).toBeTruthy();
      expect(screen.getByText("Profile client is not connected.")).toBeTruthy();
      expect(
        (screen.getByRole("button", { name: "Open Skills" }) as HTMLButtonElement).disabled,
      ).toBe(true);
    }
  },
);

test("Back restores the current Open Skills opener after the Native page unmounts", async () => {
  await mount();
  const opener = screen.getByRole("button", { name: "Open Skills" });
  opener.focus();
  fireEvent.click(opener);
  await settled();
  fireEvent.click(screen.getByRole("button", { name: "Back" }));
  await settled();
  const current = screen.getByRole("button", { name: "Open Skills" });
  expect(current).not.toBe(opener);
  expect(document.activeElement).toBe(current);
});

test("ordinary Native entry keeps All and forwards the actual profile client for agent copies", async () => {
  const profile = profileApi();
  vi.mocked(profile.create).mockResolvedValueOnce({
    profile_id: "fixture-profile",
    revision: 1,
    state_revision: 1,
  });
  await mount({ profile });
  fireEvent.click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Agents" },
    ),
  );
  await settled();
  fireEvent.click(screen.getByRole("tab", { name: "Subagents & skills" }));
  await settled();
  expect(
    within(screen.getByRole("group", { name: "Asset kind" }))
      .getByRole("button", { name: "All" })
      .getAttribute("aria-pressed"),
  ).toBe("true");
  expect(screen.getByText("SKILL")).toBeTruthy();
  expect(screen.getByText("Meeting notes skill")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Create an app profile from this agent" }));
  await settled();
  expect(profile.create).toHaveBeenCalledExactlyOnceWith(
    expect.any(String),
    expect.any(String),
    expect.objectContaining({ identity: expect.objectContaining({ name: "SKILL" }) }),
  );
  expect(screen.getByRole("tab", { name: "Profiles" }).getAttribute("aria-selected")).toBe("true");
});

test("a reported Recovery context disposes the early summary read and offers no Skills entry", async () => {
  const native = nativeApi();
  let finish: (sources: Awaited<ReturnType<NativeAssetsApi["sources"]>>) => void = () => {};
  vi.mocked(native.sources).mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  await mount({
    native,
    shell: fakeShellApi({ capabilities: capabilitiesFixture({ app: { mode: "recovery" } }) }),
  });
  expect(vi.mocked(native.sources).mock.calls[0]?.[0]?.aborted).toBe(true);
  expect(screen.queryByRole("button", { name: "Open Skills" })).toBeNull();
  expect(screen.queryByText("Skills returned: 1")).toBeNull();
  await act(async () => finish([{ source_id: "old", provider: "claude", scope: "workspace" }]));
  expect(native.catalog).not.toHaveBeenCalled();
  expect(screen.queryByText("Skills returned: 1")).toBeNull();
});

test("actual capability workspace identity aborts an unknown-context read before a late source response", async () => {
  const native = nativeApi();
  let finish: (sources: Awaited<ReturnType<NativeAssetsApi["sources"]>>) => void = () => {};
  vi.mocked(native.sources).mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  await mount({
    native,
    shell: fakeShellApi({ settings: settingsFixture({ "onboarding.completed": true }) }),
  });
  expect(vi.mocked(native.sources).mock.calls[0]?.[0]?.aborted).toBe(true);
  expect(vi.mocked(native.sources).mock.calls[1]?.[0]?.aborted).toBe(false);
  expect(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" }))
      .getByRole("button", { name: "Chat" })
      .getAttribute("aria-current"),
  ).toBe("page");
  expect(screen.getByText("Skills returned: 1")).toBeTruthy();
  expect(native.sources).toHaveBeenCalledTimes(2);
  expect(native.catalog).toHaveBeenCalledTimes(1);
  await act(async () => finish([{ source_id: "old", provider: "claude", scope: "workspace" }]));
  expect(native.catalog).toHaveBeenCalledTimes(1);
  expect(screen.getByText("Skills returned: 1")).toBeTruthy();
});

// --- Needs you, the Approvals count and the background proposals read -----------------------

const VAULT = FAKE_WORKSPACE_ID;
const OTHER_VAULT = "44444444-4444-4444-8444-444444444444";
const SESSION = "55555555-5555-4555-8555-555555555555";
const USAGE = { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_creation_tokens: 0 };

/** A vault window, the one kind of window that reads proposals in the background: not
 * recovery, a code folder, the shared scratch root or a chat-only window. */
function vaultCapabilities(workspaceId = VAULT, mode: "normal" | "recovery" = "normal") {
  return capabilitiesFixture({
    app: { mode },
    workspace: {
      active: {
        id: workspaceId,
        kind: "vault",
        name: "Field Notes",
        policy: "ordinary",
        layout: "unknown",
        source: "created",
        aliased: false,
        root_available: true,
      },
      chat_only: false,
      shared_scratch: false,
    },
  });
}

function vaultShell(mode: "normal" | "recovery" = "normal") {
  return fakeShellApi({
    capabilities: vaultCapabilities(VAULT, mode),
    settings: settingsFixture({ "onboarding.completed": true }),
    workspaces: { ...listingFixture([workspaceFixture()]), active_id: VAULT, chat_only: false },
  });
}

/** One retro gate, from a file no proposal below comes from, and a known vault with 2 changes. */
function vaultRest(): FakeRest {
  return fakeRest({
    rails: {
      ...emptyRails(),
      gates: [gate({ id: "H1", item: "Recall skill" })],
      counts: { knowledge: 4, sources: 2, inbox: 1 },
      git: {
        state: "known",
        ref_state: "branch",
        branch: "main",
        head: "abc1234",
        committed_at: "2026-10-05T00:00:00.000Z",
        dirty: 2,
      },
    },
  });
}

/** `workspaceId` lets a test stand in for another vault's own read answer (e.g. after a
 * workspace switch); every other caller leaves it at this window's own `VAULT`. */
function proposal(index: number, attention = true, workspaceId = VAULT): VaultProposalItem {
  return {
    kind: "vault_proposal",
    id: String(index + 1).padStart(32, "0"),
    origin: { workspace_id: workspaceId, session_id: null },
    revision: "a".repeat(64),
    state: "open",
    attention,
    title: `Proposal ${index}`,
    ref: "system/retro/2026-10-01.md",
    origin_kind: "note_edit",
    base_revision: "b".repeat(64),
    text_bytes: 120,
    handed_at: null,
    created_at: "2026-10-01T00:00:00.000Z",
  };
}

function inbox(items: VaultProposalItem[]): DecisionInbox {
  return { items };
}

/** Every read answers that Proposal 0 and Proposal 2 need the user and Proposal 1 does not. */
function proposalApi(): DecisionsApi {
  return {
    decisions: vi.fn(async () => inbox([proposal(0), proposal(1, false), proposal(2)])),
    proposalDetail: vi.fn(),
    proposalAct: vi.fn(),
    createProposal: vi.fn(),
  };
}

function currentShell(): ShellStore {
  const store = observedStores.at(-1);
  if (!store) throw new Error("missing actual App ShellStore");
  return store;
}

/** Needs you's rows in order: `gate:<id>`, or `<kind>:<title>` for a native or proposal row. */
function needRows(): string[] {
  const section = screen.queryByText("Needs you", { selector: ".lbl" })?.closest("section");
  return [...(section?.querySelectorAll<HTMLElement>(".q, [data-need]") ?? [])].map((row) =>
    row.dataset.gateId === undefined
      ? `${row.dataset.need}:${row.querySelector(".need-title")?.textContent}`
      : `gate:${row.dataset.gateId}`,
  );
}

/** Needs you's own `.rail-section-count`, or null while the section is hidden (count 0). */
function needsCount(): string | null {
  return (
    screen
      .queryByText("Needs you", { selector: ".lbl" })
      ?.parentElement?.querySelector(".rail-section-count")?.textContent ?? null
  );
}

function frame(seq: number, type: string, data: unknown): Envelope {
  return {
    v: 2,
    type,
    session_id: SESSION,
    generation: GENERATION,
    request_id: "",
    seq,
    data,
  } as Envelope;
}

function turnDone(seq: number, contextPercent: number | null): Envelope {
  return frame(seq, "turn.done", {
    status: "success",
    usage: USAGE,
    context_percent: contextPercent,
    duration_ms: 1,
  });
}

test("proposal rows alone light the narrow VIKI panel toggle", async () => {
  vi.stubGlobal("matchMedia", (query: string) => ({
    media: query,
    matches: 400 >= Number(/min-width:\s*(\d+)px/.exec(query)?.[1] ?? "0"),
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
  await mount({ shell: vaultShell(), decisions: proposalApi(), rest: vaultRest() });
  const toggle = screen.getByRole("button", { name: "VIKI panel" });
  expect(toggle.querySelector(".rail-toggle-dot")).not.toBeNull();
  expect(toggle.getAttribute("title")).toBe("2 waiting for you");
});

test("a background read on mount fills Needs you, and a rails re-read that changes nothing reads nothing", async () => {
  const api = proposalApi();
  const rest = vaultRest();
  const client = fakeViki();
  await mount({ shell: vaultShell(), decisions: api, rest, client });
  // Approvals was never opened: the rail read the proposals itself. Proposal 1, which needs no
  // one, is not listed.
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(needRows()).toEqual(["proposal:Proposal 0", "proposal:Proposal 2"]);
  expect(needsCount()).toBe("2");
  expect(vi.mocked(api.decisions).mock.lastCall?.[0]?.aborted).toBe(false);
  const reads = vi.mocked(api.decisions).mock.calls.length;
  const railReads = () => rest.calls.filter((call) => call === "rails").length;
  const before = railReads();
  // A turn that ends re-reads the rails (state/rest.ts); the same gates and git ask for nothing.
  act(() => {
    client.socket.open();
    client.viki.actions.openSession(SESSION);
    client.socket.receive(frame(1, "turn.started", { turn_id: "t1" }));
  });
  act(() => client.socket.receive(turnDone(2, null)));
  await settled();
  expect(railReads()).toBeGreaterThan(before);
  expect(api.decisions).toHaveBeenCalledTimes(reads);
});

test("a native row names its agent and leads to its run; a proposal row opens Approvals on its inspector", async () => {
  const api = proposalApi();
  vi.mocked(api.proposalDetail).mockResolvedValue({
    ...proposal(0),
    current_revision: null,
    proposed_text: "The proposal's own paragraph",
    diff: "The proposal's own paragraph",
    diff_reason: null,
  });
  const client = fakeViki();
  const { container } = await mount({
    shell: vaultShell(),
    decisions: api,
    rest: vaultRest(),
    client,
  });
  // A background chat whose "researcher" run asks to write a note.
  act(() => {
    client.socket.open();
    client.viki.actions.openSession(SESSION);
    client.socket.receive(
      frame(1, "user.message", { text: "hi", command: "", at: "2026-10-05T00:00:00.000Z" }),
    );
    client.socket.receive(
      frame(2, "subagent.started", {
        block_id: "a1",
        agent: "researcher",
        model: "",
        description: "Survey sources",
        reason: "",
      }),
    );
    client.socket.receive(
      frame(3, "permission.requested", {
        tool_use_id: "fixture-tool",
        kind: "tool",
        tool: "Write",
        summary: "notes/draft.md",
        question: "",
        reason: "",
        agent: "researcher",
        plan: "",
      }),
    );
  });
  await settled();
  // One list and one count: the native request first, then the proposals.
  expect(needRows()).toEqual([
    "native:Write notes/draft.md",
    "proposal:Proposal 0",
    "proposal:Proposal 2",
  ]);
  expect(needsCount()).toBe("3");
  const native = screen.getByRole("button", { name: /^Write notes\/draft\.md/ });
  expect(native.querySelector(".need-meta")?.textContent).toMatch(
    / · waiting for approval · researcher$/,
  );
  // Agents does not list the waiting run a second time.
  expect(container.querySelector(".agent-runs")?.textContent).not.toContain("Needs you");
  fireEvent.click(native);
  await settled();
  const target = container.querySelector<HTMLElement>(
    '.card-column .card-target[data-tool-use-id="fixture-tool"]',
  );
  expect(target).not.toBeNull();
  expect(document.activeElement).toBe(target);
  const again = screen.getByRole("button", { name: /^Write notes\/draft\.md/ });
  again.focus();
  expect(document.activeElement).toBe(again);
  fireEvent.click(again);
  await settled();
  expect(document.activeElement).toBe(target);
  expect(
    container.querySelector('.routing[data-routing-id="a1"]')?.classList.contains("routing-target"),
  ).toBe(false);
  expect(container.querySelector(".card-column")?.textContent).toContain("notes/draft.md");
  // The context now shows in the right rail's own footer once the chat on screen reports it.
  expect(screen.queryByText("This chat")).toBeNull();
  act(() => client.socket.receive(turnDone(4, 42)));
  expect(container.querySelector(".rail-context")).toBeNull();
  expect(container.querySelector(".context-summary")?.textContent).toContain("42% used");
  fireEvent.click(screen.getByRole("button", { name: /^Proposal 0/ }));
  await settled();
  const approvals = screen.getByRole("dialog", { name: "Approvals" });
  const inspector = await screen.findByRole("dialog", { name: "system/retro/2026-10-01.md" });
  expect(approvals.contains(inspector)).toBe(true);
  expect(api.proposalDetail).toHaveBeenCalledWith(proposal(0).id, expect.anything());
  expect(inspector.textContent).toContain("The proposal's own paragraph");
});

test("Settings → Diagnostics shows the background read's time, attention count and workspace", async () => {
  const stamp = "2026-10-05T00:00:00.000Z";
  vi.spyOn(Date, "now").mockReturnValue(Date.parse(stamp));
  window.sessionStorage.setItem(
    "viki.view",
    JSON.stringify({ view: "settings", page: "diagnostics", panel: "locations" }),
  );
  await mount({ shell: vaultShell(), decisions: proposalApi(), rest: vaultRest() });
  const read = screen.getByRole("heading", { name: "Last approvals check" }).closest("section");
  const shown = new Date(stamp).toLocaleString("en-US", {
    dateStyle: "medium",
    timeStyle: "short",
  });
  expect(read?.querySelector("time")?.getAttribute("dateTime")).toBe(stamp);
  expect(read?.querySelector("time")?.textContent).toBe(shown);
  expect(read?.querySelector("p")?.textContent).toBe(
    `Last read ${shown} · 2 need attention · workspace ${VAULT}`,
  );
});

test("a recovery window never reads proposals", async () => {
  const api = proposalApi();
  const { container } = await mount({
    shell: vaultShell("recovery"),
    decisions: api,
    rest: vaultRest(),
  });
  await waitFor(() => expect(container.querySelector(".recovery-view")).not.toBeNull());
  expect(api.decisions).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "Open Approvals" })).toBeNull();
});

test("entering recovery aborts the read in flight, and its late answer never reaches the rail", async () => {
  const api = proposalApi();
  let answer: (value: DecisionInbox) => void = () => {};
  vi.mocked(api.decisions).mockImplementation(
    () =>
      new Promise((resolve) => {
        answer = resolve;
      }),
  );
  const shell = vaultShell();
  const { container } = await mount({ shell, decisions: api, rest: vaultRest() });
  const pending = vi.mocked(api.decisions).mock.lastCall?.[0];
  expect(pending?.aborted).toBe(false);
  shell.data.capabilities = vaultCapabilities(VAULT, "recovery");
  await act(async () => currentShell().refreshCapabilities());
  expect(container.querySelector(".recovery-view")).not.toBeNull();
  expect(pending?.aborted).toBe(true);
  const reads = vi.mocked(api.decisions).mock.calls.length;
  await act(async () => answer(inbox([proposal(7)])));
  expect(api.decisions).toHaveBeenCalledTimes(reads);
  // Back in a normal window a fresh read is asked; until it answers, the rail has no proposals.
  shell.data.capabilities = vaultCapabilities();
  await act(async () => currentShell().refreshCapabilities());
  expect(api.decisions).toHaveBeenCalledTimes(reads + 1);
  expect(needRows()).toEqual([]);
  await act(async () => answer(inbox([proposal(2)])));
  expect(needRows()).toEqual(["proposal:Proposal 2"]);
});

test("a workspace switch drops the last workspace's rows at once and reads the new one", async () => {
  const api = proposalApi();
  const shell = vaultShell();
  await mount({ shell, decisions: api, rest: vaultRest() });
  expect(needRows()).toEqual(["proposal:Proposal 0", "proposal:Proposal 2"]);
  const reads = vi.mocked(api.decisions).mock.calls.length;
  let answer: (value: DecisionInbox) => void = () => {};
  vi.mocked(api.decisions).mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        answer = resolve;
      }),
  );
  shell.data.capabilities = vaultCapabilities(OTHER_VAULT);
  await act(async () => currentShell().refreshCapabilities());
  expect(api.decisions).toHaveBeenCalledTimes(reads + 1);
  // No render for the new workspace listed the last one's proposals, not even the render before
  // the read hook's effect dropped them.
  const switched = renderedNeeds.filter((render) => render.workspace === OTHER_VAULT);
  expect(switched.length).toBeGreaterThan(0);
  expect(switched.some(({ needs }) => needs.rows.some((row) => row.kind === "proposal"))).toBe(
    false,
  );
  expect(needRows()).toEqual([]);
  expect(needsCount()).toBeNull();
  // The fresh read answers for OTHER_VAULT, so this proposal names it, not VAULT.
  await act(async () => answer(inbox([proposal(5, true, OTHER_VAULT)])));
  expect(needRows()).toEqual(["proposal:Proposal 5"]);
  expect(needsCount()).toBe("1");
});

test("closing Approvals reads the proposals again, and the rail follows the answer", async () => {
  const api = proposalApi();
  await mount({ shell: vaultShell(), decisions: api, rest: vaultRest() });
  fireEvent.click(screen.getByRole("button", { name: "Open Approvals" }));
  await settled();
  const dialog = screen.getByRole("dialog", { name: "Approvals" });
  const reads = vi.mocked(api.decisions).mock.calls.length;
  // Proposal 0 was decided while Approvals was open.
  vi.mocked(api.decisions).mockResolvedValueOnce(inbox([proposal(0, false), proposal(2)]));
  fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
  await settled();
  expect(screen.queryByRole("dialog", { name: "Approvals" })).toBeNull();
  expect(api.decisions).toHaveBeenCalledTimes(reads + 1);
  expect(needRows()).toEqual(["proposal:Proposal 2"]);
  expect(needsCount()).toBe("1");
});

test("the footer holds the tiles, the uncommitted count and Skills, and nothing more", async () => {
  const { container } = await mount({
    shell: vaultShell(),
    decisions: proposalApi(),
    rest: vaultRest(),
  });
  const summary = container.querySelector<HTMLElement>(".right-rail-summary");
  if (!summary) throw new Error("missing rail footer");
  expect([...summary.children].map((child) => child.className)).toEqual([
    "tiles",
    "rail-uncommitted",
    "rail-section skills-summary",
  ]);
  expect(within(summary).getByText("2 uncommitted").getAttribute("title")).toBe(
    "Vault changes not yet committed to git",
  );
  expect(within(summary).getByText("Skills returned: 1")).toBeTruthy();
  // This chat waits for a chat that reports its context.
  expect(screen.queryByText("This chat")).toBeNull();
});

test("Skills shows only the selected chat's Skill calls, without implying catalog use", async () => {
  window.sessionStorage.setItem("viki.selected", SESSION);
  const client = fakeViki();
  const native = nativeApi();
  const catalog = await native.catalog("fixture", new AbortController().signal);
  vi.mocked(native.catalog).mockResolvedValue({
    ...catalog,
    assets: catalog.assets.map((asset) =>
      asset.kind === "skill" ? { ...asset, name: "review" } : asset,
    ),
  });
  const rest = fakeRest({
    sessions: [
      sessionRow({ session_id: SESSION, title: "Skill chat" }),
      sessionRow({ session_id: "66666666-6666-4666-8666-666666666666", title: "Other chat" }),
    ],
  });
  const { container } = await mount({ client, native, shell: vaultShell(), rest });
  const rail = (): HTMLElement => {
    const section = container.querySelector<HTMLElement>(".skills-summary");
    if (!section) throw new Error("missing Skills rail section");
    return section;
  };
  act(() => {
    client.socket.open();
    client.viki.actions.openSession(SESSION);
    client.socket.receive(
      frame(1, "session.started", {
        model: "sonnet",
        mode: "auto",
        effort: "low",
        commands: [],
        agents: [],
        models: [],
        cli_version: "2.1.259",
        resumed: true,
      }),
    );
    client.socket.receive(frame(2, "turn.started", { turn_id: "t1" }));
    client.socket.receive(
      frame(3, "tool.started", {
        block_id: "skill-1",
        tool: "Skill",
        row: { head: "", rest: "review", tail: "" },
        parent_id: "",
      }),
    );
    client.socket.receive(
      frame(4, "tool.started", {
        block_id: "other-1",
        tool: "Read",
        row: { head: "", rest: "review.md", tail: "" },
        parent_id: "",
      }),
    );
  });
  await settled();
  expect(rail().querySelector(".skill-activity-name")?.textContent).toBe("review");
  expect(rail().querySelector(".skills-preview-name")?.textContent).toBe("review");
  expect(rail().querySelector(".skills-preview-row")?.className).toBe("skills-preview-row");
  expect(rail().querySelectorAll(".skill-activity-call")).toHaveLength(1);
  expect(within(rail()).getByText("Calling")).toBeTruthy();
  act(() => {
    client.socket.receive(frame(5, "tool.done", { block_id: "skill-1", is_error: false }));
  });
  expect(within(rail()).getByText("Invoked")).toBeTruthy();
  act(() => {
    client.socket.receive(
      frame(6, "tool.started", {
        block_id: "skill-2",
        tool: "Skill",
        row: { head: "", rest: "failing-skill", tail: "" },
        parent_id: "",
      }),
    );
    client.socket.receive(frame(7, "tool.done", { block_id: "skill-2", is_error: true }));
  });
  expect(within(rail()).getByText("Failed")).toBeTruthy();
  expect(rail().querySelector(".skills-preview-row")?.className).toBe("skills-preview-row");

  act(() => {
    fireEvent.click(screen.getByRole("button", { name: "Other chat" }));
  });
  await settled();
  expect(rail().querySelector(".skill-activity-name")).toBeNull();
  expect(rail().querySelector(".skill-activity-status")).toBeNull();
});

test("Skills activity remains visible without the native catalog API and clears off Chat", async () => {
  window.sessionStorage.setItem("viki.selected", SESSION);
  const client = fakeViki();
  const rest = fakeRest({
    sessions: [sessionRow({ session_id: SESSION, title: "Skill chat" })],
  });
  const { container } = await mount({
    native: null,
    client,
    shell: vaultShell(),
    rest,
  });
  const rail = (): HTMLElement => {
    const section = container.querySelector<HTMLElement>(".skills-summary");
    if (!section) throw new Error("missing Skills rail section");
    return section;
  };
  act(() => {
    client.socket.open();
    client.viki.actions.openSession(SESSION);
    client.socket.receive(
      frame(1, "session.started", {
        model: "sonnet",
        mode: "auto",
        effort: "low",
        commands: [],
        agents: [],
        models: [],
        cli_version: "2.1.259",
        resumed: true,
      }),
    );
    client.socket.receive(frame(2, "turn.started", { turn_id: "t1" }));
    client.socket.receive(
      frame(3, "tool.started", {
        block_id: "skill-1",
        tool: "Skill",
        row: { head: "", rest: "no-catalog-skill", tail: "" },
        parent_id: "",
      }),
    );
  });
  await settled();
  expect(within(rail()).getByText("Skills catalog unavailable.")).toBeTruthy();
  expect(within(rail()).getByText("no-catalog-skill")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Agents" }));
  await settled();
  expect(screen.queryByText("no-catalog-skill")).toBeNull();
});
