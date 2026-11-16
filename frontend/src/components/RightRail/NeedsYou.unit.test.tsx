/**
 * NeedsYou: a row per kind (native/proposal), each wired to its own callback; and the
 * section hidden entirely when there is nothing to show.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { type ReactNode, useMemo } from "react";
import { afterEach, expect, test, vi } from "vitest";
import type { RunsApi } from "../../clients/conversations/runsApi";
import type { NativeDecision } from "../../state/decisions";
import type {
  ChangeProposalItem,
  QuestionProposalItem,
  VaultProposalItem,
} from "../../state/decisionsApi";
import type { NeedRow, NeedsYou as NeedsYouData, ProposalNeed } from "../../state/needsYou";
import { runKey } from "../../state/runGroups";
import type { SkillProposalItem } from "../../state/skillProposalsApi";
import { readCss, ruleIn } from "../../testing/css";
import { NeedsYou } from "./NeedsYou";
import { RequestActions } from "./RequestActions";
import { type RunStops, STOP_CONFIRM_MS, useRunStops } from "./RunStop";
import { ANSWER_SETTLE_MS } from "./useAnswerHold";

/** Set by `blurOnDisable` for the one test that asks for it. */
let disabledWatcher: MutationObserver | null = null;

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  disabledWatcher?.disconnect();
  disabledWatcher = null;
});

type NativeRow = Extract<NeedRow, { kind: "native" }>;
type ProposalRow = Extract<NeedRow, { kind: "proposal" }>;

function nativeItem(overrides: Partial<NativeDecision> = {}): NativeDecision {
  return {
    kind: "permission",
    id: "s1:tu-1",
    origin: { workspace_id: null, session_id: "s1", turn_id: "t1" },
    revision: "rev-1",
    provider: "Claude Code",
    actor: "VIKI",
    action: "Write",
    resource: "30-knowledge/x.md",
    inline: true,
    card: {
      kind: "permission",
      data: {
        tool_use_id: "tu-1",
        kind: "tool",
        tool: "Write",
        summary: "30-knowledge/x.md",
        question: "",
        reason: "",
        agent: "",
        plan: "",
      },
      nativeRequest: { runtime_id: "r1", request_id: "1", turn_id: "t1" },
    },
    ...overrides,
  };
}

function nativeRow(
  itemOverrides: Partial<NativeDecision> = {},
  agent: string | null = null,
  blockId: string | null = null,
): NativeRow {
  const item = nativeItem(itemOverrides);
  return { kind: "native", key: `native:${item.id}`, item, agent, blockId };
}

function vaultProposal(overrides: Partial<VaultProposalItem> = {}): VaultProposalItem {
  return {
    kind: "vault_proposal",
    id: "p-1",
    origin: { workspace_id: "ws-1", session_id: null },
    revision: "a".repeat(64),
    state: "open",
    attention: true,
    title: "R-1 — item",
    ref: "30-knowledge/r-1.md",
    origin_kind: "note_edit",
    base_revision: "a".repeat(64),
    text_bytes: 5,
    handed_at: null,
    created_at: "2026-10-02T03:04:05.678Z",
    ...overrides,
  };
}

function skillProposal(overrides: Partial<SkillProposalItem> = {}): SkillProposalItem {
  return {
    kind: "skill_proposal",
    id: "sk-1",
    origin: { workspace_id: "ws-1", session_id: null },
    revision: "a".repeat(64),
    state: "open",
    attention: true,
    title: "my-skill",
    name: "my-skill",
    relative_id: "skills/my-skill/SKILL.md",
    trigger: "button",
    sessions: ["11111111-1111-4111-8111-111111111111"],
    base_revision: null,
    text_bytes: 42,
    approved_at: null,
    rejected_at: null,
    reason: null,
    created_at: "2026-10-02T03:04:05.678Z",
    updated_at: "2026-10-02T03:04:05.678Z",
    ...overrides,
  };
}

function changeProposal(overrides: Partial<ChangeProposalItem> = {}): ChangeProposalItem {
  return {
    kind: "change_proposal",
    id: "c-1",
    origin: { workspace_id: "ws-1", session_id: null },
    revision: "a".repeat(64),
    state: "open",
    attention: true,
    title: "Link proposal",
    change: "note_link",
    ref: "30-knowledge/a.md",
    created_at: "2026-10-02T03:04:05.678Z",
    updated_at: "2026-10-02T03:04:05.678Z",
    approved_at: null,
    rejected_at: null,
    reason: null,
    ...overrides,
  };
}

function questionProposal(overrides: Partial<QuestionProposalItem> = {}): QuestionProposalItem {
  return {
    kind: "question_proposal",
    id: "qp-1",
    origin: { workspace_id: "ws-1", session_id: null },
    revision: "a".repeat(64),
    state: "open",
    attention: true,
    title: "Question change: Worth a note?",
    name: "worth_note",
    created_at: "2026-10-02T03:04:05.678Z",
    updated_at: "2026-10-02T03:04:05.678Z",
    approved_at: null,
    rejected_at: null,
    reason: null,
    ...overrides,
  };
}

function proposalRow(item: ProposalNeed): ProposalRow {
  return { kind: "proposal", key: `proposal:${item.kind}:${item.id}`, item };
}

