import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type {
  ArtifactDetailPage,
  ConversationSearchApi,
} from "../../clients/conversations/conversationSearchApi";
import { ArtifactContext } from "../../context/artifactContext";
import type { ToolRowView } from "../../state/view";
import { ArtifactInspector, artifactFromToolRow } from "./ArtifactInspector";

afterEach(cleanup);

const row = (overrides: Partial<ToolRowView> = {}): ToolRowView => ({
  id: "tool-1",
  tool: "pytest",
  row: { head: "pytest", rest: "tests", tail: "" },
  done: true,
  isError: false,
  summary: "tool finished",
  preview: "",
  ...overrides,
});

test("Bash pytest without a descriptor stays unknown", () => {
  const artifact = artifactFromToolRow(
    row({ tool: "Bash", row: { head: "pytest", rest: "tests", tail: "" } }),
  );
  expect(artifact.kind).toBe("unknown");
  expect(artifact.status).toBe("unknown");
  expect(artifact.path).toBeNull();
  expect(artifact.origin).toBe("Bash");
});

test("explicit descriptor renders kind, status, path, and descriptor facts", () => {
  const artifactRow = row({
    artifactStart: {
      artifact_id: "art-1",
      kind: "file_change",
      operation: "write",
      path: "docs/guide.md",
      path_state: "workspace_relative",
    },
    artifactResult: {
      artifact_id: "art-1",
      observation: "result",
      byte_size: 2048,
      sha256: "ab12cd",
      preview: "the stored content",
      truncated: false,
      source_revision: "rev-1",
      detail_ref: null,
    },
  });
  const artifact = artifactFromToolRow(artifactRow);
  expect(artifact.kind).toBe("file_change");
  expect(artifact.status).toBe("completed");
  expect(artifact.path).toBe("docs/guide.md");
  expect(artifact.detail).toBe("the stored content");
  expect(artifact.hash).toBe("ab12cd");
  expect(artifact.byteSize).toBe(2048);
  expect(artifact.truncated).toBe(false);
  render(<ArtifactInspector row={artifactRow} />);
  expect(screen.getByText("file_change")).toBeTruthy();
  expect(screen.getByText("docs/guide.md")).toBeTruthy();
  expect(screen.getByText("ab12cd")).toBeTruthy();
  expect(screen.getByText("2048 B")).toBeTruthy();
});

test("path is hidden unless the backend marks it workspace_relative", () => {
  const artifactRow = row({
    artifactStart: {
      artifact_id: "art-2",
      kind: "test",
      operation: "test",
      path: "tests",
      path_state: "unknown",
    },
  });
  const artifact = artifactFromToolRow(artifactRow);
  expect(artifact.kind).toBe("test");
  expect(artifact.status).toBe("unknown");
  expect(artifact.path).toBeNull();
  render(<ArtifactInspector row={artifactRow} />);
  expect(screen.queryByText("Path")).toBeNull();
  expect(screen.queryByText("tests")).toBeNull();
});

test("error observation is failed and truncation comes from the descriptor", () => {
  const artifactRow = row({
    artifactStart: {
      artifact_id: "art-3",
      kind: "diff",
      operation: "edit",
      path: "src/app.ts",
      path_state: "workspace_relative",
    },
    artifactResult: {
      artifact_id: "art-3",
      observation: "error",
      byte_size: 0,
      sha256: "",
      preview: "boom",
      truncated: true,
      source_revision: "rev-1",
      detail_ref: null,
    },
  });
  const artifact = artifactFromToolRow(artifactRow);
  expect(artifact.status).toBe("failed");
  expect(artifact.hash).toBeNull();
  expect(artifact.truncated).toBe(true);
  render(<ArtifactInspector row={artifactRow} />);
  expect(screen.getByRole("note")).toBeTruthy();
});

test("unfinished rows run and a described row without a result stays unknown", () => {
  expect(artifactFromToolRow(row({ done: false, isError: true })).status).toBe("running");
  const described = row({
    artifactStart: {
      artifact_id: "art-4",
      kind: "file",
      operation: "read",
      path: "src/app.ts",
      path_state: "workspace_relative",
    },
  });
  expect(artifactFromToolRow(described).status).toBe("unknown");
  expect(artifactFromToolRow(described).kind).toBe("file");
});

test("unknown tool is never labeled succeeded", () => {
  render(<ArtifactInspector row={row({ tool: "mystery_tool" })} />);
  expect(screen.getAllByText("unknown").length).toBeGreaterThan(0);
  expect(screen.queryByText("succeeded")).toBeNull();
});

function pagedRow(): ToolRowView {
  return row({
    artifactResult: {
      artifact_id: "art-paged",
      observation: "result",
      byte_size: 20,
      sha256: "hash",
      preview: "preview",
      truncated: true,
      source_revision: "a".repeat(64),
      detail_ref: { entry_uuid: "id", tool_use_id: "tool", source_revision: "a".repeat(64) },
    },
  });
}

