/**
 * Unit tests for components/Sources/ChatSources.tsx: the no-env guard, the "Files in this chat"
 * title and empty line, group order/counts, the Changed-open default, a row's ×count and
 * name/folder split, the "Show all N" reveal past 5, the closed "By agents N" fold, a note's
 * `openNote` open target, a code chat's `onOpenCode`, the no-button rule for an unresolved path
 * and for a path missing just the vault or the note-path gate, a search outside the vault staying
 * text, a web row rendering `SourceChip`, and Sources.css's `.chat-digest` height cap and file-row
 * flex layout.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import type {
  Block,
  RoutingBlock,
  SessionView,
  ToolBlock,
  ToolRowView,
  Turn,
} from "../../state/view";
import { emptyView } from "../../state/view";
import { readCss, ruleIn } from "../../testing/css";
import { ChatSources } from "./ChatSources";
import { publishSourcesEnv, resetSourcesEnv, type SourcesEnv } from "./sourcesEnv";

const SID = "11111111-1111-1111-1111-111111111111";

function readRow(
  id: string,
  path: string,
  options: { pathState?: "workspace_relative" | "unknown" } = {},
): ToolRowView {
  const { pathState = "workspace_relative" } = options;
  return {
    id,
    tool: "Read",
    row: { head: "Read", rest: path, tail: "" },
    done: true,
    isError: false,
    summary: "read",
    preview: "",
    artifactStart: {
      artifact_id: id,
      kind: "file",
      operation: "read",
      path,
      path_state: pathState,
    },
    artifactResult: {
      artifact_id: id,
      observation: "result",
      byte_size: 1,
      sha256: "",
      preview: "",
      truncated: false,
      source_revision: "",
      detail_ref: null,
    },
  };
}

function writeRow(id: string, path: string): ToolRowView {
  return {
    id,
    tool: "Write",
    row: { head: "Write", rest: path, tail: "" },
    done: true,
    isError: false,
    summary: "",
    preview: "",
    artifactStart: {
      artifact_id: id,
      kind: "file_change",
      operation: "write",
      path,
      path_state: "workspace_relative",
    },
    artifactResult: {
      artifact_id: id,
      observation: "result",
      byte_size: 1,
      sha256: "",
      preview: "",
      truncated: false,
      source_revision: "",
      detail_ref: null,
    },
  };
}

/** A MultiEdit row with no resolved workspace path, as a tool that touched several ranges at once
 * would leave — only `rest` for the digest to fall back to. */
function multiEditRow(id: string, rest: string): ToolRowView {
  return {
    id,
    tool: "MultiEdit",
    row: { head: "MultiEdit", rest, tail: "" },
    done: true,
    isError: false,
    summary: "",
    preview: "",
    artifactResult: {
      artifact_id: id,
      observation: "result",
      byte_size: 1,
      sha256: "",
      preview: "",
      truncated: false,
      source_revision: "",
      detail_ref: null,
    },
  };
}

function toolRow(id: string, tool: string, rest: string): ToolRowView {
  return {
    id,
    tool,
    row: { head: tool, rest, tail: "" },
    done: true,
    isError: false,
    summary: "",
    preview: "",
  };
}

function toolsBlock(...rows: ToolRowView[]): ToolBlock {
  return { kind: "tools", id: rows[0]?.id ?? "tools", rows };
}

function routingBlock(agent: string, rows: ToolRowView[]): RoutingBlock {
  return {
    kind: "routing",
    id: `routing-${agent}`,
    agent,
    model: "sonnet",
    description: "",
    reason: "",
    status: "completed",
    tools: rows,
  };
}

function turn(blocks: Block[], id = "t1"): Turn {
  return { id, user: null, blocks, status: "success", usage: null, durationMs: 0, history: false };
}

function view(turns: Turn[]): SessionView {
  return { ...emptyView(SID), turns };
}

function fakeEnv(overrides: Partial<SourcesEnv> = {}): SourcesEnv {
  return {
    workspaceId: undefined,
    inVault: () => true,
    openNote: () => {},
    openFolder: () => {},
    openUrl: () => {},
    ...overrides,
  };
}

/** One entry in each of the four groups: a root-level changed file, a read note, a web fetch and
 * a search, all from the main thread. */
