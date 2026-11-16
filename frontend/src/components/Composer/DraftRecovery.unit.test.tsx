import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { CommandReceipt, DurableApi } from "../../clients/conversations/durableApi";
import { commandState, DraftRecovery } from "./DraftRecovery";

afterEach(cleanup);

const DRAFT_ID = "11111111-1111-4111-8111-111111111111";
const NEXT_DRAFT = "22222222-2222-4222-8222-222222222222";
const COMMAND_ID = "33333333-3333-4333-8333-333333333333";
const NEXT_COMMAND = "44444444-4444-4444-8444-444444444444";
const GENERATION = "55555555-5555-4555-8555-555555555555";
const NOW = "2026-09-14T08:00:00.000Z";

function receipt(overrides: Partial<CommandReceipt> = {}): CommandReceipt {
  return {
    command_id: COMMAND_ID,
    session_id: DRAFT_ID,
    generation: GENERATION,
    draft_revision: 2,
    state: "accepted",
    reason: "",
    created_at: NOW,
    updated_at: NOW,
    bytes: 9,
    ...overrides,
  };
}

function api(): DurableApi {
  return {
    listDrafts: vi.fn(async () => ({
      drafts: [{ key: DRAFT_ID, revision: 2, saved_at: NOW, bytes: 9 }],
      next_after: null,
    })),
    getDraft: vi.fn(async () => ({
      key: DRAFT_ID,
      revision: 2,
      saved_at: NOW,
      text: "Saved text",
    })),
    saveDraft: vi.fn(),
    clearDraft: vi.fn(),
    getTarget: vi.fn(),
    getDraftCommand: vi.fn(async () => null),
    submitCommand: vi.fn(),
    getCommand: vi.fn(async () => receipt({ state: "unknown", reason: "native_unconfirmed" })),
    listCommands: vi.fn(async () => ({ commands: [receipt()], next_after: null })),
    getSubmission: vi.fn(async () => ({ ...receipt(), text: "Sent text" })),
    findApproval: vi.fn(),
    recordApproval: vi.fn(),
    getApproval: vi.fn(),
    getApprovalChoice: vi.fn(),
    listApprovals: vi.fn(),
  };
}

async function open(): Promise<void> {
  fireEvent.click(screen.getByText("Drafts and send history"));
  await waitFor(() => expect(screen.getByLabelText("Saved drafts")).toBeTruthy());
  await screen.findByLabelText(`Restore text from draft ${DRAFT_ID}`);
}

test("opening lists metadata only; each exact body requires an explicit recovery click", async () => {
  const durable = api();
  const onRestore = vi.fn();
  render(<DraftRecovery api={durable} onRestore={onRestore} />);
  expect(durable.listDrafts).not.toHaveBeenCalled();
  expect(durable.listCommands).not.toHaveBeenCalled();
  await open();
  await screen.findByLabelText(`Restore text from send ${COMMAND_ID}`);
  expect(durable.getDraft).not.toHaveBeenCalled();
  expect(durable.getSubmission).not.toHaveBeenCalled();
  expect(durable.submitCommand).not.toHaveBeenCalled();

  fireEvent.click(screen.getByLabelText(`Restore text from draft ${DRAFT_ID}`));
  await waitFor(() => expect(onRestore).toHaveBeenCalledWith("Saved text"));
  fireEvent.click(screen.getByLabelText(`Restore text from send ${COMMAND_ID}`));
  await waitFor(() => expect(onRestore).toHaveBeenCalledWith("Sent text"));
  expect(onRestore).toHaveBeenCalledTimes(2);
  expect(durable.submitCommand).not.toHaveBeenCalled();
});

test("draft revision drift and read failure preserve the list and never restore text", async () => {
  const durable = api();
  const onRestore = vi.fn();
  vi.mocked(durable.getDraft).mockResolvedValueOnce({
    key: DRAFT_ID,
    revision: 3,
    saved_at: NOW,
    text: "Edit from another window",
  });
  render(<DraftRecovery api={durable} onRestore={onRestore} />);
  await open();
  fireEvent.click(screen.getByLabelText(`Restore text from draft ${DRAFT_ID}`));
  expect(
    await screen.findByText("The saved draft changed. Refresh the list and select it again."),
  ).toBeTruthy();
  expect(onRestore).not.toHaveBeenCalled();
  expect(screen.getByLabelText(`Restore text from draft ${DRAFT_ID}`)).toBeTruthy();

  vi.mocked(durable.getDraft).mockRejectedValueOnce(new Error("secret plaintext"));
  fireEvent.click(screen.getByLabelText(`Restore text from draft ${DRAFT_ID}`));
  expect(
    await screen.findByText("Couldn't read the draft text. Your current input is still available."),
  ).toBeTruthy();
  expect(screen.queryByText("secret plaintext")).toBeNull();
  expect(onRestore).not.toHaveBeenCalled();
});

