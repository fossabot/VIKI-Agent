import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { type ReactElement, useState } from "react";
import { afterEach, expect, test, vi } from "vitest";
import {
  type ChangeProposalItem,
  type DecisionsApi,
  DecisionsApiError,
  type DecisionInbox as Inbox,
  type QuestionProposalItem,
  type VaultProposalItem,
} from "../../state/decisionsApi";
import { SKILL_STATE_WORDS, type SkillProposalItem } from "../../state/skillProposalsApi";
import { DecisionInbox } from "./DecisionInbox";
import { DecisionSection } from "./DecisionSection";
import { DecisionQueueProvider, UNDO_MS } from "./decisionQueue";

afterEach(() => {
  cleanup();
});

const HASH = "a".repeat(64);

function proposal(over: Partial<VaultProposalItem> & { id: string }): VaultProposalItem {
  return {
    kind: "vault_proposal",
    origin: { workspace_id: "w1", session_id: null },
    revision: HASH,
    state: "open",
    attention: over.state === undefined || over.state === "open",
    title: `proposal ${over.id}`,
    ref: "notes/a.md",
    origin_kind: "vault",
    base_revision: HASH,
    text_bytes: 3,
    handed_at: null,
    created_at: "2026-09-01T00:00:00.000Z",
    ...over,
  };
}

function skill(over: Partial<SkillProposalItem> & { id: string; name: string }): SkillProposalItem {
  return {
    kind: "skill_proposal",
    origin: { workspace_id: "w1", session_id: null },
    revision: HASH,
    state: "open",
    attention: false,
    title: `title ${over.id}`,
    relative_id: `skills/${over.name}/SKILL.md`,
    trigger: "button",
    sessions: ["11111111-1111-4111-8111-111111111111"],
    base_revision: null,
    text_bytes: 10,
    approved_at: null,
    rejected_at: null,
    reason: null,
    created_at: "2026-09-01T00:00:00.000Z",
    updated_at: "2026-09-01T00:00:00.000Z",
    ...over,
  };
}

function question(over: Partial<QuestionProposalItem> & { id: string }): QuestionProposalItem {
  return {
    kind: "question_proposal",
    origin: { workspace_id: "w1", session_id: null },
    revision: HASH,
    state: "open",
    attention: over.state === undefined || over.state === "open",
    title: `Question change: ${over.id}`,
    name: "worth_note",
    created_at: "2026-09-01T00:00:00.000Z",
    updated_at: "2026-09-01T00:00:00.000Z",
    approved_at: null,
    rejected_at: null,
    reason: null,
    ...over,
  };
}

function change(over: Partial<ChangeProposalItem> & { id: string }): ChangeProposalItem {
  return {
    kind: "change_proposal",
    origin: { workspace_id: "w1", session_id: null },
    revision: HASH,
    state: "open",
    attention: over.state === undefined || over.state === "open",
    title: `Link proposal: ${over.id}`,
    change: "note_link",
    ref: "notes/a.md",
    created_at: "2026-09-01T00:00:00.000Z",
    updated_at: "2026-09-01T00:00:00.000Z",
    approved_at: null,
    rejected_at: null,
    reason: null,
    ...over,
  };
}

/** An api whose successive `decisions()` calls answer with each list in turn. */
function fakeApi(
  ...lists: (VaultProposalItem | SkillProposalItem | QuestionProposalItem | ChangeProposalItem)[][]
) {
  const decisions = vi.fn<DecisionsApi["decisions"]>();
  for (const items of lists) {
    const inbox: Inbox = { items };
    decisions.mockResolvedValueOnce(inbox);
  }
  return { decisions } as unknown as DecisionsApi & { decisions: typeof decisions };
}

type Props = Parameters<typeof DecisionSection>[0];

// The section now reads useDecisionQueue (approve/later/edit/reject all enqueue through it), so
// every render and rerender needs the provider as an ancestor, not just the section itself.
function withQueue(ui: ReactElement): ReactElement {
  return <DecisionQueueProvider>{ui}</DecisionQueueProvider>;
}

function mount(api: DecisionsApi, over: Partial<Props> = {}) {
  const props: Props = {
    api,
    refreshKey: 0,
    onInspect: vi.fn(),
    ...over,
  };
  const view = render(withQueue(<DecisionSection {...props} />));
  return { ...view, props };
}

const settled = () => act(async () => {});

