/**
 * Self-improvement as App wires it: launcher draft actions fill a composer and send nothing;
 * the inline skill/question hint moves into the launcher once Proactive VIKI is on, not paused,
 * and Self-improvement is on (and stays put otherwise); a question-wording proposal can be
 * reviewed and approved from Approvals; and "Propose this wording" on a reply creates one and
 * opens its inspector.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, GENERATION, sessionRow } from "../../accept/m3b/fakes";
import type { SettingsView } from "../../clients/app/shellApi";
import {
  createFakeDecisionLoopApi,
  decisionLoopExamples,
  type FakeDecisionLoopApi,
} from "../../clients/decisionLoop/fake";
import type { DecisionsApi, QuestionProposalItem } from "../../state/decisionsApi";
import type { SkillProposalsApi } from "../../state/skillProposalsApi";
import { fakeDurable } from "../../testDurable";
import {
  capabilitiesFixture,
  type FakeShellApi,
  fakeShellApi,
  listingFixture,
  settingsFixture,
} from "../../testShell";

beforeEach(() => {
  sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  sessionStorage.clear();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

async function settled(): Promise<void> {
  await act(async () => {
    for (let index = 0; index < 8; index++) await Promise.resolve();
  });
}
const click = async (element: Element) => act(async () => void fireEvent.click(element));
const box = () => screen.getByLabelText<HTMLTextAreaElement>("Message");

// ---- Group A: the launcher's own draft actions (no vault needed) ----------------------------

function mountLauncher(options: { decisionApi?: FakeDecisionLoopApi } = {}) {
  const { viki, socket } = fakeViki();
  const view = render(
    <App
      viki={viki}
      rest={fakeRest({ sessions: [] })}
      durable={fakeDurable()}
      shell={fakeShellApi({ settings: settingsFixture({ "onboarding.completed": true }) })}
      {...(options.decisionApi ? { decisionApi: options.decisionApi } : {})}
    />,
  );
  act(() => socket.open());
  return { socket, ...view };
}
const launcherButton = () => document.querySelector<HTMLButtonElement>("button.dloop-launcher");
const panel = () => screen.queryByRole("dialog", { name: "VIKI" });
const card = (what: string): HTMLElement => {
  const found = screen.getByText(what).closest("article");
  if (!(found instanceof HTMLElement)) throw new Error(`no card for ${what}`);
  return found;
};
async function openPanel() {
  const trigger = launcherButton();
  if (!trigger) throw new Error("no launcher");
  await click(trigger);
  await settled();
  return within(screen.getByRole("dialog", { name: "VIKI" }));
}

test("a draft action fills a new chat's composer and sends nothing", async () => {
  const api = createFakeDecisionLoopApi();
  const { socket } = mountLauncher({ decisionApi: api });
  await settled();
  await openPanel();
  const item = decisionLoopExamples().launcher.items.find(
    (candidate) => candidate.candidate === "retro_due",
  );
  const draft = item?.actions.find((action) => action.type === "draft_new");
  if (!item || !draft?.text) throw new Error("the example launcher lacks a retro_due draft item");
  await click(within(card(item.what)).getByRole("button", { name: draft.label }));
  await settled();
  expect(panel()).toBeNull();
  expect(box().value).toBe(draft.text);
  expect(socket.sent.filter((frame) => frame.type === "turn.send")).toEqual([]);
  expect(api.requests).toContainEqual({ method: "observe", args: ["acted", [item.item_key]] });
});

test("a draft_chat action opens its chat, fills the composer there, and sends nothing", async () => {
  const api = createFakeDecisionLoopApi();
  const { socket } = mountLauncher({ decisionApi: api });
  await settled();
  await openPanel();
  const item = decisionLoopExamples().launcher.items.find(
    (candidate) => candidate.candidate === "skill_offer",
  );
  const draft = item?.actions.find((action) => action.type === "draft_chat");
  if (!item || !draft?.text || !draft.session_id)
    throw new Error("the example launcher lacks a skill_offer draft_chat item");
  await click(within(card(item.what)).getByRole("button", { name: draft.label }));
  await settled();
  expect(panel()).toBeNull();
  expect(sessionStorage.getItem("viki.selected")).toBe(draft.session_id);
  // appendToDraft's own formula (reused here, keyed to the target chat): the given text plus a
  // trailing blank line, ready for the next paste — never an exact, unpadded match.
  expect(box().value).toContain(draft.text);
  expect(socket.sent.filter((frame) => frame.type === "turn.send")).toEqual([]);
  expect(api.requests).toContainEqual({ method: "observe", args: ["acted", [item.item_key]] });
});

test("a draft action observes only acted, not opened", async () => {
  // The draft branch returns after observing "acted", so "opened" is never observed.
  // Without the return, both would be sent; this test pins that the return is there.
  const api = createFakeDecisionLoopApi();
  mountLauncher({ decisionApi: api });
  await settled();
  await openPanel();
  const item = decisionLoopExamples().launcher.items.find(
    (candidate) => candidate.candidate === "retro_due",
  );
  const draft = item?.actions.find((action) => action.type === "draft_new");
  if (!item || !draft?.text) throw new Error("the example launcher lacks a retro_due draft item");
  await click(within(card(item.what)).getByRole("button", { name: draft.label }));
  await settled();
  // Check that "acted" was observed and "opened" was not
  expect(api.requests).toContainEqual({ method: "observe", args: ["acted", [item.item_key]] });
  expect(api.requests).not.toContainEqual({ method: "observe", args: ["opened", [item.item_key]] });
});

// ---- Groups B-D: a vault workspace, where the inline skill/question hint and proposals live --

// Matches decisionLoopExamples().question_proposal.proposal.origin.workspace_id, so Groups C-D
// need no override to make that proposal's row "active" (approvalKinds' own gate).
const VAULT = "315d31aa-25ed-45a7-a4b8-5c582ce841fb";
const S1 = sessionRow({ session_id: "s1", title: "Chat" });
const AT = "2026-10-09T00:00:00.000Z";
const USAGE = { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_creation_tokens: 0 };
const DONE = { status: "success", usage: USAGE, context_percent: null, duration_ms: 1 };

function stubSkillProposals(): SkillProposalsApi {
  return { create: vi.fn(), detail: vi.fn(), act: vi.fn() } as unknown as SkillProposalsApi;
}
function fakeDecisions(items: QuestionProposalItem[] = []): DecisionsApi {
  return {
    decisions: async () => ({
      items,
      retro: { state: "ok", complete: true, reason: null, sources: [] },
    }),
  } as unknown as DecisionsApi;
}
function vaultShell(effective: Record<string, unknown> = {}): FakeShellApi {
  const base = settingsFixture();
  return fakeShellApi({
    capabilities: capabilitiesFixture({
      workspace: {
        active: {
          id: VAULT,
          kind: "vault",
          name: "Field Notes",
          policy: "ordinary",
          layout: "legacy_para",
          source: "builtin",
          aliased: false,
          root_available: true,
        },
        chat_only: false,
        shared_scratch: false,
      },
    }),
    workspaces: { ...listingFixture(), chat_only: false },
    settings: { ...base, effective: { ...base.effective, ...effective } } as SettingsView,
  });
}

async function mountVault(
  options: {
    decisionApi?: FakeDecisionLoopApi;
    decisions?: DecisionsApi;
    effective?: Record<string, unknown>;
  } = {},
) {
  const { viki, socket } = fakeViki();
  const view = render(
    <App
      viki={viki}
      rest={fakeRest({ sessions: [S1] })}
      durable={fakeDurable()}
      shell={vaultShell(options.effective)}
      decisions={options.decisions ?? fakeDecisions()}
      skillProposals={stubSkillProposals()}
      {...(options.decisionApi ? { decisionApi: options.decisionApi } : {})}
    />,
  );
  await act(async () => socket.open());
  const row = await waitFor(() => {
    const found = view.container.querySelector<HTMLElement>('[data-session-id="s1"]');
    if (!found) throw new Error("the session list has not arrived");
    return found;
  });
  await click(row);
  return { ...view, socket };
}

async function plainTurn(
  socket: Awaited<ReturnType<typeof fakeViki>>["socket"],
  seq: number,
): Promise<void> {
  await act(async () => {
    socket.receive({
      v: 2,
      generation: GENERATION,
      request_id: "",
      type: "user.message",
      session_id: "s1",
      seq,
      data: { text: "continue", command: "", at: AT },
    });
    socket.receive({
      v: 2,
      generation: GENERATION,
      request_id: "",
      type: "text.done",
      session_id: "s1",
      seq: seq + 1,
      data: { block_id: "b1", text: "ok" },
    });
    socket.receive({
      v: 2,
      generation: GENERATION,
      request_id: "",
      type: "turn.done",
      session_id: "s1",
      seq: seq + 2,
      data: DONE,
    });
  });
}
/** 8 finished turns reach LONG_TURNS (state/skillTriggers.ts), the simplest skillHint to fire. */
async function longSession(socket: Awaited<ReturnType<typeof fakeViki>>["socket"]): Promise<void> {
  for (let seq = 1; seq <= 22; seq += 3) await plainTurn(socket, seq);
}