test("next pages use the server cursor, and a failed page keeps the current records", async () => {
  const durable = api();
  vi.mocked(durable.listDrafts)
    .mockResolvedValueOnce({
      drafts: [{ key: DRAFT_ID, revision: 2, saved_at: NOW, bytes: 9 }],
      next_after: DRAFT_ID,
    })
    .mockRejectedValueOnce(new Error("sensitive server failure"))
    .mockResolvedValueOnce({
      drafts: [{ key: NEXT_DRAFT, revision: 1, saved_at: NOW, bytes: 4 }],
      next_after: null,
    });
  vi.mocked(durable.listCommands)
    .mockResolvedValueOnce({ commands: [receipt()], next_after: COMMAND_ID })
    .mockResolvedValueOnce({
      commands: [receipt({ command_id: NEXT_COMMAND, state: "dispatching" })],
      next_after: null,
    });
  render(<DraftRecovery api={durable} onRestore={vi.fn()} />);
  await open();
  const drafts = screen.getByLabelText("Saved drafts");
  const commands = screen.getByLabelText("Send history");
  fireEvent.click(within(drafts).getByText("Next page"));
  await screen.findByText("Couldn't read the saved drafts.");
  expect(within(drafts).getByLabelText(`Restore text from draft ${DRAFT_ID}`)).toBeTruthy();
  expect(vi.mocked(durable.listDrafts)).toHaveBeenLastCalledWith({ after: DRAFT_ID });
  fireEvent.click(within(drafts).getByText("Next page"));
  await within(drafts).findByLabelText(`Restore text from draft ${NEXT_DRAFT}`);
  expect(within(drafts).queryByLabelText(`Restore text from draft ${DRAFT_ID}`)).toBeNull();

  fireEvent.click(within(commands).getByText("Next page"));
  await within(commands).findByLabelText(`Restore text from send ${NEXT_COMMAND}`);
  expect(vi.mocked(durable.listCommands)).toHaveBeenLastCalledWith({ after: COMMAND_ID });
  expect(
    within(commands).getByText("Dispatch started. Arrival at Claude Code is unconfirmed."),
  ).toBeTruthy();
});

test("a not_dispatched attachment refusal points at the submission's own recovery, not the draft", async () => {
  // Acceptance already cleared the draft, so both attachment reasons must say the text is
  // in Send history's own Restore text, never the generic not-sent line the other reasons
  // still get.
  expect(commandState(receipt({ state: "not_dispatched", reason: "unsupported_attachment" }))).toBe(
    "An attached image could not be used, so this was not sent to Claude Code. Restore the text from Drafts and send history → Send history → Restore text.",
  );
  expect(commandState(receipt({ state: "not_dispatched", reason: "attachment_unavailable" }))).toBe(
    "An attached image could not be used, so this was not sent to Claude Code. Restore the text from Drafts and send history → Send history → Restore text.",
  );
  expect(commandState(receipt({ state: "not_dispatched", reason: "cancelled" }))).toBe(
    "Not sent to Claude Code.",
  );

  const durable = api();
  vi.mocked(durable.listCommands).mockResolvedValueOnce({
    commands: [receipt({ state: "not_dispatched", reason: "attachment_unavailable" })],
    next_after: null,
  });
  render(<DraftRecovery api={durable} onRestore={vi.fn()} />);
  await open();
  expect(
    screen.getByText(
      "An attached image could not be used, so this was not sent to Claude Code. Restore the text from Drafts and send history → Send history → Restore text.",
    ),
  ).toBeTruthy();
});

test("every excerpt refusal names the quote and points at the submission's own recovery", async () => {
  // All five of the service's excerpt reasons (REASONS in src/viki_agent/conversations/commands.py)
  // must not fall through to the bare not-sent line, which says nothing about the quote or where
  // the text now lives.
  const EXCERPT_LINE =
    "An excerpt could not be used, so this was not sent to Claude Code. Restore the text from Drafts and send history → Send history → Restore text.";
  for (const reason of [
    "excerpt_with_command",
    "excerpt_wrong_vault",
    "excerpt_stale",
    "excerpt_unavailable",
    "excerpts_too_large",
  ])
    expect(commandState(receipt({ state: "not_dispatched", reason }))).toBe(EXCERPT_LINE);
  // Only a refused state carries it: the same reason on an unknown receipt keeps its own line.
  expect(commandState(receipt({ state: "unknown", reason: "excerpt_stale" }))).toBe(
    "Arrival at Claude Code and the execution result are unconfirmed.",
  );

  const durable = api();
  vi.mocked(durable.listCommands).mockResolvedValueOnce({
    commands: [receipt({ state: "not_dispatched", reason: "excerpt_stale" })],
    next_after: null,
  });
  render(<DraftRecovery api={durable} onRestore={vi.fn()} />);
  await open();
  expect(screen.getByText(EXCERPT_LINE)).toBeTruthy();
});

