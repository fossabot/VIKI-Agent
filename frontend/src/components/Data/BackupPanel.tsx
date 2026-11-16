/** Previews and creates a notes backup, then restores a backup into a new folder. The panel warns that backups are not encrypted, shows typed refusals, and renders server strings as text. */
import { type ReactElement, type RefObject, useRef, useState } from "react";
import { ShellApiError } from "../../clients/app/shellApi";
import type { Key } from "../../i18n/index";
import { t, tf } from "../../i18n/t";
import type { ShellStore } from "../../state/shell";
import {
  type BackupPreview,
  type BackupResult,
  type BackupSkip,
  KnowledgeError,
  type RestorePreview,
  type RestoreResult,
  type VaultKnowledgeApi,
} from "../../state/vaultKnowledgeApi";
import { useSnapshot } from "../../state/viki";
import { Loading } from "../Loading/Loading";
import { dataErrorText } from "./dataErrorText";
import type { RenderPathField } from "./ExportPanel";
import { formatBytes } from "./formatBytes";

export interface BackupPanelProps {
  knowledgeApi: VaultKnowledgeApi;
  store: ShellStore;
  renderPathField: RenderPathField;
  headingRef: RefObject<HTMLHeadingElement | null>;
}

const OWN_REFUSALS: Readonly<Record<string, Key>> = {
  denied_destination: "error.backup.denied_destination",
  invalid_path: "error.backup.invalid_path",
  backup_too_large: "error.backup.too_large",
  invalid_name: "error.backup.invalid_name",
  invalid_backup: "error.backup.invalid_backup",
  recovery_required: "error.backup.recovery_required",
};

function codeOf(error: unknown): string {
  return error instanceof KnowledgeError ? error.code : "unavailable";
}

function refusalText(code: string): string {
  const own = OWN_REFUSALS[code];
  return own ? t(own) : dataErrorText(new ShellApiError(0, code));
}

type Phase = "idle" | "loading" | "ready";

/**
 * One request's command id: reused while the request is unchanged and its outcome unknown. A
 * definitive answer (a success or a typed refusal) ends it, so the next run mints a new one; only
 * `unavailable` (a network failure, abort or timeout) keeps it.
 */
function useCommandId(): {
  get: (request: string) => string;
  answered: (code: string | null) => void;
} {
  const current = useRef<{ request: string; id: string } | null>(null);
  return {
    get: (request) => {
      if (current.current?.request !== request) {
        current.current = { request, id: crypto.randomUUID() };
      }
      return current.current.id;
    },
    answered: (code) => {
      if (code !== "unavailable") current.current = null;
    },
  };
}

function Coverage({ shown, total }: { shown: number; total: number }): ReactElement | null {
  return shown === total ? null : (
    <span>{tf("data.backup.coverage", { shown, total, omitted: total - shown })}</span>
  );
}

function Skips({
  title,
  rows,
  total,
}: {
  title: string;
  rows: BackupSkip[];
  total: number;
}): ReactElement | null {
  if (total === 0) return null;
  return (
    <>
      <p>{title}</p>
      {rows.length < total && (
        <p>
          <Coverage shown={rows.length} total={total} />
        </p>
      )}
      <ul className="data-import-workspaces">
        {rows.map((row) => (
          <li key={`${row.ref}\0${row.reason}`}>
            {tf("data.backup.skip_row", { ref: row.ref, reason: row.reason })}
          </li>
        ))}
      </ul>
    </>
  );
}

function Refusal({ code }: { code: string | null }): ReactElement | null {
  return code === null ? null : <p role="alert">{refusalText(code)}</p>;
}

function InstructionFiles({ files, total }: { files: string[]; total: number }): ReactElement {
  return (
    <>
      <dt>{t("data.backup.instruction_files")}</dt>
      <dd className="mono">
        {total === 0 ? t("data.backup.none") : files.join(", ")}
        {files.length < total && (
          <>
            {" "}
            <Coverage shown={files.length} total={total} />
          </>
        )}
      </dd>
    </>
  );
}

