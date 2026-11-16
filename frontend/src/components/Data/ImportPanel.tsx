/** Reads pasted or selected JSON as text, previews the import, and applies only the rows the user leaves checked. */
import {
  type ChangeEvent,
  type ReactElement,
  type ReactNode,
  type RefObject,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  type ImportChange,
  type ImportPreview,
  type ShellApi,
  ShellApiError,
} from "../../clients/app/shellApi";
import { t } from "../../i18n/t";
import type { ShellStore } from "../../state/shell";
import { useSnapshot } from "../../state/viki";
import { Loading } from "../Loading/Loading";
import { dataErrorText } from "./dataErrorText";

const MAX_CONTENT_UNITS = 262_144;
// UTF-8 costs at most 3 bytes per UTF-16 code unit (UTF-16LE, 2), so 4x headroom never refuses a
// file whose decoded text would in fact have fit in MAX_CONTENT_UNITS.
const MAX_FILE_BYTES = MAX_CONTENT_UNITS * 4;

export type RenderConflict = (error: ShellApiError) => ReactNode;

export interface ImportPanelProps {
  api: ShellApi | null;
  store: ShellStore;
  renderConflict: RenderConflict;
  headingRef: RefObject<HTMLHeadingElement | null>;
}

type Status = "idle" | "loading" | "ready" | "error";

function defaultChecked(preview: ImportPreview): Set<string> {
  return new Set(preview.changes.filter((c) => c.action === "apply").map((c) => c.key));
}

const PROVENANCE_KEYS = ["exported_at", "app_version", "launch_id"] as const;

function ChangeRow({
  change,
  checked,
  disabled,
  onToggle,
}: {
  change: ImportChange;
  checked: boolean;
  disabled: boolean;
  onToggle: () => void;
}): ReactElement {
  return (
    <tr>
      <td data-label={t("data.table.name")} className="mono">
        {change.key}
      </td>
      <td data-label={t("data.import.action.apply")}>
        {change.action === "apply" ? (
          <label>
            <input type="checkbox" checked={checked} disabled={disabled} onChange={onToggle} />
            {t("data.import.action.apply")}
          </label>
        ) : (
          t(`data.import.action.${change.action}`)
        )}
        {change.action === "conflict" && (
          <dl className="data-import-conflict">
            <dt>{t("data.import.current")}</dt>
            <dd>{String(change.current)}</dd>
            <dt>{t("data.import.incoming")}</dt>
            <dd>{String(change.incoming)}</dd>
          </dl>
        )}
      </td>
    </tr>
  );
}

