import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { SkillProposalDetail, SkillProposalsApi } from "../../state/skillProposalsApi";
import { SkillProposalsApiError, skillRefusal } from "../../state/skillProposalsApi";
import { SkillProposalInspector } from "./SkillProposalInspector";
import { applyHandoffText, trialHandoffText } from "./skillHandoff";

afterEach(cleanup);

const HASH = "a".repeat(64);

function detail(over: Partial<SkillProposalDetail> = {}): SkillProposalDetail {
  return {
    kind: "skill_proposal",
    id: "p1",
    origin: { workspace_id: "w1", session_id: null },
    revision: "b".repeat(64),
    state: "open",
    attention: false,
    title: "My Skill",
    name: "my-skill",
    relative_id: "skills/my-skill/SKILL.md",
    trigger: "button",
    sessions: ["s1"],
    base_revision: null,
    text_bytes: 20,
    approved_at: null,
    rejected_at: null,
    reason: null,
    created_at: "2026-10-01T00:00:00.000Z",
    updated_at: "2026-10-01T00:00:00.000Z",
    text: "---\nname: my-skill\n---\nBody",
    current_text: null,
    diff: "-old\n+new",
    diff_reason: null,
    description: "Does a thing well.",
    unsupported_fields: [],
    shadowed: false,
    policy: "ordinary",
    ...over,
  };
}

function setup(found: SkillProposalDetail, active: string | null = "w1") {
  const detailFn = vi.fn<SkillProposalsApi["detail"]>().mockResolvedValue(found);
  const actFn = vi.fn<SkillProposalsApi["act"]>();
  const api = { detail: detailFn, act: actFn } as unknown as SkillProposalsApi;
  const props = {
    onClose: vi.fn(),
    onChanged: vi.fn(),
    onHandoff: vi.fn(),
    onOpenChat: vi.fn(),
  };
  render(
    <SkillProposalInspector
      api={api}
      item={found}
      activeWorkspaceId={active}
      sessionLabel={(id: string) => `Session ${id}`}
      {...props}
    />,
  );
  return { detailFn, actFn, ...props };
}

const settled = () => act(async () => {});
const button = (name: string) => screen.getByRole("button", { name }) as HTMLButtonElement;

test("shows the name, state chip, path, description, trigger, session and the diff", async () => {
  setup(detail());
  await settled();
  const dialog = screen.getByRole("dialog");
  const label = document.getElementById(dialog.getAttribute("aria-labelledby") ?? "");
  expect(label?.textContent).toBe("my-skill");
  expect(document.querySelector(".state-chip")?.textContent).toBe("Proposed");
  expect(screen.getByText("New skill")).toBeTruthy();
  expect(screen.getByText(".claude/skills/my-skill/SKILL.md")).toBeTruthy();
  expect(screen.getByText("What it does")).toBeTruthy();
  expect(screen.getByText("Does a thing well.")).toBeTruthy();
  expect(screen.getByText("Where it came from")).toBeTruthy();
  expect(screen.getByText("You asked for it")).toBeTruthy();
  expect(screen.getByText("Session s1")).toBeTruthy();
  expect(button("Open chat")).toBeTruthy();
  expect(screen.getByText("The file")).toBeTruthy();
  expect(dialog.querySelector("pre")?.textContent).toBe("-old\n+new");
  expect(dialog.querySelector("pre")?.style.overflow).toBe("auto");
});

test("an existing skill says it changes the vault's skill, not that it's new", async () => {
  setup(detail({ base_revision: HASH }));
  await settled();
  expect(screen.getByText("Changes the vault's skill")).toBeTruthy();
  expect(screen.queryByText("New skill")).toBeNull();
});

