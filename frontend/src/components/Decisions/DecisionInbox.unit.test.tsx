import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { type ReactElement, useState } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { NativeDecision } from "../../state/decisions";
import { narrowBlock, readCss, ruleIn } from "../../testing/css";
import { DecisionInbox } from "./DecisionInbox";

// Keyboard triage arms decisionQueue.tsx's 5 s undo window; the fake-timer
// technique is decisionQueue.unit.test.tsx's own (and, before that, RunPeek.unit.test.tsx's).
beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function toolItem(overrides: Partial<NativeDecision> = {}): NativeDecision {
  return {
    kind: "permission",
    id: "s1:tu1",
    origin: { workspace_id: "vault-a", session_id: "s1", turn_id: "t1" },
    revision: "abcdef1234567890",
    provider: "Claude Code",
    actor: "VIKI",
    action: "Bash",
    resource: "<script>alert(1)</script>",
    inline: true,
    card: {
      kind: "permission",
      data: {
        tool_use_id: "tu1",
        kind: "tool",
        tool: "Bash",
        summary: "<script>alert(1)</script>",
        question: "",
        reason: "",
        agent: "",
        plan: "",
      },
      nativeRequest: { runtime_id: "r1", request_id: "abcdef1234567890", turn_id: "t1" },
    },
    ...overrides,
  };
}

function planItem(): NativeDecision {
  return toolItem({
    id: "s2:tu2",
    origin: { workspace_id: null, session_id: "s2", turn_id: "t2" },
    inline: false,
    resource: "Proceed with this plan?",
    card: {
      kind: "permission",
      data: {
        tool_use_id: "tu2",
        kind: "plan",
        tool: "",
        summary: "",
        question: "",
        reason: "",
        agent: "",
        plan: "plan text",
      },
      nativeRequest: { runtime_id: "r1", request_id: "fedcba0987654321", turn_id: "t2" },
    },
  });
}

test("a row shows its fields as text, even one that looks like HTML", () => {
  const item = toolItem();
  render(
    <DecisionInbox
      items={[item]}
      sessionLabel={() => "Untitled session"}
      workspaceName="VIKI Notes"
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
    />,
  );
  const dialog = screen.getByRole("dialog");
  expect(dialog.querySelector("script")).toBeNull();
  expect(dialog.textContent).toContain("<script>alert(1)</script>");
  expect(dialog.textContent).toContain("Untitled session");
  expect(dialog.textContent).toContain("Claude Code");
  expect(dialog.textContent).toContain("VIKI");
  expect(dialog.textContent).toContain("Bash");
  expect(dialog.textContent).toContain("#abcdef12");
});

test("a row shows the workspace's name, never the raw workspace id", () => {
  const item = toolItem();
  render(
    <DecisionInbox
      items={[item]}
      sessionLabel={() => "Untitled session"}
      workspaceName="VIKI Notes"
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
    />,
  );
  const meta = document.querySelector(".decision-row-meta");
  expect(meta?.textContent).toBe("VIKI Notes · Claude Code · VIKI");
  expect(meta?.textContent).not.toContain(item.origin.workspace_id ?? "");
});

