import { useEffect, useState } from "react";
import { type ChecksApi, ChecksApiError, type CodeCheckJson } from "../../clients/code/checksApi";
import { outcomeWords } from "../../clients/code/checkWords";

const oid12 = (oid: string) => oid.slice(0, 12);

type State =
  | { status: "loading" }
  | { status: "failed" }
  | { status: "ready"; rows: CodeCheckJson[] };

function lineFor(check: CodeCheckJson, headOid: string): string {
  const name = check.name ?? "";
  const run = check.last_run;
  if (!run) return `${name} — not run here yet`;
  if (run.outcome === "running") return `${name} — running`;
  let state: string;
  if (run.head_oid === null) state = "no Git state";
  else if (run.head_oid !== headOid) state = `another commit (${oid12(run.head_oid)})`;
  else if (run.dirty) state = "uncommitted changes at the time";
  else state = "matches this commit";
  return `${name} — ${outcomeWords(run)} · ${state}`;
}

/** Read-only: shows each check's last run in the reviewed checkout and enables nothing. */
export function CheckEvidence({
  api,
  workspaceId,
  worktreeId,
  headOid,
  shown,
  reloadKey,
}: {
  api: ChecksApi;
  workspaceId: string;
  worktreeId: string | null;
  headOid: string;
  /** False while the Review tab is hidden: no call, and one read when it shows again. */
  shown: boolean;
  /** A new value reads again at the same head: the Checks tab may have run since. */
  reloadKey: number;
}) {
  const [state, setState] = useState<State>({ status: "loading" });
  const [hidden, setHidden] = useState(false);
  // biome-ignore lint/correctness/useExhaustiveDependencies: a new head or reloadKey asks again
  useEffect(() => {
    if (!shown) return;
    const controller = new AbortController();
    setHidden(false);
    api
      .list(workspaceId, worktreeId, controller.signal)
      .then((rows) => {
        if (!controller.signal.aborted) setState({ status: "ready", rows });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        const code = error instanceof ChecksApiError ? error.code : "";
        if (code === "unsupported_platform" || code === "trust_required") setHidden(true);
        else setState({ status: "failed" });
      });
    return () => controller.abort();
  }, [api, workspaceId, worktreeId, headOid, shown, reloadKey]);

  if (hidden) return null;
  // An unreadable check has no name to show.
  const rows = state.status === "ready" ? state.rows.filter((check) => check.readable) : [];
  return (
    <section className="review-checks">
      <h3>Checks</h3>
      <p className="review-reason">
        Results of the checks saved in the Checks tab. VIKI does not require them to pass.
      </p>
      {state.status === "failed" && <p className="review-reason">Checks could not be loaded.</p>}
      {state.status === "ready" && state.rows.length === 0 && (
        <p className="review-reason">No checks yet. Add them in the Checks tab.</p>
      )}
      {rows.length > 0 && (
        <ul>
          {rows.map((check) => (
            <li key={check.check_id}>{lineFor(check, headOid)}</li>
          ))}
        </ul>
      )}
    </section>
  );
}