test("discloses technical facts without requesting detail and retains the exact target", () => {
  const artifactDetail = vi.fn<NonNullable<ConversationSearchApi["artifactDetail"]>>();
  const api: ConversationSearchApi = { search: vi.fn(), anchor: vi.fn(), artifactDetail };
  const artifactRow = {
    ...pagedRow(),
    summary: "Updated guide",
    artifactStart: {
      artifact_id: "art-paged",
      kind: "file_change" as const,
      operation: "write" as const,
      path: "notes/project/guide.md",
      path_state: "workspace_relative" as const,
    },
  };
  render(
    <ArtifactContext.Provider value={{ api, sessionId: "s" }}>
      <ArtifactInspector row={artifactRow} />
    </ArtifactContext.Provider>,
  );
  const summary = screen.getByText("Technical details");
  const details = summary.closest("details") as HTMLDetailsElement;
  expect(details).not.toBeNull();
  expect(details.open).toBe(false);
  expect(screen.getByText("hash").closest("details")).toBe(details);
  expect(screen.getByText("20 B").closest("details")).toBe(details);
  expect(screen.getByText("file_change").closest("details")).toBe(details);
  expect(screen.getByText("notes/project/guide.md").closest("details")).toBeNull();
  expect(screen.getByText("Updated guide").closest("header")).not.toBeNull();
  expect(screen.getByText("result received").closest("header")).not.toBeNull();
  expect(screen.getByText("preview").closest("details")).toBeNull();
  expect(screen.getByRole("note").closest("details")).toBeNull();
  expect(screen.getByRole("button", { name: "Load detail" }).closest("details")).toBeNull();

  fireEvent.click(summary);
  expect(details.open).toBe(true);
  fireEvent.click(summary);
  expect(details.open).toBe(false);
  expect(artifactDetail).not.toHaveBeenCalled();
  expect(api.search).not.toHaveBeenCalled();
  expect(api.anchor).not.toHaveBeenCalled();
});

test("a second detail page appends to the first instead of replacing it", async () => {
  const artifactDetail = vi.fn(
    async (_sid: string, _ref, cursor: number, id: string): Promise<ArtifactDetailPage> =>
      cursor === 0
        ? {
            request_id: id,
            state: "ready",
            reason: "",
            text: "first ",
            next_cursor: 10,
            byte_size: 20,
            truncated: false,
          }
        : {
            request_id: id,
            state: "ready",
            reason: "",
            text: "second",
            next_cursor: null,
            byte_size: 20,
            truncated: false,
          },
  );
  const api: ConversationSearchApi = { search: vi.fn(), anchor: vi.fn(), artifactDetail };
  const { container } = render(
    <ArtifactContext.Provider value={{ api, sessionId: "s" }}>
      <ArtifactInspector row={pagedRow()} />
    </ArtifactContext.Provider>,
  );
  const detailPre = () => container.querySelectorAll("pre")[1];
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Load detail" })));
  expect(detailPre()?.textContent).toBe("first ");
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Load more detail" })));
  expect(detailPre()?.textContent).toBe("first second");
});

test("an unmounted inspector aborts its in-flight detail request", () => {
  let signal: AbortSignal | undefined;
  const artifactDetail = vi.fn(
    (_sid: string, _ref, _cursor: number, _id: string, supplied?: AbortSignal) => {
      signal = supplied;
      return new Promise<ArtifactDetailPage>(() => {});
    },
  );
  const api: ConversationSearchApi = { search: vi.fn(), anchor: vi.fn(), artifactDetail };
  const { unmount } = render(
    <ArtifactContext.Provider value={{ api, sessionId: "s" }}>
      <ArtifactInspector row={pagedRow()} />
    </ArtifactContext.Provider>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Load detail" }));
  expect(signal?.aborted).toBe(false);
  unmount();
  expect(signal?.aborted).toBe(true);
});

test("a time-budget detail reason shows its own copy, not the generic failure", async () => {
  const artifactDetail = vi.fn(
    async (_sid: string, _ref, _cursor: number, id: string): Promise<ArtifactDetailPage> => ({
      request_id: id,
      state: "error",
      reason: "time_budget",
      text: "",
      next_cursor: null,
      byte_size: 0,
      truncated: false,
    }),
  );
  const api: ConversationSearchApi = { search: vi.fn(), anchor: vi.fn(), artifactDetail };
  render(
    <ArtifactContext.Provider value={{ api, sessionId: "s" }}>
      <ArtifactInspector row={pagedRow()} />
    </ArtifactContext.Provider>,
  );
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Load detail" })));
  expect(screen.getByText("This could not be loaded in time.")).toBeTruthy();
});

test("legacy text-only rows keep bounded detail without descriptor facts", () => {
  const artifactRow = row({ preview: "x".repeat(9_000) });
  const artifact = artifactFromToolRow(artifactRow);
  expect(artifact.detail.length).toBeLessThanOrEqual(8_194);
  expect(artifact.truncated).toBe(true);
  expect(artifact.hash).toBeNull();
  expect(artifact.byteSize).toBeNull();
  render(<ArtifactInspector row={artifactRow} />);
  expect(screen.getByRole("note")).toBeTruthy();
});