test("the inline skill hint is hidden once Proactive VIKI is on, not paused, and Self-improvement is on", async () => {
  const { socket } = await mountVault({
    effective: {
      "decision.enabled": true,
      "decision.paused_until": "",
      "decision.watch.improve": true,
    },
  });
  await longSession(socket);
  // Every one of the 8 finished turns draws its own "Make this a skill" button (SkillTurnActions
  // renders it unconditionally); only the newest turn's own hint note and Not now are at stake
  // here, so settling is awaited through the plain count rather than that ambiguous name.
  await waitFor(() => {
    if (screen.getAllByRole("button", { name: "Make this a skill" }).length === 0)
      throw new Error("not rendered yet");
  });
  expect(screen.queryByRole("button", { name: "Not now" })).toBeNull();
  expect(screen.queryByRole("note")).toBeNull();
});

test("the inline skill hint stays while Proactive VIKI is paused", async () => {
  const pausedUntil = new Date(Date.now() + 3_600_000).toISOString();
  const { socket } = await mountVault({
    effective: {
      "decision.enabled": true,
      "decision.paused_until": pausedUntil,
      "decision.watch.improve": true,
    },
  });
  await longSession(socket);
  expect(await screen.findByRole("button", { name: "Not now" })).toBeTruthy();
  expect(screen.getByRole("note")).toBeTruthy();
});