function needsOf(rows: NeedRow[]): NeedsYouData {
  return { rows, count: rows.length };
}

test("renders nothing when count is 0", () => {
  const { container } = render(
    <NeedsYou
      needs={needsOf([])}
      selectedId={null}
      labelOf={() => ""}
      onOpenNative={vi.fn()}
      onOpenProposal={vi.fn()}
    />,
  );
  expect(container.firstChild).toBeNull();
});

test("a native tool row titles '<action> <resource>' and metas 'this chat · waiting for approval'", () => {
  const row = nativeRow({ inline: true, action: "Write", resource: "30-knowledge/x.md" });
  const { container } = render(
    <NeedsYou
      needs={needsOf([row])}
      selectedId="s1"
      labelOf={() => "Other"}
      onOpenNative={vi.fn()}
      onOpenProposal={vi.fn()}
    />,
  );
  const button = container.querySelector('[data-need="native"]') as HTMLElement;
  expect(button.querySelector(".need-title")?.textContent).toBe("Write 30-knowledge/x.md");
  expect(button.querySelector(".need-meta")?.textContent).toBe("this chat · waiting for approval");
});

test("a native plan row titles 'Plan · <resource>' and metas 'plan to approve'", () => {
  const row = nativeRow({ inline: false, resource: "Ship the rail section" });
  const { container } = render(
    <NeedsYou
      needs={needsOf([row])}
      selectedId="s1"
      labelOf={() => "Other"}
      onOpenNative={vi.fn()}
      onOpenProposal={vi.fn()}
    />,
  );
  const button = container.querySelector('[data-need="native"]') as HTMLElement;
  expect(button.querySelector(".need-title")?.textContent).toBe("Plan · Ship the rail section");
  expect(button.querySelector(".need-meta")?.textContent).toBe("this chat · plan to approve");
});

test("a native question row titles its resource alone and metas 'question'", () => {
  const row = nativeRow({ kind: "question", inline: false, resource: "Which folder?" });
  const { container } = render(
    <NeedsYou
      needs={needsOf([row])}
      selectedId="s1"
      labelOf={() => "Other"}
      onOpenNative={vi.fn()}
      onOpenProposal={vi.fn()}
    />,
  );
  const button = container.querySelector('[data-need="native"]') as HTMLElement;
  expect(button.querySelector(".need-title")?.textContent).toBe("Which folder?");
  expect(button.querySelector(".need-meta")?.textContent).toBe("this chat · question");
});

test.each([
  ["tool", { inline: true, action: "Bash" }, "Bash "],
  ["plan", { inline: false }, "Plan · "],
  ["question", { kind: "question" as const, inline: false }, ""],
])(
  "a native %s row's title writes hidden characters as codes and draws line breaks",
  (_kind, overrides, lead) => {
    // Painted raw, the right-to-left override would show the text after it reversed, and the one
    // clipped line would show the line break as a space.
    const resource = `echo ok${String.fromCodePoint(0x202e)}\nrm -rf ./build`;
    const { container } = render(
      <NeedsYou
        needs={needsOf([nativeRow({ ...overrides, resource })])}
        selectedId="s1"
        labelOf={() => "Other"}
        onOpenNative={vi.fn()}
        onOpenProposal={vi.fn()}
      />,
    );
    expect(container.querySelector(".need-title")?.textContent).toBe(
      `${lead}echo ok<U+202E> ↵ rm -rf ./build`,
    );
  },
);

test("a native row not in the selected session names its chat via labelOf, with the agent appended", () => {
  const row = nativeRow({}, "researcher");
  const { container } = render(
    <NeedsYou
      needs={needsOf([row])}
      selectedId="other-session"
      labelOf={(id) => `Chat ${id}`}
      onOpenNative={vi.fn()}
      onOpenProposal={vi.fn()}
    />,
  );
  expect(container.querySelector(".need-meta")?.textContent).toBe(
    "Chat s1 · waiting for approval · researcher",
  );
});

test("clicking a native row calls onOpenNative with its row", () => {
  const row = nativeRow();
  const onOpenNative = vi.fn();
  const { container } = render(
    <NeedsYou
      needs={needsOf([row])}
      selectedId={null}
      labelOf={() => ""}
      onOpenNative={onOpenNative}
      onOpenProposal={vi.fn()}
    />,
  );
  fireEvent.click(container.querySelector('[data-need="native"]') as HTMLElement);
  expect(onOpenNative).toHaveBeenCalledExactlyOnceWith(row);
});

test("a proposal row shows its title and '<source> · <chip text>', and opens on click", () => {
  const item = vaultProposal({
    title: "R-1 — item",
    ref: "30-knowledge/r-1.md",
    state: "open",
  });
  const row = proposalRow(item);
  const onOpenProposal = vi.fn();
  const { container } = render(
    <NeedsYou
      needs={needsOf([row])}
      selectedId={null}
      labelOf={() => ""}
      onOpenNative={vi.fn()}
      onOpenProposal={onOpenProposal}
    />,
  );
  const button = container.querySelector('[data-need="proposal"]') as HTMLElement;
  expect(button.querySelector(".need-title")?.textContent).toBe("R-1 — item");
  expect(button.querySelector(".need-meta")?.textContent).toBe("30-knowledge/r-1.md · Proposed");
  fireEvent.click(button);
  expect(onOpenProposal).toHaveBeenCalledExactlyOnceWith(item);
});

