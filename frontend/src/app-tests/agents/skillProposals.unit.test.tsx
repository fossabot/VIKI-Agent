/**
 * Skill proposals as App wires them into the main Conversation and the decision inbox: "Make this
 * a skill" fills this chat's own message box and sends nothing; "Propose this skill" posts the
 * parsed SKILL.md block; "Open in Approvals" opens the skill inspector, found by the skill's own
 * name (never the inbox's own title, which can be renamed); approving it hands off to a NEW editor
 * with the apply text, sending nothing. Without the `skillProposals` prop none of it renders.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, GENERATION, sessionRow } from "../../accept/m3b/fakes";
import type { SessionRow } from "../../protocol";
import type { DecisionsApi } from "../../state/decisionsApi";
import type {
  SkillProposalDetail,
  SkillProposalItem,
  SkillProposalsApi,
} from "../../state/skillProposalsApi";
import { fakeDurable } from "../../testDurable";
import { capabilitiesFixture, fakeShellApi, listingFixture } from "../../testShell";

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  vi.useRealTimers();
});

const WORKSPACE = "88888888-8888-4888-8888-888888888888";
const SKILL_ID = "99999999-9999-4999-8999-999999999999";
const SKILL_NAME = "my-test-skill";
const SKILL_REVISION = "f".repeat(64);
const AT = "2026-10-09T00:00:00.000Z";
const USAGE = { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_creation_tokens: 0 };
const DONE = { status: "success", usage: USAGE, context_percent: null, duration_ms: 1 };
const S1 = sessionRow({ session_id: "s1", title: "Chat" });

// One reply holding exactly one fenced SKILL.md block, so `skillBlock` (state/skillTriggers.ts)
// parses it: SKILL_TEXT is the assistant's whole reply, SKILL_BLOCK the content it should parse
// out of the fence.
const SKILL_TEXT = [
  "Here is the skill:",
  "```SKILL.md",
  "---",
  "name: my-test-skill",
  "description: handles the test flow end to end",
  "---",
  "Step 1. Do the thing.",
  "```",
].join("\n");
const SKILL_BLOCK = [
  "---",
  "name: my-test-skill",
  "description: handles the test flow end to end",
  "---",
  "Step 1. Do the thing.",
  "",
].join("\n");

/** A vault workspace, active and not shared scratch, so the skill UI's own gate is open. Also
 * not chat-only at the workspaces-listing level, so Needs you's own background proposals read
 * (App.tsx's `useProposalAttention`) is enabled and a skill proposal can reach Open Approvals. */