function allGroupsTurn(): Turn {
  return turn([
    toolsBlock(
      writeRow("w1", "a.md"),
      readRow("r1", "30-knowledge/b.md"),
      toolRow("f1", "WebFetch", "example.com"),
      toolRow("g1", "Grep", "TODO"),
    ),
  ]);
}

afterEach(() => {
  cleanup();
  resetSourcesEnv();
});

test("renders nothing before a sources env is published", () => {
  const { container } = render(<ChatSources view={view([])} />);
  expect(container.firstChild).toBeNull();
});

test("shows the title and the empty line with no sources", () => {
  publishSourcesEnv(fakeEnv());
  render(<ChatSources view={view([])} />);
  expect(screen.getByText("Files in this chat", { selector: ".lbl" })).toBeTruthy();
  expect(
    screen.getByText(
      "No files yet. Files VIKI changes or reads, pages it fetches and searches it runs in this chat show up here.",
    ),
  ).toBeTruthy();
});

test("orders groups Changed, Read, Web, Searches with each group's entry count", () => {
  publishSourcesEnv(fakeEnv());
  const { container } = render(<ChatSources view={view([allGroupsTurn()])} />);
  const summaries = [...container.querySelectorAll(".digest-group > summary")].map(
    (el) => el.textContent,
  );
  expect(summaries).toEqual(["Changed 1", "Read 1", "Web 1", "Searches 1"]);
});

test("Changed starts open, the other groups start closed", () => {
  publishSourcesEnv(fakeEnv());
  const { container } = render(<ChatSources view={view([allGroupsTurn()])} />);
  const groups = [...container.querySelectorAll(".digest-group")] as HTMLDetailsElement[];
  expect(groups.map((el) => el.open)).toEqual([true, false, false, false]);
});

test("a repeated changed file shows ×count, its name first and its folder dimmed", () => {
  publishSourcesEnv(fakeEnv());
  const older = turn([toolsBlock(writeRow("w1", "frontend/src/app.ts"))], "t1");
  const newer = turn([toolsBlock(writeRow("w2", "frontend/src/app.ts"))], "t2");
  const { container } = render(<ChatSources view={view([older, newer])} />);
  expect(container.querySelector(".digest-name")?.textContent).toBe("app.ts");
  expect(container.querySelector(".digest-folder")?.textContent).toBe("frontend/src");
  expect(container.querySelector(".digest-times")?.textContent).toBe("×2");
});

test('shows 5 rows then "Show all N", revealing the rest on click', () => {
  publishSourcesEnv(fakeEnv());
  const rows = Array.from({ length: 7 }, (_, i) => writeRow(`w${i}`, `file-${i}.ts`));
  const { container } = render(<ChatSources view={view([turn([toolsBlock(...rows)])])} />);
  expect(container.querySelectorAll(".digest-row")).toHaveLength(5);
  fireEvent.click(screen.getByRole("button", { name: "Show all 7" }));
  expect(container.querySelectorAll(".digest-row")).toHaveLength(7);
});

test('an agent-only touch sits in a closed "By agents N" fold with its agent named', () => {
  publishSourcesEnv(fakeEnv());
  const t = turn([routingBlock("researcher", [readRow("r1", "30-knowledge/claim.md")])]);
  const { container } = render(<ChatSources view={view([t])} />);
  const fold = container.querySelector(".digest-agents") as HTMLDetailsElement | null;
  if (!fold) throw new Error("missing the By agents fold");
  expect(fold.open).toBe(false);
  expect(fold.querySelector("summary")?.textContent).toBe("By agents 1");
  expect(fold.querySelector(".digest-agent")?.textContent).toBe("researcher");
});

test("a note's row opens it through env.openNote", () => {
  const opened: string[] = [];
  publishSourcesEnv(fakeEnv({ openNote: (ref) => opened.push(ref) }));
  const t = turn([toolsBlock(readRow("r1", "30-knowledge/claim.md"))]);
  const { container } = render(<ChatSources view={view([t])} />);
  const button = container.querySelector('[title="30-knowledge/claim.md"]');
  if (!button) throw new Error("missing the note's open button");
  expect(button.tagName).toBe("BUTTON");
  fireEvent.click(button);
  expect(opened).toEqual(["30-knowledge/claim.md"]);
});