// Pins sourceOf's skill branch to relative_id: name and relative_id never collide (relative_id
// always wraps name in "skills/.../SKILL.md"), so a regression swapping one for the other shows up.
test("a skill proposal row metas its relative id, not its name, · chip text", () => {
  const item = skillProposal({ name: "my-skill", relative_id: "skills/my-skill/SKILL.md" });
  const row = proposalRow(item);
  const { container } = render(
    <NeedsYou
      needs={needsOf([row])}
      selectedId={null}
      labelOf={() => ""}
      onOpenNative={vi.fn()}
      onOpenProposal={vi.fn()}
    />,
  );
  const button = container.querySelector('[data-need="proposal"]') as HTMLElement;
  expect(button.querySelector(".need-meta")?.textContent).toBe(
    "skills/my-skill/SKILL.md · Proposed",
  );
});

// Pins sourceOf's question branch to the literal "Decision", the one kind with no file of its own.
test("a question proposal row metas 'Decision', not a file path, · chip text", () => {
  const item = questionProposal();
  const row = proposalRow(item);
  const { container } = render(
    <NeedsYou
      needs={needsOf([row])}
      selectedId={null}
      labelOf={() => ""}
      onOpenNative={vi.fn()}
      onOpenProposal={vi.fn()}
    />,
  );
  const button = container.querySelector('[data-need="proposal"]') as HTMLElement;
  expect(button.querySelector(".need-title")?.textContent).toBe("Question change: Worth a note?");
  expect(button.querySelector(".need-meta")?.textContent).toBe("Decision · Being tried");
});

// Pins sourceOf's change branch to ref (the same fallback a vault proposal takes) and chipWord
// to the fixed "Vault change" word, not the state-based i18n label a vault/skill proposal's
// "open" gets ("Proposed") — Approvals, not Needs you, shows the change's own state word.
test("shows a change proposal row's title and '<ref> · Vault change', and opens on click", () => {
  const item = changeProposal({
    title: "Link proposal",
    ref: "30-knowledge/a.md",
    state: "open",
  });
  const row = proposalRow(item);
  const onOpenProposal = vi.fn();
  const { container } = render(
    <NeedsYou
      needs={needsOf([row])}
      selectedId={null}
      labelOf={() => ""}
      onOpenNative={vi.fn()}
      onOpenProposal={onOpenProposal}
    />,
  );
  const button = container.querySelector('[data-need="proposal"]') as HTMLElement;
  expect(button.querySelector(".need-title")?.textContent).toBe("Link proposal");
  expect(button.querySelector(".need-meta")?.textContent).toBe("30-knowledge/a.md · Vault change");
  fireEvent.click(button);
  expect(onOpenProposal).toHaveBeenCalledExactlyOnceWith(item);
});

test("the count badge shows only when count > 0, and 'Open Approvals' only when the handler is passed", () => {
  const row = nativeRow();
  const { container, rerender } = render(
    <NeedsYou
      needs={needsOf([row])}
      selectedId={null}
      labelOf={() => ""}
      onOpenNative={vi.fn()}
      onOpenProposal={vi.fn()}
    />,
  );
  expect(container.querySelector(".rail-section-count")?.textContent).toBe("1");
  expect(screen.queryByText("Open Approvals")).toBeNull();

  const onOpenApprovals = vi.fn();
  rerender(
    <NeedsYou
      needs={needsOf([row])}
      selectedId={null}
      labelOf={() => ""}
      onOpenApprovals={onOpenApprovals}
      onOpenNative={vi.fn()}
      onOpenProposal={vi.fn()}
    />,
  );
  fireEvent.click(screen.getByText("Open Approvals"));
  expect(onOpenApprovals).toHaveBeenCalledOnce();
});

/** Wires NeedsYou to the real `useRunStops`, the same pairing App.tsx makes: a click can
 * drive a real ask -> confirm -> send transition, not just a static phase. */
function NeedsYouWithStops({
  row,
  runs,
  waiting,
  nowMs,
}: {
  row: NativeRow;
  runs: RunsApi;
  waiting?: (row: NativeRow, now: number) => string | null;
  nowMs?: number;
}) {
  const key = runKey(row.item.origin.session_id, row.blockId ?? "");
  const keys = useMemo(() => new Set([key]), [key]);
  const stops = useRunStops(runs, keys);
  return (
    <NeedsYou
      needs={needsOf([row])}
      selectedId="s1"
      labelOf={() => ""}
      onOpenNative={vi.fn()}
      onOpenProposal={vi.fn()}
      stops={stops}
      {...(waiting ? { waiting } : {})}
      {...(nowMs !== undefined ? { nowMs } : {})}
    />
  );
}

// --- actions, waiting and Stop ---------------------------------------------------------------

