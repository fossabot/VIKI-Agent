/** Prevents this app from resuming a session already open in another process, which could corrupt its transcript. The card offers a fork or close action, disables both after a choice, and renders holder details as text. */
import { type ReactElement, useState } from "react";
import type { SessionBlockedData } from "../../protocol";
import { entrypointBadge } from "../../state/entrypoint";
import "./Cards.css"; // .card, .card-head, .card-question, .card-actions, reused rather than a second shell
import "./Errors.css";

export interface BlockedCardProps {
  data: SessionBlockedData;
  onFork: () => void;
  onClose: () => void;
}

export function BlockedCard({ data, onFork, onClose }: BlockedCardProps): ReactElement {
  const [done, setDone] = useState(false);
  const where = entrypointBadge(data.holder.entrypoint);

  function fork(): void {
    if (done) return;
    setDone(true);
    onFork();
  }

  function close(): void {
    if (done) return;
    setDone(true);
    onClose();
  }

  return (
    <div className="card">
      <div className="card-head">
        <span className="chip">In use</span>
        <span className="blocked-holder">{`${where} · pid ${data.holder.pid}`}</span>
      </div>
      <div className="card-question jp">{`This conversation is still open in ${where}`}</div>
      <div className="blocked-line jp">
        Writing from both places can corrupt the record, so continue in a fork here.
      </div>
      <div className="card-actions jp">
        <button type="button" className="btn go" disabled={done} onClick={fork}>
          Continue in a fork
        </button>
        <button type="button" className="btn txt" disabled={done} onClick={close}>
          Close
        </button>
      </div>
    </div>
  );
}