test("a load failure shows the message with Retry, which reloads", async () => {
  const found = detail();
  const detailFn = vi
    .fn<SkillProposalsApi["detail"]>()
    .mockRejectedValueOnce(new SkillProposalsApiError("unavailable"))
    .mockResolvedValueOnce(found);
  const api = { detail: detailFn, act: vi.fn() } as unknown as SkillProposalsApi;
  render(
    <SkillProposalInspector
      api={api}
      item={found}
      activeWorkspaceId="w1"
      sessionLabel={(id) => id}
      onClose={vi.fn()}
      onChanged={vi.fn()}
      onHandoff={vi.fn()}
      onOpenChat={vi.fn()}
    />,
  );
  await settled();
  expect(screen.getByText("Could not read this proposal.")).toBeTruthy();
  fireEvent.click(button("Retry"));
  await settled();
  expect(detailFn).toHaveBeenCalledTimes(2);
  expect(screen.queryByText("Could not read this proposal.")).toBeNull();
  expect(screen.getByText("What it does")).toBeTruthy();
});

test.each([
  ["too_large", "Too large to compare line by line."],
  ["unreadable", "Could not read the installed file."],
  ["no_source", null],
] as const)("diff_reason %s shows the full text, not the diff", async (reason, caption) => {
  setup(detail({ diff_reason: reason, text: "FULLTEXT", diff: "DIFFTEXT" }));
  await settled();
  if (caption) expect(screen.getByText(caption)).toBeTruthy();
  expect(screen.getByRole("dialog").querySelector("pre")?.textContent).toBe("FULLTEXT");
});

test("a null diff_reason shows the diff, not the full text", async () => {
  setup(detail({ diff_reason: null, text: "FULLTEXT", diff: "DIFFTEXT" }));
  await settled();
  expect(screen.getByRole("dialog").querySelector("pre")?.textContent).toBe("DIFFTEXT");
});

test.each([
  [
    true,
    "A personal skill with this name is in your Claude folder. Claude Code runs the personal one, so this one would not run.",
  ],
  [null, "Could not check your personal skills."],
] as const)("shadowed %s shows its warning", async (shadowed, text) => {
  setup(detail({ shadowed }));
  await settled();
  expect(screen.getByText(text)).toBeTruthy();
});

test("shadowed false shows no shadow warning", async () => {
  setup(detail({ shadowed: false }));
  await settled();
  expect(screen.queryByText(/personal skill/)).toBeNull();
  expect(screen.queryByText("Could not check your personal skills.")).toBeNull();
});

test("unsupported fields are listed", async () => {
  setup(detail({ unsupported_fields: ["a", "b"] }));
  await settled();
  expect(screen.getByText("Fields VIKI Agent did not check: a, b.")).toBeTruthy();
});

test.each([
  [
    "stale",
    null,
    "The skill changed since this proposal was made. Edit it to rebase on the current file.",
  ],
  ["rejected", null, "Rejected."],
  ["rejected", "not helpful", "Rejected: not helpful."],
  ["handed", null, "Approved. It applies when you send the apply draft and VIKI writes the file."],
  ["applied", null, "The skill file matches this proposal."],
] as const)("state %s (reason %s) shows its policy text", async (state, reason, text) => {
  setup(detail({ state, reason }));
  await settled();
  expect(screen.getByText(text)).toBeTruthy();
});

test("Try in a new chat hands off a dry run and calls no api", async () => {
  const found = detail();
  const { onHandoff, actFn } = setup(found);
  await settled();
  fireEvent.click(button("Try in a new chat"));
  expect(onHandoff).toHaveBeenCalledWith(trialHandoffText(found));
  expect(actFn).not.toHaveBeenCalled();
});

test("Try in a new chat while the edit draft is dirty asks once, and hands off nothing on no", async () => {
  const found = detail({ state: "open", text: "v1" });
  const { onHandoff, actFn } = setup(found);
  await settled();
  fireEvent.click(button("Edit"));
  fireEvent.change(screen.getByLabelText("SKILL.md"), { target: { value: "v2" } });
  const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
  fireEvent.click(button("Try in a new chat"));
  expect(confirmSpy).toHaveBeenCalledOnce();
  expect(onHandoff).not.toHaveBeenCalled();
  expect(actFn).not.toHaveBeenCalled();
  confirmSpy.mockRestore();
});