test("actions wraps the row in div.need-native alongside the unchanged button.need", () => {
  const row = nativeRow();
  const { container } = render(
    <NeedsYou
      needs={needsOf([row])}
      selectedId={null}
      labelOf={() => ""}
      onOpenNative={vi.fn()}
      onOpenProposal={vi.fn()}
      actions={() => <button type="button">Fake action</button>}
    />,
  );
  const wrap = container.querySelector(".need-native") as HTMLElement;
  expect(wrap).not.toBeNull();
  expect(wrap.querySelector('[data-need="native"]')).not.toBeNull();
  expect(screen.getByText("Fake action")).toBeTruthy();
});

test("without actions or stops, a native row's markup is unchanged (no .need-native)", () => {
  const row = nativeRow();
  const { container } = render(
    <NeedsYou
      needs={needsOf([row])}
      selectedId={null}
      labelOf={() => ""}
      onOpenNative={vi.fn()}
      onOpenProposal={vi.fn()}
    />,
  );
  expect(container.querySelector(".need-native")).toBeNull();
  expect(container.querySelector('[data-need="native"]')).not.toBeNull();
});

test("the native urgency band names the region and keeps the count contract", () => {
  const { container } = render(
    <NeedsYou
      needs={needsOf([nativeRow(), proposalRow(vaultProposal())])}
      selectedId={null}
      labelOf={() => ""}
      onOpenApprovals={vi.fn()}
      onOpenNative={vi.fn()}
      onOpenProposal={vi.fn()}
    />,
  );
  expect(screen.getByRole("region", { name: "Needs you, 2 waiting" })).toBeTruthy();
  expect(container.querySelector('[data-icon="shield"]')).not.toBeNull();
  expect(
    container.querySelector(".needs-you-band--urgent .rail-section-count.attention-count")
      ?.textContent,
  ).toBe("2");
  expect(container.querySelector('[data-need-group="native"]')).not.toBeNull();
  expect(container.querySelector('[data-need-group="proposal"] .lbl')?.textContent).toBe(
    "Proposals",
  );
});

test("a proposal-only band stays subdued", () => {
  const { container } = render(
    <NeedsYou
      needs={needsOf([proposalRow(vaultProposal())])}
      selectedId={null}
      labelOf={() => ""}
      onOpenNative={vi.fn()}
      onOpenProposal={vi.fn()}
    />,
  );
  expect(
    container.querySelector(".needs-you-band--subdued .rail-section-count--dim")?.textContent,
  ).toBe("1");
  expect(container.querySelector(".attention-count")).toBeNull();
});

test("only later count rises mount a fresh two-ring pulse", () => {
  const rows = [nativeRow(), nativeRow({ id: "s1:tu-2" }), nativeRow({ id: "s1:tu-3" })];
  const props = {
    selectedId: null,
    labelOf: () => "",
    onOpenNative: vi.fn(),
    onOpenProposal: vi.fn(),
  };
  const { container, rerender } = render(<NeedsYou {...props} needs={needsOf(rows.slice(0, 1))} />);
  expect(container.querySelector(".needs-you-pulse")).toBeNull();

  rerender(<NeedsYou {...props} needs={needsOf(rows.slice(0, 2))} />);
  const firstPulse = container.querySelector(".needs-you-pulse");
  expect(firstPulse).not.toBeNull();

  rerender(<NeedsYou {...props} needs={needsOf(rows)} />);
  expect(container.querySelector(".needs-you-pulse")).not.toBe(firstPulse);
});

test("removing the last native row moves focus to the first proposal row", () => {
  const native = nativeRow();
  const proposal = proposalRow(vaultProposal());
  const { container, rerender } = render(
    <NeedsYou
      needs={needsOf([native, proposal])}
      selectedId={null}
      labelOf={() => ""}
      onOpenApprovals={vi.fn()}
      onOpenNative={vi.fn()}
      onOpenProposal={vi.fn()}
    />,
  );
  const button = container.querySelector('[data-need="native"]') as HTMLElement;
  button.focus();
  rerender(
    <NeedsYou
      needs={needsOf([proposal])}
      selectedId={null}
      labelOf={() => ""}
      onOpenApprovals={vi.fn()}
      onOpenNative={vi.fn()}
      onOpenProposal={vi.fn()}
    />,
  );
  expect(document.activeElement).toBe(container.querySelector('[data-need="proposal"]'));
});

test("an Open-only native row gets focus rescue when removed", () => {
  const native = nativeRow();
  const proposal = proposalRow(vaultProposal());
  const props = {
    selectedId: null,
    labelOf: () => "",
    onOpenApprovals: vi.fn(),
    onOpenNative: vi.fn(),
    onOpenProposal: vi.fn(),
  };
  const { container, rerender } = render(
    <NeedsYou {...props} needs={needsOf([native, proposal])} />,
  );
  const button = container.querySelector('[data-need="native"]') as HTMLElement;
  button.focus();
  rerender(<NeedsYou {...props} needs={needsOf([proposal])} />);
  expect(document.activeElement).toBe(container.querySelector('[data-need="proposal"]'));
});

