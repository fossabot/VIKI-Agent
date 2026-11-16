/**
 * Vault-change proposals as App wires them: a change row reads the same way in the right rail's
 * Needs you and in Approvals, Approve opens a fixed draft in the composer (relabelled Draft again
 * once approved, answering the same draft once more with no further write), Reject takes an
 * optional reason and drafts nothing, the Decision page's "What VIKI may do" row shows the day's
 * vault-change count against its limit, and the launcher — a wholly separate surface, fed by a
 * separate endpoint — never lists one.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, sessionRow } from "../../accept/m3b/fakes";
import {
  createFakeDecisionLoopApi,
  decisionLoopExamples,
  type FakeDecisionLoopApi,
} from "../../clients/decisionLoop/fake";
import { UNDO_MS } from "../../components/Decisions/decisionQueue";
import type { ChangeProposalItem, DecisionsApi } from "../../state/decisionsApi";
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
const setValue = async (element: Element, value: string) =>
  act(async () => void fireEvent.change(element, { target: { value } }));
const box = () => screen.getByLabelText<HTMLTextAreaElement>("Message");

const VAULT = "44444444-4444-4444-8444-444444444444";
const S1 = sessionRow({ session_id: "s1", title: "Chat" });

function changeItem(over: Partial<ChangeProposalItem> & { id: string }): ChangeProposalItem {
  return {
    kind: "change_proposal",
    origin: { workspace_id: VAULT, session_id: null },
    revision: "a".repeat(64),
    state: "open",
    attention: true,
    title: `change ${over.id}`,
    change: "note_link",
    ref: "30-knowledge/a-note.md",
    created_at: "2026-10-22T09:00:00.000Z",
    updated_at: "2026-10-22T09:00:00.000Z",
    approved_at: null,
    rejected_at: null,
    reason: null,
    ...over,
  };
}

const LINK_ITEM = changeItem({
  id: "11111111-1111-4111-8111-111111111111",
  revision: "a".repeat(64),
  change: "note_link",
  ref: "30-knowledge/garden-notes.md",
  title: "Link garden-notes to composting-basics",
});
const LINK_DRAFT =
  "Link two notes in this vault by adding one wikilink line to the first note, as described below:\n" +
  "- In `30-knowledge/garden-notes.md`, add one line `- [[composting-basics]]` under a " +
  "`## Related` heading at the end of the note (add the heading if the note has none).\n" +
  "Change no other line, and keep every word already there as it is.";

const UPKEEP_ITEM = changeItem({
  id: "22222222-2222-4222-8222-222222222222",
  revision: "b".repeat(64),
  change: "tasks_ticked",
  ref: "10-projects/garden-project/tasks.md",
  title: "Archive ticked tasks in garden-project",
});

/** `decisions()` backing Approvals and Needs you: the given list, with whichever row the fake
 * Decision API's own `actChange` last touched (`api.data.change_act.change`, a single mutable
 * slot — fake.ts's own contract) replacing the matching id. A test needs no bookkeeping of its
 * own between actions: whatever `actChange` just wrote is what the next read shows. */
function fakeChangeInbox(
  api: FakeDecisionLoopApi,
  items: readonly ChangeProposalItem[],
): DecisionsApi {
  return {
    decisions: async () => ({
      items: items.map((item) =>
        item.id === api.data.change_act.change.id ? api.data.change_act.change : item,
      ),
    }),
  } as unknown as DecisionsApi;
}

function vaultShell(): FakeShellApi {
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
  });
}