test("the last pending row keeps keyboard focus after its queued decision moves it to handed", async () => {
  const open = proposal({ id: "focused", state: "open", attention: true });
  const handed = { ...open, state: "handed" as const, attention: false };
  const api = fakeApi([open], [handed]);
  const onAct = vi.fn().mockResolvedValue(undefined);
  function Host(): ReactElement {
    const [refreshKey, setRefreshKey] = useState(0);
    return (
      <DecisionInbox
        items={[]}
        sessionLabel={() => "Synthetic session"}
        workspaceName={null}
        renderAnswer={() => null}
        onOpenConversation={vi.fn()}
        onClose={vi.fn()}
      >
        <DecisionSection
          api={api}
          refreshKey={refreshKey}
          onInspect={vi.fn()}
          activeWorkspaceId="w1"
          onAct={async (row, action, note) => {
            await onAct(row, action, note);
            setRefreshKey((key) => key + 1);
          }}
        />
      </DecisionInbox>
    );
  }
  vi.useFakeTimers();
  try {
    render(<Host />);
    await settled();
    const row = document.querySelector<HTMLElement>('[data-id="focused"]') as HTMLElement;
    row.focus();
    fireEvent.keyDown(row, { key: "a" });
    expect(document.activeElement).toBe(row);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(UNDO_MS);
    });
    expect(onAct).toHaveBeenCalledExactlyOnceWith(open, "approve", "");
    expect(api.decisions).toHaveBeenCalledTimes(2);
    const replacement = document.querySelector<HTMLElement>('[data-id="focused"]') as HTMLElement;
    expect(replacement.getAttribute("data-state")).toBe("handed");
    expect(replacement).not.toBe(row);
    expect(document.activeElement).toBe(replacement);
  } finally {
    vi.useRealTimers();
  }
});

test.each(["Source", "Status"])(
  "a refresh does not take focus from the chosen %s filter",
  async (label) => {
    const pending = proposal({ id: "focused", state: "open", attention: true });
    const api = fakeApi([pending]);
    let finish: (inbox: Inbox) => void = () => {};
    api.decisions.mockImplementationOnce(
      () =>
        new Promise<Inbox>((resolve) => {
          finish = resolve;
        }),
    );
    const { props, rerender } = mount(api);
    await settled();
    const row = document.querySelector<HTMLElement>('[data-id="focused"]') as HTMLElement;
    row.focus();
    rerender(withQueue(<DecisionSection {...props} refreshKey={1} />));
    const filter = screen.getByLabelText(label);
    filter.focus();
    await act(async () => finish({ items: [{ ...pending, state: "handed", attention: false }] }));
    expect(row.isConnected).toBe(false);
    expect(document.activeElement).toBe(filter);
  },
);

test.each(["history", "filtered", "removed"] as const)(
  "a focused row moved to %s falls back to a stable section control",
  async (destination) => {
    const pending = proposal({ id: "focused", state: "open", attention: true });
    const next =
      destination === "history"
        ? proposal({ id: "focused", state: "applied", attention: false })
        : proposal({ id: "focused", state: "stale", attention: true });
    const api = fakeApi([pending], destination === "removed" ? [] : [next]);
    const { props, rerender } = mount(api);
    await settled();
    if (destination === "filtered")
      fireEvent.change(screen.getByLabelText("Status"), { target: { value: "open" } });
    const row = document.querySelector<HTMLElement>('[data-id="focused"]') as HTMLElement;
    row.focus();
    rerender(withQueue(<DecisionSection {...props} refreshKey={1} />));
    await settled();
    expect(row.isConnected).toBe(false);
    expect(document.querySelector('[data-id="focused"]')).toBeNull();
    expect(document.activeElement).toBe(screen.getByLabelText("Source"));
  },
);

test("recorded history is optional and contributes no mounted approval rows until expanded", async () => {
  mount(
    fakeApi([
      proposal({ id: "review", attention: true }),
      skill({ id: "rejected", name: "r", state: "rejected" }),
      proposal({ id: "applied", state: "applied", attention: false }),
      proposal({ id: "stale", state: "stale", attention: false }),
    ]),
  );
  await settled();
  expect(document.querySelector('[data-id="rejected"]')).toBeNull();
  expect(document.querySelector('[data-id="applied"]')).toBeNull();
  expect(document.querySelector('[data-id="stale"]')).toBeNull();
  expect(document.querySelectorAll("[data-approval-row]")).toHaveLength(1);
  const history = screen.getByRole("button", { name: "History (3 returned)" });
  expect(history.getAttribute("aria-expanded")).toBe("false");
  expect(document.getElementById(history.getAttribute("aria-controls") ?? "")).toBeTruthy();
  history.focus();
  fireEvent.click(history);
  expect(history.getAttribute("aria-expanded")).toBe("true");
  expect(document.querySelectorAll("[data-approval-row]")).toHaveLength(4);
  expect(document.activeElement).toBe(history);
  fireEvent.click(history);
  expect(document.querySelectorAll("[data-approval-row]")).toHaveLength(1);
  expect(document.activeElement).toBe(history);
});

test("each card explains its kind, exact source, and what a decision changes", async () => {
  const items = [
    proposal({ id: "note", ref: "notes/<i>exact</i>.md", attention: true }),
    skill({ id: "skill", name: "example", attention: true }),
  ];
  const onInspect = vi.fn();
  mount(fakeApi(items), { onInspect, activeWorkspaceId: "w1" });
  await settled();
  const row = (id: string) => document.querySelector(`[data-id="${id}"]`) as HTMLElement;
  expect(within(row("note")).getByText("Note proposal")).toBeTruthy();
  expect(
    within(row("note")).getByText(
      "Approve creates a chat draft asking VIKI to apply this note change. Review and send it in the conversation.",
    ),
  ).toBeTruthy();
  expect(within(row("note")).getByText("notes/<i>exact</i>.md")).toBeTruthy();
  expect(within(row("skill")).getByText("Skill proposal")).toBeTruthy();
  expect(
    within(row("skill")).getByText(
      "Approve creates a chat draft asking VIKI to apply this skill. Review and send it in the conversation.",
    ),
  ).toBeTruthy();
  expect(
    within(row("skill")).getByText(
      "Reject: Records a rejection; an optional reason stays with the proposal.",
    ),
  ).toBeTruthy();
  expect(document.querySelector("i")).toBeNull();
  fireEvent.click(
    within(row("note")).getByRole("button", {
      name: "Review details proposal note · notes/<i>exact</i>.md",
    }),
  );
  expect(onInspect).toHaveBeenCalledExactlyOnceWith(items[0]);
});