test("offers Copy SKILL.md alongside Try in a new chat", async () => {
  setup(detail());
  await settled();
  expect(screen.getByRole("button", { name: "Copy SKILL.md" })).toBeTruthy();
});

test("Open chat hands the session to the caller with the dialog as origin", async () => {
  const found = detail({ sessions: ["s9"] });
  const { onOpenChat } = setup(found);
  await settled();
  fireEvent.click(button("Open chat"));
  expect(onOpenChat).toHaveBeenCalledWith("s9", screen.getByRole("dialog"));
});

test("Approve acts, reports changed, and hands off the apply draft", async () => {
  const found = detail({ state: "open" });
  const { actFn, onChanged, onHandoff } = setup(found);
  actFn.mockResolvedValue(null);
  await settled();
  fireEvent.click(button("Approve"));
  await settled();
  expect(actFn).toHaveBeenCalledWith(found.id, "approve", found.revision);
  expect(onChanged).toHaveBeenCalledOnce();
  expect(onHandoff).toHaveBeenCalledWith(applyHandoffText(found));
});

test("Approve after Escape keeps a dirty draft asks once, and hands off nothing on no", async () => {
  const found = detail({ state: "open", text: "v1" });
  const { actFn, onHandoff } = setup(found);
  await settled();
  fireEvent.click(button("Edit"));
  fireEvent.change(screen.getByLabelText("SKILL.md"), { target: { value: "v2" } });
  fireEvent.keyDown(window, { key: "Escape" });
  const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
  fireEvent.click(button("Approve"));
  expect(confirmSpy).toHaveBeenCalledOnce();
  expect(onHandoff).not.toHaveBeenCalled();
  expect(actFn).not.toHaveBeenCalled();
  confirmSpy.mockRestore();
});

test("Edit saves the draft, reloads the detail and reports changed", async () => {
  const found = detail({ state: "open", text: "v1" });
  const { actFn, detailFn, onChanged } = setup(found);
  await settled();
  const reloaded = detail({ state: "open", text: "v2" });
  detailFn.mockResolvedValueOnce(reloaded);
  actFn.mockResolvedValue(null);
  fireEvent.click(button("Edit"));
  fireEvent.change(screen.getByLabelText("SKILL.md"), { target: { value: "v2" } });
  fireEvent.click(button("Save changes"));
  await settled();
  expect(actFn).toHaveBeenCalledWith(found.id, "edit", found.revision, "v2");
  expect(detailFn).toHaveBeenCalledTimes(2);
  expect(onChanged).toHaveBeenCalledOnce();
});

test("Reject sends the trimmed reason, reloads and reports changed", async () => {
  const found = detail({ state: "open" });
  const { actFn, detailFn, onChanged } = setup(found);
  await settled();
  const reloaded = detail({ state: "rejected", reason: "not helpful" });
  detailFn.mockResolvedValueOnce(reloaded);
  actFn.mockResolvedValue(null);
  fireEvent.click(button("Reject"));
  fireEvent.change(screen.getByLabelText("Reason (optional)"), {
    target: { value: "not helpful" },
  });
  fireEvent.click(button("Reject"));
  await settled();
  expect(actFn).toHaveBeenCalledWith(found.id, "reject", found.revision, undefined, "not helpful");
  expect(detailFn).toHaveBeenCalledTimes(2);
  expect(onChanged).toHaveBeenCalledOnce();
});

test("Reject with a blank reason sends no reason to the server", async () => {
  const found = detail({ state: "open" });
  const { actFn } = setup(found);
  actFn.mockResolvedValue(null);
  await settled();
  fireEvent.click(button("Reject"));
  fireEvent.click(button("Reject"));
  await settled();
  expect(actFn).toHaveBeenCalledWith(found.id, "reject", found.revision, undefined, undefined);
});

