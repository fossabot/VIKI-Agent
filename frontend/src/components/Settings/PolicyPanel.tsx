/**
 * A workspace root's markers, instructions, identity and the policy transition form. Used two
 * ways: inline under the selected row of `WorkspacesPage`
 * (which passes only `workspaceId`/`api`, so the panel fetches `policy(id)` itself) and as the
 * `renderInspection` render prop the onboarding open-form supplies from this file (which already
 * holds the freshly opened folder's `PolicyInspection` from `openVault`, passed as `inspection` so
 * the panel does not re-fetch what the caller already has).
 *
 * Nothing here writes vault content: note saves happen in the Vault view, never this panel.
 * The only mutation this panel can cause is the policy transition itself, gated on
 * every `acknowledgements_required` item and the backend's own `admissible` verdict.
 */
import { useEffect, useState } from "react";
import type {
  InstructionFile,
  PolicyInspection,
  PolicyKind,
  ShellApi,
  WorkspaceJson,
} from "../../clients/app/shellApi";
import { ShellApiError } from "../../clients/app/shellApi";
import type { Key } from "../../i18n/index";
import { errorText, t, tf } from "../../i18n/t";
import { Loading } from "../Loading/Loading";
import "./Pages.css";
import "./Settings.css";

export interface PolicyPanelProps {
  workspaceId: string;
  api: ShellApi | null;
  /** Pre-loaded inspection (the onboarding open-form's `openVault` result); when given, the panel
   * renders it directly instead of calling `policy(workspaceId)` itself. */
  inspection?: PolicyInspection;
  /** Called after a successful policy change, so a caller listing workspaces elsewhere (its
   * policy chip, its revision) can re-read. */
  onChanged?: () => void;
  /** The caller's own workspace listing, used only to resolve `overlaps` and
   * `identity.inherits_from` — both bare `workspace_id`s on the wire — to a name and root the
   * user can recognise. `WorkspacesPage` already holds this listing; the onboarding host does
   * not, and correctly falls back to the "not found" chip below rather than plumbing the shell
   * snapshot through a render prop just for this. */
  workspaces?: readonly WorkspaceJson[];
}

/** A bare `workspace_id` (from `overlaps` or `identity.inherits_from`) resolved against a known
 * listing, or the catalog's own "not found" chip when the id is not in it — never the raw uuid,
 * which gives the user no way to tell which workspace the "Overlapping workspaces" list (or the
 * inherited-from line) means. */
function registrationRef(id: string, workspaces: readonly WorkspaceJson[] | undefined) {
  const found = workspaces?.find((workspace) => workspace.workspace_id === id);
  if (found === undefined) return <span className="mono">{t("common.chip.not_found")}</span>;
  return (
    <>
      {found.name} <span className="mono">{found.root}</span>
    </>
  );
}

const MARKER_KEYS = [
  "claude_md",
  "agents_md",
  "claude_dir",
  "hooks",
  "git",
  "viki_manifest",
] as const;

const TRANSITIONS: Readonly<Record<PolicyKind, readonly PolicyKind[]>> = {
  ordinary: ["existing_readonly", "protected"],
  existing_readonly: ["ordinary", "protected"],
  protected: [],
};

/** A short label for one of the three acknowledgement item codes (`instructions_present`,
 * `hooks_present`, `git_present`) — never the raw snake_case name, including in a 409
 * `acknowledgement_required`'s `extra.required` list. An unrecognised value (a future backend
 * addition) falls back to the catalog's own visible fallback, the dotted key itself, rather than
 * the raw code, so the text stays a translation, never a wire-format identifier on screen. */
function ackItemLabel(item: unknown): string {
  return t(`workspaces.ack_item.${String(item)}` as Key);
}

/** Whether the workspace this inspection describes admits vault note writes (matches the policy
 * response's `note_write`, `src/viki_agent/app/workspace_api.py`: `admits_note_writes(workspace)`):
 * `ordinary`, persistent, and bootstrap `complete` (App.tsx's write
 * grant checks the same row fields). Read from the listing row the panel already
 * receives; a caller that has no listing (the onboarding open-form) or no matching row gets the
 * conservative `false`, never a claim the Vault view cannot back up. */