test("review groups keep their priority and each row belongs to the first matching group", async () => {
  const api = fakeApi([
    proposal({ id: "a", state: "applied" }),
    skill({ id: "b", name: "b", state: "stale" }),
    proposal({ id: "c", state: "handed" }),
    proposal({ id: "d", state: "open", attention: true }),
    skill({ id: "e", name: "e", state: "handed", attention: true }),
  ]);
  mount(api);
  await settled();
  const titles = screen.getAllByRole("heading").map((heading) => heading.textContent);
  expect(titles).toEqual(["Needs review", "Draft handoffs", "History (2 returned)"]);
  fireEvent.click(screen.getByRole("button", { name: "History (2 returned)" }));
  const idsIn = (key: string) =>
    [...document.querySelectorAll(`[data-group="${key}"] .decision-row`)].map((row) =>
      row.getAttribute("data-id"),
    );
  expect(idsIn("attention")).toEqual(["d", "e"]);
  expect(idsIn("handed")).toEqual(["c"]);
  expect(idsIn("recorded")).toEqual(["a", "b"]);
});

test("a row carries kind, id, state, title and source as text, and the section is labelled", async () => {
  const api = fakeApi([
    skill({ id: "a", name: "a", title: "<b>x</b>", attention: true }),
    proposal({ id: "p" }),
  ]);
  mount(api);
  await settled();
  expect(screen.getByLabelText("Proposals").tagName).toBe("SECTION");
  const row = document.querySelector('.decision-row[data-id="a"]') as HTMLElement;
  expect(row.getAttribute("data-kind")).toBe("skill_proposal");
  expect(row.getAttribute("data-state")).toBe("open");
  expect(row.textContent).toContain("<b>x</b>");
  expect(row.querySelector("b")).toBeNull();
  expect(row.textContent).toContain("skills/a/SKILL.md");
  const vault = document.querySelector('.decision-row[data-id="p"]') as HTMLElement;
  expect(vault.getAttribute("data-kind")).toBe("vault_proposal");
  expect(vault.textContent).toContain("notes/a.md");
});

test("a skill row lists in attention when open, shows its SKILL.md path and skill wording, never Note matches the proposal", async () => {
  const open = skill({ id: "s-open", name: "x", state: "open", attention: true });
  const applied = skill({ id: "s-applied", name: "y", state: "applied", attention: false });
  const api = fakeApi([open, applied]);
  mount(api);
  await settled();
  expect(
    [...document.querySelectorAll('[data-group="attention"] .decision-row')].map((row) =>
      row.getAttribute("data-id"),
    ),
  ).toEqual(["s-open"]);
  const openRow = document.querySelector('.decision-row[data-id="s-open"]') as HTMLElement;
  expect(openRow.textContent).toContain("skills/x/SKILL.md");
  expect(openRow.querySelector(".state-chip")?.textContent).toBe(SKILL_STATE_WORDS.open);
  fireEvent.click(screen.getByRole("button", { name: /^History/ }));
  const appliedRow = document.querySelector('.decision-row[data-id="s-applied"]') as HTMLElement;
  expect(appliedRow.textContent).toContain("skills/y/SKILL.md");
  expect(appliedRow.querySelector(".state-chip")?.textContent).toBe(SKILL_STATE_WORDS.applied);
  expect(appliedRow.querySelector("input[type=checkbox]")).toBeNull();
  expect(document.body.textContent).not.toContain("Note matches the proposal");
});

test("no checkbox ever renders, for any row or state", async () => {
  const api = fakeApi([
    proposal({ id: "a" }),
    proposal({ id: "b", state: "handed" }),
    proposal({ id: "c", state: "applied" }),
    skill({ id: "d", name: "d", state: "rejected" }),
    proposal({ id: "e", state: "stale" }),
  ]);
  mount(api);
  await settled();
  fireEvent.click(screen.getByRole("button", { name: /^History/ }));
  expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
});

test("Review details calls onInspect with the row's item", async () => {
  const item = proposal({ id: "a" });
  const { props } = mount(fakeApi([item]));
  await settled();
  fireEvent.click(screen.getByRole("button", { name: "Review details proposal a · notes/a.md" }));
  expect(props.onInspect).toHaveBeenCalledWith(item);
});

test("a failed load shows a line and keeps the section", async () => {
  const api = { decisions: vi.fn().mockRejectedValue(new DecisionsApiError("unavailable")) };
  mount(api as unknown as DecisionsApi);
  await settled();
  expect(screen.getByLabelText("Proposals").textContent).toContain("Could not load the list.");
  expect(
    screen.queryByText("Displayed proposals are from the last successful response."),
  ).toBeNull();
});