test("removing a proposal row moves focus to the previous native row", () => {
  const native = nativeRow();
  const proposal = proposalRow(vaultProposal());
  const props = {
    selectedId: null,
    labelOf: () => "",
    onOpenApprovals: vi.fn(),
    onOpenNative: vi.fn(),
    onOpenProposal: vi.fn(),
  };
  const { container, rerender } = render(
    <NeedsYou {...props} needs={needsOf([native, proposal])} />,
  );
  const button = container.querySelector('[data-need="proposal"]') as HTMLElement;
  button.focus();
  rerender(<NeedsYou {...props} needs={needsOf([native])} />);
  expect(document.activeElement).toBe(container.querySelector('[data-need="native"]'));
});

test("waiting appends ' · <text>' to .need-meta; null adds no suffix", () => {
  const row = nativeRow({ inline: true, action: "Write", resource: "30-knowledge/x.md" });
  const { container, rerender } = render(
    <NeedsYou
      needs={needsOf([row])}
      selectedId="s1"
      labelOf={() => ""}
      onOpenNative={vi.fn()}
      onOpenProposal={vi.fn()}
      waiting={() => "waiting 1m 20s"}
    />,
  );
  expect(container.querySelector(".need-meta")?.textContent).toBe(
    "this chat · waiting for approval · waiting 1m 20s",
  );
  rerender(
    <NeedsYou
      needs={needsOf([row])}
      selectedId="s1"
      labelOf={() => ""}
      onOpenNative={vi.fn()}
      onOpenProposal={vi.fn()}
      waiting={() => null}
    />,
  );
  expect(container.querySelector(".need-meta")?.textContent).toBe(
    "this chat · waiting for approval",
  );
});

test("stops given and the row names a run: Stop renders keyed by session and block, and Stop run reaches runs.stop with them", () => {
  const row = nativeRow({}, "researcher", "r1");
  const stop = vi.fn(async () => {});
  const runs: RunsApi = { requests: vi.fn(async () => []), stop };
  render(<NeedsYouWithStops row={row} runs={runs} />);
  fireEvent.click(screen.getByRole("button", { name: "Stop researcher in this chat" }));
  fireEvent.click(screen.getByRole("button", { name: "Stop run" }));
  expect(stop).toHaveBeenCalledWith("s1", "r1");
});

test("an unattributed row (blockId null) gets no Stop even when stops is given", () => {
  const row = nativeRow({}, "researcher", null);
  const stops: RunStops = { phase: () => undefined, ask: vi.fn(), cancel: vi.fn(), send: vi.fn() };
  const { container } = render(
    <NeedsYou
      needs={needsOf([row])}
      selectedId="s1"
      labelOf={() => ""}
      onOpenNative={vi.fn()}
      onOpenProposal={vi.fn()}
      stops={stops}
    />,
  );
  expect(container.querySelector(".need-native")).toBeNull();
  expect(screen.queryByRole("button", { name: "Stop researcher in this chat" })).toBeNull();
});

// --- ticking, and focus surviving an inline answer --------------------------------------------

test("ticks once a second: the meta's waiting text advances, and a stop sent from Needs you goes stale after 30 s with no new frame", async () => {
  vi.useFakeTimers();
  try {
    const since = Date.now();
    const row = nativeRow({}, "researcher", "r1");
    const stop = vi.fn(async () => {});
    const runs: RunsApi = { requests: vi.fn(async () => []), stop };
    const { container } = render(
      <NeedsYouWithStops
        row={row}
        runs={runs}
        nowMs={since}
        waiting={(_row, now) => `waiting ${Math.floor((now - since) / 1000)}s`}
      />,
    );
    const meta = () => container.querySelector(".need-meta")?.textContent;
    expect(meta()).toContain("waiting 0s");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(meta()).toContain("waiting 5s");

    fireEvent.click(screen.getByRole("button", { name: "Stop researcher in this chat" }));
    fireEvent.click(screen.getByRole("button", { name: "Stop run" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByRole("status").textContent).toBe("Stopping…");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(STOP_CONFIRM_MS);
    });
    expect(screen.getByText("Stop sent; Claude Code has not confirmed it yet.")).toBeTruthy();
  } finally {
    vi.useRealTimers();
  }
});

/** What a browser does and jsdom does not: a focused button that becomes `disabled` loses focus
 * to the page. For the rest of the test that calls this, the test sees what the browser would if
 * a button that had just answered were disabled. */
function blurOnDisable(): void {
  disabledWatcher = new MutationObserver((records) => {
    for (const { target } of records) {
      if (
        !(target instanceof HTMLButtonElement) ||
        !target.disabled ||
        target !== document.activeElement
      )
        continue;
      // jsdom's own `blur()` does nothing on a disabled element, so focus leaves by way of a
      // throwaway button that is then removed.
      const sink = document.createElement("button");
      document.body.append(sink);
      sink.focus();
      sink.remove();
    }
  });
  disabledWatcher.observe(document.body, { attributeFilter: ["disabled"], subtree: true });
}