async function mountVault(decisionApi: FakeDecisionLoopApi, decisions: DecisionsApi) {
  const { viki, socket } = fakeViki();
  const view = render(
    <App
      viki={viki}
      rest={fakeRest({ sessions: [S1] })}
      durable={fakeDurable()}
      shell={vaultShell()}
      decisions={decisions}
      decisionApi={decisionApi}
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

async function mountPlain(decisionApi: FakeDecisionLoopApi) {
  const { viki, socket } = fakeViki();
  const view = render(
    <App
      viki={viki}
      rest={fakeRest({ sessions: [] })}
      durable={fakeDurable()}
      shell={fakeShellApi({ settings: settingsFixture({ "onboarding.completed": true }) })}
      decisionApi={decisionApi}
    />,
  );
  await act(async () => socket.open());
  return { socket, ...view };
}

async function openApprovals(): Promise<HTMLElement> {
  await click(screen.getByRole("button", { name: "Open Approvals" }));
  return screen.findByRole("region", { name: "Proposals" });
}

const launcherButton = () => document.querySelector<HTMLButtonElement>("button.dloop-launcher");
async function openLauncher(): Promise<HTMLElement> {
  const trigger = launcherButton();
  if (!trigger) throw new Error("no launcher button");
  await click(trigger);
  return screen.findByRole("dialog", { name: "VIKI" });
}

const sidebar = () => within(screen.getByRole("navigation", { name: "Navigation and sessions" }));
async function openDecisionPage(): Promise<void> {
  await click(sidebar().getByRole("button", { name: "Decision" }));
  await settled();
}
async function expandRow(name: string): Promise<HTMLElement> {
  const row = screen.getByRole("region", { name });
  await click(within(row).getByRole("button", { name }));
  return row;
}

test("a change row reads the same in Needs you and Approvals, opens no inspector, and the launcher lists neither", async () => {
  const api = createFakeDecisionLoopApi();
  const decisions = fakeChangeInbox(api, [LINK_ITEM, UPKEEP_ITEM]);
  await mountVault(api, decisions);
  await settled();

  // Needs you: both rows, each chipped "Vault change" regardless of its own kind.
  const linkNeedRow = screen.getByText(LINK_ITEM.title).closest("button");
  const upkeepNeedRow = screen.getByText(UPKEEP_ITEM.title).closest("button");
  if (!linkNeedRow || !upkeepNeedRow) throw new Error("Needs you is missing a change row");
  expect(within(linkNeedRow).getByText(`${LINK_ITEM.ref} · Vault change`)).toBeTruthy();
  expect(within(upkeepNeedRow).getByText(`${UPKEEP_ITEM.ref} · Vault change`)).toBeTruthy();

  // Clicking a Needs-you change row opens Approvals, never an inspector.
  await click(linkNeedRow);
  expect(await screen.findByRole("dialog", { name: "Approvals" })).toBeTruthy();
  expect(screen.queryByRole("dialog", { name: LINK_ITEM.title })).toBeNull();

  const section = await screen.findByRole("region", { name: "Proposals" });
  const linkRow = section.querySelector(`[data-id="${LINK_ITEM.id}"]`);
  const upkeepRow = section.querySelector(`[data-id="${UPKEEP_ITEM.id}"]`);
  if (!(linkRow instanceof HTMLElement) || !(upkeepRow instanceof HTMLElement))
    throw new Error("Approvals is missing a change row");

  expect(within(linkRow).getByText("Link proposal")).toBeTruthy();
  expect(within(upkeepRow).getByText("Upkeep proposal")).toBeTruthy();
  expect(linkRow.querySelector(".state-chip")?.textContent).toBe("Proposed");
  expect(upkeepRow.querySelector(".state-chip")?.textContent).toBe("Proposed");
  expect(
    within(linkRow).getByText(
      "Approve opens a chat draft asking VIKI to make this change; nothing in the vault changes until you send it.",
    ),
  ).toBeTruthy();

  for (const row of [linkRow, upkeepRow]) {
    expect(row.querySelector("[data-approval-open]")).toBeNull();
    expect(
      within(row)
        .getAllByRole("button")
        .map((button) => button.textContent),
    ).toEqual(["Approve", "Reject"]);
    const approve = within(row).getByRole("button", { name: "Approve" }) as HTMLButtonElement;
    const reject = within(row).getByRole("button", { name: "Reject" }) as HTMLButtonElement;
    expect(approve.title).toBe("Opens a draft asking VIKI to make this change.");
    expect(reject.title).toBe("Rejects this change; nothing in the vault changes.");
  }

  await click(screen.getByRole("button", { name: "Close" }));
  await settled();

  // The launcher is a separate surface fed by a separate endpoint: it lists no change item, by
  // title or by either kind's own label (wording.ts's CANDIDATE_LABELS), even while both rows
  // above are live in Needs you and Approvals.
  const launcher = await openLauncher();
  for (const text of [
    LINK_ITEM.title,
    UPKEEP_ITEM.title,
    "Note links",
    "Superseded notes",
    "Ticked tasks",
  ]) {
    expect(within(launcher).queryByText(text)).toBeNull();
  }
});

test("Approve opens a draft in the composer; Draft again opens it once more", async () => {
  const api = createFakeDecisionLoopApi({ change_act: { change: LINK_ITEM, draft: LINK_DRAFT } });
  const decisions = fakeChangeInbox(api, [LINK_ITEM]);
  await mountVault(api, decisions);
  await settled();

  const section = await openApprovals();
  let row = section.querySelector(`[data-id="${LINK_ITEM.id}"]`);
  if (!(row instanceof HTMLElement)) throw new Error("the open row is missing");
  const approve = within(row).getByRole("button", { name: "Approve" }) as HTMLButtonElement;
  expect(approve.title).toBe("Opens a draft asking VIKI to make this change.");

  vi.useFakeTimers();
  try {
    await click(approve);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(UNDO_MS);
    });
  } finally {
    vi.useRealTimers();
  }
  expect(api.requests).toContainEqual({
    method: "actChange",
    args: [LINK_ITEM.id, "approve", LINK_ITEM.revision, undefined],
  });
  expect(box().value).toBe(LINK_DRAFT);

  // Dirty the composer so the next check proves Draft again actually refills it, rather than
  // just finding the first draft's text still sitting there untouched.
  await setValue(box(), "a note the owner was mid-typing");
  expect(box().value).not.toBe(LINK_DRAFT);

  await click(screen.getByRole("button", { name: /^History/ }));
  row = section.querySelector(`[data-id="${LINK_ITEM.id}"]`);
  if (!(row instanceof HTMLElement)) throw new Error("the approved row is missing from History");
  expect(row.querySelector(".state-chip")?.textContent).toBe("Draft made");
  expect(
    within(row).getByText(
      "A draft was made. Draft again opens it once more. The app never changes the vault itself; only a chat you send can.",
    ),
  ).toBeTruthy();
  const draftAgain = within(row).getByRole("button", { name: "Draft again" }) as HTMLButtonElement;
  expect(draftAgain.title).toBe("Opens a draft asking VIKI to make this change.");
  expect((within(row).getByRole("button", { name: "Reject" }) as HTMLButtonElement).disabled).toBe(
    true,
  );

  vi.useFakeTimers();
  try {
    await click(draftAgain);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(UNDO_MS);
    });
  } finally {
    vi.useRealTimers();
  }
  expect(api.requests.filter((request) => request.method === "actChange")).toHaveLength(2);
  expect(box().value).toBe(LINK_DRAFT);
});

