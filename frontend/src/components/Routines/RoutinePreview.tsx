/**
 * The rehearsal panel: what a run of the routine would use, and what would stop it. Nothing is
 * run. Every server string (names, the first turn, zone, reasons) is a text node.
 */
import type { ReactElement } from "react";
import type { RoutinePreviewData } from "../../clients/automation/routinesApi";
import { en, type Key } from "../../i18n";
import { t, tf } from "../../i18n/t";
import { reasonText } from "../../state/routines";
import { modelLabel } from "../Profiles/modelLabel";
import "./RoutinePreview.css";

export interface RoutinePreviewProps {
  preview: RoutinePreviewData;
  kind: "manual" | "daily" | "weekly";
  stale?: boolean;
  collapsePrompt?: boolean;
  scheduleZone?: string | null;
  /** The seat a scheduled run waits for a todo card for. */
  cardSeat?: string | undefined;
  /** The folder whose changed notes a scheduled run waits for. */
  changedFolder?: string | undefined;
  /** A confirmation stops the run at a draft instead of waiting. */
  stopAtDraft?: boolean;
}

function policyText(policy: string): string {
  const key = `routines.policy.${policy}`;
  return Object.hasOwn(en, key) ? t(key as Key) : policy;
}

/** The server's warning code; the one it sends today has its own sentence. */
function warningText(code: string): string {
  return code === "workspace_not_active"
    ? t("routines.warning.workspace_not_active")
    : reasonText(code);
}

/** One text a run sends, under its label: folded when the caller collapses prompts. */
function SentText({ label, text, collapse }: { label: string; text: string; collapse: boolean }) {
  return collapse ? (
    <details className="routine-preview-prompt">
      <summary>{label}</summary>
      <pre className="routine-preview-turn">{text}</pre>
    </details>
  ) : (
    <section className="routine-preview-block">
      <h4 className="lbl">{label}</h4>
      <pre className="routine-preview-turn">{text}</pre>
    </section>
  );
}

function Issues({
  title,
  codes,
  text,
}: {
  title: string;
  codes: string[];
  text(code: string): string;
}) {
  if (codes.length === 0) return null;
  return (
    <section className="routine-preview-block">
      {title ? <h4 className="lbl">{title}</h4> : null}
      <ul>
        {codes.map((code) => (
          <li key={code}>{text(code)}</li>
        ))}
      </ul>
    </section>
  );
}

export function RoutinePreview({
  preview,
  kind,
  stale = false,
  collapsePrompt = false,
  scheduleZone,
  cardSeat,
  changedFolder,
  stopAtDraft = false,
}: RoutinePreviewProps): ReactElement {
  const { resolved, next_five: slots, readiness } = preview;
  const { profile, workspace } = resolved;
  const modelText = profile?.model
    ? modelLabel(profile.model, null, [], t("profiles.model.connected"))
    : t("routines.preview.model_default");
  return (
    <section className="routine-preview">
      <h3>{t("routines.preview.title")}</h3>
      {stale ? <p role="status">Rehearsal is stale. Waiting for a successful refresh.</p> : null}
      <dl className="routine-preview-facts">
        <dt className="lbl">{t("routines.preview.profile")}</dt>
        <dd>
          {profile ? (
            <>
              <span>{profile.name}</span>
              <br />
              <span>
                {t("routines.preview.model")}: {modelText}
              </span>
              {profile.permission_mode ? (
                <>
                  <br />
                  <span>
                    {t("routines.preview.mode")}: {profile.permission_mode}
                    {profile.mode_source === "settings"
                      ? ` ${t("routines.preview.mode_settings")}`
                      : ""}
                  </span>
                </>
              ) : null}
            </>
          ) : (
            t("routines.preview.missing_profile")
          )}
        </dd>
        <dt className="lbl">{t("routines.preview.workspace")}</dt>
        <dd>
          {workspace ? (
            <>
              <span>
                {workspace.name} ({policyText(workspace.policy)})
              </span>
              {workspace.active ? null : (
                <>
                  <br />
                  <span>{t("routines.preview.not_active")}</span>
                </>
              )}
            </>
          ) : (
            t("routines.preview.missing_workspace")
          )}
        </dd>
        {scheduleZone !== undefined ? (
          <>
            <dt className="lbl">Schedule zone</dt>
            <dd>{scheduleZone ?? "Not specified"}</dd>
          </>
        ) : null}
        <dt className="lbl">Time limit</dt>
        <dd>{tf("routines.preview.deadline", { minutes: resolved.deadline_minutes })}</dd>
      </dl>
      {profile?.connection_pinned ? (
        <p className="routine-preview-note">
          {tf("routines.preview.connection_native", { model: modelText })}
        </p>
      ) : null}
      {resolved.system_prompt ? (
        <SentText
          label={t("routines.preview.system_prompt")}
          text={resolved.system_prompt}
          collapse={collapsePrompt}
        />
      ) : null}
      {resolved.first_turn !== null ? (
        <SentText
          label={t("routines.preview.first_turn")}
          text={resolved.first_turn}
          collapse={collapsePrompt}
        />
      ) : null}
      <section className="routine-preview-block">
        <h4 className="lbl">{t("routines.preview.next")}</h4>
        {kind === "manual" ? (
          <p>{t("routines.preview.manual")}</p>
        ) : (
          <ul className="routine-preview-slots">
            {slots.map((slot, index) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: slots are a fixed, ordered list; two can share a local time
              <li key={index}>
                <span className="mono">
                  {slot.local} {slot.zone}
                </span>
                {slot.note ? (
                  <span> {t(`routines.preview.${slot.note === "dst_gap" ? "gap" : "fold"}`)}</span>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </section>
      <Issues title={t("routines.preview.blockers")} codes={readiness.blockers} text={reasonText} />
      <Issues
        title={t("routines.preview.run_blockers")}
        codes={readiness.run_blockers}
        text={reasonText}
      />
      <Issues title="" codes={readiness.warnings} text={warningText} />
      {cardSeat === undefined ? null : (
        <p className="routine-preview-note">{tf("routines.preview.card", { seat: cardSeat })}</p>
      )}
      {changedFolder === undefined ? null : (
        <p className="routine-preview-note">
          {tf("routines.preview.changed", { folder: changedFolder })}
        </p>
      )}
      {stopAtDraft ? (
        <p className="routine-preview-note">{t("routines.preview.stop")}</p>
      ) : kind === "manual" ? null : (
        <p className="routine-preview-note">{t("routines.preview.unattended")}</p>
      )}
    </section>
  );
}