function BackupSection({
  knowledgeApi,
  store,
  renderPathField,
}: Omit<BackupPanelProps, "headingRef">): ReactElement {
  const vaults = useSnapshot(store).workspaces?.workspaces.filter((w) => w.kind === "vault") ?? [];
  const [picked, setPicked] = useState("");
  const vaultId = vaults.some((w) => w.workspace_id === picked)
    ? picked
    : (vaults[0]?.workspace_id ?? "");
  const [destination, setDestination] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [preview, setPreview] = useState<BackupPreview | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<BackupResult | null>(null);
  const [code, setCode] = useState<string | null>(null);
  const runningRef = useRef(false);
  const commandId = useCommandId();
  const generation = useRef(0);

  function changed(): void {
    generation.current += 1;
    setPhase("idle");
    setPreview(null);
    setConfirming(false);
    setResult(null);
    setCode(null);
  }

  function runPreview(): void {
    changed();
    setPhase("loading");
    const mine = generation.current;
    knowledgeApi.backupPreview(vaultId, destination).then(
      (value) => {
        if (mine !== generation.current) return;
        setPreview(value);
        setPhase("ready");
      },
      (error: unknown) => {
        if (mine !== generation.current) return;
        setPhase("idle");
        setCode(codeOf(error));
      },
    );
  }

  function runBackup(): void {
    if (runningRef.current) return;
    runningRef.current = true;
    setRunning(true);
    setCode(null);
    const id = commandId.get(JSON.stringify([vaultId, destination]));
    knowledgeApi
      .backup(vaultId, destination, id)
      .then(
        (value) => {
          commandId.answered(null);
          setResult(value);
        },
        (error: unknown) => {
          commandId.answered(codeOf(error));
          setCode(codeOf(error));
        },
      )
      .finally(() => {
        runningRef.current = false;
        setRunning(false);
        setConfirming(false);
      });
  }

  const fieldError = code === "invalid_path" ? refusalText(code) : null;

  return (
    <>
      <label htmlFor="backup-vault">{t("data.backup.vault")}</label>
      <select
        id="backup-vault"
        value={vaultId}
        onChange={(event) => {
          setPicked(event.target.value);
          changed();
        }}
      >
        {vaults.map((vault) => (
          <option key={vault.workspace_id} value={vault.workspace_id}>
            {vault.name}
          </option>
        ))}
      </select>
      {renderPathField({
        id: "backup-destination",
        label: t("data.backup.destination"),
        value: destination,
        onChange: (value) => {
          setDestination(value);
          changed();
        },
        error: fieldError,
        onSubmit: runPreview,
      })}
      <button
        type="button"
        className="btn alt sm"
        onClick={runPreview}
        disabled={vaultId === ""}
        aria-busy={phase === "loading"}
      >
        {t("data.button.preview")}
      </button>
      {phase === "loading" && <Loading>{t("common.state.loading")}</Loading>}
      <Refusal code={code === "invalid_path" ? null : code} />

      {phase === "ready" && preview && (
        <div className="data-export-preview">
          <dl className="data-export-result">
            <dt>{t("data.backup.files")}</dt>
            <dd className="mono">{preview.files}</dd>
            <dt>{t("data.export.result.bytes")}</dt>
            <dd className="mono">{formatBytes(preview.bytes)}</dd>
            <InstructionFiles
              files={preview.instruction_files}
              total={preview.instruction_files_total}
            />
          </dl>
          <Skips
            title={t("data.backup.skipped")}
            rows={preview.skipped}
            total={preview.skipped_total}
          />
          <p className="data-backup-warning">{t("data.backup.not_encrypted")}</p>
          {preview.local_recovery && (
            <p className="data-backup-warning" role="alert">
              {t("data.backup.local_recovery")}
            </p>
          )}
          {!confirming && result === null && (
            <button type="button" className="btn go sm" onClick={() => setConfirming(true)}>
              {t("data.backup.run")}
            </button>
          )}
          {confirming && (
            <fieldset>
              <legend>{t("data.backup.confirm_prompt")}</legend>
              <button type="button" className="btn go sm" onClick={runBackup} aria-busy={running}>
                {t("data.backup.confirm")}
              </button>
              <button type="button" className="btn txt sm" onClick={() => setConfirming(false)}>
                {t("common.button.cancel")}
              </button>
            </fieldset>
          )}
          {running && <Loading>{t("common.state.loading")}</Loading>}
          {result && (
            <>
              <p role="status">
                {result.replayed ? t("data.backup.replayed") : t("data.backup.done")}
              </p>
              <dl className="data-export-result">
                <dt>{t("data.backup.result.folder")}</dt>
                <dd className="mono">{result.folder}</dd>
                <dt>{t("data.backup.files")}</dt>
                <dd className="mono">{result.files}</dd>
                <dt>{t("data.export.result.bytes")}</dt>
                <dd className="mono">{formatBytes(result.bytes)}</dd>
              </dl>
              <Skips
                title={t("data.backup.skipped")}
                rows={result.skipped}
                total={result.skipped_total}
              />
              <p className="data-backup-warning">{t("data.backup.not_encrypted_done")}</p>
            </>
          )}
        </div>
      )}
    </>
  );
}

