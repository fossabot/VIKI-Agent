import { type ReactElement, useContext, useEffect, useRef, useState } from "react";
import { ArtifactContext } from "../../context/artifactContext";
import type { ToolRowView } from "../../state/view";
import "./ArtifactInspector.css";

export type ArtifactKind = "file" | "file_change" | "diff" | "test" | "unknown";
export type ArtifactStatus = "running" | "completed" | "failed" | "unknown";

export interface Artifact {
  readonly id: string;
  readonly kind: ArtifactKind;
  readonly status: ArtifactStatus;
  readonly origin: string;
  readonly path: string | null;
  readonly title: string;
  readonly detail: string;
  readonly hash: string | null;
  readonly byteSize: number | null;
  readonly truncated: boolean;
}

const MAX_DETAIL = 8_192;

function bounded(value: string): { value: string; truncated: boolean } {
  if (value.length <= MAX_DETAIL) return { value, truncated: false };
  return { value: `${value.slice(0, MAX_DETAIL)}\n…`, truncated: true };
}

export function artifactFromToolRow(row: ToolRowView): Artifact {
  const start = row.artifactStart;
  const result = row.artifactResult;
  const kind: ArtifactKind = start?.kind ?? "unknown";
  const boundedText = bounded([row.summary, row.preview].filter(Boolean).join("\n"));
  // Descriptor fields are the only source of artifact facts; output text is never parsed.
  const path = start && start.path_state === "workspace_relative" ? start.path : null;
  const status: ArtifactStatus = !row.done
    ? "running"
    : result?.observation === "result"
      ? "completed"
      : result?.observation === "error"
        ? "failed"
        : "unknown";
  return {
    id: row.id,
    kind,
    status,
    origin: row.tool || "unknown tool",
    path,
    title: row.summary.slice(0, 256) || row.tool || "Tool result",
    detail: result?.preview ?? boundedText.value,
    hash: result?.sha256 || null,
    byteSize: result?.byte_size ?? null,
    truncated: result
      ? result.truncated
      : boundedText.truncated || row.preview.endsWith("…") || row.row.rest.endsWith("…"),
  };
}

export interface ArtifactInspectorProps {
  readonly row: ToolRowView;
}

export function ArtifactInspector({ row }: ArtifactInspectorProps): ReactElement {
  const artifact = artifactFromToolRow(row);
  const context = useContext(ArtifactContext);
  const [detail, setDetail] = useState("");
  const [cursor, setCursor] = useState<number | null>(0);
  const [busy, setBusy] = useState(false);
  const [reason, setReason] = useState("");
  const operation = useRef<AbortController | null>(null);
  const ref = row.artifactResult?.detail_ref;
  useEffect(() => () => operation.current?.abort(), []);
  async function loadDetail(): Promise<void> {
    if (!context?.api.artifactDetail || !ref || cursor === null || operation.current) return;
    const controller = new AbortController();
    operation.current = controller;
    setBusy(true);
    setReason("");
    try {
      const page = await context.api.artifactDetail(
        context.sessionId,
        ref,
        cursor,
        crypto.randomUUID(),
        controller.signal,
      );
      if (controller.signal.aborted) return;
      if (page.state !== "ready") setReason(page.reason);
      else {
        setDetail((previous) => previous + page.text);
        setCursor(page.next_cursor);
        if (page.truncated) setReason("too_large");
      }
    } catch {
      if (!controller.signal.aborted) setReason("read_failed");
    } finally {
      if (operation.current === controller) {
        operation.current = null;
        setBusy(false);
      }
    }
  }
  return (
    <article className="artifact-inspector" aria-label="Artifact inspector">
      <header>
        <strong>{artifact.title}</strong>
        <span className={`artifact-status artifact-status-${artifact.status}`}>
          {
            {
              running: "running",
              completed: "result received",
              failed: "error",
              unknown: "unknown",
            }[artifact.status]
          }
        </span>
      </header>
      {artifact.path ? (
        <dl className="artifact-target">
          <div>
            <dt>Path</dt>
            <dd className="artifact-path">{artifact.path}</dd>
          </div>
        </dl>
      ) : null}
      <details className="artifact-facts">
        <summary>Technical details</summary>
        <dl>
          <div>
            <dt>Type</dt>
            <dd>{artifact.kind}</dd>
          </div>
          <div>
            <dt>Tool</dt>
            <dd>{artifact.origin}</dd>
          </div>
          {artifact.hash ? (
            <div>
              <dt>Hash</dt>
              <dd className="artifact-path">{artifact.hash}</dd>
            </div>
          ) : null}
          {artifact.byteSize !== null ? (
            <div>
              <dt>Size</dt>
              <dd>{artifact.byteSize} B</dd>
            </div>
          ) : null}
        </dl>
      </details>
      <pre>{artifact.detail}</pre>
      {detail ? <pre>{detail}</pre> : null}
      {ref && context?.api.artifactDetail && cursor !== null ? (
        <button type="button" disabled={busy} onClick={() => void loadDetail()}>
          {cursor ? "Load more detail" : "Load detail"}
        </button>
      ) : null}
      {reason ? (
        <p role="status">
          {reason === "source_changed"
            ? "The source changed. The preview is retained."
            : reason === "too_large"
              ? "Further detail exceeds the display limit."
              : reason === "time_budget"
                ? "This could not be loaded in time."
                : "Detail could not be loaded. The preview is retained."}
        </p>
      ) : null}
      {artifact.kind === "test" ? (
        <p>Native tool output. A completed tool call does not verify that every test passed.</p>
      ) : null}
      {artifact.truncated ? <p role="note">Some output is omitted from this preview.</p> : null}
    </article>
  );
}
