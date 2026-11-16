/**
 * One vault proposal in full: the note it changes, where it came from, and the diff against the
 * note as it is now. Every server string, the diff included, is a text node: the proposed text is
 * never rendered as markdown or HTML. Handing off and withdrawing only change the app's own
 * proposal row; the note itself is changed by the user, in the editor or through the model.
 */
import { type ReactElement, useEffect, useId, useRef, useState } from "react";
import type { Key } from "../../i18n";
import { t } from "../../i18n/t";
import type { DecisionsApi, ProposalDetail, VaultProposalItem } from "../../state/decisionsApi";
import { CopyButton } from "../Conversation/Copy";
import { useModalFocus } from "../Dialog/useModalFocus";
import "./DecisionInbox.css";
import { proposalHandoffText } from "./handoff";

export interface ProposalInspectorProps {
  api: DecisionsApi;
  item: VaultProposalItem;
  /** The proposal's vault name; null shows none. */
  workspaceName: string | null;
  /** The window's active workspace: hand-off and the editor are offered only for its proposals. */
  activeWorkspaceId: string | null;
  onClose: () => void;
  /** The proposal the server returned after a hand-off, or null once it was withdrawn. */
  onChanged: (item: VaultProposalItem | null) => void;
  onHandoff: (text: string) => void;
  onOpenEditor: (detail: ProposalDetail, origin: HTMLDivElement) => void;
}

// An origin kind with no row shows the kind itself.
const ORIGIN_KEY: Record<string, Key> = {
  note_edit: "decisions.proposal.origin_note_edit",
  memory_promotion: "decisions.proposal.origin_memory_promotion",
};

function originLabel(kind: string): string {
  const key = ORIGIN_KEY[kind];
  return key ? t(key) : kind;
}

// A proposal hands off only while it waits for the user.
const HANDOFF_STATES: ReadonlySet<string> = new Set(["open", "handed"]);

export function ProposalInspector({
  api,
  item,
  workspaceName,
  activeWorkspaceId,
  onClose,
  onChanged,
  onHandoff,
  onOpenEditor,
}: ProposalInspectorProps): ReactElement {
  const [detail, setDetail] = useState<ProposalDetail | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [actionFailed, setActionFailed] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();

  useModalFocus({ active: true, dialogRef, initialFocusRef: closeRef, onDismiss: onClose });

  useEffect(() => {
    const controller = new AbortController();
    api
      .proposalDetail(item.id, controller.signal)
      .then(setDetail)
      .catch(() => {
        if (!controller.signal.aborted) setLoadFailed(true);
      });
    return () => controller.abort();
  }, [api, item.id]);

  async function handoff() {
    if (!detail || busy) return;
    setBusy(true);
    setActionFailed(false);
    try {
      const next = await api.proposalAct(detail.id, "handoff");
      if (next) {
        setDetail({ ...detail, ...next });
        onChanged(next);
      }
      onHandoff(proposalHandoffText(detail));
    } catch {
      setActionFailed(true);
    } finally {
      setBusy(false);
    }
  }

  async function withdraw() {
    if (!detail || busy) return;
    setBusy(true);
    setActionFailed(false);
    try {
      await api.proposalAct(detail.id, "withdraw");
      onChanged(null);
      onClose();
    } catch {
      setActionFailed(true);
      setConfirming(false);
    } finally {
      setBusy(false);
    }
  }

  const active = !!detail && detail.origin.workspace_id === activeWorkspaceId;
  const canHandoff = active && !!detail && HANDOFF_STATES.has(detail.state);

  return (
    <div className="decision-inbox-backdrop">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="decision-inbox"
      >
        <div className="decision-inbox-head">
          <h2 id={titleId} className="decision-inbox-title jp">
            {item.ref}
          </h2>
          <button type="button" ref={closeRef} className="btn txt sm" onClick={onClose}>
            {t("decisions.retro.close")}
          </button>
        </div>
        {loadFailed ? (
          <p className="decision-inbox-note jp">{t("decisions.retro.load_failed")}</p>
        ) : null}
        {detail ? (
          <>
            <span className="state-chip jp" data-state={detail.state}>
              {t(`decisions.chip.${detail.state}` as Key)}
            </span>
            <p className="decision-inbox-note jp">
              {workspaceName ? `${workspaceName} · ` : ""}
              {originLabel(detail.origin_kind)}
            </p>
            {detail.diff_reason === "too_large" ? (
              <p className="decision-inbox-note jp">{t("decisions.proposal.too_large")}</p>
            ) : detail.diff_reason === "no_source" ? (
              <p className="decision-inbox-note jp">{t("decisions.proposal.no_source")}</p>
            ) : (
              <pre className="mono" style={{ maxHeight: "40vh", overflow: "auto" }}>
                {detail.diff}
              </pre>
            )}
            {active ? null : (
              <p className="decision-inbox-note jp">{t("decisions.proposal.other_vault")}</p>
            )}
            {actionFailed ? (
              <p className="decision-inbox-note jp" role="status">
                {t("decisions.proposal.action_failed")}
              </p>
            ) : null}
            {confirming ? (
              <>
                <p className="decision-inbox-note jp">{t("decisions.proposal.withdraw_ask")}</p>
                <div className="decisions-batch">
                  <button type="button" className="btn alt sm" disabled={busy} onClick={withdraw}>
                    {t("decisions.proposal.withdraw_yes")}
                  </button>
                  <button
                    type="button"
                    className="btn alt sm"
                    disabled={busy}
                    onClick={() => setConfirming(false)}
                  >
                    {t("decisions.proposal.withdraw_no")}
                  </button>
                </div>
              </>
            ) : (
              <div className="decisions-batch">
                <CopyButton
                  getText={() => detail.proposed_text}
                  label={t("decisions.proposal.copy_label")}
                />
                <button
                  type="button"
                  className="btn alt sm"
                  disabled={busy || !canHandoff}
                  onClick={handoff}
                >
                  {t("decisions.retro.handoff")}
                </button>
                <button
                  type="button"
                  className="btn alt sm"
                  disabled={!active}
                  onClick={() => {
                    if (dialogRef.current) onOpenEditor(detail, dialogRef.current);
                  }}
                >
                  {t("decisions.proposal.open_editor")}
                </button>
                <button
                  type="button"
                  className="btn alt sm"
                  disabled={busy}
                  onClick={() => setConfirming(true)}
                >
                  {t("decisions.proposal.withdraw")}
                </button>
              </div>
            )}
          </>
        ) : null}
      </div>
    </div>
  );
}