function vaultShell() {
  return fakeShellApi({
    capabilities: capabilitiesFixture({
      workspace: {
        active: {
          id: WORKSPACE,
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
  });
}

/** A code-folder workspace: same shape as `vaultShell`, except the skill UI's own gate
 * (App.tsx's `kind === "vault"` check) stays shut. */
function codeShell() {
  return fakeShellApi({
    capabilities: capabilitiesFixture({
      workspace: {
        active: {
          id: WORKSPACE,
          kind: "code",
          name: "A code folder",
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
  });
}

/** Only `decisions()` is exercised here (the section's own mount read, parameterized with
 * whatever items a test needs); the rest of the panel has tests of its own. */
function fakeDecisions(items: SkillProposalItem[] = []): DecisionsApi {
  return {
    decisions: async () => ({
      items,
      retro: { state: "ok", complete: true, reason: null, sources: [] },
    }),
  } as unknown as DecisionsApi;
}

function skillItem(over: Partial<SkillProposalItem> = {}): SkillProposalItem {
  return {
    kind: "skill_proposal",
    id: SKILL_ID,
    origin: { workspace_id: WORKSPACE, session_id: null },
    revision: SKILL_REVISION,
    state: "open",
    attention: true,
    title: SKILL_NAME,
    name: SKILL_NAME,
    relative_id: `skills/${SKILL_NAME}/SKILL.md`,
    trigger: "button",
    sessions: ["s1"],
    base_revision: null,
    text_bytes: 120,
    approved_at: null,
    rejected_at: null,
    reason: null,
    created_at: AT,
    updated_at: AT,
    ...over,
  };
}

function skillDetail(over: Partial<SkillProposalDetail> = {}): SkillProposalDetail {
  return {
    ...skillItem(),
    text: SKILL_BLOCK,
    current_text: null,
    diff: "",
    diff_reason: "no_source",
    description: "handles the test flow end to end",
    unsupported_fields: [],
    shadowed: false,
    policy: "ordinary",
    ...over,
  };
}

function fakeSkillProposals(
  resultItem: SkillProposalItem | null,
  detailOver: Partial<SkillProposalDetail> = {},
) {
  const create = vi.fn(async () => ({
    id: SKILL_ID,
    item: resultItem,
    replayed: resultItem === null,
  }));
  const detail = vi.fn(async () => skillDetail(detailOver));
  const act = vi.fn(async () => null);
  return { api: { create, detail, act } as unknown as SkillProposalsApi, create, detail, act };
}

const box = () => screen.getByLabelText<HTMLTextAreaElement>("Message");

/** A vault workspace, this window's one session selected, and that session's last turn a
 * finished, successful reply holding one SKILL.md block. `skillProposals` is the one prop this
 * test varies; `decisions` stays present throughout so an absent `skillProposals` is what a
 * "no skill UI" test is actually about. */
async function mount(
  props: {
    skillProposals?: SkillProposalsApi;
    sessions?: SessionRow[];
    shell?: ReturnType<typeof fakeShellApi>;
    decisions?: DecisionsApi;
  } = {},
) {
  const rest = fakeRest({ sessions: props.sessions ?? [S1] });
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  const utils = render(
    <App
      viki={viki}
      rest={rest}
      durable={durable}
      shell={props.shell ?? vaultShell()}
      decisions={props.decisions ?? fakeDecisions()}
      {...(props.skillProposals ? { skillProposals: props.skillProposals } : {})}
    />,
  );
  await act(async () => {
    socket.open();
  });
  const row = await waitFor(() => {
    const found = utils.container.querySelector<HTMLElement>('[data-session-id="s1"]');
    if (!found) throw new Error("the list has not arrived");
    return found;
  });
  await act(async () => {
    fireEvent.click(row);
  });
  await act(async () => {
    socket.receive({
      v: 2,
      generation: GENERATION,
      request_id: "",
      type: "user.message",
      session_id: "s1",
      seq: 1,
      data: { text: "please help", command: "", at: AT },
    });
    socket.receive({
      v: 2,
      generation: GENERATION,
      request_id: "",
      type: "text.done",
      session_id: "s1",
      seq: 2,
      data: { block_id: "b1", text: SKILL_TEXT },
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
  return { ...utils, socket, durable };
}

/** Mounts, proposes and opens the inspector: the shared setup for the tests past "Propose". */
async function openInspector(skillProposals: ReturnType<typeof fakeSkillProposals>) {
  const mounted = await mount({ skillProposals: skillProposals.api });
  await act(async () => {
    fireEvent.click(await screen.findByRole("button", { name: "Propose this skill" }));
  });
  await act(async () => {
    fireEvent.click(await screen.findByRole("button", { name: "Open in Approvals" }));
  });
  const inspector = await screen.findByRole("dialog", { name: SKILL_NAME });
  return { ...mounted, inspector };
}

test("Make this a skill fills this chat's own message box and sends nothing", async () => {
  const skillProposals = fakeSkillProposals(skillItem());
  const { socket, durable } = await mount({ skillProposals: skillProposals.api });
  const make = await screen.findByRole("button", { name: "Make this a skill" });
  await act(async () => {
    fireEvent.click(make);
  });
  expect(box().value).toContain(
    "Make this a skill. Draft a reusable Claude Code skill from the work in this conversation",
  );
  expect(box().value).toContain(
    "Reply with exactly one fenced code block whose info string is SKILL.md, holding the whole file:",
  );
  expect(skillProposals.create).not.toHaveBeenCalled();
  expect(socket.sent.filter((frame) => frame.type === "turn.send")).toEqual([]);
  expect(durable.submissions).toEqual([]);
});

test("Propose this skill posts the parsed block under this chat's id", async () => {
  const skillProposals = fakeSkillProposals(skillItem());
  await mount({ skillProposals: skillProposals.api });
  const propose = await screen.findByRole("button", { name: "Propose this skill" });
  await act(async () => {
    fireEvent.click(propose);
  });
  await waitFor(() => expect(skillProposals.create).toHaveBeenCalledTimes(1));
  expect(skillProposals.create).toHaveBeenCalledWith(WORKSPACE, SKILL_BLOCK, "button", ["s1"]);
});

test("Open in Approvals shows the skill proposal inspector, found by the skill's own name", async () => {
  const skillProposals = fakeSkillProposals(skillItem());
  const { inspector } = await openInspector(skillProposals);
  expect(skillProposals.detail).toHaveBeenCalledWith(SKILL_ID, expect.anything());
  expect(await within(inspector).findByText("handles the test flow end to end")).toBeTruthy();
});

test("approving the proposal opens a NEW editor with the apply text, sending nothing", async () => {
  const skillProposals = fakeSkillProposals(skillItem());
  const { socket, inspector } = await openInspector(skillProposals);
  const approve = await within(inspector).findByRole("button", { name: "Approve" });
  await act(async () => {
    fireEvent.click(approve);
  });
  expect(skillProposals.act).toHaveBeenCalledWith(SKILL_ID, "approve", SKILL_REVISION);
  await waitFor(() =>
    expect(box().value).toContain("Create .claude/skills/my-test-skill/SKILL.md"),
  );
  expect(box().value).toContain(SKILL_BLOCK.trim());
  expect(screen.queryByRole("dialog", { name: SKILL_NAME })).toBeNull();
  expect(socket.sent.filter((frame) => frame.type === "turn.send")).toEqual([]);
});

test("Approve on a skill row in Approvals, after the 5 s undo, calls act approve and opens the hand-off draft", async () => {
  const skillProposals = fakeSkillProposals(skillItem());
  await mount({ skillProposals: skillProposals.api, decisions: fakeDecisions([skillItem()]) });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Open Approvals" }));
  });
  const section = await screen.findByRole("region", { name: "Proposals" });
  const approve = await within(section).findByRole("button", { name: "Approve" });
  vi.useFakeTimers();
  await act(async () => {
    fireEvent.click(approve);
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(4_999);
  });
  expect(skillProposals.act).not.toHaveBeenCalled();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1);
  });
  expect(skillProposals.act).toHaveBeenCalledWith(SKILL_ID, "approve", SKILL_REVISION);
  expect(box().value).toContain("Create .claude/skills/my-test-skill/SKILL.md");
  expect(box().value).toContain(SKILL_BLOCK.trim());
  vi.useRealTimers();
});

test("Open chat reselects that proposal's own chat and closes Approvals", async () => {
  // The proposal's own session (s2) differs from s1, the chat whose reply produced it: s1 stays
  // selected (and the inspector open) until Open chat is clicked, so only a real re-selection
  // moves "sess-selected" off s1 and onto s2 — not just the dialog closing.
  const S2 = sessionRow({ session_id: "s2", title: "Other chat" });
  const skillProposals = fakeSkillProposals(skillItem({ sessions: ["s2"] }), { sessions: ["s2"] });
  const { container } = await mount({ skillProposals: skillProposals.api, sessions: [S1, S2] });
  await act(async () => {
    fireEvent.click(await screen.findByRole("button", { name: "Propose this skill" }));
  });
  await act(async () => {
    fireEvent.click(await screen.findByRole("button", { name: "Open in Approvals" }));
  });
  const inspector = await screen.findByRole("dialog", { name: SKILL_NAME });

  const s1Row = container.querySelector<HTMLElement>('[data-session-id="s1"]');
  if (!s1Row) throw new Error("the s1 row is missing");
  expect(s1Row.classList.contains("sess-selected")).toBe(true);

  await act(async () => {
    fireEvent.click(await within(inspector).findByRole("button", { name: "Open chat" }));
  });

  expect(screen.queryByRole("dialog", { name: SKILL_NAME })).toBeNull();
  const s2Row = container.querySelector<HTMLElement>('[data-session-id="s2"]');
  if (!s2Row) throw new Error("the s2 row is missing");
  expect(s2Row.classList.contains("sess-selected")).toBe(true);
  expect(s1Row.classList.contains("sess-selected")).toBe(false);
});

test("no skill UI renders without the skillProposals prop", async () => {
  const { container } = await mount();
  await waitFor(() => {
    if (!container.querySelector("[data-turn-id]")) throw new Error("turn not rendered yet");
  });
  expect(screen.queryByRole("button", { name: "Make this a skill" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Propose this skill" })).toBeNull();
});

test("a code-folder active workspace shows no skill UI on a finished reply", async () => {
  const skillProposals = fakeSkillProposals(skillItem());
  const { container } = await mount({ skillProposals: skillProposals.api, shell: codeShell() });
  await waitFor(() => {
    if (!container.querySelector("[data-turn-id]")) throw new Error("turn not rendered yet");
  });
  expect(screen.queryByRole("button", { name: "Make this a skill" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Propose this skill" })).toBeNull();
});

test("Make this a skill appends the prompt after the box's own text and keeps the chat selected", async () => {
  const skillProposals = fakeSkillProposals(skillItem());
  const { container, socket, durable } = await mount({ skillProposals: skillProposals.api });
  await act(async () => {
    fireEvent.change(box(), { target: { value: "existing draft" } });
  });
  const make = await screen.findByRole("button", { name: "Make this a skill" });
  await act(async () => {
    fireEvent.click(make);
  });
  expect(box().value.startsWith("existing draft\n\n")).toBe(true);
  expect(box().value).toContain(
    "Make this a skill. Draft a reusable Claude Code skill from the work in this conversation",
  );
  const row = container.querySelector<HTMLElement>('[data-session-id="s1"]');
  if (!row) throw new Error("the s1 row is missing");
  expect(row.classList.contains("sess-selected")).toBe(true);
  expect(skillProposals.create).not.toHaveBeenCalled();
  expect(socket.sent.filter((frame) => frame.type === "turn.send")).toEqual([]);
  expect(durable.submissions).toEqual([]);
});

test("approving leaves the current chat's own draft untouched; the apply text lands in a different editor", async () => {
  const skillProposals = fakeSkillProposals(skillItem());
  const { container } = await mount({ skillProposals: skillProposals.api });
  await act(async () => {
    fireEvent.change(box(), { target: { value: "draft kept for s1" } });
  });
  await act(async () => {
    fireEvent.click(await screen.findByRole("button", { name: "Propose this skill" }));
  });
  await act(async () => {
    fireEvent.click(await screen.findByRole("button", { name: "Open in Approvals" }));
  });
  const inspector = await screen.findByRole("dialog", { name: SKILL_NAME });
  const approve = await within(inspector).findByRole("button", { name: "Approve" });
  await act(async () => {
    fireEvent.click(approve);
  });

  await waitFor(() =>
    expect(box().value).toContain("Create .claude/skills/my-test-skill/SKILL.md"),
  );
  // The visible box now belongs to a freshly minted editor, not to s1's own.
  expect(box().value).not.toContain("draft kept for s1");

  const row = container.querySelector<HTMLElement>('[data-session-id="s1"]');
  if (!row) throw new Error("the s1 row is missing");
  await act(async () => {
    fireEvent.click(row);
  });
  // s1's own draft is exactly as it was left: the approval never touched it.
  expect(box().value).toBe("draft kept for s1");
});

test("Not now keeps the hint gone on the next render", async () => {
  const skillProposals = fakeSkillProposals(skillItem());
  const { socket } = await mount({ skillProposals: skillProposals.api });

  async function plainTurn(seq: number): Promise<void> {
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

  // mount() already left one finished turn (seq 1-3); 7 more reach LONG_TURNS (8), so the hint
  // fires on this, the newest, turn.
  for (let seq = 4; seq <= 22; seq += 3) await plainTurn(seq);

  const notNow = await screen.findByRole("button", { name: "Not now" });
  await act(async () => {
    fireEvent.click(notNow);
  });
  expect(screen.queryByRole("note")).toBeNull();

  // One more turn is a fresh render under a brand new turn id; the dismissal must still hold
  // because the hint is quieted per session, not per turn.
  await plainTurn(25);
  expect(screen.queryByRole("note")).toBeNull();
  expect(screen.queryByRole("button", { name: "Not now" })).toBeNull();
});
