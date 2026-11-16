import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import {
  AT,
  historyView,
  subagentStarted,
  textDone,
  toolDone,
  toolStarted,
  turnDone,
  turnStarted,
  userMessage,
  view,
} from "../../accept/m2b/fakes";
import type { HistoryItem, SessionSnapshot } from "../../protocol";
import { applyEnvelope, applySnapshot } from "../../state/sessionReducer";
import { emptyView, type Turn } from "../../state/view";
import { narrowBlock, readCss, ruleIn } from "../../testing/css";
import { cutPromptNotice, RetryReview, retryFlags } from "./RetryReview";

const SNAPSHOT_STATE = {
  live: false,
  info: null,
  mode: "",
  blocked: null,
  closed: false,
  error: null,
  compacting: false,
  context_percent: null,
  rate_limit: null,
};

afterEach(cleanup);

function turnWithTools(): Turn {
  const v = view([
    userMessage("do it"),
    turnStarted,
    toolStarted("b1", "Bash", { head: "ls", rest: "" }),
    toolDone("b1", { summary: "listed files" }),
    toolStarted("b2", "mcp__vault__search", { head: "search", rest: "" }),
    toolDone("b2", { summary: "found 3" }),
    subagentStarted("a1", "researcher", "sonnet"),
    toolStarted("b3", "WebSearch", { head: "search", rest: "" }, "a1"),
    toolDone("b3", { summary: "3 results" }),
    toolStarted("b4", "Read", { head: "Read", rest: "note.md" }),
    toolDone("b4", { summary: "read" }),
    turnDone("error"),
  ]);
  const turn = v.turns.at(-1);
  if (!turn) throw new Error("no turn");
  return turn;
}

test("retryFlags flattens tools and routing rows in arrival order, flagging write, mcp and web", () => {
  expect(retryFlags(turnWithTools())).toEqual([
    { tool: "Bash", summary: "listed files", flag: "write" },
    { tool: "mcp__vault__search", summary: "found 3", flag: "mcp" },
    { tool: "WebSearch", summary: "3 results", flag: "web" },
    { tool: "Read", summary: "read", flag: null },
  ]);
});

test("lists every tool row with its own flag under the warning sentence", () => {
  render(
    <RetryReview
      turn={turnWithTools()}
      editorEmpty
      incomplete={false}
      onFill={vi.fn()}
      onClose={vi.fn()}
    />,
  );
  const dialog = screen.getByRole("dialog");
  expect(dialog.textContent).toContain("Before you retry");
  expect(dialog.textContent).toContain(
    "These tools ran in this turn. Write, MCP and web tools may already have changed files or sent data. Network effects are not undone.",
  );
  // Each row's own flag, read off its own <li> (not the dialog as a whole, which already
  // carries the words Write/MCP/web inside the fixed warning sentence above).
  const rows = [...dialog.querySelectorAll("li")].map((li) => li.textContent);
  expect(rows).toEqual([
    "Bashwritelisted files",
    "mcp__vault__searchMCPfound 3",
    "WebSearchWeb3 results",
    "Readread",
  ]);
});

test("a turn with no tool rows says so, instead of the warning sentence", () => {
  const v = view([userMessage("hi"), turnStarted, textDone("nope"), turnDone("cancelled")]);
  const turn = v.turns.at(-1);
  if (!turn) throw new Error("no turn");
  render(
    <RetryReview turn={turn} editorEmpty incomplete={false} onFill={vi.fn()} onClose={vi.fn()} />,
  );
  const dialog = screen.getByRole("dialog");
  expect(dialog.textContent).toContain("No tools ran in this turn.");
  expect(dialog.textContent).not.toContain("Write, MCP and web");
});

function liveTurn(items: HistoryItem[]): Turn {
  const built = items.reduce(
    (current, item) =>
      applyEnvelope(current, {
        v: 2,
        session_id: current.sessionId,
        generation: "g",
        request_id: "r",
        seq: current.seq + 1,
        ...item,
      }),
    emptyView("s1"),
  );
  const turn = built.turns[0];
  if (!turn) throw new Error("missing live turn");
  expect(turn.history).toBe(false);
  expect(turn.status).toBe("error");
  return turn;
}

