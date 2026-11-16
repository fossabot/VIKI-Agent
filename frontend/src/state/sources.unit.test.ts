/**
 * Unit tests for state/sources.ts: the gating rule shared by every kind (done, not an error, no
 * errored artifact), the vault gate on note/search vs. an any-session web fetch, noteLabel's
 * SKILL/README/index special case, dedupe within a turn, and chatDigest's grouping, folding and
 * per-group ordering.
 */
import { expect, test } from "vitest";
import { chatDigest, noteLabel, turnSources } from "./sources";
import type { Block, RoutingBlock, ToolBlock, ToolRowView, Turn } from "./view";

function readRow(
  id: string,
  path: string,
  options: {
    pathState?: "workspace_relative" | "unknown";
    done?: boolean;
    isError?: boolean;
    observation?: "result" | "error" | "unknown";
  } = {},
): ToolRowView {
  const {
    pathState = "workspace_relative",
    done = true,
    isError = false,
    observation = "result",
  } = options;
  return {
    id,
    tool: "Read",
    row: { head: "Read", rest: path, tail: "" },
    done,
    isError,
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
      observation,
      byte_size: 1,
      sha256: "",
      preview: "",
      truncated: false,
      source_revision: "",
      detail_ref: null,
    },
  };
}

function toolRow(
  id: string,
  tool: string,
  rest: string,
  options: { done?: boolean; isError?: boolean } = {},
): ToolRowView {
  const { done = true, isError = false } = options;
  return { id, tool, row: { head: tool, rest, tail: "" }, done, isError, summary: "", preview: "" };
}

/** A Write/Edit/MultiEdit/NotebookEdit row. `noArtifact` drops `artifactStart` entirely, the way
 * a tool that never resolved a single workspace path would, leaving only `rest` for the digest
 * to fall back to. */