test("an unknown workspace name shows no workspace part in the row's meta", () => {
  const item = toolItem();
  render(
    <DecisionInbox
      items={[item]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
    />,
  );
  const meta = document.querySelector(".decision-row-meta");
  expect(meta?.textContent).toBe("Claude Code · VIKI");
});

test("the revision reads exactly the first 8 characters", () => {
  const item = toolItem({ revision: "abcdef1234567890" });
  render(
    <DecisionInbox
      items={[item]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
    />,
  );
  const revision = document.querySelector(".decision-row-revision");
  expect(revision?.textContent).toBe("#abcdef12");
});

test("the panel is a modal dialog", () => {
  render(
    <DecisionInbox
      items={[]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
    />,
  );
  expect(screen.getByRole("dialog").getAttribute("aria-modal")).toBe("true");
});

test("an empty conversation section does not dismiss waiting child work", () => {
  render(
    <DecisionInbox
      items={[]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
    >
      <section aria-label="Proposal controls">
        <button type="button">Review proposed changes</button>
      </section>
    </DecisionInbox>,
  );
  expect(screen.getByRole("heading", { name: "Conversation requests" })).toBeTruthy();
  expect(document.querySelector(".decision-inbox-intro")?.textContent).toBe(
    "What waits for you: tool requests, plans and questions from your chats, and the note and skill changes VIKI proposes.",
  );
  expect(screen.getByText("No conversation requests are waiting.")).toBeTruthy();
  expect(screen.queryByText("Nothing is waiting for you.")).toBeNull();
  expect(screen.getByRole("button", { name: "Review proposed changes" })).toBeTruthy();
});

test("native rows name their kind and explain the permission or conversation answer", () => {
  const question = toolItem({
    kind: "question",
    id: "s3:tu3",
    inline: false,
    card: {
      kind: "question",
      data: { tool_use_id: "tu3", questions: [] },
      nativeRequest: { runtime_id: "r1", request_id: "question-revision", turn_id: "t3" },
    },
  });
  render(
    <DecisionInbox
      items={[toolItem(), planItem(), question]}
      sessionLabel={(id) => id}
      workspaceName="Synthetic workspace"
      renderAnswer={() => <button type="button">Native answer</button>}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
    />,
  );
  const rows = [...document.querySelectorAll<HTMLElement>("[data-approval-row]")];
  expect(rows.map((row) => row.querySelector(".decision-row-kind")?.textContent)).toEqual([
    "Tool request",
    "Plan review",
    "Question",
  ]);
  expect(rows[0]?.textContent).toContain(
    "Allow once lets the tool try this action. Deny refuses this request.",
  );
  for (const row of rows.slice(1))
    expect(row.textContent).toContain("Review and answer in its conversation.");
  expect(screen.getByRole("button", { name: "Native answer" })).toBeTruthy();
});

test("main shell and native styles exclude nested inspector headers, lists and proposal kinds", () => {
  render(
    <DecisionInbox
      items={[toolItem()]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
    >
      <div className="decision-row">
        <div className="decision-row-kind">Note proposal</div>
      </div>
      <div className="decision-inbox" role="dialog" aria-label="Nested inspector">
        <div className="decision-inbox-head">
          <h2 className="decision-inbox-title">Inspector</h2>
        </div>
        <ul className="decision-inbox-rows">
          <li className="decision-inbox-row">
            <span className="decision-row-kind">Recorded item</span>
          </li>
        </ul>
      </div>
    </DecisionInbox>,
  );
  const panel = screen.getByRole("dialog", { name: "Approvals" });
  const inspector = screen.getByRole("dialog", { name: "Nested inspector" });
  expect(panel.classList.contains("decision-inbox-panel")).toBe(true);
  const css = readCss(import.meta.url, "DecisionInbox.css");
  expect(ruleIn(css, ".decision-inbox")).toMatch(/width:\s*420px/);
  expect(ruleIn(css, ".decision-inbox-panel")).toMatch(/width:\s*520px/);
  expect(ruleIn(narrowBlock(css), ".decision-inbox")).toMatch(/width:\s*100%/);
  const native = ".decision-inbox-row[data-approval-row]";
  for (const selector of [
    ".decision-inbox-panel > .decision-inbox-head",
    ".decision-inbox-panel > .decision-inbox-head > .decision-inbox-title",
    native,
    ...["kind", "session", "meta", "resource", "consequence"].map(
      (field) => `${native} > .decision-row-${field}`,
    ),
  ]) {
    expect(css).toContain(selector);
    expect(document.querySelectorAll(selector), selector).toHaveLength(1);
    expect(inspector.querySelector(selector)).toBeNull();
  }
  expect(ruleIn(css, `${native} > .decision-row-resource`)).toMatch(/overflow-wrap:\s*anywhere/);
});

test("only an inline item calls renderAnswer; a plan item offers only the open-conversation button", () => {
  const renderAnswer = vi.fn(() => <span data-testid="answer" />);
  render(
    <DecisionInbox
      items={[toolItem(), planItem()]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={renderAnswer}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
    />,
  );
  expect(renderAnswer).toHaveBeenCalledOnce();
  expect(renderAnswer).toHaveBeenCalledWith(expect.objectContaining({ id: "s1:tu1" }));
  expect(screen.getAllByTestId("answer")).toHaveLength(1);
  expect(screen.getAllByText("Open conversation")).toHaveLength(2);
});

test("Open conversation opens the row's own session", () => {
  const onOpenConversation = vi.fn();
  const items = [toolItem(), planItem()] as const;
  render(
    <DecisionInbox
      items={items}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={onOpenConversation}
      onClose={vi.fn()}
    />,
  );
  const [first, second] = screen.getAllByText("Open conversation");
  fireEvent.click(second as HTMLElement);
  expect(onOpenConversation).toHaveBeenCalledWith(items[1], screen.getByRole("dialog"));
  fireEvent.click(first as HTMLElement);
  expect(onOpenConversation).toHaveBeenCalledWith(items[0], screen.getByRole("dialog"));
});

test("Escape closes the panel", () => {
  const onClose = vi.fn();
  render(
    <DecisionInbox
      items={[]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={onClose}
    />,
  );
  fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
  expect(onClose).toHaveBeenCalledOnce();
});

test("no items shows the empty note, and never the rail's waiting label", () => {
  render(
    <DecisionInbox
      items={[]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
    />,
  );
  const dialog = screen.getByRole("dialog");
  expect(dialog.textContent).toContain("No conversation requests are waiting.");
  expect(dialog.textContent).not.toContain("Needs you");
});

test("never shows the rail's waiting label even with rows present", () => {
  render(
    <DecisionInbox
      items={[toolItem(), planItem()]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
    />,
  );
  expect(screen.getByRole("dialog").textContent).not.toContain("Needs you");
});

test("every chrome string is English", () => {
  render(
    <DecisionInbox
      items={[]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
    />,
  );
  const dialog = screen.getByRole("dialog");
  expect(dialog.getAttribute("aria-label")).toBe("Approvals");
  expect(dialog.textContent).toContain("Approvals");
  expect(dialog.textContent).toContain("Close");
  expect(dialog.textContent).toContain(
    "For conversation requests, allowing a tool permits an attempt. Check the conversation for the result.",
  );
  expect(dialog.textContent).toContain("No conversation requests are waiting.");
});

test("children render after the rows", () => {
  render(
    <DecisionInbox
      items={[toolItem()]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
    >
      <div data-testid="extra-section">Later section</div>
    </DecisionInbox>,
  );
  const dialog = screen.getByRole("dialog");
  expect(dialog.innerHTML.indexOf("</ul>")).toBeLessThan(
    dialog.innerHTML.indexOf('data-testid="extra-section"'),
  );
});

test("100 native reviews keep the header and reassurance outside the bounded details", () => {
  const items = Array.from({ length: 100 }, (_, index) =>
    toolItem({
      id: `session-${index}:tu1`,
      origin: { workspace_id: "vault-a", session_id: `session-${index}`, turn_id: "t1" },
    }),
  );
  const renderAnswer = vi.fn<Parameters<typeof DecisionInbox>[0]["renderAnswer"]>(() => (
    <button type="button">Native answer</button>
  ));
  const onOpenConversation = vi.fn();
  render(
    <DecisionInbox
      items={items}
      sessionLabel={(id) => id}
      workspaceName="Synthetic workspace"
      renderAnswer={renderAnswer}
      onOpenConversation={onOpenConversation}
      onClose={vi.fn()}
    >
      <section aria-label="Proposal controls">
        <button type="button">Review proposals</button>
      </section>
    </DecisionInbox>,
  );
  const dialog = screen.getByRole("dialog");
  const details = dialog.querySelector(".decision-inbox-details");
  if (!details) throw new Error("Missing bounded approval details");
  const close = screen.getByRole("button", { name: "Close" });
  const note = dialog.querySelector(".decision-inbox-note");
  expect(details.parentElement).toBe(dialog);
  expect(details.contains(close)).toBe(false);
  expect(details.contains(note)).toBe(false);
  expect(details.querySelectorAll(".decision-inbox-row")).toHaveLength(100);
  expect(details.lastElementChild?.getAttribute("aria-label")).toBe("Proposal controls");
  expect(renderAnswer.mock.calls.map(([item]) => item)).toEqual(items);
  for (const button of screen.getAllByRole("button", { name: "Open conversation" }))
    fireEvent.click(button);
  expect(onOpenConversation.mock.calls.map(([item]) => item.origin.session_id)).toEqual(
    items.map((item) => item.origin.session_id),
  );
  expect(document.activeElement).toBe(close);
  fireEvent.keyDown(window, { key: "Tab", shiftKey: true });
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Review proposals" }));
  fireEvent.keyDown(window, { key: "Tab" });
  expect(document.activeElement).toBe(close);
  const css = readCss(import.meta.url, "DecisionInbox.css");
  expect(ruleIn(css, ".decision-inbox")).toMatch(/min-height:\s*0/);
  expect(ruleIn(css, ".decision-inbox")).toMatch(/overflow:\s*hidden/);
  for (const selector of [".decision-inbox-details", ".decision-inbox > .decision-inbox-rows"]) {
    expect(ruleIn(css, selector)).toMatch(/min-height:\s*0/);
    expect(ruleIn(css, selector)).toMatch(/flex:\s*1/);
    expect(ruleIn(css, selector)).toMatch(/overflow-y:\s*auto/);
  }
  expect(ruleIn(css, ".decision-inbox-head")).toMatch(/flex:\s*none/);
  expect(ruleIn(css, ".decision-inbox > .decisions-batch")).toMatch(/flex:\s*none/);
  expect(ruleIn(css, ".decision-inbox > .decision-inbox-note")).toMatch(/max-height:\s*25%/);
  expect(ruleIn(css, ".decision-inbox > .decision-inbox-note")).toMatch(/overflow-y:\s*auto/);
});

test("scrolled approval details retain current callbacks and restore the actual opener", () => {
  render(<button type="button">Open approvals</button>);
  const opener = screen.getByRole("button", { name: "Open approvals" });
  opener.focus();
  const oldOpen = vi.fn();
  const newOpen = vi.fn();
  const onClose = vi.fn();
  const original = toolItem();
  const updated = toolItem({ revision: "new-revision", action: "Read current" });
  const props = {
    items: [original],
    sessionLabel: (id: string) => id,
    workspaceName: null,
    renderAnswer: vi.fn(() => null),
    onOpenConversation: oldOpen,
    onClose,
  };
  const { rerender, unmount } = render(<DecisionInbox {...props} />);
  const dialog = screen.getByRole("dialog");
  const details = dialog.querySelector<HTMLElement>(".decision-inbox-details");
  if (!details) throw new Error("Missing current approval details");
  details.scrollTop = 180;
  const close = screen.getByRole("button", { name: "Close" });
  close.focus();
  rerender(<DecisionInbox {...props} items={[updated]} onOpenConversation={newOpen} />);
  expect(screen.getByRole("dialog")).toBe(dialog);
  expect(dialog.querySelector(".decision-inbox-details")).toBe(details);
  expect(details.scrollTop).toBe(180);
  expect(document.activeElement).toBe(close);
  expect(props.renderAnswer).toHaveBeenLastCalledWith(updated);
  expect(screen.getByText("Read current")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Open conversation" }));
  expect(oldOpen).not.toHaveBeenCalled();
  expect(newOpen).toHaveBeenCalledExactlyOnceWith(updated, dialog);
  fireEvent.click(close);
  expect(onClose).toHaveBeenCalledOnce();
  unmount();
  expect(document.activeElement).toBe(opener);
});

/* AMENDMENT (2026-10-20): Retiring the retro
   feed retires BatchConfirm, so this file's own "bounded batch errors stay in the actual modal
   Tab cycle" test is deleted here too. Its point was BatchConfirm-specific (a long error message,
   role="alert", correctly sharing BatchConfirm's own Tab cycle with its Cancel/Approve buttons),
   not a generic nested-dialog claim, and ReasonDialog has no error/alert element to carry an
   equivalent case, so it is deleted rather than converted. Every other case is unaffected. */

// Keyboard triage: the dialog-root key handler, its guards, and a native row's a/e/r/l.

test("keyboard shortcuts disclose on demand while the polite status line stays mounted", () => {
  render(
    <DecisionInbox
      items={[]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
    />,
  );
  const dialog = screen.getByRole("dialog");
  const disclosure = screen.getByRole("button", { name: "Keyboard shortcuts" });
  const controls = disclosure.getAttribute("aria-controls");
  const legend = document.getElementById(controls ?? "");
  expect(disclosure.getAttribute("aria-expanded")).toBe("false");
  expect(legend?.hidden).toBe(true);
  fireEvent.click(disclosure);
  expect(disclosure.getAttribute("aria-expanded")).toBe("true");
  expect(legend?.hidden).toBe(false);
  expect(controls).toBeTruthy();
  expect(document.getElementById(controls ?? "")?.textContent).toContain("Enter opens");
  expect(dialog.textContent).toContain(
    "Keys: j and k move · a approve · e edit · r reject · Enter opens",
  );
  const status = dialog.querySelector(".decision-inbox-status");
  if (!status) throw new Error("Missing the polite status line");
  expect(status.getAttribute("role")).toBe("status");
  expect(status.getAttribute("aria-live")).toBe("polite");
  expect(status.textContent).toBe("");
  fireEvent.click(disclosure);
  expect(disclosure.getAttribute("aria-expanded")).toBe("false");
  expect(legend?.hidden).toBe(true);
  expect(dialog.querySelector(".decision-inbox-status")).toBe(status);
});

test("the intro says what can wait in Approvals", () => {
  render(
    <DecisionInbox
      items={[]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
    />,
  );
  expect(document.querySelector(".decision-inbox-intro")?.textContent).toBe(
    "What waits for you: tool requests, plans and questions from your chats, and the note and skill changes VIKI proposes.",
  );
});

test("a native row carries the 4.7 keyboard attributes; the open button is the open control", () => {
  render(
    <DecisionInbox
      items={[toolItem()]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
    />,
  );
  const row = document.querySelector("li.decision-inbox-row");
  if (!row) throw new Error("Missing the native row");
  expect(row.hasAttribute("data-approval-row")).toBe(true);
  expect((row as HTMLElement).tabIndex).toBe(-1);
  const open = row.querySelector("[data-approval-open]");
  expect(open?.tagName).toBe("BUTTON");
});

function secondItem(): NativeDecision {
  return toolItem({
    id: "s2:tu2",
    origin: { workspace_id: null, session_id: "s2", turn_id: "t2" },
  });
}

test("j moves focus to the next row and k to the previous", () => {
  render(
    <DecisionInbox
      items={[toolItem(), secondItem()]}
      sessionLabel={(id) => id}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
    />,
  );
  const rows = document.querySelectorAll<HTMLElement>("[data-approval-row]");
  expect(rows).toHaveLength(2);
  rows[0]?.focus();
  fireEvent.keyDown(rows[0] as HTMLElement, { key: "j" });
  expect(document.activeElement).toBe(rows[1]);
  fireEvent.keyDown(rows[1] as HTMLElement, { key: "k" });
  expect(document.activeElement).toBe(rows[0]);
});

test("j from outside any row focuses the first row; k focuses the last", () => {
  render(
    <DecisionInbox
      items={[toolItem(), secondItem()]}
      sessionLabel={(id) => id}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
    />,
  );
  const close = screen.getByRole("button", { name: "Close" });
  const rows = document.querySelectorAll<HTMLElement>("[data-approval-row]");
  close.focus();
  fireEvent.keyDown(close, { key: "j" });
  expect(document.activeElement).toBe(rows[0]);
  close.focus();
  fireEvent.keyDown(close, { key: "k" });
  expect(document.activeElement).toBe(rows[1]);
});

test("ArrowDown and ArrowUp move focus the same way as j and k", () => {
  render(
    <DecisionInbox
      items={[toolItem(), secondItem()]}
      sessionLabel={(id) => id}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
    />,
  );
  const rows = document.querySelectorAll<HTMLElement>("[data-approval-row]");
  rows[0]?.focus();
  fireEvent.keyDown(rows[0] as HTMLElement, { key: "ArrowDown" });
  expect(document.activeElement).toBe(rows[1]);
  fireEvent.keyDown(rows[1] as HTMLElement, { key: "ArrowUp" });
  expect(document.activeElement).toBe(rows[0]);
});

test("Enter on the focused row container clicks its own open control", () => {
  const onOpenConversation = vi.fn();
  const item = toolItem();
  render(
    <DecisionInbox
      items={[item]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={onOpenConversation}
      onClose={vi.fn()}
    />,
  );
  const row = document.querySelector<HTMLElement>("[data-approval-row]");
  if (!row) throw new Error("Missing the native row");
  row.focus();
  fireEvent.keyDown(row, { key: "Enter" });
  expect(onOpenConversation).toHaveBeenCalledExactlyOnceWith(item, screen.getByRole("dialog"));
});

test("Enter is left alone once focus is already on the open button itself", () => {
  const onOpenConversation = vi.fn();
  render(
    <DecisionInbox
      items={[toolItem()]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={onOpenConversation}
      onClose={vi.fn()}
    />,
  );
  const open = document.querySelector<HTMLElement>("[data-approval-open]");
  if (!open) throw new Error("Missing the open control");
  open.focus();
  fireEvent.keyDown(open, { key: "Enter" });
  // The dialog's own handler only drives Enter when the row container itself is the target
  // (`event.target === currentRow`); a focused button's own activation is the button's to own,
  // so the handler must never click it a second time.
  expect(onOpenConversation).not.toHaveBeenCalled();
});

test("a then 5 s calls onDecide(allow)", async () => {
  const onDecide = vi.fn().mockResolvedValue(undefined);
  const item = toolItem();
  render(
    <DecisionInbox
      items={[item]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
      onDecide={onDecide}
    />,
  );
  const row = document.querySelector<HTMLElement>("[data-approval-row]");
  if (!row) throw new Error("Missing the native row");
  row.focus();
  fireEvent.keyDown(row, { key: "a" });
  expect(onDecide).not.toHaveBeenCalled();
  expect(document.querySelector(".decision-undo-label")?.textContent).toBe(
    "Allow once · <script>alert(1)</script> · sends in 5 seconds",
  );
  await advance(5000);
  expect(onDecide).toHaveBeenCalledExactlyOnceWith(item, true);
});

test("a row and its undo label write hidden characters as codes and draw line breaks", () => {
  // A right-to-left override would reorder the rest of the line; a zero-width space paints nothing.
  const item = toolItem({ action: "Bash​", resource: "rm -rf tmp‮\ncd /" });
  render(
    <DecisionInbox
      items={[item]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
      onDecide={vi.fn().mockResolvedValue(undefined)}
    />,
  );
  const row = document.querySelector<HTMLElement>("[data-approval-row]");
  if (!row) throw new Error("Missing the native row");
  expect(row.querySelector(".decision-row-action")?.textContent).toBe("Bash<U+200B>");
  expect(row.querySelector(".decision-row-resource")?.textContent).toBe(
    "rm -rf tmp<U+202E> ↵ cd /",
  );
  row.focus();
  fireEvent.keyDown(row, { key: "a" });
  expect(document.querySelector(".decision-undo-label")?.textContent).toBe(
    "Allow once · rm -rf tmp<U+202E> ↵ cd / · sends in 5 seconds",
  );
});

test("r then 5 s calls onDecide(item, false), labelled Deny in the undo bar", async () => {
  const onDecide = vi.fn().mockResolvedValue(undefined);
  const item = toolItem();
  render(
    <DecisionInbox
      items={[item]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
      onDecide={onDecide}
    />,
  );
  const row = document.querySelector<HTMLElement>("[data-approval-row]");
  if (!row) throw new Error("Missing the native row");
  row.focus();
  fireEvent.keyDown(row, { key: "r" });
  expect(document.querySelector(".decision-undo-label")?.textContent).toBe(
    "Deny · <script>alert(1)</script> · sends in 5 seconds",
  );
  await advance(5000);
  expect(onDecide).toHaveBeenCalledExactlyOnceWith(item, false);
});

test("a decision the close sends that then fails is handed up through onLateFailure", async () => {
  const onLateFailure = vi.fn();
  const error = Object.assign(new Error("closed"), { ownerText: "Not connected." });
  const onDecide = vi.fn().mockRejectedValue(error);
  const { unmount } = render(
    <DecisionInbox
      items={[toolItem()]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
      onDecide={onDecide}
      onLateFailure={onLateFailure}
    />,
  );
  const row = document.querySelector<HTMLElement>("[data-approval-row]");
  if (!row) throw new Error("Missing the native row");
  row.focus();
  fireEvent.keyDown(row, { key: "r" });
  unmount();
  await advance(0);
  expect(onDecide).toHaveBeenCalledTimes(1);
  expect(onLateFailure).toHaveBeenCalledExactlyOnceWith(
    "Deny · <script>alert(1)</script>",
    "Not connected.",
  );
});

test("undo cancels the pending decision; onDecide is never called", async () => {
  const onDecide = vi.fn().mockResolvedValue(undefined);
  render(
    <DecisionInbox
      items={[toolItem()]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
      onDecide={onDecide}
    />,
  );
  const row = document.querySelector<HTMLElement>("[data-approval-row]");
  if (!row) throw new Error("Missing the native row");
  row.focus();
  fireEvent.keyDown(row, { key: "a" });
  fireEvent.click(screen.getByRole("button", { name: /^Undo / }));
  expect(document.querySelector(".decision-undo-label")).toBeNull();
  await advance(5000);
  expect(onDecide).not.toHaveBeenCalled();
});

test("pressing a moves focus to the next row after arming the undo", () => {
  const onDecide = vi.fn().mockResolvedValue(undefined);
  render(
    <DecisionInbox
      items={[toolItem(), secondItem()]}
      sessionLabel={(id) => id}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
      onDecide={onDecide}
    />,
  );
  const rows = document.querySelectorAll<HTMLElement>("[data-approval-row]");
  rows[0]?.focus();
  fireEvent.keyDown(rows[0] as HTMLElement, { key: "a" });
  expect(document.activeElement).toBe(rows[1]);
});

test("without onDecide, a shows the note instead of deciding anything", () => {
  render(
    <DecisionInbox
      items={[toolItem()]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
    />,
  );
  const row = document.querySelector<HTMLElement>("[data-approval-row]");
  if (!row) throw new Error("Missing the native row");
  row.focus();
  fireEvent.keyDown(row, { key: "a" });
  expect(document.querySelector(".decision-inbox-status")?.textContent).toBe(
    "Answer this one in its conversation: press Enter.",
  );
  expect(document.querySelector(".decision-undo-label")).toBeNull();
});

test("a plan row is never decided by a or r; it answers only through Enter", async () => {
  const onDecide = vi.fn().mockResolvedValue(undefined);
  render(
    <DecisionInbox
      items={[planItem()]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
      onDecide={onDecide}
    />,
  );
  const row = document.querySelector<HTMLElement>("[data-approval-row]");
  if (!row) throw new Error("Missing the plan row");
  row.focus();
  fireEvent.keyDown(row, { key: "a" });
  expect(document.querySelector(".decision-inbox-status")?.textContent).toBe(
    "Answer this one in its conversation: press Enter.",
  );
  await advance(5000);
  expect(onDecide).not.toHaveBeenCalled();
});

test("e always shows its own note on a native row", () => {
  render(
    <DecisionInbox
      items={[toolItem()]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
      onDecide={vi.fn().mockResolvedValue(undefined)}
    />,
  );
  const row = document.querySelector<HTMLElement>("[data-approval-row]");
  if (!row) throw new Error("Missing the native row");
  row.focus();
  fireEvent.keyDown(row, { key: "e" });
  expect(document.querySelector(".decision-inbox-status")?.textContent).toBe(
    "Edit is not available for this item.",
  );
});

test("l just moves focus to the next row, with no note and no decision", async () => {
  const onDecide = vi.fn().mockResolvedValue(undefined);
  render(
    <DecisionInbox
      items={[toolItem(), secondItem()]}
      sessionLabel={(id) => id}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
      onDecide={onDecide}
    />,
  );
  const rows = document.querySelectorAll<HTMLElement>("[data-approval-row]");
  rows[0]?.focus();
  fireEvent.keyDown(rows[0] as HTMLElement, { key: "l" });
  expect(document.activeElement).toBe(rows[1]);
  expect(document.querySelector(".decision-inbox-status")?.textContent).toBe("");
  await advance(5000);
  expect(onDecide).not.toHaveBeenCalled();
});

test("a composing keydown is ignored", () => {
  render(
    <DecisionInbox
      items={[toolItem()]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
      onDecide={vi.fn().mockResolvedValue(undefined)}
    />,
  );
  const row = document.querySelector<HTMLElement>("[data-approval-row]");
  if (!row) throw new Error("Missing the native row");
  row.focus();
  fireEvent.keyDown(row, { key: "a", isComposing: true });
  expect(document.querySelector(".decision-undo-label")).toBeNull();
  expect(document.querySelector(".decision-inbox-status")?.textContent).toBe("");
});

test("a Ctrl-held key is ignored", () => {
  render(
    <DecisionInbox
      items={[toolItem()]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
      onDecide={vi.fn().mockResolvedValue(undefined)}
    />,
  );
  const row = document.querySelector<HTMLElement>("[data-approval-row]");
  if (!row) throw new Error("Missing the native row");
  row.focus();
  fireEvent.keyDown(row, { key: "a", ctrlKey: true });
  expect(document.querySelector(".decision-undo-label")).toBeNull();
});

test("a key from a text-entry target inside the panel is ignored", () => {
  render(
    <DecisionInbox
      items={[toolItem()]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
      onDecide={vi.fn().mockResolvedValue(undefined)}
    >
      <input aria-label="Stray field" />
    </DecisionInbox>,
  );
  const input = screen.getByRole("textbox", { name: "Stray field" });
  fireEvent.keyDown(input, { key: "a" });
  expect(document.querySelector(".decision-undo-label")).toBeNull();
});

test.each(["ArrowDown", "ArrowUp", "j", "k", "a", "e", "r", "l", "Enter"])(
  "a select keeps its own %s key without navigating or deciding a row",
  async (key) => {
    const onDecide = vi.fn().mockResolvedValue(undefined);
    const onOpenConversation = vi.fn();
    render(
      <DecisionInbox
        items={[toolItem(), secondItem()]}
        sessionLabel={(id) => id}
        workspaceName={null}
        renderAnswer={(item) =>
          item.id === "s1:tu1" ? (
            <select aria-label="Request filter" defaultValue="all">
              <option value="all">All requests</option>
              <option value="tool">Tool requests</option>
            </select>
          ) : null
        }
        onOpenConversation={onOpenConversation}
        onClose={vi.fn()}
        onDecide={onDecide}
      />,
    );
    const select = screen.getByRole("combobox", { name: "Request filter" });
    select.focus();
    expect(fireEvent.keyDown(select, { key })).toBe(true);
    expect(document.activeElement).toBe(select);
    expect(document.querySelector(".decision-inbox-status")?.textContent).toBe("");
    expect(document.querySelector(".decision-undo-label")).toBeNull();
    await advance(5000);
    expect(onDecide).not.toHaveBeenCalled();
    expect(onOpenConversation).not.toHaveBeenCalled();
  },
);

test("a key from inside a nested dialog never reaches this dialog's own rows", () => {
  render(
    <DecisionInbox
      items={[toolItem()]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
      onDecide={vi.fn().mockResolvedValue(undefined)}
    >
      <div role="dialog">
        <button type="button">Nested control</button>
      </div>
    </DecisionInbox>,
  );
  const nested = screen.getByRole("button", { name: "Nested control" });
  fireEvent.keyDown(nested, { key: "a" });
  expect(document.querySelector(".decision-undo-label")).toBeNull();
});

test("a focus ring marks the keyboard-focused row", () => {
  const css = readCss(import.meta.url, "DecisionInbox.css");
  expect(ruleIn(css, "[data-approval-row]:focus-visible")).toMatch(
    /outline:\s*1px solid var\(--or\)/,
  );
});

// Section rows: a focused row that is not a native is treated as a section row
// (DecisionSection's) — it carries its own [data-approval-action] buttons, one per action it
// offers. Fixtures below mark rows up the same way: a vault row never offers Later; a snoozed or
// stale retro row's Later is gated off while approve/edit/reject remain; an owner-applies row
// offers only Later; a fully recorded row offers nothing.

function sectionRow(
  id: string,
  buttons: readonly {
    action: "approve" | "edit" | "reject" | "later";
    label: string;
    disabled?: boolean;
    onClick?: () => void;
  }[],
): ReactElement {
  return (
    <div data-id={id} data-approval-row tabIndex={-1}>
      {buttons.map((button) => (
        <button
          key={button.action}
          type="button"
          data-approval-action={button.action}
          disabled={button.disabled ?? false}
          onClick={button.onClick}
        >
          {button.label}
        </button>
      ))}
    </div>
  );
}

function plainRow(id: string): ReactElement {
  return <div data-id={id} data-approval-row tabIndex={-1} />;
}

/** A row whose edit and reject open their own "dialog" (a stand-in for ReasonDialog), the way a
 * retro or vault row's do in DecisionSection — to show this file only clicks the button and
 * leaves focus to whatever that click opens. */
function RetroLikeRow({ id }: { id: string }): ReactElement {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <>
      <div data-id={id} data-approval-row tabIndex={-1}>
        <button type="button" data-approval-action="approve">
          Approve
        </button>
        <button type="button" data-approval-action="edit" onClick={() => setOpen("Edit")}>
          Edit
        </button>
        <button type="button" data-approval-action="reject" onClick={() => setOpen("Reject")}>
          Reject
        </button>
      </div>
      {open ? <div role="dialog" aria-label={`${open} reason`} /> : null}
    </>
  );
}

test("a vault-shaped row (no Later): an enabled a clicks its button and moves on; l moves on too", () => {
  const onApprove = vi.fn();
  render(
    <DecisionInbox
      items={[]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
    >
      {sectionRow("vault-1", [
        { action: "approve", label: "Approve", onClick: onApprove },
        { action: "edit", label: "Edit" },
        { action: "reject", label: "Reject" },
      ])}
      {plainRow("vault-2")}
    </DecisionInbox>,
  );
  const rows = document.querySelectorAll<HTMLElement>("[data-approval-row]");
  rows[0]?.focus();
  fireEvent.keyDown(rows[0] as HTMLElement, { key: "a" });
  expect(onApprove).toHaveBeenCalledOnce();
  expect(document.activeElement).toBe(rows[1]);
  expect(document.querySelector(".decision-inbox-status")?.textContent).toBe("");
  rows[0]?.focus();
  fireEvent.keyDown(rows[0] as HTMLElement, { key: "l" });
  expect(document.activeElement).toBe(rows[1]);
  expect(document.querySelector(".decision-inbox-status")?.textContent).toBe("");
});

test("a snoozed-or-stale-shaped retro row: a disabled approve, edit or reject each show their own note; l still moves on since Later is missing", () => {
  const onApprove = vi.fn();
  render(
    <DecisionInbox
      items={[]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
    >
      {sectionRow("retro-1", [
        { action: "approve", label: "Approve", disabled: true, onClick: onApprove },
        { action: "edit", label: "Edit", disabled: true },
        { action: "reject", label: "Reject", disabled: true },
      ])}
      {plainRow("retro-2")}
    </DecisionInbox>,
  );
  const rows = document.querySelectorAll<HTMLElement>("[data-approval-row]");
  rows[0]?.focus();
  fireEvent.keyDown(rows[0] as HTMLElement, { key: "a" });
  expect(onApprove).not.toHaveBeenCalled();
  expect(document.querySelector(".decision-inbox-status")?.textContent).toBe(
    "Approve is not available for this item.",
  );
  fireEvent.keyDown(rows[0] as HTMLElement, { key: "e" });
  expect(document.querySelector(".decision-inbox-status")?.textContent).toBe(
    "Edit is not available for this item.",
  );
  fireEvent.keyDown(rows[0] as HTMLElement, { key: "r" });
  expect(document.querySelector(".decision-inbox-status")?.textContent).toBe(
    "Reject is not available for this item.",
  );
  expect(document.activeElement).toBe(rows[0]);
  fireEvent.keyDown(rows[0] as HTMLElement, { key: "l" });
  expect(document.activeElement).toBe(rows[1]);
  expect(document.querySelector(".decision-inbox-status")?.textContent).toBe("");
});

test("an owner-applies-shaped retro row offering only Later: a, r and e each show the note; the enabled l clicks and moves on", () => {
  const onLater = vi.fn();
  render(
    <DecisionInbox
      items={[]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
    >
      {sectionRow("retro-owner-1", [{ action: "later", label: "Later", onClick: onLater }])}
      {plainRow("retro-owner-2")}
    </DecisionInbox>,
  );
  const rows = document.querySelectorAll<HTMLElement>("[data-approval-row]");
  rows[0]?.focus();
  fireEvent.keyDown(rows[0] as HTMLElement, { key: "a" });
  expect(document.querySelector(".decision-inbox-status")?.textContent).toBe(
    "Approve is not available for this item.",
  );
  fireEvent.keyDown(rows[0] as HTMLElement, { key: "r" });
  expect(document.querySelector(".decision-inbox-status")?.textContent).toBe(
    "Reject is not available for this item.",
  );
  fireEvent.keyDown(rows[0] as HTMLElement, { key: "e" });
  expect(document.querySelector(".decision-inbox-status")?.textContent).toBe(
    "Edit is not available for this item.",
  );
  expect(onLater).not.toHaveBeenCalled();
  fireEvent.keyDown(rows[0] as HTMLElement, { key: "l" });
  expect(onLater).toHaveBeenCalledOnce();
  expect(document.activeElement).toBe(rows[1]);
  expect(document.querySelector(".decision-inbox-status")?.textContent).toBe("");
});

test("a fully recorded row with no buttons at all: a, r and e show the note; l just moves on", () => {
  render(
    <DecisionInbox
      items={[]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
    >
      {sectionRow("recorded-1", [])}
      {plainRow("recorded-2")}
    </DecisionInbox>,
  );
  const rows = document.querySelectorAll<HTMLElement>("[data-approval-row]");
  rows[0]?.focus();
  fireEvent.keyDown(rows[0] as HTMLElement, { key: "a" });
  expect(document.querySelector(".decision-inbox-status")?.textContent).toBe(
    "Approve is not available for this item.",
  );
  fireEvent.keyDown(rows[0] as HTMLElement, { key: "r" });
  expect(document.querySelector(".decision-inbox-status")?.textContent).toBe(
    "Reject is not available for this item.",
  );
  fireEvent.keyDown(rows[0] as HTMLElement, { key: "e" });
  expect(document.querySelector(".decision-inbox-status")?.textContent).toBe(
    "Edit is not available for this item.",
  );
  fireEvent.keyDown(rows[0] as HTMLElement, { key: "l" });
  expect(document.activeElement).toBe(rows[1]);
  expect(document.querySelector(".decision-inbox-status")?.textContent).toBe("");
});

test("a present but disabled Later shows the note, unlike a missing one which just moves on", () => {
  const onLater = vi.fn();
  render(
    <DecisionInbox
      items={[]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
    >
      {sectionRow("retro-disabled-later-1", [
        { action: "later", label: "Later", disabled: true, onClick: onLater },
      ])}
      {plainRow("retro-disabled-later-2")}
    </DecisionInbox>,
  );
  const rows = document.querySelectorAll<HTMLElement>("[data-approval-row]");
  rows[0]?.focus();
  fireEvent.keyDown(rows[0] as HTMLElement, { key: "l" });
  expect(onLater).not.toHaveBeenCalled();
  expect(document.activeElement).toBe(rows[0]);
  expect(document.querySelector(".decision-inbox-status")?.textContent).toBe(
    "Later is not available for this item.",
  );
});

test("an enabled edit or reject clicks its button and opens its own dialog, without this file moving focus itself", () => {
  render(
    <DecisionInbox
      items={[]}
      sessionLabel={() => "Untitled session"}
      workspaceName={null}
      renderAnswer={() => null}
      onOpenConversation={vi.fn()}
      onClose={vi.fn()}
    >
      <RetroLikeRow id="retro-reason-1" />
      {plainRow("retro-reason-2")}
    </DecisionInbox>,
  );
  const rows = document.querySelectorAll<HTMLElement>("[data-approval-row]");
  rows[0]?.focus();
  fireEvent.keyDown(rows[0] as HTMLElement, { key: "e" });
  expect(screen.getByRole("dialog", { name: "Edit reason" })).toBeTruthy();
  expect(document.activeElement).toBe(rows[0]);
  fireEvent.keyDown(rows[0] as HTMLElement, { key: "r" });
  expect(screen.getByRole("dialog", { name: "Reject reason" })).toBeTruthy();
  expect(screen.queryByRole("dialog", { name: "Edit reason" })).toBeNull();
  expect(document.activeElement).toBe(rows[0]);
});