const liveTurnFailed: HistoryItem = {
  type: "turn.done",
  data: {
    status: "error",
    usage: { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_creation_tokens: 0 },
    context_percent: null,
    duration_ms: 1200,
  },
};

test.each([undefined, 0, 1, 3])(
  "observed agent work with reported tool count %s keeps unknown effects visible before retry",
  (toolUses) => {
    const turn = liveTurn([
      { type: "user.message", data: { text: "delegate it", command: "", at: AT } },
      { type: "turn.started", data: { turn_id: "t1" } },
      {
        type: "subagent.started",
        data: { block_id: "a1", agent: "worker", model: "inherit", description: "", reason: "" },
      },
      {
        type: "subagent.done",
        data: {
          block_id: "a1",
          status: "completed",
          ...(toolUses === undefined ? {} : { tool_uses: toolUses }),
          summary: "The agent reports completion.",
        },
      },
      liveTurnFailed,
    ]);
    const routing = turn.blocks[0];
    if (routing?.kind !== "routing") throw new Error("missing routing evidence");
    expect(routing.toolUses).toBe(toolUses);
    expect(routing.tools).toHaveLength(0);
    expect(retryFlags(turn)).toEqual([]);
    const onFill = vi.fn();
    render(
      <RetryReview turn={turn} editorEmpty incomplete={false} onFill={onFill} onClose={vi.fn()} />,
    );
    const dialog = screen.getByRole("dialog");
    expect(dialog.textContent).not.toContain("No tools ran in this turn.");
    expect(dialog.textContent).toContain("Agent work was observed in this turn.");
    expect(dialog.textContent).toContain("Individual tool calls may be missing from this window.");
    expect(dialog.textContent).toContain(
      "File or network effects may already exist; retrying does not undo them.",
    );
    expect(dialog.textContent).toContain("worker");
    expect(dialog.textContent).toContain(
      toolUses === undefined
        ? "Tool count unavailable."
        : `Provider reported ${toolUses} tool use${toolUses === 1 ? "" : "s"}.`,
    );
    expect(dialog.textContent).not.toContain("The agent reports completion.");
    expect(onFill).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Put the prompt back in the message box" }));
    expect(onFill).toHaveBeenCalledExactlyOnceWith("delegate it", []);
  },
);

test("a live turn with neither tools nor routing keeps the no-work receipt and manual fill", () => {
  const turn = liveTurn([
    { type: "user.message", data: { text: "hi", command: "", at: AT } },
    { type: "turn.started", data: { turn_id: "t1" } },
    { type: "text.done", data: { block_id: "b1", text: "nope" } },
    liveTurnFailed,
  ]);
  const onFill = vi.fn();
  render(
    <RetryReview turn={turn} editorEmpty incomplete={false} onFill={onFill} onClose={vi.fn()} />,
  );
  const dialog = screen.getByRole("dialog");
  expect(dialog.textContent).toContain("No tools ran in this turn.");
  expect(dialog.textContent).not.toContain("Agent work was observed in this turn.");
  expect(onFill).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Put the prompt back in the message box" }));
  expect(onFill).toHaveBeenCalledExactlyOnceWith("hi", []);
});