test("a code chat's changed/read rows call onOpenCode with the group's diff flag", () => {
  publishSourcesEnv(fakeEnv());
  const calls: [string, boolean][] = [];
  const t = turn([toolsBlock(writeRow("w1", "src/app.ts"), readRow("r1", "docs/readme.md"))]);
  const { container } = render(
    <ChatSources view={view([t])} onOpenCode={(path, withDiff) => calls.push([path, withDiff])} />,
  );
  const changed = container.querySelector('[title="src/app.ts"]');
  const read = container.querySelector('[title="docs/readme.md"]');
  if (!changed || !read) throw new Error("missing an open button");
  fireEvent.click(changed);
  fireEvent.click(read);
  expect(calls).toEqual([
    ["src/app.ts", true],
    ["docs/readme.md", false],
  ]);
});

test("a non-vault chat renders a workspace-relative note path as text, not a button", () => {
  publishSourcesEnv(fakeEnv({ inVault: () => false }));
  const t = turn([toolsBlock(readRow("r1", "notes/a.md"))]);
  const { container } = render(<ChatSources view={view([t])} />);
  const cell = container.querySelector('[title="notes/a.md"]');
  if (!cell) throw new Error("missing the row");
  expect(cell.tagName).toBe("SPAN");
});

test("a vault chat renders a workspace-relative non-note path as text, not a button", () => {
  publishSourcesEnv(fakeEnv());
  const t = turn([toolsBlock(readRow("r1", "src/app.ts"))]);
  const { container } = render(<ChatSources view={view([t])} />);
  const cell = container.querySelector('[title="src/app.ts"]');
  if (!cell) throw new Error("missing the row");
  expect(cell.tagName).toBe("SPAN");
});

test("a MultiEdit row and an unresolved-path Read row render as text, never a button", () => {
  publishSourcesEnv(fakeEnv());
  const t = turn([
    toolsBlock(
      multiEditRow("m1", "src/app.ts (3 edits)"),
      readRow("u1", "30-knowledge/a.md", { pathState: "unknown" }),
    ),
  ]);
  const { container } = render(<ChatSources view={view([t])} onOpenCode={() => {}} />);
  const multi = container.querySelector('[title="src/app.ts (3 edits)"]');
  const unresolved = container.querySelector('[title="30-knowledge/a.md"]');
  expect(multi?.tagName).toBe("SPAN");
  expect(unresolved?.tagName).toBe("SPAN");
});

test("a search outside the vault renders as text, not a chip", () => {
  publishSourcesEnv(fakeEnv({ inVault: () => false }));
  const t = turn([toolsBlock(toolRow("g1", "Grep", "TODO 30-knowledge/"))]);
  render(<ChatSources view={view([t])} />);
  expect(screen.getByText("TODO 30-knowledge/")).toBeTruthy();
  expect(screen.queryByRole("button", { name: /Searched/ })).toBeNull();
});

test("a web fetch renders SourceChip", () => {
  publishSourcesEnv(fakeEnv());
  const t = turn([toolsBlock(toolRow("f1", "WebFetch", "example.com"))]);
  const { container } = render(<ChatSources view={view([t])} />);
  expect(container.querySelector('[title="Fetched a page on example.com"]')).toBeTruthy();
});

test("Sources.css caps .chat-digest's height and scrolls", () => {
  const css = readCss(import.meta.url, "Sources.css");
  const rule = ruleIn(css, ".chat-digest");
  expect(rule).toContain("max-height: min(320px, 45vh)");
  expect(rule).toContain("overflow-y: auto");
});

test("Sources.css lays a file row's span out like its button, folder shrinking first", () => {
  const css = readCss(import.meta.url, "Sources.css");
  const textRule = ruleIn(css, ".digest-text:has(.digest-name)");
  expect(textRule).toContain("display: flex");
  expect(textRule).toMatch(/gap:\s*6px;/);
  const folderRule = ruleIn(css, ".digest-folder");
  expect(folderRule).toContain("min-width: 0");
  expect(folderRule).toContain("text-overflow: ellipsis");
  expect(folderRule).not.toMatch(/flex:\s*none/);
});
