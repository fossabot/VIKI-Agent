/**
 * The "Open a folder" form: one path field, registered `existing_readonly`. On success the
 * inspection is rendered through the `renderInspection` render prop the Settings PolicyPanel
 * supplies — this form never imports a Settings file — then the folder is selected for the
 * next launch, exactly like Create.
 */
import { type ReactElement, type ReactNode, useRef, useState } from "react";
import { type PolicyInspection, type ShellApi, ShellApiError } from "../../clients/app/shellApi";
import { errorText, t } from "../../i18n/t";
import { EXAMPLE_PATH_KEY, platformKind } from "../../platform";
import type { ShellStore } from "../../state/shell";
import { PathForm } from "./PathForm";
import { ambiguousFailure, fieldMessage, useSelectAfterVaultAction } from "./useVaultAction";

export interface OpenVaultFormProps {
  api: ShellApi;
  store: ShellStore;
  initialPath?: string | undefined;
  onBack: () => void;
  onDone: () => void;
  onGoToWorkspaces: () => void;
  renderInspection: (inspection: PolicyInspection, workspaceId: string) => ReactNode;
}

type Refusal =
  | { kind: "field"; message: string }
  | { kind: "form"; message: string; retryable: boolean };

function classify(error: ShellApiError): Refusal {
  // 429 is the backend saying try again later; ambiguousFailure (status 0 apart from the
  // client's own invalid_input, any 5xx, or 408) is the client not knowing whether the write
  // landed at all — a dropped connection or a timed-out POST may have completed on the server.
  // Both get the same form-level, same-command-id retry rather than a false accusation against
  // the one path field.
  if (error.status === 429 || ambiguousFailure(error))
    return { kind: "form", message: fieldMessage(error), retryable: true };
  return { kind: "field", message: fieldMessage(error) };
}

export function OpenVaultForm({
  api,
  store,
  initialPath,
  onBack,
  onDone,
  onGoToWorkspaces,
  renderInspection,
}: OpenVaultFormProps): ReactElement {
  const [path, setPath] = useState(initialPath ?? "");
  const [refusal, setRefusal] = useState<Refusal | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [opened, setOpened] = useState<{
    workspaceId: string;
    inspection: PolicyInspection;
  } | null>(null);
  const commandIdRef = useRef(crypto.randomUUID());
  // The path last submitted, kept so a same-path resubmission after an ambiguous failure reuses
  // its command_id instead of minting a fresh one the backend cannot replay against.
  const lastAttemptPathRef = useRef<string | null>(null);
  const select = useSelectAfterVaultAction(api, store);

  async function submit(reuseCommandId: boolean): Promise<void> {
    if (path === "") return;
    if (!reuseCommandId) {
      // Same reasoning as CreateVaultForm's submit: reuse the id when the user presses
      // Open/Enter again with the exact path that just failed ambiguously, so a resubmission
      // that never touched the labelled Retry button still replays instead of minting anew.
      const sameAsFailedAttempt =
        refusal?.kind === "form" && refusal.retryable && lastAttemptPathRef.current === path;
      if (!sameAsFailedAttempt) commandIdRef.current = crypto.randomUUID();
    }
    setSubmitting(true);
    setRefusal(null);
    try {
      const result = await api.openVault(commandIdRef.current, path);
      setOpened({ workspaceId: result.workspace.workspace_id, inspection: result.inspection });
      select.run(result.workspace.workspace_id);
    } catch (error) {
      const shellError =
        error instanceof ShellApiError ? error : new ShellApiError(0, "unavailable");
      lastAttemptPathRef.current = path;
      setRefusal(classify(shellError));
    } finally {
      setSubmitting(false);
    }
  }

  if (opened !== null) {
    return (
      <div className="onboarding-form" data-region="open-vault-result">
        <p>{t("onboarding.open.registered_readonly")}</p>
        {renderInspection(opened.inspection, opened.workspaceId)}
        {select.outcome === null || select.outcome.kind === "pending" ? (
          <p role="status" aria-live="polite">
            {t("onboarding.select.selecting")}
          </p>
        ) : select.outcome.kind === "done" ? (
          <div role="status" aria-live="polite">
            {select.outcome.restartRequired ? (
              <p>{t("onboarding.select.restart_required")}</p>
            ) : null}
            <button type="button" className="btn go" onClick={onDone}>
              {t("shell.nav.back_to_chat")}
            </button>
          </div>
        ) : (
          <div role="alert">
            <p>{t("onboarding.select.failed_registered")}</p>
            <p>{errorText(select.outcome.error)}</p>
            <button type="button" className="btn alt" onClick={onGoToWorkspaces}>
              {t("onboarding.select.go_to_workspaces")}
            </button>
          </div>
        )}
      </div>
    );
  }

  return (
    <form
      className="onboarding-form"
      aria-label={t("cmd.workspace.open")}
      aria-busy={submitting}
      onSubmit={(event) => event.preventDefault()}
    >
      <button
        type="button"
        className="btn txt"
        aria-disabled={submitting}
        onClick={() => {
          if (submitting) return;
          onBack();
        }}
      >
        {t("onboarding.back")}
      </button>
      <h2>{t("cmd.workspace.open")}</h2>

      <PathForm
        id="open-path"
        label={t("onboarding.open.path_label")}
        value={path}
        onChange={setPath}
        onEnter={() => void submit(false)}
        browse
        error={refusal?.kind === "field" ? refusal.message : undefined}
        exampleText={t(EXAMPLE_PATH_KEY[platformKind()])}
        disabled={submitting}
      />

      {refusal?.kind === "form" ? (
        <div role="alert" className="onboarding-form-error">
          <p>{refusal.message}</p>
          {refusal.retryable ? (
            <button
              type="button"
              className="btn go"
              aria-disabled={submitting}
              onClick={() => {
                if (submitting) return;
                void submit(true);
              }}
            >
              {t("common.button.retry")}
            </button>
          ) : null}
        </div>
      ) : null}

      <button
        type="button"
        className="btn go"
        aria-disabled={submitting || path === ""}
        onClick={() => {
          if (submitting || path === "") return;
          void submit(false);
        }}
      >
        {t("onboarding.open.submit")}
      </button>
    </form>
  );
}
