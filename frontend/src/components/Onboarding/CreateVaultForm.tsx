/**
 * The "Create a vault" form: name, parent, folder, a portable metadata
 * checkbox. On blur or 150ms of quiet typing the name suggests parent/folder, but only
 * into fields the user has not edited themselves. Every backend refusal lands on the field or the
 * form it concerns; a stalled previous attempt (`hint: "cancel_bootstrap"`) offers to cancel it,
 * and `hint: "open_existing"` hands the attempted destination to the Open form instead — but only
 * when the attempt actually named a full destination (both parent and folder set); otherwise the
 * refusal is the plain `destination_not_empty` field message, never a bare leaf handed downstream.
 */
import { type ReactElement, useEffect, useRef, useState } from "react";
import {
  type CreateVaultInput,
  type CreateVaultResult,
  type ShellApi,
  ShellApiError,
} from "../../clients/app/shellApi";
import { errorText, t } from "../../i18n/t";
import { EXAMPLE_PATH_KEY, platformKind } from "../../platform";
import type { ShellStore } from "../../state/shell";
import { isComposingKeydown, PathForm } from "./PathForm";
import { StarterChoice, type StarterTemplate } from "./StarterChoice";
import { ambiguousFailure, fieldMessage, useSelectAfterVaultAction } from "./useVaultAction";

/** `/` only when the parent is already POSIX-shaped (has `/`, no `\`); otherwise `\`, so a
 * Windows parent still joins as it always has. */
function joinSeparator(parent: string): "/" | "\\" {
  return parent.includes("/") && !parent.includes("\\") ? "/" : "\\";
}

export interface CreateVaultFormProps {
  api: ShellApi;
  store: ShellStore;
  onBack: () => void;
  onDone: () => void;
  onGoToWorkspaces: () => void;
  /** Switches the owning view to the Open form, prefilled with the destination this attempt was
   * aimed at (the `open_existing` hint). */
  onOpenExisting: (path: string) => void;
  /** Called once with a successful create's answer, after the select starts; must not throw. */
  onCreated?: (result: CreateVaultResult) => void;
}

const SUGGESTION_DEBOUNCE_MS = 150;

type Refusal =
  | { kind: "stalled_cancel"; workspaceId: string }
  // Named for what the hint actually means (occupied, not stalled — see the render site below,
  // which still owes this branch its own copy; `stalled_open` was the misreading that let the
  // wrong sentence through in the first place).
  | { kind: "occupied_open"; path: string }
  | { kind: "field"; field: "name" | "folder"; message: string }
  | { kind: "form"; message: string; retryable: boolean };

function classify(error: ShellApiError, parent: string, folder: string): Refusal {
  if (error.hint === "cancel_bootstrap") {
    const workspaceId = error.extra.workspace_id;
    if (typeof workspaceId === "string") return { kind: "stalled_cancel", workspaceId };
  }
  // Only a full destination (both fields set) is a real path to hand the Open form; a partial
  // or empty attempt falls through to the plain field message below instead of switching views
  // with a bare leaf or nothing at all.
  if (error.hint === "open_existing" && parent !== "" && folder !== "")
    return { kind: "occupied_open", path: `${parent}${joinSeparator(parent)}${folder}` };
  if (error.code === "invalid_name")
    return { kind: "field", field: "name", message: fieldMessage(error) };
  // 429 is the backend saying try again later; ambiguousFailure (status 0 apart from the
  // client's own invalid_input, any 5xx, or 408) is the client not knowing whether the write
  // landed at all — a dropped connection or a timed-out POST may have completed on the server.
  // Both get the same form-level, same-command-id retry rather than a false accusation against
  // the folder field.
  if (error.status === 429 || ambiguousFailure(error))
    return { kind: "form", message: fieldMessage(error), retryable: true };
  // case_collision, destination_not_empty (including a stand-down from open_existing above) and
  // everything else path-shaped: the folder field is this form's closest analogue to the single
  // "path" field the record describes for the Open form.
  return { kind: "field", field: "folder", message: fieldMessage(error) };
}

