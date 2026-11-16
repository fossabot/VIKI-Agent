import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import {
  type DecisionsApi,
  DecisionsApiError,
  type ProposalDetail,
  type VaultProposalItem,
} from "../../state/decisionsApi";
import { proposalHandoffText } from "./handoff";
import { ProposalInspector } from "./ProposalInspector";

afterEach(() => {
  cleanup();
});

const HASH = "a".repeat(64);

function detail(over: Partial<ProposalDetail> = {}): ProposalDetail {
  return {
    kind: "vault_proposal",
    id: "p1",
    origin: { workspace_id: "w1", session_id: null },
    revision: "b".repeat(64),
    state: "open",
    attention: false,
    title: "notes/a.md",
    ref: "notes/a.md",
    origin_kind: "note_edit",
    base_revision: HASH,
    text_bytes: 10,
    handed_at: null,
    created_at: "2026-10-01T00:00:00.000Z",
    current_revision: HASH,
    proposed_text: "new text",
    diff: "-old\n+new",
    diff_reason: null,
    ...over,
  };
}

function setup(found: ProposalDetail, active: string | null = "w1") {
  const proposalDetail = vi.fn<DecisionsApi["proposalDetail"]>().mockResolvedValue(found);
  const proposalAct = vi.fn<DecisionsApi["proposalAct"]>();
  const api = { proposalDetail, proposalAct } as unknown as DecisionsApi;
  const props = {
    onClose: vi.fn(),
    onChanged: vi.fn(),
    onHandoff: vi.fn(),
    onOpenEditor: vi.fn(),
  };
  render(
    <ProposalInspector
      api={api}
      item={found}
      workspaceName="Notes"
      activeWorkspaceId={active}
      {...props}
    />,
  );
  return { proposalDetail, proposalAct, ...props };
}

const settled = () => act(async () => {});
const button = (name: string) => screen.getByRole("button", { name }) as HTMLButtonElement;
const HANDOFF = "Create a draft";
const EDITOR = "Open in the editor";

test("it is a labelled modal that shows the ref, the vault name, the origin and the diff", async () => {
  setup(detail());
  await settled();
  const dialog = screen.getByRole("dialog");
  expect(dialog.getAttribute("aria-modal")).toBe("true");
  const label = document.getElementById(dialog.getAttribute("aria-labelledby") ?? "");
  expect(label?.textContent).toBe("notes/a.md");
  expect(dialog.textContent).toContain("Notes · Note edit");
  expect(dialog.querySelector("pre")?.textContent).toBe("-old\n+new");
  expect(dialog.querySelector("pre")?.style.overflow).toBe("auto");
  expect(document.querySelector(".state-chip")?.textContent).toBe("Proposed");
});

test("a diff holding script tags and markdown stays literal text", async () => {
  setup(detail({ diff: "+<script>alert(1)</script>\n+<img src=x onerror=1>\n+# **bold**" }));
  await settled();
  const dialog = screen.getByRole("dialog");
  expect(dialog.querySelector("script, img, b, strong, h1")).toBeNull();
  expect(dialog.querySelector("pre")?.textContent).toBe(
    "+<script>alert(1)</script>\n+<img src=x onerror=1>\n+# **bold**",
  );
});

test("a too-large diff shows the one line and no pre", async () => {
  setup(detail({ diff: "", diff_reason: "too_large" }));
  await settled();
  expect(screen.getByText("The diff is too large to show.")).toBeTruthy();
  expect(document.querySelector("pre")).toBeNull();
});

test.each([
  ["open", "w1", true, true],
  ["handed", "w1", true, true],
  ["stale", "w1", false, true],
  ["applied", "w1", false, true],
  ["source_missing", "w1", false, true],
  ["unknown", "w1", false, true],
  ["open", "w2", false, false],
  ["handed", null, false, false],
] as const)(
  "a %s proposal with active vault %s: draft enabled %s, editor enabled %s",
  async (state, active, draft, editor) => {
    setup(detail({ state }), active);
    await settled();
    expect(button(HANDOFF).disabled).toBe(!draft);
    expect(button(EDITOR).disabled).toBe(!editor);
    expect(button("Copy the proposal").disabled).toBe(false);
    expect(button("Withdraw the proposal").disabled).toBe(false);
  },
);