function RestoreSection({
  knowledgeApi,
  store,
  renderPathField,
}: Omit<BackupPanelProps, "headingRef">): ReactElement {
  const [backup, setBackup] = useState("");
  const [parent, setParent] = useState("");
  const [folder, setFolder] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [preview, setPreview] = useState<RestorePreview | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<RestoreResult | null>(null);
  const [code, setCode] = useState<string | null>(null);
  const runningRef = useRef(false);
  const commandId = useCommandId();
  const generation = useRef(0);

  function resetRun(): void {
    setConfirming(false);
    setResult(null);
    setCode(null);
  }

  function runPreview(): void {
    resetRun();
    setPreview(null);
    setPhase("loading");
    generation.current += 1;
    const mine = generation.current;
    knowledgeApi.backupRestorePreview(backup).then(
      (value) => {
        if (mine !== generation.current) return;
        setPreview(value);
        setPhase("ready");
      },
      (error: unknown) => {
        if (mine !== generation.current) return;
        setPhase("idle");
        setCode(codeOf(error));
      },
    );
  }

  function runRestore(): void {
    if (runningRef.current) return;
    runningRef.current = true;
    setRunning(true);
    setCode(null);
    const id = commandId.get(JSON.stringify([backup, parent, folder]));
    knowledgeApi
      .backupRestore(id, { backup, parent, folder })
      .then(
        (value) => {
          commandId.answered(null);
          setResult(value);
          void store.afterMutation(["capabilities", "workspaces"]);
        },
        (error: unknown) => {
          commandId.answered(codeOf(error));
          setCode(codeOf(error));
        },
      )
      .finally(() => {
        runningRef.current = false;
        setRunning(false);
        setConfirming(false);
      });
  }

  const sourceError = phase !== "ready" && code === "invalid_path" ? refusalText(code) : null;
  const parentError = phase === "ready" && code === "invalid_path" ? refusalText(code) : null;
  const panelCode = code === "invalid_path" ? null : code;

  return (
    <div className="data-backup-restore">
      <h3>{t("data.backup.restore.title")}</h3>
      {renderPathField({
        id: "restore-backup",
        label: t("data.backup.restore.source"),
        value: backup,
        onChange: (value) => {
          setBackup(value);
          generation.current += 1;
          setPhase("idle");
          setPreview(null);
          resetRun();
        },
        error: sourceError,
        onSubmit: runPreview,
      })}
      <button
        type="button"
        className="btn alt sm"
        onClick={runPreview}
        aria-busy={phase === "loading"}
      >
        {t("data.button.preview")}
      </button>
      {phase === "loading" && <Loading>{t("common.state.loading")}</Loading>}
      {phase !== "ready" && <Refusal code={panelCode} />}

      {phase === "ready" && preview && (
        <div className="data-export-preview">
          <dl className="data-export-result">
            <dt>{t("data.backup.restore.name")}</dt>
            <dd>{preview.source_name}</dd>
            <dt>{t("data.backup.restore.policy")}</dt>
            <dd>{preview.source_policy}</dd>
            <dt>{t("data.backup.files")}</dt>
            <dd className="mono">{preview.files}</dd>
            <dt>{t("data.export.result.bytes")}</dt>
            <dd className="mono">{formatBytes(preview.bytes)}</dd>
            <InstructionFiles
              files={preview.instruction_files}
              total={preview.instruction_files_total}
            />
            <dt>{t("data.backup.restore.backup_skipped")}</dt>
            <dd className="mono">{preview.backup_skipped}</dd>
          </dl>
          <Skips
            title={t("data.backup.skipped")}
            rows={preview.problems}
            total={preview.problems_total}
          />
          <p>
            {preview.protected
              ? t("data.backup.restore.protected")
              : t("data.backup.restore.unprotected")}
          </p>
          {renderPathField({
            id: "restore-parent",
            label: t("data.backup.restore.parent"),
            value: parent,
            onChange: (value) => {
              setParent(value);
              resetRun();
            },
            error: parentError,
            onSubmit: () => setConfirming(true),
          })}
          <label htmlFor="restore-folder">{t("data.backup.restore.folder")}</label>
          <input
            id="restore-folder"
            value={folder}
            onChange={(event) => {
              setFolder(event.target.value);
              resetRun();
            }}
          />
          {!confirming && result === null && (
            <button type="button" className="btn go sm" onClick={() => setConfirming(true)}>
              {t("data.backup.restore.run")}
            </button>
          )}
          {confirming && (
            <fieldset>
              <legend>{t("data.backup.restore.confirm_prompt")}</legend>
              <button type="button" className="btn go sm" onClick={runRestore} aria-busy={running}>
                {t("data.backup.restore.confirm")}
              </button>
              <button type="button" className="btn txt sm" onClick={() => setConfirming(false)}>
                {t("common.button.cancel")}
              </button>
            </fieldset>
          )}
          {running && <Loading>{t("common.state.loading")}</Loading>}
          <Refusal code={panelCode} />
          {result && (
            <>
              <p role="status">
                {result.replayed ? t("data.backup.replayed") : t("data.backup.restore.done")}
              </p>
              <dl className="data-export-result">
                <dt>{t("data.backup.restore.registered")}</dt>
                <dd>{result.workspace.name}</dd>
                <dt>{t("data.backup.files")}</dt>
                <dd className="mono">{result.files}</dd>
                <dt>{t("data.export.result.bytes")}</dt>
                <dd className="mono">{formatBytes(result.bytes)}</dd>
              </dl>
              <p>
                {result.verified
                  ? t("data.backup.restore.verified")
                  : t("data.backup.restore.not_verified")}
              </p>
              <Skips
                title={t("data.backup.skipped")}
                rows={result.problems}
                total={result.problems_total}
              />
            </>
          )}
        </div>
      )}
    </div>
  );
}

export function BackupPanel({
  knowledgeApi,
  store,
  renderPathField,
  headingRef,
}: BackupPanelProps): ReactElement {
  return (
    <section className="data-panel" aria-labelledby="data-panel-backup-h">
      <h2 id="data-panel-backup-h" ref={headingRef} tabIndex={-1}>
        {t("data.panel.backup.title")}
      </h2>
      <BackupSection knowledgeApi={knowledgeApi} store={store} renderPathField={renderPathField} />
      <RestoreSection knowledgeApi={knowledgeApi} store={store} renderPathField={renderPathField} />
    </section>
  );
}