test("Reject takes a reason; the row records it and drafts nothing", async () => {
  const api = createFakeDecisionLoopApi({ change_act: { change: UPKEEP_ITEM, draft: null } });
  const decisions = fakeChangeInbox(api, [UPKEEP_ITEM]);
  await mountVault(api, decisions);
  await settled();

  const section = await openApprovals();
  const row = section.querySelector(`[data-id="${UPKEEP_ITEM.id}"]`);
  if (!(row instanceof HTMLElement)) throw new Error("the open row is missing");
  const reject = within(row).getByRole("button", { name: "Reject" }) as HTMLButtonElement;
  expect(reject.title).toBe("Rejects this change; nothing in the vault changes.");
  await click(reject);

  const dialog = await screen.findByRole("dialog", { name: UPKEEP_ITEM.title });
  await setValue(within(dialog).getByLabelText("Reason (optional)"), "Not needed right now");

  vi.useFakeTimers();
  try {
    await click(within(dialog).getByRole("button", { name: "Reject" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(UNDO_MS);
    });
  } finally {
    vi.useRealTimers();
  }

  expect(api.requests).toContainEqual({
    method: "actChange",
    args: [UPKEEP_ITEM.id, "reject", UPKEEP_ITEM.revision, "Not needed right now"],
  });

  await click(screen.getByRole("button", { name: /^History/ }));
  const rejectedRow = section.querySelector(`[data-id="${UPKEEP_ITEM.id}"]`);
  if (!(rejectedRow instanceof HTMLElement))
    throw new Error("the rejected row is missing from History");
  expect(rejectedRow.querySelector(".state-chip")?.textContent).toBe("Rejected");
  expect(
    within(rejectedRow).getByText(
      "Your rejection is recorded. The vault was not changed by that decision.",
    ),
  ).toBeTruthy();
  for (const button of rejectedRow.querySelectorAll<HTMLButtonElement>("[data-approval-action]"))
    expect(button.disabled).toBe(true);
});

test("DoRow shows today's vault-change count against the daily limit", async () => {
  const api = createFakeDecisionLoopApi({
    page: { ...decisionLoopExamples().page, changes: { today: 2, limit: 5 } },
  });
  await mountPlain(api);
  await settled();
  await openDecisionPage();
  const row = await expandRow("What VIKI may do");
  expect(within(row).getByText("Daily limit: vault changes")).toBeTruthy();
  expect(within(row).getByText("Today: 2 of 5 vault changes")).toBeTruthy();
});