function admitsNoteWrites(
  policy: PolicyKind,
  workspaceId: string,
  workspaces: readonly WorkspaceJson[] | undefined,
): boolean {
  if (policy !== "ordinary") return false;
  return rowAdmitsNoteWrites(workspaceId, workspaces);
}

/** Whether the listing row is persistent and bootstrap-complete: the case an ordinary workspace
 * admits note writes in. Used by `admitsNoteWrites`. */
function rowAdmitsNoteWrites(
  workspaceId: string,
  workspaces: readonly WorkspaceJson[] | undefined,
): boolean {
  const row = workspaces?.find((workspace) => workspace.workspace_id === workspaceId);
  return row?.persistent === true && row.bootstrap === "complete";
}

/** Whether the workspace row's own `bootstrap` is `"pending"` — the case where writes are not
 * admitted because setup has not finished, distinct from `read_only_notes` (a folder VIKI never
 * writes to at all). No row to check gives `false`, the same conservative fallback as
 * `admitsNoteWrites`. */
function bootstrapPending(
  workspaceId: string,
  workspaces: readonly WorkspaceJson[] | undefined,
): boolean {
  const row = workspaces?.find((workspace) => workspace.workspace_id === workspaceId);
  return row?.bootstrap === "pending";
}

function targetAdmissible(
  target: PolicyKind,
  admissible: PolicyInspection["admissible"],
): { ok: boolean; reason: string | null } {
  if (target === "ordinary") return { ok: admissible.ordinary, reason: admissible.reason };
  if (target === "protected") return { ok: admissible.protected, reason: admissible.reason };
  return { ok: true, reason: null };
}

function InstructionRow({ file }: { file: InstructionFile }) {
  return (
    <li className="policy-instruction">
      <span className="mono">{file.path}</span>
      <span className="policy-instruction-bytes">{file.bytes}</span>
      {file.too_large ? (
        <span className="chip dim">{t("workspaces.instruction.too_large")}</span>
      ) : null}
      {file.sha256 !== null ? <span className="mono">{file.sha256.slice(0, 12)}</span> : null}
      {file.head !== null ? <pre className="policy-instruction-head mono">{file.head}</pre> : null}
    </li>
  );
}