test("after an applied inline answer removes its row, focus moves to the next row's own open button", async () => {
  blurOnDisable();
  const row1 = nativeRow({}, "researcher", "r1");
  const row2 = nativeRow({ id: "s1:tu-2" }, "writer", "r2");
  const onDecide = vi.fn(async () => "applied" as const);
  const { container, rerender } = render(
    <NeedsYou
      needs={needsOf([row1, row2])}
      selectedId="s1"
      labelOf={() => ""}
      onOpenNative={vi.fn()}
      onOpenProposal={vi.fn()}
      actions={(row) =>
        row === row1 ? (
          <RequestActions
            request={{
              tool_use_id: "tu-1",
              kind: "tool",
              tool: "Write",
              summary: "30-knowledge/x.md",
              question: "",
              reason: "",
              agent: "researcher",
              plan: "",
            }}
            waitingSince={null}
            nowMs={0}
            onDecide={onDecide}
          />
        ) : null
      }
    />,
  );
  const allow = screen.getByRole("button", { name: "Allow once: Write 30-knowledge/x.md" });
  allow.focus();
  await act(async () => {
    fireEvent.click(allow);
    await Promise.resolve();
    await Promise.resolve();
  });
  // The answered button still holds focus, as it does in a browser only because it is marked
  // `aria-disabled` and never `disabled` (`blurOnDisable` above stands for the browser's rule).
  expect(document.activeElement).toBe(allow);
  expect(allow.hasAttribute("disabled")).toBe(false);
  expect(allow.getAttribute("aria-disabled")).toBe("true");

  // request.answered (applied): the whole row leaves, same as App dropping it from `needs`.
  rerender(
    <NeedsYou
      needs={needsOf([row2])}
      selectedId="s1"
      labelOf={() => ""}
      onOpenNative={vi.fn()}
      onOpenProposal={vi.fn()}
    />,
  );
  expect(document.activeElement).toBe(container.querySelector('[data-need="native"]'));
});

test("a row withdrawn while focus is on its Allow once never hands focus to another row's answer or Stop", async () => {
  const runs: RunsApi = { requests: vi.fn(async () => []), stop: vi.fn(async () => {}) };
  const rows = [nativeRow({}, "researcher", "r1"), nativeRow({ id: "s1:tu-2" }, "writer", "r2")];
  function Rows({ shown }: { shown: NativeRow[] }) {
    const keys = useMemo(() => new Set(rows.map((row) => runKey("s1", row.blockId ?? ""))), []);
    const stops = useRunStops(runs, keys);
    return (
      <NeedsYou
        needs={needsOf(shown)}
        selectedId={null}
        labelOf={() => "Other chat"}
        onOpenNative={vi.fn()}
        onOpenProposal={vi.fn()}
        stops={stops}
        actions={(row) => (
          <RequestActions
            request={toolRequest(row.item.id)}
            waitingSince={null}
            nowMs={0}
            onDecide={vi.fn()}
          />
        )}
      />
    );
  }
  const { container, rerender } = render(<Rows shown={rows} />);
  const openers = () => [...container.querySelectorAll<HTMLElement>("button.need")];

  // The last row goes: the row before it is the only one left. Its last controls are Deny and
  // Stop, which act at one key press; its first is the button that only opens it.
  screen.getByRole("button", { name: "Allow once: Bash s1:tu-2" }).focus();
  rerender(<Rows shown={rows.slice(0, 1)} />);
  expect(openers()).toHaveLength(1);
  expect(document.activeElement).toBe(openers()[0]);

  // The first row goes: the row after it takes focus the same way.
  rerender(<Rows shown={rows} />);
  screen.getByRole("button", { name: "Allow once: Bash s1:tu-1" }).focus();
  rerender(<Rows shown={rows.slice(1)} />);
  expect(openers()).toHaveLength(1);
  expect(document.activeElement).toBe(openers()[0]);
  expect(document.activeElement?.closest(".request-actions, .run-stop")).toBeNull();
});

// --- focus surviving the section's own last row ----------------------------------------------

test("the only row leaves and names a run: focus lands on the run's own row in Working", async () => {
  const row = nativeRow({}, "researcher", "r1");
  const onDecide = vi.fn(async () => "applied" as const);
  const { container, rerender } = render(
    <>
      <div data-run-key="s1:r1">
        <button type="button">Jump</button>
      </div>
      <NeedsYou
        needs={needsOf([row])}
        selectedId="s1"
        labelOf={() => ""}
        onOpenNative={vi.fn()}
        onOpenProposal={vi.fn()}
        actions={(candidate) =>
          candidate === row ? (
            <RequestActions
              request={{
                tool_use_id: "tu-1",
                kind: "tool",
                tool: "Write",
                summary: "30-knowledge/x.md",
                question: "",
                reason: "",
                agent: "researcher",
                plan: "",
              }}
              waitingSince={null}
              nowMs={0}
              onDecide={onDecide}
            />
          ) : null
        }
      />
    </>,
  );
  const allow = screen.getByRole("button", { name: "Allow once: Write 30-knowledge/x.md" });
  allow.focus();
  await act(async () => {
    fireEvent.click(allow);
    await Promise.resolve();
    await Promise.resolve();
  });

  // request.answered (applied): the row was Needs you's only one, so the whole section — Open
  // Approvals included — unmounts with it (unlike the sibling-row case above).
  rerender(
    <>
      <div data-run-key="s1:r1">
        <button type="button">Jump</button>
      </div>
      <NeedsYou
        needs={needsOf([])}
        selectedId="s1"
        labelOf={() => ""}
        onOpenNative={vi.fn()}
        onOpenProposal={vi.fn()}
      />
    </>,
  );
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(document.activeElement).toBe(container.querySelector('[data-run-key="s1:r1"] button'));
});