export function CreateVaultForm({
  api,
  store,
  onBack,
  onDone,
  onGoToWorkspaces,
  onOpenExisting,
  onCreated,
}: CreateVaultFormProps): ReactElement {
  const [name, setName] = useState("");
  const [parent, setParent] = useState("");
  const [folder, setFolder] = useState("");
  const [portable, setPortable] = useState(false);
  const [template, setTemplate] = useState<StarterTemplate>("viki");
  const [refusal, setRefusal] = useState<Refusal | null>(null);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [createdFiles, setCreatedFiles] = useState<string[] | null>(null);
  const [workspaceId, setWorkspaceId] = useState<string | null>(null);
  const commandIdRef = useRef(crypto.randomUUID());
  // The last submitted attempt, kept so a same-input resubmission after an ambiguous failure
  // reuses its command_id instead of minting a fresh one the backend cannot replay against.
  const lastAttemptRef = useRef<{
    name: string;
    parent: string;
    folder: string;
    portable: boolean;
    template: StarterTemplate;
  } | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  // Read inside applySuggestion's `.then`, not the state the render that scheduled it closed
  // over: typing into parent/folder while a suggestion request is in flight must win, whichever
  // order the two settle in.
  const parentDirtyRef = useRef(false);
  const folderDirtyRef = useRef(false);
  // The debounce only cancels a pending *timer*, never a request already sent, and a blur while
  // one is outstanding fires a second — so two suggestion requests can be in flight at once, for
  // two different names. This ticket lets only the answer to the newest request apply, whichever
  // order they land in.
  const suggestionTicketRef = useRef(0);
  // Skips firing a request when the name has not changed since the last one was sent — a blur
  // right after the debounce already fired for the same name would otherwise double it.
  const lastRequestedNameRef = useRef<string | null>(null);
  const select = useSelectAfterVaultAction(api, store);

  useEffect(
    () => () => {
      if (debounceRef.current !== undefined) clearTimeout(debounceRef.current);
    },
    [],
  );

  function applySuggestion(forName: string): void {
    if (lastRequestedNameRef.current === forName) return;
    lastRequestedNameRef.current = forName;
    const ticket = ++suggestionTicketRef.current;
    void api.vaultSuggestion(forName).then(
      (suggestion) => {
        // A newer request may have been sent (and even already answered) since this one went
        // out; an older answer landing after it must not overwrite the newer prefill.
        if (ticket !== suggestionTicketRef.current) return;
        if (!parentDirtyRef.current) setParent(suggestion.parent);
        if (!folderDirtyRef.current) setFolder(suggestion.folder);
      },
      () => {
        // A failed suggestion changes nothing: whatever the user typed, or left blank, stands.
      },
    );
  }

  function onNameChange(value: string): void {
    setName(value);
    if (debounceRef.current !== undefined) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => applySuggestion(value), SUGGESTION_DEBOUNCE_MS);
  }

  function onNameBlur(): void {
    if (debounceRef.current !== undefined) clearTimeout(debounceRef.current);
    applySuggestion(name);
  }

  async function submit(reuseCommandId: boolean): Promise<void> {
    if (!reuseCommandId) {
      // A retryable form-level refusal (5xx/timeout/network/408, or 429) does not tell us
      // whether the write landed. If the user presses Create/Enter again with the exact input
      // that just failed, treat it as the SAME attempt so the backend can replay it — the same
      // guarantee the dedicated Retry button gives, without depending on the user finding that
      // button instead of the one they already know. Any edit since the failure means a
      // different attempt, so it still gets a fresh id.
      const last = lastAttemptRef.current;
      const sameAsFailedAttempt =
        refusal?.kind === "form" &&
        refusal.retryable &&
        last !== null &&
        last.name === name &&
        last.parent === parent &&
        last.folder === folder &&
        last.portable === portable &&
        last.template === template;
      if (!sameAsFailedAttempt) commandIdRef.current = crypto.randomUUID();
    }
    setSubmitting(true);
    setRefusal(null);
    setCancelError(null);
    const input: CreateVaultInput = {
      name,
      ...(parent !== "" ? { parent } : {}),
      ...(folder !== "" ? { folder } : {}),
      portable_metadata: template === "viki" || portable,
      template,
    };
    try {
      const result = await api.createVault(commandIdRef.current, input);
      setCreatedFiles(result.created_files);
      setWorkspaceId(result.workspace.workspace_id);
      select.run(result.workspace.workspace_id);
      onCreated?.(result);
    } catch (error) {
      const shellError =
        error instanceof ShellApiError ? error : new ShellApiError(0, "unavailable");
      lastAttemptRef.current = { name, parent, folder, portable, template };
      setRefusal(classify(shellError, parent, folder));
    } finally {
      setSubmitting(false);
    }
  }

  async function cancelStalled(stalledWorkspaceId: string): Promise<void> {
    setSubmitting(true);
    setCancelError(null);
    try {
      await api.cancelBootstrap(crypto.randomUUID(), stalledWorkspaceId);
      setRefusal(null);
    } catch (error) {
      const shellError =
        error instanceof ShellApiError ? error : new ShellApiError(0, "unavailable");
      setCancelError(fieldMessage(shellError));
    } finally {
      setSubmitting(false);
    }
  }

  // While a Retry is on screen, the template and portable choices stay locked to whatever the
  // reservation already began with: the server finishes (and replays) that reservation's own
  // template regardless of what a changed choice would send, so letting the user change either
  // control here would only mislead, never change the outcome.
  const retryLocked =
    refusal?.kind === "stalled_cancel" || (refusal?.kind === "form" && refusal.retryable);

  if (workspaceId !== null && createdFiles !== null) {
    return (
      <div className="onboarding-form" data-region="create-vault-result">
        {createdFiles.length > 0 ? (
          <>
            <h3>{t("onboarding.create.created_files_heading")}</h3>
            <ul>
              {createdFiles.map((file) => (
                <li key={file} className="mono">
                  {file}
                </li>
              ))}
            </ul>
          </>
        ) : null}
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
      aria-label={t("cmd.workspace.create")}
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
      <h2>{t("cmd.workspace.create")}</h2>

      <StarterChoice
        store={store}
        template={template}
        onChange={setTemplate}
        disabled={submitting || retryLocked}
        {...(retryLocked ? { describedBy: "create-retry-lock-reason" } : {})}
      />
      {retryLocked ? (
        <p id="create-retry-lock-reason" className="onboarding-card-desc">
          {refusal?.kind === "stalled_cancel"
            ? t("onboarding.create.retry_cancel_reason")
            : t("onboarding.create.retry_lock_reason")}
        </p>
      ) : null}

      <div className="path-form-field">
        <label htmlFor="create-name">{t("onboarding.create.name_label")}</label>
        <input
          id="create-name"
          type="text"
          maxLength={256}
          value={name}
          disabled={submitting}
          aria-invalid={refusal?.kind === "field" && refusal.field === "name"}
          {...(refusal?.kind === "field" && refusal.field === "name"
            ? { "aria-describedby": "create-name-error" }
            : {})}
          onChange={(event) => onNameChange(event.target.value)}
          onBlur={onNameBlur}
          onKeyDown={(event) => {
            if (event.key !== "Enter" || isComposingKeydown(event)) return;
            event.preventDefault();
            void submit(false);
          }}
        />
        {refusal?.kind === "field" && refusal.field === "name" ? (
          <p id="create-name-error" className="field-error" role="alert">
            {refusal.message}
          </p>
        ) : null}
      </div>

      <PathForm
        id="create-parent"
        label={t("onboarding.create.parent_label")}
        value={parent}
        onChange={(value) => {
          parentDirtyRef.current = true;
          setParent(value);
        }}
        onEnter={() => void submit(false)}
        browse
        disabled={submitting}
      />
      <PathForm
        id="create-folder"
        label={t("onboarding.create.folder_label")}
        value={folder}
        onChange={(value) => {
          folderDirtyRef.current = true;
          setFolder(value);
        }}
        onEnter={() => void submit(false)}
        error={
          refusal?.kind === "field" && refusal.field === "folder" ? refusal.message : undefined
        }
        exampleText={t(EXAMPLE_PATH_KEY[platformKind()])}
        disabled={submitting}
      />

      {template === "blank" ? (
        <label className="onboarding-checkbox">
          <input
            type="checkbox"
            checked={portable}
            disabled={submitting || retryLocked}
            {...(retryLocked ? { "aria-describedby": "create-retry-lock-reason" } : {})}
            onChange={(event) => setPortable(event.target.checked)}
          />
          {t("onboarding.create.portable_metadata")}
        </label>
      ) : null}

      {refusal?.kind === "stalled_cancel" ? (
        <div role="alert" className="onboarding-stalled">
          <p>{t("onboarding.create.stalled")}</p>
          {cancelError !== null ? <p>{cancelError}</p> : null}
          <button
            type="button"
            className="btn alt"
            aria-disabled={submitting}
            onClick={() => {
              if (submitting) return;
              void cancelStalled(refusal.workspaceId);
            }}
          >
            {t("common.button.cancel_bootstrap")}
          </button>
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
        </div>
      ) : null}

      {refusal?.kind === "occupied_open" ? (
        <div role="alert" className="onboarding-stalled">
          {/* The hint means the destination already holds something, or a completed
           * registration already owns it — never a creation left running, which is what the
           * stalled sentence says, so this branch does not reuse that sentence. */}
          <p>{t("onboarding.create.occupied")}</p>
          <button
            type="button"
            className="btn go"
            aria-disabled={submitting}
            onClick={() => {
              if (submitting) return;
              onOpenExisting(refusal.path);
            }}
          >
            {t("onboarding.create.open_existing_button")}
          </button>
        </div>
      ) : null}

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
        aria-disabled={submitting || name === ""}
        onClick={() => {
          if (submitting || name === "") return;
          void submit(false);
        }}
      >
        {t("onboarding.create.submit")}
      </button>
    </form>
  );
}