export function PolicyPanel({
  workspaceId,
  api,
  inspection: seeded,
  onChanged,
  workspaces,
}: PolicyPanelProps) {
  const [inspection, setInspection] = useState<PolicyInspection | null>(seeded ?? null);
  const [loadError, setLoadError] = useState<ShellApiError | null>(null);
  const [target, setTarget] = useState<PolicyKind | null>(null);
  const [checked, setChecked] = useState<ReadonlySet<string>>(new Set());
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<ShellApiError | null>(null);
  // setPolicy's own truthful `note_write`, held only until the next fresh load (a new workspace
  // or api instance): overrides the listing-row lookup so the sentence below updates the moment
  // an apply resolves, without waiting on the caller to re-pass a refreshed `workspaces` listing.
  const [applyNoteWrite, setApplyNoteWrite] = useState<boolean | null>(null);

  function reload(): void {
    if (api === null) return;
    setLoadError(null);
    api.policy(workspaceId).then(
      (value) => setInspection(value),
      (error: unknown) => {
        setLoadError(error instanceof ShellApiError ? error : new ShellApiError(0, "unavailable"));
      },
    );
  }

  useEffect(() => {
    setApplyNoteWrite(null);
    if (seeded !== undefined) {
      setInspection(seeded);
      setLoadError(null);
      return;
    }
    if (api === null) return;
    setLoadError(null);
    // Inlined rather than calling `reload` (a fresh function identity every render, which would
    // make the effect fire every render if listed as its dependency): re-fetches only when the
    // caller points the panel at a different workspace or api instance. `seeded` is compared by
    // reference deliberately, since a caller that wants a fresh render passes a fresh object.
    api.policy(workspaceId).then(
      (value) => setInspection(value),
      (error: unknown) => {
        setLoadError(error instanceof ShellApiError ? error : new ShellApiError(0, "unavailable"));
      },
    );
  }, [api, workspaceId, seeded]);

  if (inspection === null) {
    if (loadError !== null)
      return (
        <p className="settings-error" role="alert">
          {errorText(loadError)}
        </p>
      );
    return <Loading className="policy-loading">{t("common.state.loading")}</Loading>;
  }

  // A refetch after a successful apply (or a Retry click) can fail even though the panel already
  // has a document on screen: the error must stay visible, not just gate the very first render,
  // or a stale policy_revision sits there silently and the next Apply comes back stale_revision
  // for no reason the user can see.
  const staleLoadError =
    loadError !== null ? (
      <p className="settings-error" role="alert">
        {errorText(loadError)}
      </p>
    ) : null;

  const isCode = workspaces?.find((row) => row.workspace_id === workspaceId)?.kind === "code";
  const markerKeys = isCode
    ? [...MARKER_KEYS.slice(0, 3), "mcp_json" as const, ...MARKER_KEYS.slice(3)]
    : MARKER_KEYS;
  // A code folder has only the trust pair: Protected is a vault target with no way back.
  const targets = isCode
    ? TRANSITIONS[inspection.policy].filter((kind) => kind !== "protected")
    : TRANSITIONS[inspection.policy];
  const required = inspection.acknowledgements_required;
  const allChecked = required.every((item) => checked.has(item));

  function toggleAck(item: string): void {
    setChecked((previous) => {
      const next = new Set(previous);
      if (next.has(item)) next.delete(item);
      else next.add(item);
      return next;
    });
  }

  function chooseTarget(kind: PolicyKind, enabled: boolean): void {
    if (!enabled) return;
    setTarget(kind);
    setChecked(new Set());
    setSubmitError(null);
  }

  async function apply(): Promise<void> {
    if (api === null || inspection === null || target === null || !allChecked || submitting) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const result = await api.setPolicy(
        crypto.randomUUID(),
        workspaceId,
        inspection.policy_revision,
        target,
        [...checked],
      );
      setApplyNoteWrite(result.note_write);
      setTarget(null);
      setChecked(new Set());
      reload();
      onChanged?.();
    } catch (error) {
      setSubmitError(error instanceof ShellApiError ? error : new ShellApiError(0, "unavailable"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="policy-panel">
      {staleLoadError}
      <ul className="policy-markers">
        {markerKeys.map((key) => {
          const present = inspection.markers[key] === true;
          return (
            <li key={key} data-present={present}>
              <span className="policy-marker-mark" aria-hidden="true">
                {present ? "✓" : ""}
              </span>{" "}
              {t(`workspaces.marker.${key}`)}{" "}
              <span className="sr-only">
                {present ? t("workspaces.marker.present") : t("workspaces.marker.absent")}
              </span>
            </li>
          );
        })}
      </ul>

      {inspection.instructions.length > 0 ? (
        <ul className="policy-instructions">
          {inspection.instructions.map((file) => (
            <InstructionRow key={file.path} file={file} />
          ))}
        </ul>
      ) : null}

      <dl className="settings-facts">
        <div>
          <dt>{t("workspaces.identity.established")}</dt>
          <dd>
            {inspection.identity.established === true
              ? t("workspaces.marker.present")
              : t("workspaces.marker.absent")}
          </dd>
        </div>
        <div>
          <dt>{t("workspaces.identity.git_root_oids")}</dt>
          <dd>{String(inspection.identity.git_root_oids ?? 0)}</dd>
        </div>
        <div>
          <dt>{t("workspaces.identity.inherits_from")}</dt>
          <dd>
            {inspection.identity.inherits_from === null ||
            inspection.identity.inherits_from === undefined
              ? "—"
              : registrationRef(String(inspection.identity.inherits_from), workspaces)}
          </dd>
        </div>
      </dl>

      {inspection.overlaps.length > 0 ? (
        <section>
          <h3>{t("workspaces.overlaps.title")}</h3>
          <ul>
            {inspection.overlaps.map((id) => (
              <li key={id}>{registrationRef(id, workspaces)}</li>
            ))}
          </ul>
        </section>
      ) : null}

      {inspection.excluded_from_writes.length > 0 ? (
        <section>
          <h3>{t(isCode ? "workspaces.excluded.title_code" : "workspaces.excluded.title")}</h3>
          <ul>
            {inspection.excluded_from_writes.map((path) => (
              <li key={path} className="mono">
                {path}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {isCode ? (
        inspection.policy === "existing_readonly" ? (
          <p>{t("workspaces.code.untrusted_note")}</p>
        ) : inspection.policy === "ordinary" ? (
          <ul>
            {(
              [
                "workspaces.code.trusted_note",
                "workspaces.code.trust_writes",
                "workspaces.code.trust_loads",
                "workspaces.code.trust_sandbox",
              ] as const
            ).map((key) => (
              <li key={key}>{t(key)}</li>
            ))}
          </ul>
        ) : null
      ) : inspection.policy === "ordinary" ? (
        (applyNoteWrite ?? admitsNoteWrites(inspection.policy, workspaceId, workspaces)) ? (
          <p>{t("workspaces.policy.edit_in_vault")}</p>
        ) : bootstrapPending(workspaceId, workspaces) ? (
          <p>{t("workspaces.policy.bootstrap_pending")}</p>
        ) : (
          <p>{t("workspaces.policy.read_only_notes")}</p>
        )
      ) : null}

      {inspection.policy === "protected" ? (
        <p>{t("workspaces.policy.protected_locked")}</p>
      ) : (
        <fieldset className="policy-form">
          <legend>{t("workspaces.policy.form_title")}</legend>
          <div className="policy-targets">
            {targets.map((kind) => {
              const { ok, reason } = targetAdmissible(kind, inspection.admissible);
              const reasonId = `policy-target-reason-${workspaceId}-${kind}`;
              const reasonText =
                reason === null ? null : t(`workspaces.admissible.${reason}` as Key);
              return (
                <span key={kind}>
                  <button
                    type="button"
                    className="btn alt sm"
                    aria-pressed={target === kind}
                    aria-disabled={!ok}
                    aria-describedby={ok || reasonText === null ? undefined : reasonId}
                    title={ok ? undefined : (reasonText ?? undefined)}
                    onClick={() => chooseTarget(kind, ok)}
                  >
                    {isCode && kind === "ordinary"
                      ? t("workspaces.code.trust")
                      : isCode && kind === "existing_readonly"
                        ? t("workspaces.code.untrust")
                        : t(`workspaces.policy.${kind}`)}
                  </button>
                  {!ok && reasonText !== null ? (
                    <span id={reasonId} className="sr-only">
                      {reasonText}
                    </span>
                  ) : null}
                </span>
              );
            })}
          </div>

          {target !== null ? (
            <div className="policy-acknowledgements">
              {required.map((item) => (
                <label key={item}>
                  <input
                    type="checkbox"
                    checked={checked.has(item)}
                    onChange={() => toggleAck(item)}
                  />
                  {t(`workspaces.ack.${item}` as Key)}
                </label>
              ))}
              <button
                type="button"
                className="btn go sm"
                aria-disabled={!allChecked || submitting}
                onClick={() => {
                  if (allChecked && !submitting) void apply();
                }}
              >
                {t("common.button.apply")}
              </button>
            </div>
          ) : null}

          {submitError !== null ? (
            <p className="settings-error" role="alert">
              {submitError.code === "acknowledgement_required" &&
              Array.isArray(submitError.extra.required)
                ? tf("error.acknowledgement_required", {
                    required: submitError.extra.required.map(ackItemLabel).join(", "),
                  })
                : errorText(submitError)}
              {submitError.code === "stale_revision" ? (
                <button type="button" className="btn alt sm" onClick={reload}>
                  {t("common.button.retry")}
                </button>
              ) : null}
            </p>
          ) : null}
        </fieldset>
      )}
    </div>
  );
}