test("the only row leaves and names no run: focus falls back to the rail's first focusable control", async () => {
  const row = nativeRow({}, "researcher", null);
  const onDecide = vi.fn(async () => "applied" as const);
  const { rerender } = render(
    <aside data-region="right-rail">
      <button type="button">Other control</button>
      <NeedsYou
        needs={needsOf([row])}
        selectedId="s1"
        labelOf={() => ""}
        onOpenNative={vi.fn()}
        onOpenProposal={vi.fn()}
        actions={(candidate) =>
          candidate === row ? (
            <RequestActions
              request={{
                tool_use_id: "tu-1",
                kind: "tool",
                tool: "Write",
                summary: "30-knowledge/x.md",
                question: "",
                reason: "",
                agent: "researcher",
                plan: "",
              }}
              waitingSince={null}
              nowMs={0}
              onDecide={onDecide}
            />
          ) : null
        }
      />
    </aside>,
  );
  const allow = screen.getByRole("button", { name: "Allow once: Write 30-knowledge/x.md" });
  allow.focus();
  await act(async () => {
    fireEvent.click(allow);
    await Promise.resolve();
    await Promise.resolve();
  });

  rerender(
    <aside data-region="right-rail">
      <button type="button">Other control</button>
      <NeedsYou
        needs={needsOf([])}
        selectedId="s1"
        labelOf={() => ""}
        onOpenNative={vi.fn()}
        onOpenProposal={vi.fn()}
      />
    </aside>,
  );
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Other control" }));
});

// --- the command an inline answer authorises, and which request a click reaches ---------------

test("the inline command wraps, keeps its line breaks and scrolls instead of being clipped, wherever it is mounted", () => {
  // The rule is RequestActions' own, not one scoped to a Needs you row: Peek's list of requests
  // shows the same headline and must paint a second line as a second line too.
  const head = ruleIn(readCss(import.meta.url, "RequestActions.css"), ".request-actions-head");
  expect(head).toMatch(/max-height:\s*\d+px/);
  expect(head).toMatch(/overflow-y:\s*auto/);
  expect(head).toMatch(/white-space:\s*pre-wrap/);
  expect(head).toMatch(/overflow-wrap:\s*anywhere/);
  expect(head).not.toMatch(/nowrap|text-overflow|overflow:\s*hidden/);
  // Nothing here narrows it again for one caller.
  expect(readCss(import.meta.url, "NeedsYou.css")).not.toContain("request-actions-head");
});

test("held buttons look held: the dimming rule travels with the buttons, not with this list", () => {
  const dimmed = ruleIn(
    readCss(import.meta.url, "RequestActions.css"),
    ".request-actions-buttons[data-settling] .btn",
  );
  expect(dimmed).toMatch(/opacity:\s*0\.5/);
  expect(readCss(import.meta.url, "NeedsYou.css")).not.toContain("data-settling");
});

function toolRequest(toolUseId: string) {
  return {
    tool_use_id: toolUseId,
    kind: "tool" as const,
    tool: "Bash",
    summary: toolUseId,
    question: "",
    reason: "",
    agent: "",
    plan: "",
  };
}

/** Off-screen tool rows, each answered in place through its own `onDecide`, above whatever
 * proposal rows and footer the test adds. */
function answerable(
  rows: NeedRow[],
  decided: (toolUseId: string) => void,
  footer?: ReactNode,
  nowMs = 0,
) {
  return (
    <NeedsYou
      needs={needsOf(rows)}
      selectedId={null}
      labelOf={() => "Other chat"}
      onOpenNative={vi.fn()}
      onOpenProposal={vi.fn()}
      footer={footer}
      nowMs={nowMs}
      actions={(row) => {
        const toolUseId = row.item.card.data.tool_use_id;
        return (
          <RequestActions
            request={toolRequest(toolUseId)}
            waitingSince={null}
            nowMs={0}
            onDecide={async () => {
              decided(toolUseId);
              return "applied" as const;
            }}
          />
        );
      }}
    />
  );
}

function rowFor(toolUseId: string): NativeRow {
  const base = nativeItem();
  return nativeRow({
    id: `s2:${toolUseId}`,
    origin: { workspace_id: null, session_id: "s2", turn_id: "t1" },
    card: { ...base.card, data: toolRequest(toolUseId) } as NativeDecision["card"],
  });
}

async function settle(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ANSWER_SETTLE_MS);
  });
}

/** A pointer click: `detail` counts the clicks, and is 0 only for a keyboard activation. */
async function pointerClick(name: string, detail = 1): Promise<void> {
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name }), { detail });
  });
}

/**
 * Stands for the browser's layout, which jsdom does not have (every box sits at 0,0 there): the
 * list's rows and its footer are stacked a hundred pixels each. `end` says which edge the list
 * is scrolled to. At "top", a box sits below everything before it, so only a change above it
 * moves it. At "bottom" the list has been scrolled to its end: its last box sits at the visible
 * edge, so a box is placed by what comes after it, and a row or footer leaving below pulls every
 * box above down, as the browser does when a scrolled list gets shorter.
 */