test("a truncated window's turn shows the incomplete notice even with 255 tool rows, never the no-tools line", () => {
  const events = [userMessage("do it"), turnStarted];
  for (let i = 0; i < 255; i++) {
    const id = `b${i}`;
    events.push(toolStarted(id, i === 0 ? "Write" : "Read", { head: "x", rest: "" }));
    events.push(toolDone(id, { summary: "ok" }));
  }
  events.push(turnDone("error"));
  const snapshot = {
    v: 2,
    type: "session.snapshot",
    session_id: "s1",
    generation: "g1",
    request_id: "",
    seq: events.length,
    data: { events, truncated: true, state: SNAPSHOT_STATE },
  } as unknown as SessionSnapshot;
  const snapshotView = applySnapshot(emptyView("s1"), snapshot);
  const turn = snapshotView.turns.at(-1);
  if (!turn) throw new Error("no turn");
  render(
    <RetryReview
      turn={turn}
      editorEmpty
      incomplete={snapshotView.truncated}
      onFill={vi.fn()}
      onClose={vi.fn()}
    />,
  );
  const dialog = screen.getByRole("dialog");
  expect(dialog.textContent).toContain(
    "This list may be incomplete: this window holds only part of the conversation. Open conversation record for the full record.",
  );
  expect(dialog.textContent).not.toContain("No tools ran in this turn.");
});

test("an incomplete window with no tool rows shows the incomplete notice, never the no-tools line", () => {
  const v = view([userMessage("hi"), turnStarted, textDone("nope"), turnDone("cancelled")]);
  const turn = v.turns.at(-1);
  if (!turn) throw new Error("no turn");
  render(<RetryReview turn={turn} editorEmpty incomplete onFill={vi.fn()} onClose={vi.fn()} />);
  const dialog = screen.getByRole("dialog");
  expect(dialog.textContent).toContain(
    "This list may be incomplete: this window holds only part of the conversation. Open conversation record for the full record.",
  );
  expect(dialog.textContent).not.toContain("No tools ran in this turn.");
});