test("another vault's proposal says why its actions are off", async () => {
  setup(detail(), "w2");
  await settled();
  expect(
    screen.getByText(
      "This is not a proposal for the vault that is open, so this action is unavailable.",
    ),
  ).toBeTruthy();
});

test("hand-off records it, then passes the message on, and shows the returned state", async () => {
  const found = detail();
  const { proposalAct, onHandoff, onChanged } = setup(found);
  const next = { ...found, state: "handed" as VaultProposalItem["state"] };
  proposalAct.mockResolvedValue(next);
  await settled();
  fireEvent.click(button(HANDOFF));
  await settled();
  expect(proposalAct).toHaveBeenCalledWith("p1", "handoff");
  expect(onChanged).toHaveBeenCalledWith(next);
  expect(onHandoff).toHaveBeenCalledWith(proposalHandoffText(found));
  expect(document.querySelector(".state-chip")?.textContent).toBe("Draft created");
});

test("a failed hand-off passes nothing on and says so", async () => {
  const { proposalAct, onHandoff } = setup(detail());
  proposalAct.mockRejectedValue(new DecisionsApiError("unavailable"));
  await settled();
  fireEvent.click(button(HANDOFF));
  await settled();
  expect(onHandoff).not.toHaveBeenCalled();
  expect(screen.getByRole("status").textContent).toBe("Could not do that.");
});

test("the editor button hands the loaded proposal to the caller", async () => {
  const found = detail();
  const { onOpenEditor } = setup(found);
  await settled();
  fireEvent.click(button(EDITOR));
  expect(onOpenEditor).toHaveBeenCalledWith(found, screen.getByRole("dialog"));
});

test("a withdraw asks first, and cancelling sends nothing", async () => {
  const { proposalAct, onChanged } = setup(detail());
  await settled();
  fireEvent.click(button("Withdraw the proposal"));
  expect(proposalAct).not.toHaveBeenCalled();
  expect(screen.getByText(/Withdraw this proposal\?/)).toBeTruthy();
  fireEvent.click(button("Cancel"));
  expect(proposalAct).not.toHaveBeenCalled();
  expect(onChanged).not.toHaveBeenCalled();
  expect(screen.queryByText(/Withdraw this proposal\?/)).toBeNull();
});

test("confirming a withdraw sends it, reports null and closes", async () => {
  const { proposalAct, onChanged, onClose } = setup(detail());
  proposalAct.mockResolvedValue(null);
  await settled();
  fireEvent.click(button("Withdraw the proposal"));
  fireEvent.click(button("Withdraw"));
  await settled();
  expect(proposalAct).toHaveBeenCalledWith("p1", "withdraw");
  expect(onChanged).toHaveBeenCalledWith(null);
  expect(onClose).toHaveBeenCalled();
});

test("a load failure says so and offers no actions", async () => {
  const proposalDetail = vi
    .fn<DecisionsApi["proposalDetail"]>()
    .mockRejectedValue(new DecisionsApiError("unavailable"));
  const api = { proposalDetail } as unknown as DecisionsApi;
  render(
    <ProposalInspector
      api={api}
      item={detail()}
      workspaceName={null}
      activeWorkspaceId="w1"
      onClose={vi.fn()}
      onChanged={vi.fn()}
      onHandoff={vi.fn()}
      onOpenEditor={vi.fn()}
    />,
  );
  await settled();
  expect(screen.getByText("Could not load the item.")).toBeTruthy();
  expect(screen.queryByRole("button", { name: HANDOFF })).toBeNull();
});

test("a memory_promotion proposal shows its origin label, not the raw code", async () => {
  setup(detail({ origin_kind: "memory_promotion" }));
  await settled();
  const text = screen.getByRole("dialog").textContent;
  expect(text).toContain("Notes · Memory proposal");
  expect(text).not.toContain("memory_promotion");
});