function fileRow(
  id: string,
  tool: string,
  path: string,
  options: {
    pathState?: "workspace_relative" | "unknown";
    rest?: string;
    done?: boolean;
    isError?: boolean;
    observation?: "result" | "error" | "unknown";
    noArtifact?: boolean;
  } = {},
): ToolRowView {
  const {
    pathState = "workspace_relative",
    rest = path,
    done = true,
    isError = false,
    observation = "result",
    noArtifact = false,
  } = options;
  return {
    id,
    tool,
    row: { head: tool, rest, tail: "" },
    done,
    isError,
    summary: "",
    preview: "",
    ...(noArtifact
      ? {}
      : {
          artifactStart: {
            artifact_id: id,
            kind: "file_change",
            operation: "write",
            path,
            path_state: pathState,
          },
        }),
    artifactResult: {
      artifact_id: id,
      observation,
      byte_size: 1,
      sha256: "",
      preview: "",
      truncated: false,
      source_revision: "",
      detail_ref: null,
    },
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

test("drops a Read row that failed, is unfinished, or whose artifact observed an error", () => {
  const failed = readRow("b1", "a.md", { isError: true });
  const unfinished = readRow("b2", "b.md", { done: false });
  const erroredArtifact = readRow("b3", "c.md", { observation: "error" });
  expect(turnSources(turn([toolsBlock(failed, unfinished, erroredArtifact)]), true)).toEqual([]);
});

test("a successful Read of a non-markdown file yields no source", () => {
  const image = readRow("b1", "70-people/photo.png");
  expect(turnSources(turn([toolsBlock(image)]), true)).toEqual([]);
});

test("a Read with an unknown path_state yields no note source", () => {
  const outside = readRow("b1", "30-knowledge/a.md", { pathState: "unknown" });
  expect(turnSources(turn([toolsBlock(outside)]), true)).toEqual([]);
});

test("a successful Read of a vault note produces a note source, labeled by its stem", () => {
  const read = readRow("b1", "30-knowledge/Deny rules hold even in bypass mode.md");
  expect(turnSources(turn([toolsBlock(read)]), true)).toEqual([
    {
      kind: "note",
      key: "note:30-knowledge/Deny rules hold even in bypass mode.md",
      label: "Deny rules hold even in bypass mode",
      ref: "30-knowledge/Deny rules hold even in bypass mode.md",
      toolUseId: "b1",
      tool: "Read",
      agent: "",
    },
  ]);
});

test("accepts the .markdown extension, case-insensitively", () => {
  const read = readRow("b1", "40-sources/plan.MARKDOWN");
  expect(turnSources(turn([toolsBlock(read)]), true)).toEqual([
    {
      kind: "note",
      key: "note:40-sources/plan.MARKDOWN",
      label: "plan",
      ref: "40-sources/plan.MARKDOWN",
      toolUseId: "b1",
      tool: "Read",
      agent: "",
    },
  ]);
});

test("vault=false suppresses note and search sources but keeps web", () => {
  const note = readRow("b1", "30-knowledge/a.md");
  const search = toolRow("b2", "Grep", "TODO 30-knowledge/");
  const web = toolRow("b3", "WebFetch", "example.com");
  const result = turnSources(turn([toolsBlock(note, search, web)]), false);
  expect(result).toEqual([
    {
      kind: "web",
      key: "web:example.com",
      label: "example.com",
      ref: "",
      toolUseId: "b3",
      tool: "WebFetch",
      agent: "",
    },
  ]);
});

test("search and web sources appear only with a non-empty rest, under vault", () => {
  const grep = toolRow("b1", "Grep", "TODO 30-knowledge/");
  const glob = toolRow("b2", "Glob", "**/*.md");
  const emptyRest = toolRow("b3", "Grep", "");
  const web = toolRow("b4", "WebFetch", "example.com");
  const result = turnSources(turn([toolsBlock(grep, glob, emptyRest, web)]), true);
  expect(result).toEqual([
    {
      kind: "search",
      key: "search:Grep:TODO 30-knowledge/",
      label: "TODO 30-knowledge/",
      ref: "",
      toolUseId: "b1",
      tool: "Grep",
      agent: "",
    },
    {
      kind: "search",
      key: "search:Glob:**/*.md",
      label: "**/*.md",
      ref: "",
      toolUseId: "b2",
      tool: "Glob",
      agent: "",
    },
    {
      kind: "web",
      key: "web:example.com",
      label: "example.com",
      ref: "",
      toolUseId: "b4",
      tool: "WebFetch",
      agent: "",
    },
  ]);
});

test("a Read nested in a routing block keeps the subagent's name", () => {
  const nested = readRow("b1", "30-knowledge/claim.md");
  const result = turnSources(turn([routingBlock("researcher", [nested])]), true);
  expect(result).toEqual([
    {
      kind: "note",
      key: "note:30-knowledge/claim.md",
      label: "claim",
      ref: "30-knowledge/claim.md",
      toolUseId: "b1",
      tool: "Read",
      agent: "researcher",
    },
  ]);
});

test("dedupes repeated reads of the same note within a turn, keeping the first", () => {
  const first = readRow("b1", "30-knowledge/claim.md");
  const second = readRow("b2", "30-knowledge/claim.md");
  const result = turnSources(turn([toolsBlock(first, second)]), true);
  expect(result).toEqual([
    {
      kind: "note",
      key: "note:30-knowledge/claim.md",
      label: "claim",
      ref: "30-knowledge/claim.md",
      toolUseId: "b1",
      tool: "Read",
      agent: "",
    },
  ]);
});

test("noteLabel uses the last segment without its extension", () => {
  expect(noteLabel("30-knowledge/Deny rules hold even in bypass mode.md")).toBe(
    "Deny rules hold even in bypass mode",
  );
});

test("noteLabel keeps the parent segment for SKILL, README and index stems, any case", () => {
  expect(noteLabel(".claude/skills/research/SKILL.md")).toBe("research/SKILL");
  expect(noteLabel("30-knowledge/readme.MD")).toBe("30-knowledge/readme");
  expect(noteLabel("50-maps/Index.markdown")).toBe("50-maps/Index");
});

test("noteLabel drops the parent for a special stem at the root", () => {
  expect(noteLabel("README.md")).toBe("README");
});

test("chatDigest groups rows by kind and orders each group newest first", () => {
  const older = turn([toolsBlock(fileRow("b1", "Write", "10-projects/old.md"))], "t1");
  const newer = turn(
    [
      toolsBlock(
        fileRow("b2", "Write", "10-projects/new.md"),
        readRow("b3", "30-knowledge/claim.md"),
        toolRow("b4", "WebFetch", "example.com"),
        toolRow("b5", "Grep", "TODO"),
      ),
    ],
    "t2",
  );
  const digest = chatDigest([older, newer], true);
  expect(digest.changed.map((entry) => entry.label)).toEqual([
    "10-projects/new.md",
    "10-projects/old.md",
  ]);
  expect(digest.changed[0]).toEqual({
    key: "changed:10-projects/new.md",
    path: "10-projects/new.md",
    label: "10-projects/new.md",
    count: 1,
    agents: [],
    viki: true,
    source: null,
  });
  expect(digest.read.map((entry) => entry.label)).toEqual(["30-knowledge/claim.md"]);
  expect(digest.search.map((entry) => entry.key)).toEqual(["search:Grep:TODO"]);
  expect(digest.web).toEqual([
    {
      key: "web:example.com",
      path: null,
      label: "example.com",
      count: 1,
      agents: [],
      viki: true,
      source: {
        kind: "web",
        key: "web:example.com",
        label: "example.com",
        ref: "",
        toolUseId: "b4",
        tool: "WebFetch",
        agent: "",
      },
    },
  ]);
});

test("chatDigest folds a subagent's earlier touch into the main thread's later one", () => {
  const bySub = routingBlock("researcher", [readRow("b1", "30-knowledge/claim.md")]);
  const byMain = toolsBlock(readRow("b2", "30-knowledge/claim.md"));
  const digest = chatDigest([turn([bySub, byMain])], true);
  expect(digest.read).toEqual([
    {
      key: "read:30-knowledge/claim.md",
      path: "30-knowledge/claim.md",
      label: "30-knowledge/claim.md",
      count: 2,
      agents: ["researcher"],
      viki: true,
      source: null,
    },
  ]);
});

test("chatDigest folds touches across subagents only, leaving viki false", () => {
  const byResearcher = routingBlock("researcher", [readRow("b1", "30-knowledge/claim.md")]);
  const byCurator = routingBlock("curator", [readRow("b2", "30-knowledge/claim.md")]);
  const digest = chatDigest([turn([byResearcher, byCurator])], true);
  expect(digest.read).toEqual([
    {
      key: "read:30-knowledge/claim.md",
      path: "30-knowledge/claim.md",
      label: "30-knowledge/claim.md",
      count: 2,
      agents: ["curator", "researcher"],
      viki: false,
      source: null,
    },
  ]);
});

test("chatDigest falls back to the row's text for a MultiEdit with no resolved path", () => {
  const edit = fileRow("b1", "MultiEdit", "", { noArtifact: true, rest: "src/app.ts (3 edits)" });
  const digest = chatDigest([turn([toolsBlock(edit)])], true);
  expect(digest.changed).toEqual([
    {
      key: "changed:src/app.ts (3 edits)",
      path: null,
      label: "src/app.ts (3 edits)",
      count: 1,
      agents: [],
      viki: true,
      source: null,
    },
  ]);
});

test("chatDigest drops rows that failed, are unfinished, or whose artifact errored", () => {
  const rows = [
    fileRow("b1", "Write", "a.md", { isError: true }),
    readRow("b2", "b.md", { done: false }),
    fileRow("b3", "Edit", "c.md", { observation: "error" }),
    toolRow("b4", "WebFetch", "example.com", { isError: true }),
    toolRow("b5", "Grep", "TODO", { done: false }),
  ];
  const digest = chatDigest([turn([toolsBlock(...rows)])], true);
  expect(digest).toEqual({ changed: [], read: [], web: [], search: [] });
});

test("chatDigest's search entry exists under vault=false but resolves a source only under vault", () => {
  const grep = toolRow("b1", "Grep", "TODO 30-knowledge/");
  const outside = chatDigest([turn([toolsBlock(grep)])], false);
  expect(outside.search).toEqual([
    {
      key: "search:Grep:TODO 30-knowledge/",
      path: null,
      label: "TODO 30-knowledge/",
      count: 1,
      agents: [],
      viki: true,
      source: null,
    },
  ]);
  const inside = chatDigest([turn([toolsBlock(grep)])], true);
  expect(inside.search[0]?.source).toEqual({
    kind: "search",
    key: "search:Grep:TODO 30-knowledge/",
    label: "TODO 30-knowledge/",
    ref: "",
    toolUseId: "b1",
    tool: "Grep",
    agent: "",
  });
});

test("chatDigest keeps the newest row as a repeated web fetch's source", () => {
  const first = toolRow("b1", "WebFetch", "example.com");
  const second = toolRow("b2", "WebFetch", "EXAMPLE.com");
  const digest = chatDigest([turn([toolsBlock(first, second)])], true);
  expect(digest.web).toEqual([
    {
      key: "web:example.com",
      path: null,
      label: "EXAMPLE.com",
      count: 2,
      agents: [],
      viki: true,
      source: {
        kind: "web",
        key: "web:example.com",
        label: "EXAMPLE.com",
        ref: "",
        toolUseId: "b2",
        tool: "WebFetch",
        agent: "",
      },
    },
  ]);
});

test("chatDigest still digests a Read with an unknown path_state, falling back to its row text", () => {
  const outside = readRow("b1", "30-knowledge/a.md", { pathState: "unknown" });
  const digest = chatDigest([turn([toolsBlock(outside)])], true);
  expect(digest.read).toEqual([
    {
      key: "read:30-knowledge/a.md",
      path: null,
      label: "30-knowledge/a.md",
      count: 1,
      agents: [],
      viki: true,
      source: null,
    },
  ]);
});