test("after a success, a refresh that fails keeps the stale list and Retry can resolve it empty", async () => {
  const api = fakeApi([proposal({ id: "a" })]);
  api.decisions.mockRejectedValueOnce(new DecisionsApiError("unavailable"));
  let finish: (inbox: Inbox) => void = () => {};
  api.decisions.mockImplementationOnce(
    () =>
      new Promise<Inbox>((resolve) => {
        finish = resolve;
      }),
  );
  const { props, rerender } = mount(api);
  await settled();
  expect(
    screen.queryByText("Displayed proposals are from the last successful response."),
  ).toBeNull();
  rerender(withQueue(<DecisionSection {...props} refreshKey={1} />));
  await settled();
  expect(screen.getByText("Could not load the list.")).toBeTruthy();
  expect(
    screen.getByText("Displayed proposals are from the last successful response."),
  ).toBeTruthy();
  expect(document.querySelector('[data-id="a"]')).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  expect((screen.getByRole("button", { name: "Retry" }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText("Reading proposals…")).toBeTruthy();
  await act(async () => finish({ items: [] }));
  expect(api.decisions).toHaveBeenCalledTimes(3);
  expect(screen.getByText("No proposals were returned in this response.")).toBeTruthy();
  expect(
    screen.queryByText("Displayed proposals are from the last successful response."),
  ).toBeNull();
});

test.each(["refresh", "api", "unmount"] as const)(
  "%s abort rejects an old list",
  async (boundary) => {
    const oldApi = fakeApi();
    let finish: (inbox: Inbox) => void = () => {};
    oldApi.decisions.mockImplementationOnce(
      () =>
        new Promise<Inbox>((resolve) => {
          finish = resolve;
        }),
    );
    const { props, rerender, unmount } = mount(oldApi);
    const signal = oldApi.decisions.mock.calls[0]?.[0];
    const current = [proposal({ id: "current", attention: true })];
    if (boundary === "unmount") unmount();
    else {
      const api = boundary === "api" ? fakeApi(current) : oldApi;
      if (boundary === "refresh") api.decisions.mockResolvedValueOnce({ items: current });
      rerender(withQueue(<DecisionSection {...props} api={api} refreshKey={1} />));
      await settled();
      expect(document.querySelector('[data-id="current"]')).not.toBeNull();
    }
    expect(signal?.aborted).toBe(true);
    await act(async () => finish({ items: [proposal({ id: "old", attention: true })] }));
    expect(document.querySelector('[data-id="old"]')).toBeNull();
    if (boundary !== "unmount") {
      expect(document.querySelector('[data-id="current"]')).not.toBeNull();
    }
  },
);

test("returned-proposal filter counts appear only once a filter is set", async () => {
  const api = fakeApi([proposal({ id: "a" }), skill({ id: "p", name: "p" })]);
  mount(api);
  await settled();
  expect(screen.queryByText(/^\d+ of \d+ returned proposals match these filters\.$/)).toBeNull();
  fireEvent.change(screen.getByLabelText("Source"), {
    target: { value: "notes/a.md" },
  });
  expect(screen.getByText("1 of 2 returned proposals match these filters.")).toBeTruthy();
});

test("status filters use readable labels, retain raw values and preserve missing selections", async () => {
  const ref = "notes/<b>exact</b>.md";
  const api = fakeApi(
    [
      proposal({ id: "recorded", ref, state: "applied", attention: false }),
      proposal({ id: "review", ref: "notes/other.md", attention: true }),
    ],
    [skill({ id: "replacement", name: "replacement", state: "open", attention: true })],
  );
  const { props, rerender } = mount(api);
  await settled();
  expect(screen.getByRole("group", { name: "Filter proposals" })).toBeTruthy();
  const status = screen.getByLabelText<HTMLSelectElement>("Status");
  const sources = screen.getByLabelText<HTMLSelectElement>("Source");
  expect(
    within(status).getByRole<HTMLOptionElement>("option", { name: "File matches the proposal" })
      .value,
  ).toBe("applied");
  expect(
    within(status).getByRole<HTMLOptionElement>("option", { name: "All statuses" }).value,
  ).toBe("");
  fireEvent.change(sources, { target: { value: ref } });
  fireEvent.change(status, { target: { value: "applied" } });
  expect(screen.getByText("1 of 2 returned proposals match these filters.")).toBeTruthy();
  expect(document.querySelector("[data-approval-row]")).toBeNull();
  expect(
    screen.getByRole("button", { name: "History (1 returned)" }).getAttribute("aria-expanded"),
  ).toBe("false");
  rerender(withQueue(<DecisionSection {...props} refreshKey={1} />));
  await settled();
  expect(status.value).toBe("applied");
  expect(sources.value).toBe(ref);
  expect(
    within(status).getByRole<HTMLOptionElement>("option", {
      name: "File matches the proposal (not returned)",
    }).value,
  ).toBe("applied");
  expect(within(sources).getByRole("option", { name: `${ref} (not returned)` })).toBeTruthy();
  expect(screen.getByText("0 of 1 returned proposals match these filters.")).toBeTruthy();
  expect(document.querySelector("b")).toBeNull();
});

test("a mixed inbox keeps Proposed and Being tried as separate Status options", async () => {
  const vaultOpen = proposal({ id: "vault-open", state: "open", attention: true });
  const questionOpen = question({ id: "question-open", state: "open" });
  mount(fakeApi([vaultOpen, questionOpen]));
  await settled();
  const status = screen.getByLabelText<HTMLSelectElement>("Status");
  const proposedOption = within(status).getByRole<HTMLOptionElement>("option", {
    name: "Proposed",
  });
  const beingTriedOption = within(status).getByRole<HTMLOptionElement>("option", {
    name: "Being tried",
  });
  expect(proposedOption.value).not.toBe(beingTriedOption.value);
  const idsShown = () =>
    [...document.querySelectorAll("[data-approval-row]")].map((row) => row.getAttribute("data-id"));
  fireEvent.change(status, { target: { value: proposedOption.value } });
  expect(idsShown()).toEqual(["vault-open"]);
  fireEvent.change(status, { target: { value: beingTriedOption.value } });
  expect(idsShown()).toEqual(["question-open"]);
});

test("cards show why a stale, missing-source or other-vault row is limited", async () => {
  mount(
    fakeApi([
      proposal({ id: "stale", state: "stale", attention: true }),
      proposal({ id: "missing", state: "source_missing", attention: true }),
      proposal({ id: "other", attention: true }),
    ]),
    { activeWorkspaceId: "another-vault" },
  );
  await settled();
  const row = (id: string) => document.querySelector(`[data-id="${id}"]`) as HTMLElement;
  expect(
    within(row("stale")).getByText("This item changed. Review details before deciding again."),
  ).toBeTruthy();
  expect(
    within(row("missing")).getByText(
      "The source could not be found. Review details before taking action.",
    ),
  ).toBeTruthy();
  expect(
    within(row("other")).getByText(
      "This proposal is for another vault. Switch to its vault to use these actions.",
    ),
  ).toBeTruthy();
  for (const button of row("other").querySelectorAll<HTMLButtonElement>("[data-approval-action]"))
    expect(button.disabled).toBe(true);
  expect(
    within(row("other")).getByRole<HTMLButtonElement>("button", { name: /^Review details/ })
      .disabled,
  ).toBe(false);
});

test("a row offers exactly its approvalActions buttons, labelled, hinted and gated", async () => {
  const active = proposal({ id: "a", state: "open" });
  const otherVault = proposal({
    id: "p",
    state: "open",
    origin: { workspace_id: "elsewhere", session_id: null },
  });
  mount(fakeApi([active, otherVault]), { activeWorkspaceId: "w1" });
  await settled();
  const row = document.querySelector('[data-id="a"]') as HTMLElement;
  expect(row.hasAttribute("data-approval-row")).toBe(true);
  expect(row.tabIndex).toBe(-1);
  expect(
    within(row)
      .getAllByRole("button")
      .map((button) => button.textContent),
  ).toEqual(["Approve", "Edit", "Reject", "Review details"]);
  const approve = within(row).getByRole("button", { name: "Approve" }) as HTMLButtonElement;
  expect(approve.title).toBe("Asks VIKI, in a new draft, to apply this change.");
  expect(approve.getAttribute("data-approval-action")).toBe("approve");
  expect(approve.disabled).toBe(false);
  expect(row.querySelector("[data-approval-open]")?.textContent).toBe("Review details");

  const vaultRow = document.querySelector('[data-id="p"]') as HTMLElement;
  const vaultApprove = within(vaultRow).getByRole("button", {
    name: "Approve",
  }) as HTMLButtonElement;
  expect(vaultApprove.disabled).toBe(true);
  expect(vaultApprove.title).toBe("This proposal is for another vault.");
});

test("approve enqueues directly with an empty note and calls onAct once the undo window expires", async () => {
  const item = proposal({ id: "a", state: "open" });
  const onAct = vi.fn().mockResolvedValue(undefined);
  mount(fakeApi([item]), { onAct, activeWorkspaceId: "w1" });
  await settled();
  const row = document.querySelector('[data-id="a"]') as HTMLElement;
  const approve = within(row).getByRole("button", { name: "Approve" }) as HTMLButtonElement;
  vi.useFakeTimers();
  try {
    fireEvent.click(approve);
    expect(onAct).not.toHaveBeenCalled();
    expect(approve.disabled).toBe(true);
    expect(
      (within(row).getByRole("button", { name: "Reject" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(UNDO_MS);
    });
  } finally {
    vi.useRealTimers();
  }
  expect(onAct).toHaveBeenCalledExactlyOnceWith(item, "approve", "");
});

test("a missing onAct still enqueues a no-op send, and the row re-enables once it settles", async () => {
  const item = proposal({ id: "a", state: "open" });
  mount(fakeApi([item]), { activeWorkspaceId: "w1" });
  await settled();
  const row = document.querySelector('[data-id="a"]') as HTMLElement;
  vi.useFakeTimers();
  try {
    fireEvent.click(within(row).getByRole("button", { name: "Approve" }));
    expect(
      (within(row).getByRole("button", { name: "Approve" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(UNDO_MS);
    });
  } finally {
    vi.useRealTimers();
  }
  expect((within(row).getByRole("button", { name: "Approve" }) as HTMLButtonElement).disabled).toBe(
    false,
  );
});

test("Review details disables while the row's decision is queued, like its action buttons", async () => {
  const item = proposal({ id: "a", state: "open" });
  const onAct = vi.fn().mockResolvedValue(undefined);
  mount(fakeApi([item]), { onAct, activeWorkspaceId: "w1" });
  await settled();
  const row = document.querySelector('[data-id="a"]') as HTMLElement;
  const open = within(row).getByRole("button", {
    name: "Review details proposal a · notes/a.md",
  }) as HTMLButtonElement;
  expect(open.disabled).toBe(false);
  vi.useFakeTimers();
  try {
    fireEvent.click(within(row).getByRole("button", { name: "Approve" }));
    expect(open.disabled).toBe(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(UNDO_MS);
    });
  } finally {
    vi.useRealTimers();
  }
  expect(open.disabled).toBe(false);
});

test("vault edit opens the inspector directly, without the queue or a dialog", async () => {
  const item = proposal({ id: "p", state: "open" });
  const onInspect = vi.fn();
  const onAct = vi.fn();
  mount(fakeApi([item]), { onInspect, onAct, activeWorkspaceId: "w1" });
  await settled();
  const row = document.querySelector('[data-id="p"]') as HTMLElement;
  fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
  expect(onInspect).toHaveBeenCalledExactlyOnceWith(item);
  expect(onAct).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog")).toBeNull();
});

test("a skill row renders Approve, Edit and Reject with data-approval-action, and Edit opens the inspector directly", async () => {
  const item = skill({ id: "s-1", name: "x", state: "open", attention: true });
  const onInspect = vi.fn();
  const onAct = vi.fn();
  mount(fakeApi([item]), { onInspect, onAct, activeWorkspaceId: "w1" });
  await settled();
  const row = document.querySelector('[data-id="s-1"]') as HTMLElement;
  expect(
    [...row.querySelectorAll("[data-approval-action]")].map((button) =>
      button.getAttribute("data-approval-action"),
    ),
  ).toEqual(["approve", "edit", "reject"]);
  fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
  expect(onInspect).toHaveBeenCalledExactlyOnceWith(item);
  expect(onAct).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog")).toBeNull();
});

test("a skill row's approve enqueues, then calls onAct once the undo window expires", async () => {
  const item = skill({ id: "s-2", name: "y", state: "open", attention: true });
  const onAct = vi.fn().mockResolvedValue(undefined);
  mount(fakeApi([item]), { onAct, activeWorkspaceId: "w1" });
  await settled();
  const row = document.querySelector('[data-id="s-2"]') as HTMLElement;
  const approve = within(row).getByRole("button", { name: "Approve" }) as HTMLButtonElement;
  vi.useFakeTimers();
  try {
    fireEvent.click(approve);
    expect(onAct).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(UNDO_MS);
    });
  } finally {
    vi.useRealTimers();
  }
  expect(onAct).toHaveBeenCalledExactlyOnceWith(item, "approve", "");
});

test("reject opens the reason dialog labelled Reason (optional), for a vault row too", async () => {
  const item = proposal({ id: "p", state: "open" });
  const onAct = vi.fn().mockResolvedValue(undefined);
  mount(fakeApi([item]), { onAct, activeWorkspaceId: "w1" });
  await settled();
  const row = document.querySelector('[data-id="p"]') as HTMLElement;
  fireEvent.click(within(row).getByRole("button", { name: "Reject" }));
  const dialog = screen.getByRole("dialog");
  within(dialog).getByLabelText("Reason (optional)");
  vi.useFakeTimers();
  try {
    fireEvent.click(within(dialog).getByRole("button", { name: "Reject" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(UNDO_MS);
    });
  } finally {
    vi.useRealTimers();
  }
  expect(onAct).toHaveBeenCalledExactlyOnceWith(item, "reject", "");
});

test("Cancel on the reason dialog records nothing and leaves the row usable", async () => {
  const item = proposal({ id: "a", state: "open" });
  const onAct = vi.fn();
  mount(fakeApi([item]), { onAct, activeWorkspaceId: "w1" });
  await settled();
  const row = document.querySelector('[data-id="a"]') as HTMLElement;
  fireEvent.click(within(row).getByRole("button", { name: "Reject" }));
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(onAct).not.toHaveBeenCalled();
  expect((within(row).getByRole("button", { name: "Reject" }) as HTMLButtonElement).disabled).toBe(
    false,
  );
});

test("confirming the reason dialog moves focus to the next row", async () => {
  const items = [proposal({ id: "a", state: "open" }), proposal({ id: "b", state: "open" })];
  const onAct = vi.fn().mockResolvedValue(undefined);
  render(
    <div role="dialog">
      {withQueue(
        <DecisionSection
          api={fakeApi(items)}
          refreshKey={0}
          onInspect={vi.fn()}
          activeWorkspaceId="w1"
          onAct={onAct}
        />,
      )}
    </div>,
  );
  await settled();
  const rowA = document.querySelector('[data-id="a"]') as HTMLElement;
  const rowB = document.querySelector('[data-id="b"]') as HTMLElement;
  fireEvent.click(within(rowA).getByRole("button", { name: "Reject" }));
  const dialogs = screen.getAllByRole("dialog");
  const reasonDialog = dialogs[dialogs.length - 1] as HTMLElement;
  fireEvent.click(within(reasonDialog).getByRole("button", { name: "Reject" }));
  await settled();
  expect(screen.getAllByRole("dialog")).toHaveLength(1);
  expect(document.activeElement).toBe(rowB);
});

test("mounted as the inbox's children, the section follows the native rows", async () => {
  const api = fakeApi([proposal({ id: "a" })]);
  render(
    <DecisionInbox
      items={[]}
      sessionLabel={() => "s"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
    >
      <DecisionSection api={api} refreshKey={0} onInspect={vi.fn()} />
    </DecisionInbox>,
  );
  await settled();
  const dialog = screen.getByRole("dialog");
  expect(within(dialog).getByLabelText("Proposals")).toBeTruthy();
  expect(dialog.querySelector(".decision-inbox-empty")).toBeTruthy();
  expect(dialog.lastElementChild?.className).toBe("decision-inbox-details");
  expect(dialog.lastElementChild?.lastElementChild?.getAttribute("aria-label")).toBe("Proposals");
});

test("vault Review details labels distinguish literal refs without adding selection or markup", async () => {
  const first = proposal({
    id: "3".repeat(32),
    title: "<i>Shared title</i>",
    ref: "notes/alpha.md",
  });
  const second = proposal({
    id: "4".repeat(32),
    title: "<i>Shared title</i>",
    ref: "notes/beta.md",
  });
  const onInspect = vi.fn();
  const api = fakeApi([first, second]);
  mount(api, { onInspect });
  await settled();
  expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
  expect(document.querySelector(`[data-id="${first.id}"] .decision-row-source`)?.textContent).toBe(
    first.ref,
  );
  expect(document.querySelector(`[data-id="${second.id}"] .decision-row-source`)?.textContent).toBe(
    second.ref,
  );
  expect(document.querySelector("i")).toBeNull();
  fireEvent.click(
    screen.getByRole("button", { name: "Review details <i>Shared title</i> · notes/beta.md" }),
  );
  expect(onInspect).toHaveBeenCalledExactlyOnceWith(second);
  expect(api.decisions).toHaveBeenCalledOnce();
});

test("a question proposal shows its kind, source and the four status words, grouped by attention", async () => {
  const open = question({ id: "open", state: "open" });
  const inUse = question({ id: "in-use", state: "approved" });
  const replaced = question({ id: "replaced", state: "replaced" });
  const rejected = question({ id: "rejected", state: "rejected" });
  mount(fakeApi([open, inUse, replaced, rejected]));
  await settled();
  expect(
    [...document.querySelectorAll('[data-group="attention"] .decision-row')].map((row) =>
      row.getAttribute("data-id"),
    ),
  ).toEqual(["open"]);
  const row = (id: string) => document.querySelector(`[data-id="${id}"]`) as HTMLElement;
  expect(within(row("open")).getByText("Question proposal")).toBeTruthy();
  expect(within(row("open")).getByText("Decision")).toBeTruthy();
  expect(row("open").querySelector(".state-chip")?.textContent).toBe("Being tried");
  fireEvent.click(screen.getByRole("button", { name: /^History/ }));
  expect(row("in-use").querySelector(".state-chip")?.textContent).toBe("In use");
  expect(row("replaced").querySelector(".state-chip")?.textContent).toBe("Replaced");
  expect(row("rejected").querySelector(".state-chip")?.textContent).toBe("Rejected");
});

test("an open question proposal offers Approve and Reject only; approve enqueues directly", async () => {
  const item = question({ id: "q1", state: "open" });
  const onAct = vi.fn().mockResolvedValue(undefined);
  mount(fakeApi([item]), { onAct, activeWorkspaceId: "w1" });
  await settled();
  const row = document.querySelector('[data-id="q1"]') as HTMLElement;
  expect(
    within(row)
      .getAllByRole("button")
      .map((button) => button.textContent),
  ).toEqual(["Approve", "Reject", "Review details"]);
  const approve = within(row).getByRole("button", { name: "Approve" }) as HTMLButtonElement;
  expect(approve.title).toBe("Starts using this wording for new decisions.");
  vi.useFakeTimers();
  try {
    fireEvent.click(approve);
    expect(onAct).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(UNDO_MS);
    });
  } finally {
    vi.useRealTimers();
  }
  expect(onAct).toHaveBeenCalledExactlyOnceWith(item, "approve", "");
});

test("an approved question proposal disables Approve and confirms Undo through the reason dialog under that label", async () => {
  const item = question({ id: "q2", state: "approved" });
  const onAct = vi.fn().mockResolvedValue(undefined);
  mount(fakeApi([item]), { onAct, activeWorkspaceId: "w1" });
  await settled();
  // Approved means no longer attention: the row sits in the collapsed History group.
  fireEvent.click(screen.getByRole("button", { name: /^History/ }));
  const row = document.querySelector('[data-id="q2"]') as HTMLElement;
  expect((within(row).getByRole("button", { name: "Approve" }) as HTMLButtonElement).disabled).toBe(
    true,
  );
  const undo = within(row).getByRole("button", { name: "Undo" }) as HTMLButtonElement;
  expect(undo.title).toBe("Goes back to the wording before this change.");
  fireEvent.click(undo);
  const dialog = screen.getByRole("dialog");
  within(dialog).getByLabelText("Reason (optional)");
  vi.useFakeTimers();
  try {
    fireEvent.click(within(dialog).getByRole("button", { name: "Undo" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(UNDO_MS);
    });
  } finally {
    vi.useRealTimers();
  }
  expect(onAct).toHaveBeenCalledExactlyOnceWith(item, "reject", "");
});

test("a replaced or rejected question proposal offers no enabled action", async () => {
  const replaced = question({ id: "q3", state: "replaced" });
  const rejected = question({ id: "q4", state: "rejected" });
  mount(fakeApi([replaced, rejected]), { activeWorkspaceId: "w1" });
  await settled();
  fireEvent.click(screen.getByRole("button", { name: /^History/ }));
  for (const id of ["q3", "q4"]) {
    const row = document.querySelector(`[data-id="${id}"]`) as HTMLElement;
    for (const button of row.querySelectorAll<HTMLButtonElement>("[data-approval-action]"))
      expect(button.disabled).toBe(true);
  }
});

test("a change proposal shows its kind label by change type, source and the three status words, grouped by attention, with no Review details", async () => {
  const link = change({ id: "link", state: "open", change: "note_link", ref: "notes/a.md" });
  const superseded = change({
    id: "superseded",
    state: "approved",
    change: "note_superseded",
    ref: "notes/b.md",
  });
  const ticked = change({
    id: "ticked",
    state: "rejected",
    change: "tasks_ticked",
    ref: "notes/c.md",
  });
  mount(fakeApi([link, superseded, ticked]));
  await settled();
  expect(
    [...document.querySelectorAll('[data-group="attention"] .decision-row')].map((row) =>
      row.getAttribute("data-id"),
    ),
  ).toEqual(["link"]);
  const row = (id: string) => document.querySelector(`[data-id="${id}"]`) as HTMLElement;
  expect(within(row("link")).getByText("Link proposal")).toBeTruthy();
  expect(within(row("link")).getByText("notes/a.md")).toBeTruthy();
  expect(row("link").querySelector(".state-chip")?.textContent).toBe("Proposed");
  expect(row("link").querySelector("[data-approval-open]")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /^History/ }));
  expect(within(row("superseded")).getByText("Upkeep proposal")).toBeTruthy();
  expect(row("superseded").querySelector(".state-chip")?.textContent).toBe("Draft made");
  expect(within(row("ticked")).getByText("Upkeep proposal")).toBeTruthy();
  expect(row("ticked").querySelector(".state-chip")?.textContent).toBe("Rejected");
  for (const id of ["link", "superseded", "ticked"])
    expect(row(id).querySelector("[data-approval-open]")).toBeNull();
});

test("an open change proposal offers Approve and Reject only, no Review details; approve enqueues directly", async () => {
  const item = change({ id: "c1", state: "open" });
  const onAct = vi.fn().mockResolvedValue(undefined);
  mount(fakeApi([item]), { onAct, activeWorkspaceId: "w1" });
  await settled();
  const row = document.querySelector('[data-id="c1"]') as HTMLElement;
  expect(
    within(row)
      .getAllByRole("button")
      .map((button) => button.textContent),
  ).toEqual(["Approve", "Reject"]);
  const approve = within(row).getByRole("button", { name: "Approve" }) as HTMLButtonElement;
  expect(approve.title).toBe("Opens a draft asking VIKI to make this change.");
  vi.useFakeTimers();
  try {
    fireEvent.click(approve);
    expect(onAct).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(UNDO_MS);
    });
  } finally {
    vi.useRealTimers();
  }
  expect(onAct).toHaveBeenCalledExactlyOnceWith(item, "approve", "");
});