test("Withdraw confirms, acts, reports changed and closes", async () => {
  const found = detail({ state: "open" });
  const { actFn, onChanged, onClose } = setup(found);
  actFn.mockResolvedValue(null);
  await settled();
  fireEvent.click(button("Withdraw"));
  fireEvent.click(button("Withdraw"));
  await settled();
  expect(actFn).toHaveBeenCalledWith(found.id, "withdraw", found.revision);
  expect(onChanged).toHaveBeenCalledOnce();
  expect(onClose).toHaveBeenCalledOnce();
});

test("a failed action shows the server's refusal as status text", async () => {
  const found = detail({ state: "open" });
  const { actFn } = setup(found);
  actFn.mockRejectedValue(new SkillProposalsApiError("not_active"));
  await settled();
  fireEvent.click(button("Approve"));
  await settled();
  expect(screen.getByRole("status").textContent).toBe(
    skillRefusal(new SkillProposalsApiError("not_active")),
  );
});

test("another vault's proposal explains why actions are off and disables them", async () => {
  setup(detail(), "w2");
  await settled();
  expect(screen.getByText("Open this proposal's vault to act on it.")).toBeTruthy();
  expect(button("Approve").disabled).toBe(true);
  expect(button("Withdraw").disabled).toBe(true);
});

test("a description holding a tag renders literally, never as markup", async () => {
  setup(detail({ description: "Wraps steps in <b>bold</b> text." }));
  await settled();
  const dialog = screen.getByRole("dialog");
  expect(dialog.querySelector("b")).toBeNull();
  expect(screen.getByText("Wraps steps in <b>bold</b> text.")).toBeTruthy();
});

test("Escape while the edit draft is dirty leaves edit mode instead of closing the dialog", async () => {
  const found = detail({ state: "open", text: "v1" });
  const { onClose } = setup(found);
  await settled();
  fireEvent.click(button("Edit"));
  fireEvent.change(screen.getByLabelText("SKILL.md"), { target: { value: "v2" } });
  fireEvent.keyDown(window, { key: "Escape" });
  expect(onClose).not.toHaveBeenCalled();
  expect(screen.getByRole("dialog")).toBeTruthy();
  expect(screen.queryByLabelText("SKILL.md")).toBeNull();
  expect(button("Edit")).toBeTruthy();
});

test("Escape while not editing still closes the dialog", async () => {
  const { onClose } = setup(detail({ state: "open" }));
  await settled();
  fireEvent.keyDown(window, { key: "Escape" });
  expect(onClose).toHaveBeenCalledOnce();
});

test("Open chat while the edit draft is dirty asks first, and backs out when declined", async () => {
  const found = detail({ state: "open", text: "v1" });
  const { onOpenChat } = setup(found);
  await settled();
  fireEvent.click(button("Edit"));
  fireEvent.change(screen.getByLabelText("SKILL.md"), { target: { value: "v2" } });
  const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
  fireEvent.click(button("Open chat"));
  expect(confirmSpy).toHaveBeenCalledOnce();
  expect(onOpenChat).not.toHaveBeenCalled();
  confirmSpy.mockRestore();
});

test("Open chat while the edit draft is dirty proceeds once the user confirms", async () => {
  const found = detail({ state: "open", text: "v1" });
  const { onOpenChat } = setup(found);
  await settled();
  fireEvent.click(button("Edit"));
  fireEvent.change(screen.getByLabelText("SKILL.md"), { target: { value: "v2" } });
  const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
  fireEvent.click(button("Open chat"));
  expect(onOpenChat).toHaveBeenCalledWith("s1", screen.getByRole("dialog"));
  confirmSpy.mockRestore();
});