test("the inline skill hint stays while Self-improvement is off", async () => {
  const { socket } = await mountVault({
    effective: {
      "decision.enabled": true,
      "decision.paused_until": "",
      "decision.watch.improve": false,
    },
  });
  await longSession(socket);
  expect(await screen.findByRole("button", { name: "Not now" })).toBeTruthy();
  expect(screen.getByRole("note")).toBeTruthy();
});

test("the inline skill hint is hidden when the pause has expired", async () => {
  // A paused_until time in the past should not count as paused; the hint moves to the launcher
  const pastTime = new Date(Date.now() - 3_600_000).toISOString();
  const { socket } = await mountVault({
    effective: {
      "decision.enabled": true,
      "decision.paused_until": pastTime,
      "decision.watch.improve": true,
    },
  });
  await longSession(socket);
  // Same as the "not paused, not off" case: the hint is hidden and only "Make this a skill"
  // buttons remain on the turns (no "Not now" button or hint note inline)
  await waitFor(() => {
    if (screen.getAllByRole("button", { name: "Make this a skill" }).length === 0)
      throw new Error("not rendered yet");
  });
  expect(screen.queryByRole("button", { name: "Not now" })).toBeNull();
  expect(screen.queryByRole("note")).toBeNull();
});

// ---- Group C: a question-wording proposal in Approvals, acted on through the questions API ---

test("Approve in the question-proposal inspector calls actProposal through the questions API", async () => {
  const seed = decisionLoopExamples().question_proposal;
  const openProposal: QuestionProposalItem = {
    ...seed.proposal,
    state: "open",
    attention: true,
    approved_at: null,
    rejected_at: null,
    reason: null,
  };
  const api = createFakeDecisionLoopApi({ question_proposal: { ...seed, proposal: openProposal } });
  await mountVault({ decisionApi: api, decisions: fakeDecisions([openProposal]) });
  await click(screen.getByRole("button", { name: "Open Approvals" }));
  const section = await screen.findByRole("region", { name: "Proposals" });
  await click(within(section).getByRole("button", { name: /^Review details/ }));
  const inspector = await screen.findByRole("dialog", { name: openProposal.title });
  const pres = inspector.querySelectorAll("pre");
  expect(pres[0]?.textContent).toBe(seed.current_text);
  expect(pres[1]?.textContent).toBe(seed.proposed_text);
  await click(within(inspector).getByRole("button", { name: "Approve" }));
  await waitFor(() =>
    expect(api.requests).toContainEqual({
      method: "actProposal",
      args: [openProposal.id, "approve", openProposal.revision, undefined],
    }),
  );
});

// ---- Group D: proposing a wording straight from a reply ---------------------------------------

test("Propose this wording creates a proposal and Open in Approvals shows its inspector", async () => {
  const api = createFakeDecisionLoopApi();
  const { socket } = await mountVault({ decisionApi: api });
  await act(async () => {
    socket.receive({
      v: 2,
      generation: GENERATION,
      request_id: "",
      type: "user.message",
      session_id: "s1",
      seq: 1,
      data: { text: "tune it", command: "", at: AT },
    });
    socket.receive({
      v: 2,
      generation: GENERATION,
      request_id: "",
      type: "text.done",
      session_id: "s1",
      seq: 2,
      data: {
        block_id: "b1",
        text: '```decision-question\n{"name":"news","question":{"foo":"bar"}}\n```',
      },
    });
    socket.receive({
      v: 2,
      generation: GENERATION,
      request_id: "",
      type: "turn.done",
      session_id: "s1",
      seq: 3,
      data: DONE,
    });
  });
  await click(await screen.findByRole("button", { name: "Propose this wording" }));
  await waitFor(() =>
    expect(api.requests).toContainEqual({
      method: "createProposal",
      args: ["news", { foo: "bar" }],
    }),
  );
  await click(await screen.findByRole("button", { name: "Open in Approvals" }));
  const title = decisionLoopExamples().question_proposal.proposal.title;
  expect(await screen.findByRole("dialog", { name: title })).toBeTruthy();
});