function stackRows(end: "top" | "bottom" = "top"): void {
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (
    this: HTMLElement,
  ) {
    const side =
      end === "top" ? Node.DOCUMENT_POSITION_FOLLOWING : Node.DOCUMENT_POSITION_PRECEDING;
    const between = [...document.querySelectorAll("[data-need-row], .needs-you-footer")].filter(
      (block) => !block.contains(this) && (block.compareDocumentPosition(this) & side) !== 0,
    );
    return new DOMRect(0, (end === "top" ? 100 : -100) * between.length, 200, 24);
  });
}

const settling = (container: HTMLElement): number =>
  container.querySelectorAll(".request-actions-buttons[data-settling]").length;

test("a row leaving holds the next row's Allow once against the second click of a double-click", async () => {
  vi.useFakeTimers();
  try {
    stackRows();
    const decided: string[] = [];
    const [first, second] = [rowFor("tu-a"), rowFor("tu-b")];
    const { container, rerender } = render(answerable([first, second], (id) => decided.push(id)));
    await settle();
    expect(settling(container)).toBe(0);

    await pointerClick("Allow once: Bash tu-a");
    expect(decided).toEqual(["tu-a"]);

    // request.answered: the first row leaves and the second moves up under the pointer.
    rerender(answerable([second], (id) => decided.push(id)));
    expect(settling(container)).toBe(1);
    await pointerClick("Allow once: Bash tu-b", 2);
    expect(decided).toEqual(["tu-a"]);

    await settle();
    expect(settling(container)).toBe(0);
    await pointerClick("Allow once: Bash tu-b");
    expect(decided).toEqual(["tu-a", "tu-b"]);
  } finally {
    vi.useRealTimers();
  }
});

test("a row arriving, the section's first appearance included, holds pointer clicks but not the keyboard", async () => {
  vi.useFakeTimers();
  try {
    stackRows();
    const decided: string[] = [];
    const [first, second] = [rowFor("tu-a"), rowFor("tu-b")];
    const { rerender } = render(answerable([second], (id) => decided.push(id)));
    await pointerClick("Deny: Bash tu-b");
    expect(decided).toEqual([]);
    await settle();

    // A request from an earlier-tracked chat is inserted above and pushes the row down.
    rerender(answerable([first, second], (id) => decided.push(id)));
    await pointerClick("Allow once: Bash tu-a");
    await pointerClick("Allow once: Bash tu-b");
    expect(decided).toEqual([]);

    // Enter or Space on a focused button: focus stays with its button wherever the rows go, and
    // a row that leaves never hands focus to an answer (the focus tests above), so a key is not
    // held.
    await pointerClick("Allow once: Bash tu-b", 0);
    expect(decided).toEqual(["tu-b"]);
  } finally {
    vi.useRealTimers();
  }
});

test("a proposal row or the footer leaving a list scrolled to its end holds every answer above it", async () => {
  vi.useFakeTimers();
  try {
    stackRows("bottom");
    const decided: string[] = [];
    const natives = [rowFor("tu-a"), rowFor("tu-b")];
    const proposal = proposalRow(vaultProposal());
    const footer = <button type="button">Not now</button>;
    const { container, rerender } = render(
      answerable([...natives, proposal], (id) => decided.push(id), footer),
    );
    await settle();
    expect(settling(container)).toBe(0);

    // The proposal is decided in Approvals: no native row changed, yet both moved down a row.
    rerender(answerable(natives, (id) => decided.push(id), footer));
    expect(settling(container)).toBe(2);
    await pointerClick("Allow once: Bash tu-a", 2);
    await pointerClick("Deny: Bash tu-b");
    expect(decided).toEqual([]);
    await settle();
    expect(settling(container)).toBe(0);

    // "Not now" takes the notice, and with it the list's last box.
    rerender(answerable(natives, (id) => decided.push(id)));
    expect(settling(container)).toBe(2);
    await pointerClick("Allow once: Bash tu-b", 2);
    expect(decided).toEqual([]);

    await settle();
    await pointerClick("Allow once: Bash tu-b");
    expect(decided).toEqual(["tu-b"]);
  } finally {
    vi.useRealTimers();
  }
});

test("a change that moves no buttons holds nothing", async () => {
  vi.useFakeTimers();
  try {
    stackRows();
    const decided: string[] = [];
    const natives = [rowFor("tu-a"), rowFor("tu-b")];
    const proposal = proposalRow(vaultProposal());
    const { container, rerender } = render(
      answerable([...natives, proposal], (id) => decided.push(id)),
    );
    await settle();

    // The clock moves on, and a row below the buttons goes from a list that is not scrolled.
    rerender(answerable([...natives, proposal], (id) => decided.push(id), undefined, 5_000));
    rerender(answerable(natives, (id) => decided.push(id), undefined, 6_000));
    expect(settling(container)).toBe(0);
    await pointerClick("Allow once: Bash tu-a");
    expect(decided).toEqual(["tu-a"]);
  } finally {
    vi.useRealTimers();
  }
});