test("Put the prompt back in the message box fills the empty editor with the turn's own prompt", () => {
  const onFill = vi.fn();
  const turn = turnWithTools();
  render(
    <RetryReview turn={turn} editorEmpty incomplete={false} onFill={onFill} onClose={vi.fn()} />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Put the prompt back in the message box" }));
  expect(onFill).toHaveBeenCalledWith(turn.user?.text, turn.user?.attachments ?? []);
});

test("the fill rejoins a known command with its argument text, as the live path split them", () => {
  const onFill = vi.fn();
  const v = view([userMessage("src/app.py", "/review"), turnStarted, turnDone("error")]);
  const turn = v.turns.at(-1);
  if (!turn) throw new Error("no turn");
  render(
    <RetryReview turn={turn} editorEmpty incomplete={false} onFill={onFill} onClose={vi.fn()} />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Put the prompt back in the message box" }));
  expect(onFill).toHaveBeenCalledWith("/review src/app.py", []);
});

test("the fill puts back only the command when an argument-less skill fills nothing", () => {
  const onFill = vi.fn();
  const v = view([userMessage("", "/status"), turnStarted, turnDone("error")]);
  const turn = v.turns.at(-1);
  if (!turn) throw new Error("no turn");
  render(
    <RetryReview turn={turn} editorEmpty incomplete={false} onFill={onFill} onClose={vi.fn()} />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Put the prompt back in the message box" }));
  expect(onFill).toHaveBeenCalledWith("/status", []);
});

test("a turn replayed from a cut snapshot item disables the fill and names the send time and Send history's Restore text", () => {
  const onFill = vi.fn();
  const turn: Turn = { ...turnWithTools(), userCut: true };
  render(
    <RetryReview turn={turn} editorEmpty incomplete={false} onFill={onFill} onClose={vi.fn()} />,
  );
  const button = screen.getByRole("button", {
    name: "Put the prompt back in the message box",
  }) as HTMLButtonElement;
  expect(button.disabled).toBe(true);
  const sent = new Date(AT).toLocaleString("en-US");
  expect(screen.getByRole("dialog").textContent).toContain(
    `Only this window's copy of this prompt was cut short (sent ${sent}). Drafts and send history → Send history → Restore text opens the full text in a new conversation's message box; copy it from there into this one to retry here.`,
  );
  fireEvent.click(button);
  expect(onFill).not.toHaveBeenCalled();
});

function historyCutTurn(at: string): Turn {
  const cutText = "x".repeat(16384);
  const v = historyView([userMessage(cutText, "", at), turnStarted, turnDone("error")]);
  const turn = v.turns.at(-1);
  if (!turn) throw new Error("no turn");
  expect(turn.history).toBe(true);
  expect(turn.userCut).toBe(true);
  return turn;
}

test("cutPromptNotice names both routes for a cut history turn, with a send time", () => {
  const sent = new Date(AT).toLocaleString("en-US");
  expect(cutPromptNotice(historyCutTurn(AT), true)).toBe(
    `Only this window's copy of this prompt was cut short (sent ${sent}). Open its full text: Drafts and send history → Send history → Restore text for a prompt sent from this app, Open conversation record for an earlier conversation.`,
  );
});

test("cutPromptNotice names both routes for a cut history turn, with no send time", () => {
  expect(cutPromptNotice(historyCutTurn(""), true)).toBe(
    "Only this window's copy of this prompt was cut short. Open its full text: Drafts and send history → Send history → Restore text for a prompt sent from this app, Open conversation record for an earlier conversation.",
  );
});

test("cutPromptNotice drops Open conversation record for a cut history turn while the record is missing", () => {
  const sent = new Date(AT).toLocaleString("en-US");
  expect(cutPromptNotice(historyCutTurn(AT), false, true)).toBe(
    `Only this window's copy of this prompt was cut short (sent ${sent}). Open its full text: Drafts and send history → Send history → Restore text for a prompt sent from this app.`,
  );
  expect(cutPromptNotice(historyCutTurn(""), false, true)).toBe(
    "Only this window's copy of this prompt was cut short. Open its full text: Drafts and send history → Send history → Restore text for a prompt sent from this app.",
  );
});

test("an empty send time drops the (sent …) clause instead of an invalid date", () => {
  const turn: Turn = { ...turnWithTools(), userCut: true };
  const withoutAt: Turn = { ...turn, user: turn.user ? { ...turn.user, at: "" } : null };
  render(
    <RetryReview
      turn={withoutAt}
      editorEmpty
      incomplete={false}
      onFill={vi.fn()}
      onClose={vi.fn()}
    />,
  );
  expect(screen.getByRole("dialog").textContent).toContain(
    "Only this window's copy of this prompt was cut short. Drafts and send history → Send history → Restore text opens the full text in a new conversation's message box; copy it from there into this one to retry here.",
  );
});

test("a two-turn snapshot fills the whole first prompt; a cut tool result there does not mark it cut, only the second turn's own cut user.message does", () => {
  const secondAt = "2026-09-12T09:06:00.000Z";
  const events = [
    userMessage("first prompt"),
    turnStarted,
    toolStarted("b1", "Bash", { head: "ls", rest: "" }),
    { ...toolDone("b1"), original_bytes: 999 },
    turnDone("error"),
    { ...userMessage("second prompt", "", secondAt), original_bytes: 999 },
    turnStarted,
    turnDone("error"),
  ];
  const snapshot = {
    v: 2,
    type: "session.snapshot",
    session_id: "s1",
    generation: "g1",
    request_id: "",
    seq: events.length,
    data: { events, truncated: false, state: SNAPSHOT_STATE },
  } as unknown as SessionSnapshot;
  const snapshotView = applySnapshot(emptyView("s1"), snapshot);
  expect(snapshotView.turns).toHaveLength(2);
  const [first, second] = snapshotView.turns;
  if (!first || !second) throw new Error("expected two turns");

  const onFill = vi.fn();
  render(
    <RetryReview turn={first} editorEmpty incomplete={false} onFill={onFill} onClose={vi.fn()} />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Put the prompt back in the message box" }));
  expect(onFill).toHaveBeenCalledWith("first prompt", []);
  cleanup();

  render(
    <RetryReview turn={second} editorEmpty incomplete={false} onFill={vi.fn()} onClose={vi.fn()} />,
  );
  const button = screen.getByRole("button", {
    name: "Put the prompt back in the message box",
  }) as HTMLButtonElement;
  expect(button.disabled).toBe(true);
  const sent = new Date(secondAt).toLocaleString("en-US");
  expect(screen.getByRole("dialog").textContent).toContain(
    `Only this window's copy of this prompt was cut short (sent ${sent}). Drafts and send history → Send history → Restore text opens the full text in a new conversation's message box; copy it from there into this one to retry here.`,
  );
});

test("a non-empty editor disables the fill action and explains why, instead of filling it", () => {
  const onFill = vi.fn();
  render(
    <RetryReview
      turn={turnWithTools()}
      editorEmpty={false}
      incomplete={false}
      onFill={onFill}
      onClose={vi.fn()}
    />,
  );
  const button = screen.getByRole("button", {
    name: "Put the prompt back in the message box",
  }) as HTMLButtonElement;
  expect(button.disabled).toBe(true);
  expect(screen.getByRole("dialog").textContent).toContain(
    "Only an empty message box can take it.",
  );
  fireEvent.click(button);
  expect(onFill).not.toHaveBeenCalled();
});

test("closing the review calls onClose; nothing here ever sends", () => {
  const onClose = vi.fn();
  const onFill = vi.fn();
  render(
    <RetryReview
      turn={turnWithTools()}
      editorEmpty
      incomplete={false}
      onFill={onFill}
      onClose={onClose}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(onClose).toHaveBeenCalledOnce();
  expect(onFill).not.toHaveBeenCalled();
});

test("escape dismisses the review through useModalFocus, the same as Close", () => {
  const onClose = vi.fn();
  render(
    <RetryReview
      turn={turnWithTools()}
      editorEmpty
      incomplete={false}
      onFill={vi.fn()}
      onClose={onClose}
    />,
  );
  fireEvent.keyDown(window, { key: "Escape" });
  expect(onClose).toHaveBeenCalledOnce();
});

test("a WebFetch row carries the web flag", () => {
  const v = view([
    userMessage("fetch it"),
    turnStarted,
    toolStarted("f1", "WebFetch", { head: "fetch", rest: "" }),
    toolDone("f1", { summary: "fetched" }),
    turnDone("error"),
  ]);
  const turn = v.turns.at(-1);
  if (!turn) throw new Error("no turn");
  expect(retryFlags(turn)).toEqual([{ tool: "WebFetch", summary: "fetched", flag: "web" }]);
});

// jsdom applies no CSS from stylesheets (only inline styles), so the fixed, scrollable layout
// is pinned against the source, the way Conversation.css.unit.test.ts pins its own rules.
test("RetryReview.css: .retry-review is a fixed, scroll-bounded panel above the page", () => {
  const css = readCss(import.meta.url, "RetryReview.css");
  const rule = ruleIn(css, ".retry-review");
  expect(rule).toMatch(/position:\s*fixed;/);
  expect(rule).toMatch(/max-height:/);
  expect(rule).toMatch(/overflow-y:\s*auto;/);
  expect(rule).toMatch(/z-index:\s*51;/);
});

test("RetryReview.css: .retry-review li wraps its cells instead of overflowing", () => {
  const css = readCss(import.meta.url, "RetryReview.css");
  const rule = ruleIn(css, ".retry-review li");
  expect(rule).toMatch(/display:\s*flex;/);
  expect(rule).toMatch(/flex-wrap:\s*wrap;/);
  expect(rule).toMatch(/column-gap:\s*8px;/);
  expect(rule).toMatch(/overflow-wrap:\s*anywhere;/);
});

test("RetryReview.css: the narrow block uses the narrow layout's negated media query", () => {
  const css = readCss(import.meta.url, "RetryReview.css");
  const narrow = narrowBlock(css);
  expect(narrow).toMatch(/width:\s*100%;/);
});

test("RetryReview.tsx imports its own stylesheet", () => {
  const source = readCss(import.meta.url, "RetryReview.tsx");
  expect(source).toContain('import "./RetryReview.css"');
});