test("status refresh changes only the chosen receipt; malicious metadata stays text", async () => {
  const durable = api();
  vi.mocked(durable.listDrafts).mockResolvedValueOnce({
    drafts: [{ key: DRAFT_ID, revision: 2, saved_at: "<img src=x onerror=alert(1)>", bytes: 9 }],
    next_after: null,
  });
  render(<DraftRecovery api={durable} onRestore={vi.fn()} />);
  await open();
  expect(screen.getByText(/<img src=x onerror=alert\(1\)>/)).toBeTruthy();
  expect(document.querySelector("img")).toBeNull();
  expect(screen.getByText("Accepted. The text is saved.")).toBeTruthy();
  await act(async () => {
    fireEvent.click(screen.getByLabelText(`Refresh status for send ${COMMAND_ID}`));
  });
  expect(
    screen.getByText("Arrival at Claude Code and the execution result are unconfirmed."),
  ).toBeTruthy();
  expect(durable.getSubmission).not.toHaveBeenCalled();
  expect(durable.submitCommand).not.toHaveBeenCalled();
});

test("every command record this panel reads is reported to the caller", async () => {
  // The submission POST answers before the dispatch runs, so these reads are where App learns a
  // dispatch outcome — an excerpt refusal among them — and takes its chips back.
  const durable = api();
  const onCommandState = vi.fn();
  render(<DraftRecovery api={durable} onRestore={vi.fn()} onCommandState={onCommandState} />);
  await open();
  expect(onCommandState).toHaveBeenCalledWith(receipt());
  await act(async () => {
    fireEvent.click(screen.getByLabelText(`Refresh status for send ${COMMAND_ID}`));
  });
  expect(onCommandState).toHaveBeenLastCalledWith(
    receipt({ state: "unknown", reason: "native_unconfirmed" }),
  );
});

test("pending and empty lists use English without reading or sending saved text", async () => {
  const durable = api();
  let drafts: () => void = () => {};
  let commands: () => void = () => {};
  vi.mocked(durable.listDrafts).mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        drafts = () => resolve({ drafts: [], next_after: null });
      }),
  );
  vi.mocked(durable.listCommands).mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        commands = () => resolve({ commands: [], next_after: null });
      }),
  );
  render(<DraftRecovery api={durable} onRestore={vi.fn()} />);
  fireEvent.click(screen.getByText("Drafts and send history"));
  await screen.findAllByText("Loading…");
  expect(screen.getAllByRole("status")).toHaveLength(2);
  expect(
    screen.getByText(
      "Restored text opens in a new message box. Your current input stays available.",
    ),
  ).toBeTruthy();
  expect(durable.getDraft).not.toHaveBeenCalled();
  expect(durable.getSubmission).not.toHaveBeenCalled();
  expect(durable.submitCommand).not.toHaveBeenCalled();
  await act(async () => {
    drafts();
    commands();
  });
  expect(screen.getByText("No saved drafts.")).toBeTruthy();
  expect(screen.getByText("No send history.")).toBeTruthy();
  expect(screen.queryByRole("status")).toBeNull();
});

test("failed explicit reads retain the receipt and display English without restoring or sending", async () => {
  const durable = api();
  const onRestore = vi.fn();
  vi.mocked(durable.getSubmission).mockRejectedValueOnce(new Error("private submitted text"));
  vi.mocked(durable.getCommand).mockRejectedValueOnce(new Error("private receipt"));
  vi.mocked(durable.listCommands)
    .mockResolvedValueOnce({ commands: [receipt()], next_after: null })
    .mockRejectedValueOnce(new Error("private list"));
  render(<DraftRecovery api={durable} onRestore={onRestore} />);
  await open();
  fireEvent.click(screen.getByLabelText(`Restore text from send ${COMMAND_ID}`));
  await screen.findByText(
    "Couldn't read the submitted text. Your current input is still available.",
  );
  fireEvent.click(screen.getByLabelText(`Refresh status for send ${COMMAND_ID}`));
  await screen.findByText("Couldn't refresh the send status. The displayed record is unchanged.");
  fireEvent.click(within(screen.getByLabelText("Send history")).getByText("Refresh"));
  await screen.findByText("Couldn't read the send history.");
  expect(screen.getByText("Accepted. The text is saved.")).toBeTruthy();
  expect(screen.getByLabelText(`Restore text from send ${COMMAND_ID}`)).toBeTruthy();
  expect(screen.queryByText("private submitted text")).toBeNull();
  expect(screen.queryByText("private receipt")).toBeNull();
  expect(screen.queryByText("private list")).toBeNull();
  expect(onRestore).not.toHaveBeenCalled();
  expect(durable.submitCommand).not.toHaveBeenCalled();
  expect(durable.getSubmission).toHaveBeenCalledTimes(1);
  expect(durable.getCommand).toHaveBeenCalledTimes(1);
  expect(durable.listCommands).toHaveBeenCalledTimes(2);
});