test("Close while the edit draft is dirty asks once, and declining keeps the dialog and the draft", async () => {
  const found = detail({ state: "open", text: "v1" });
  const { onClose } = setup(found);
  await settled();
  fireEvent.click(button("Edit"));
  fireEvent.change(screen.getByLabelText("SKILL.md"), { target: { value: "v2" } });
  const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
  fireEvent.click(button("Close"));
  expect(confirmSpy).toHaveBeenCalledOnce();
  expect(onClose).not.toHaveBeenCalled();
  expect(screen.getByRole("dialog")).toBeTruthy();
  expect((screen.getByLabelText("SKILL.md") as HTMLTextAreaElement).value).toBe("v2");
  confirmSpy.mockRestore();
});

test("a clean Close never asks", async () => {
  const { onClose } = setup(detail({ state: "open" }));
  await settled();
  const confirmSpy = vi.spyOn(window, "confirm");
  fireEvent.click(button("Close"));
  expect(confirmSpy).not.toHaveBeenCalled();
  expect(onClose).toHaveBeenCalledOnce();
  confirmSpy.mockRestore();
});

test("entering edit mode without changing the draft still lets Close through", async () => {
  const { onClose } = setup(detail({ state: "open", text: "v1" }));
  await settled();
  fireEvent.click(button("Edit"));
  const confirmSpy = vi.spyOn(window, "confirm");
  fireEvent.click(button("Close"));
  expect(confirmSpy).not.toHaveBeenCalled();
  expect(onClose).toHaveBeenCalledOnce();
  confirmSpy.mockRestore();
});

test("Cancel clears the draft, so a later Close never asks", async () => {
  const { onClose } = setup(detail({ state: "open", text: "v1" }));
  await settled();
  fireEvent.click(button("Edit"));
  fireEvent.change(screen.getByLabelText("SKILL.md"), { target: { value: "v2" } });
  fireEvent.click(button("Cancel"));
  const confirmSpy = vi.spyOn(window, "confirm");
  fireEvent.click(button("Close"));
  expect(confirmSpy).not.toHaveBeenCalled();
  expect(onClose).toHaveBeenCalledOnce();
  confirmSpy.mockRestore();
});

test("Escape then Close asks once, and declining keeps the dialog and the kept draft", async () => {
  const found = detail({ state: "open", text: "v1" });
  const { onClose } = setup(found);
  await settled();
  fireEvent.click(button("Edit"));
  fireEvent.change(screen.getByLabelText("SKILL.md"), { target: { value: "v2" } });
  fireEvent.keyDown(window, { key: "Escape" });
  expect(onClose).not.toHaveBeenCalled();
  expect(screen.queryByLabelText("SKILL.md")).toBeNull();

  const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
  fireEvent.click(button("Close"));
  expect(confirmSpy).toHaveBeenCalledOnce();
  expect(onClose).not.toHaveBeenCalled();
  expect(screen.getByRole("dialog")).toBeTruthy();
  confirmSpy.mockRestore();

  // The kept draft is still there once the user goes back into Edit.
  fireEvent.click(button("Edit"));
  expect((screen.getByLabelText("SKILL.md") as HTMLTextAreaElement).value).toBe("v2");
});

test("Escape then Escape asks once, and declining keeps the dialog and the kept draft", async () => {
  const found = detail({ state: "open", text: "v1" });
  const { onClose } = setup(found);
  await settled();
  fireEvent.click(button("Edit"));
  fireEvent.change(screen.getByLabelText("SKILL.md"), { target: { value: "v2" } });
  fireEvent.keyDown(window, { key: "Escape" });
  expect(onClose).not.toHaveBeenCalled();
  expect(screen.queryByLabelText("SKILL.md")).toBeNull();

  const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
  fireEvent.keyDown(window, { key: "Escape" });
  expect(confirmSpy).toHaveBeenCalledOnce();
  expect(onClose).not.toHaveBeenCalled();
  expect(screen.getByRole("dialog")).toBeTruthy();
  confirmSpy.mockRestore();

  // The kept draft is still there once the user goes back into Edit.
  fireEvent.click(button("Edit"));
  expect((screen.getByLabelText("SKILL.md") as HTMLTextAreaElement).value).toBe("v2");
});
