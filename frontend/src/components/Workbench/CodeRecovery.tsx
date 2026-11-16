import { useEffect, useRef, useState } from "react";
import type { VaultRecoveryStatus } from "../../state/vaultApi";
import { VaultOpsError } from "../../state/vaultOpsApi";

export interface CodeRecoveryApi {
  list(signal?: AbortSignal): Promise<VaultRecoveryStatus[]>;
  forget(operationId: string): Promise<unknown>;
}

export function CodeRecovery({
  recovery,
  workspaceId,
  refreshKey,
}: {
  recovery: CodeRecoveryApi;
  workspaceId: string;
  refreshKey: number;
}) {
  const [rows, setRows] = useState<VaultRecoveryStatus[]>([]);
  const [failed, setFailed] = useState(false);
  const readTarget = useRef<{ recovery: CodeRecoveryApi; workspaceId: string } | null>(null);
  const readCall = useRef<AbortController | null>(null);

  function read() {
    const target = readTarget.current;
    if (target === null) return;
    readCall.current?.abort();
    const controller = new AbortController();
    readCall.current = controller;
    return target.recovery.list(controller.signal).then(
      (list) => {
        if (!controller.signal.aborted) {
          setRows(list.filter((r) => r.workspace_id === target.workspaceId));
        }
      },
      () => {
        if (!controller.signal.aborted) setRows([]);
      },
    );
  }

  // biome-ignore lint/correctness/useExhaustiveDependencies: read uses the current mounted target; refreshKey is the caller's reload signal.
  useEffect(() => {
    readTarget.current = { recovery, workspaceId };
    setRows([]);
    setFailed(false);
    void read();
    return () => {
      readTarget.current = null;
      readCall.current?.abort();
    };
  }, [recovery, workspaceId, refreshKey]);

  async function forget(row: VaultRecoveryStatus) {
    const target = readTarget.current;
    if (target === null || row.workspace_id !== target.workspaceId) return;
    const ask =
      "This drops the app's record only; the file does not change. Temporary files starting .viki-stage- or .viki-backup- may remain in the folder.";
    if (!window.confirm(ask)) return;
    setFailed(false);
    try {
      await target.recovery.forget(row.operation_id);
    } catch (error) {
      // The record is already gone: the reread is the whole answer.
      if (!(error instanceof VaultOpsError && error.code === "unknown_publication")) {
        if (readTarget.current === target) setFailed(true);
        return;
      }
    }
    await read();
  }

  if (rows.length === 0) return null;
  return (
    <section className="code-recovery">
      <h3>Stopped saves</h3>
      <p>Restarting VIKI Agent finishes most of them.</p>
      <ul>
        {rows.map((row) => (
          <li key={row.operation_id}>
            <span className="mono">{row.ref}</span>
            <span>
              {row.reason === "conflict"
                ? "Stopped because the file changed before the save."
                : "The previous save did not finish and needs recovery."}
            </span>
            <button type="button" onClick={() => void forget(row)}>
              Forget the record
            </button>
          </li>
        ))}
      </ul>
      {failed ? <p role="status">That did not work. Try again in a moment.</p> : null}
    </section>
  );
}