export function ImportPanel({
  api,
  store,
  renderConflict,
  headingRef,
}: ImportPanelProps): ReactElement {
  const snapshot = useSnapshot(store);
  const [content, setContent] = useState("");
  const [previewStatus, setPreviewStatus] = useState<Status>("idle");
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [previewError, setPreviewError] = useState<ShellApiError | null>(null);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [applyStatus, setApplyStatus] = useState<Status>("idle");
  const [applyError, setApplyError] = useState<ShellApiError | null>(null);
  // `applyStatus` alone cannot stop two clicks dispatched in the same tick: React's state update
  // from the first click is not committed yet when the second click's handler reads it, so both
  // would pass an `applyStatus === "loading"` check. A ref is synchronous and unbatched.
  const applyingRef = useRef(false);
  const generation = useRef(0);
  const [previewGeneration, setPreviewGeneration] = useState(0);
  const settingsRevision = snapshot.settings?.revision;

  useEffect(() => {
    if (
      preview === null ||
      settingsRevision === undefined ||
      settingsRevision <= preview.settings_revision ||
      applyStatus === "loading" ||
      applyStatus === "ready"
    )
      return;
    // An older client snapshot cannot invalidate a newer server preview. Once this window
    // observes a later revision, its old eligibility and selection need an explicit Preview.
    setPreview(null);
    setPreviewStatus("idle");
    setChecked(new Set());
    setApplyStatus("error");
    setApplyError(new ShellApiError(404, "preview_expired"));
  }, [preview, settingsRevision, applyStatus]);

  function changed(): void {
    generation.current += 1;
    setPreview(null);
    setPreviewStatus("idle");
    setPreviewError(null);
    setApplyStatus("idle");
    setApplyError(null);
  }

  function handleFile(event: ChangeEvent<HTMLInputElement>): void {
    if (applyingRef.current) return;
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    changed();
    const mine = generation.current;
    if (file.size > MAX_FILE_BYTES) {
      setPreviewStatus("error");
      setPreviewError(new ShellApiError(0, "too_large"));
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      if (mine !== generation.current) return;
      if (typeof reader.result === "string") setContent(reader.result.slice(0, MAX_CONTENT_UNITS));
    };
    // A read can fail after a good pick (a OneDrive placeholder that will not hydrate, a
    // removable drive pulled mid-read, a file another process holds) — silence here would
    // leave the textarea's old content on screen with nothing to say the new pick never arrived.
    reader.onerror = reader.onabort = () => {
      if (mine !== generation.current) return;
      setPreviewStatus("error");
      setPreviewError(new ShellApiError(0, "unavailable"));
    };
    reader.readAsText(file);
  }

  function runPreview(): void {
    if (applyingRef.current) return;
    changed();
    const mine = generation.current;
    if (api === null) {
      setPreviewStatus("error");
      setPreviewError(new ShellApiError(0, "unavailable"));
      return;
    }
    setPreviewStatus("loading");
    api
      .importPreview(content)
      .then((value) => {
        if (mine !== generation.current) return;
        setPreviewGeneration(mine);
        setPreview(value);
        setChecked(defaultChecked(value));
        setPreviewStatus("ready");
      })
      .catch((error: unknown) => {
        if (mine !== generation.current) return;
        setPreviewStatus("error");
        setPreviewError(error as ShellApiError);
      });
  }

  function toggle(key: string): void {
    if (applyingRef.current) return;
    setChecked((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function runApply(): void {
    const current = store.getSnapshot().settings;
    if (
      api === null ||
      preview === null ||
      previewGeneration !== generation.current ||
      (current !== null && current.revision > preview.settings_revision) ||
      applyingRef.current
    )
      return;
    applyingRef.current = true;
    setApplyStatus("loading");
    setApplyError(null);
    api
      .importApply(preview.preview_id, crypto.randomUUID(), preview.settings_revision, [...checked])
      .then((result) => {
        store.adoptSettings(result);
        setApplyStatus("ready");
      })
      .catch((error: unknown) => {
        const failure = error as ShellApiError;
        if (failure.code === "stale_revision" || failure.code === "preview_expired") {
          setPreview(null);
          setPreviewStatus("idle");
          setChecked(new Set());
        }
        setApplyStatus("error");
        setApplyError(failure);
      })
      .finally(() => {
        applyingRef.current = false;
      });
  }

  return (
    <section className="data-panel" aria-labelledby="data-panel-import-h">
      <h2 id="data-panel-import-h" ref={headingRef} tabIndex={-1}>
        {t("data.panel.import.title")}
      </h2>
      <textarea
        className="data-import-textarea mono"
        aria-label={t("data.panel.import.title")}
        maxLength={MAX_CONTENT_UNITS}
        value={content}
        disabled={applyStatus === "loading"}
        onChange={(e) => {
          if (applyingRef.current) return;
          setContent(e.target.value);
          changed();
        }}
      />
      <input
        type="file"
        accept=".json"
        disabled={applyStatus === "loading"}
        onChange={handleFile}
        aria-label={t("data.import.file_label")}
      />
      <button
        type="button"
        className="btn alt sm"
        onClick={runPreview}
        disabled={applyStatus === "loading"}
        aria-busy={previewStatus === "loading"}
      >
        {t("data.button.preview")}
      </button>
      {previewStatus === "loading" && <Loading>{t("common.state.loading")}</Loading>}
      {previewStatus === "error" && previewError && (
        <p role="alert">{dataErrorText(previewError)}</p>
      )}

      {previewStatus === "ready" && preview && (
        <div className="data-import-preview">
          <dl>
            {PROVENANCE_KEYS.filter((key) => key in preview.provenance).map((key) => (
              <div key={key}>
                <dt>{t(`data.import.provenance.${key}`)}</dt>
                <dd>{String(preview.provenance[key])}</dd>
              </div>
            ))}
          </dl>
          {!preview.supported && preview.unsupported_reason && (
            <p role="alert">{t(`error.import.${preview.unsupported_reason}`)}</p>
          )}
          <table className="data-table">
            <thead>
              <tr>
                <th>{t("data.table.name")}</th>
                <th>{t("data.import.action.apply")}</th>
              </tr>
            </thead>
            <tbody>
              {preview.changes.map((change) => (
                <ChangeRow
                  key={change.key}
                  change={change}
                  checked={checked.has(change.key)}
                  disabled={applyStatus === "loading"}
                  onToggle={() => toggle(change.key)}
                />
              ))}
            </tbody>
          </table>
          {preview.workspaces.length > 0 && (
            <ul className="data-import-workspaces">
              {preview.workspaces.map((row) => (
                <li key={row.name}>
                  {row.name} — {t("data.import.workspaces_revalidate")}
                </li>
              ))}
            </ul>
          )}
          {preview.executable_definitions.length > 0 && (
            <p>{t("data.import.executable_disabled")}</p>
          )}
          {preview.duplicate_ids.length > 0 && (
            <div>
              <p>{t("data.import.duplicate_ids_title")}</p>
              <ul>
                {preview.duplicate_ids.map((id) => (
                  <li key={id} className="mono">
                    {id}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {preview.supported && applyStatus !== "ready" && (
            <button
              type="button"
              className="btn go sm"
              onClick={runApply}
              disabled={applyStatus === "loading"}
              aria-busy={applyStatus === "loading"}
            >
              {t("common.button.apply")}
            </button>
          )}
          {applyStatus === "loading" && <Loading>{t("common.state.loading")}</Loading>}
          {applyStatus === "ready" && <p role="status">{t("data.import.applied")}</p>}
        </div>
      )}
      {applyStatus === "error" && applyError && applyError.code === "stale_revision"
        ? renderConflict(applyError)
        : applyStatus === "error" && applyError && <p role="alert">{dataErrorText(applyError)}</p>}
    </section>
  );
}