test("an approved change proposal relabels Approve as Draft again and disables Reject; a rejected one disables both", async () => {
  const approved = change({ id: "c2", state: "approved" });
  const rejected = change({ id: "c3", state: "rejected" });
  const onAct = vi.fn().mockResolvedValue(undefined);
  mount(fakeApi([approved, rejected]), { onAct, activeWorkspaceId: "w1" });
  await settled();
  fireEvent.click(screen.getByRole("button", { name: /^History/ }));
  const approvedRow = document.querySelector('[data-id="c2"]') as HTMLElement;
  const draftAgain = within(approvedRow).getByRole("button", {
    name: "Draft again",
  }) as HTMLButtonElement;
  expect(draftAgain.disabled).toBe(false);
  const reject = within(approvedRow).getByRole("button", { name: "Reject" }) as HTMLButtonElement;
  expect(reject.disabled).toBe(true);
  vi.useFakeTimers();
  try {
    fireEvent.click(draftAgain);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(UNDO_MS);
    });
  } finally {
    vi.useRealTimers();
  }
  expect(onAct).toHaveBeenCalledExactlyOnceWith(approved, "approve", "");

  const rejectedRow = document.querySelector('[data-id="c3"]') as HTMLElement;
  for (const button of rejectedRow.querySelectorAll<HTMLButtonElement>("[data-approval-action]"))
    expect(button.disabled).toBe(true);
});
