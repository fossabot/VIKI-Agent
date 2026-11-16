/** Lets the user choose export contents, review the text preview and provide a destination through the caller's path field. */
import { type ReactElement, type ReactNode, type RefObject, useRef, useState } from "react";
import type {
  ExportInclude,
  ExportKind,
  ExportPreview,
  ExportResult,
  ShellApi,
} from "../../clients/app/shellApi";
import { ShellApiError } from "../../clients/app/shellApi";
import { t } from "../../i18n/t";
import { Loading } from "../Loading/Loading";
import { dataErrorText } from "./dataErrorText";
import { formatBytes } from "./formatBytes";

/** What `ExportPanel` hands `renderPathField`, so the real `PathForm` (or a test double) can
 * draw the destination field without this panel knowing its markup. */
export interface PathFieldProps {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  error: string | null;
  onSubmit: () => void;
}

export type RenderPathField = (props: PathFieldProps) => ReactNode;

export interface ExportPanelProps {
  api: ShellApi | null;
  renderPathField: RenderPathField;
  headingRef: RefObject<HTMLHeadingElement | null>;
}

type Status = "idle" | "loading" | "ready" | "error";

const DIAGNOSTICS_ONLY = ["log_tail", "settings", "registrations"] as const;
const INCLUDE_KEYS = ["paths", "identity", ...DIAGNOSTICS_ONLY] as const;

function emptyInclude(): Record<(typeof INCLUDE_KEYS)[number], boolean> {
  return { paths: false, identity: false, log_tail: false, settings: false, registrations: false };
}

function toInclude(kind: ExportKind, flags: Record<string, boolean>): ExportInclude {
  const keys = kind === "diagnostics" ? INCLUDE_KEYS : (["paths", "identity"] as const);
  const include: Record<string, boolean> = {};
  for (const key of keys) include[key] = flags[key] ?? false;
  return include;
}

export function ExportPanel({ api, renderPathField, headingRef }: ExportPanelProps): ReactElement {
  const [kind, setKind] = useState<ExportKind>("diagnostics");
  const [flags, setFlags] = useState(emptyInclude());
  const [previewStatus, setPreviewStatus] = useState<Status>("idle");
  const [preview, setPreview] = useState<ExportPreview | null>(null);
  const [previewError, setPreviewError] = useState<ShellApiError | null>(null);
  // `navigator.clipboard.writeText` is a permission-gated API that can refuse at any time (lost
  // focus, denied permission, no secure context) and must never throw out of a click handler or
  // leave a rejection unhandled — components/Conversation/Copy.tsx's contract for the same call.
  const [copyFailed, setCopyFailed] = useState(false);
  const [destination, setDestination] = useState("");
  const [writeStatus, setWriteStatus] = useState<Status>("idle");
  const [writeResult, setWriteResult] = useState<ExportResult | null>(null);
  const [writeError, setWriteError] = useState<ShellApiError | null>(null);
  // The write command_id is keyed on (preview_id, destination), kept as two fields rather than
  // one joined string (a destination could contain any separator): a retry of the *same* attempt
  // (double-click, or the user clicking again after a failure) reuses it so the backend's
  // replay-by-command_id logic answers with the first result instead of a second, false `exists`.
  // A new pair mints a fresh id, exactly once, on first use.
  const [writeCommand, setWriteCommand] = useState<{
    previewId: string;
    destination: string;
    id: string;
  } | null>(null);
  // `writeStatus` alone cannot stop two clicks dispatched in the same tick: React's state update
  // from the first click is not committed yet when the second click's handler reads it, so both
  // would pass a `writeStatus === "loading"` check. A ref is synchronous and unbatched.
  const writingRef = useRef(false);
  const generation = useRef(0);
  const [previewGeneration, setPreviewGeneration] = useState(0);

  function commandIdFor(previewId: string, dest: string): string {
    if (writeCommand && writeCommand.previewId === previewId && writeCommand.destination === dest) {
      return writeCommand.id;
    }
    const id = crypto.randomUUID();
    setWriteCommand({ previewId, destination: dest, id });
    return id;
  }

  function changed(): void {
    generation.current += 1;
    setPreview(null);
    setPreviewStatus("idle");
    setPreviewError(null);
    setWriteResult(null);
    setWriteError(null);
  }

  function changeKind(next: ExportKind): void {
    setKind(next);
    changed();
  }

  function toggle(key: (typeof INCLUDE_KEYS)[number]): void {
    setFlags((current) => ({ ...current, [key]: !current[key] }));
    changed();
  }

  function copyText(value: string): void {
    // The METHOD, not the object: `navigator.clipboard` can exist without `writeText` (a
    // non-secure context, or a test double with nothing on it), and calling through a missing
    // method throws synchronously out of the click handler — the one thing that must never
    // happen here.
    const write = navigator.clipboard?.writeText;
    if (typeof write !== "function") {
      setCopyFailed(true);
      return;
    }
    navigator.clipboard.writeText(value).then(
      () => setCopyFailed(false),
      () => setCopyFailed(true), // permission denied, focus moved away, or any other refusal
    );
  }

  function runPreview(): void {
    changed();
    const mine = generation.current;
    if (api === null) {
      setPreviewStatus("error");
      setPreviewError(new ShellApiError(0, "unavailable"));
      return;
    }
    setPreviewStatus("loading");
    api
      .exportPreview(kind, toInclude(kind, flags))
      .then((value) => {
        if (mine !== generation.current) return;
        setPreviewGeneration(mine);
        setPreview(value);
        setPreviewStatus("ready");
        setCopyFailed(false);
      })
      .catch((error: unknown) => {
        if (mine !== generation.current) return;
        setPreviewStatus("error");
        setPreviewError(error as ShellApiError);
      });
  }

  function runWrite(): void {
    if (
      api === null ||
      preview === null ||
      previewGeneration !== generation.current ||
      writingRef.current
    )
      return;
    writingRef.current = true;
    const commandId = commandIdFor(preview.preview_id, destination);
    setWriteStatus("loading");
    setWriteError(null);
    api
      .exportWrite(commandId, preview.preview_id, destination)
      .then((value) => {
        setWriteResult(value);
        setWriteStatus("ready");
      })
      .catch((error: unknown) => {
        setWriteStatus("error");
        setWriteError(error as ShellApiError);
        // A stale preview cannot be retried as-is; the user runs Preview again with the same
        // choices, which is the only "re-run" this panel ever performs on its own initiative.
        if ((error as ShellApiError).code === "preview_expired") {
          setPreview(null);
          setPreviewStatus("idle");
        }
      })
      .finally(() => {
        writingRef.current = false;
      });
  }

  const destinationFieldError =
    writeError?.code === "invalid_path" ? t("field.invalid_path") : null;
  const panelWriteError =
    writeError && writeError.code !== "invalid_path" ? (
      <p role="alert">
        {dataErrorText(writeError)}
        {writeError.code === "exists" && <span className="mono"> {destination}</span>}
      </p>
    ) : null;

  return (
    <section className="data-panel" aria-labelledby="data-panel-export-h">
      <h2 id="data-panel-export-h" ref={headingRef} tabIndex={-1}>
        {t("data.panel.export.title")}
      </h2>

      <fieldset className="data-export-kind">
        <legend>{t("data.export.legend")}</legend>
        {(["diagnostics", "settings"] as const).map((option) => (
          <label key={option}>
            <input
              type="radio"
              name="export-kind"
              checked={kind === option}
              onChange={() => changeKind(option)}
            />
            {t(`data.export.kind.${option}`)}
          </label>
        ))}
      </fieldset>

      <fieldset className="data-export-include">
        <legend>{t("data.export.include.legend")}</legend>
        <label>
          <input type="checkbox" checked={flags.paths} onChange={() => toggle("paths")} />
          {t("data.export.include.paths")}
        </label>
        <p className="data-export-warning">{t("data.export.include.paths_warning")}</p>
        <label>
          <input type="checkbox" checked={flags.identity} onChange={() => toggle("identity")} />
          {t("data.export.include.identity")}
        </label>
        <p className="data-export-warning">{t("data.export.include.identity_warning")}</p>
        {kind === "diagnostics" &&
          DIAGNOSTICS_ONLY.map((key) => (
            <label key={key}>
              <input type="checkbox" checked={flags[key]} onChange={() => toggle(key)} />
              {t(`data.export.include.${key}`)}
            </label>
          ))}
      </fieldset>

      <button
        type="button"
        className="btn alt sm"
        onClick={runPreview}
        aria-busy={previewStatus === "loading"}
      >
        {t("data.button.preview")}
      </button>
      {previewStatus === "loading" && <Loading>{t("common.state.loading")}</Loading>}
      {previewStatus === "error" && previewError && (
        <p role="alert">{dataErrorText(previewError)}</p>
      )}

      {previewStatus === "ready" && preview && (
        <div className="data-export-preview">
          <table className="data-table">
            <thead>
              <tr>
                <th>{t("data.export.files.name")}</th>
                <th>{t("data.export.files.bytes")}</th>
                <th>{t("data.export.files.redactions")}</th>
              </tr>
            </thead>
            <tbody>
              {preview.files.map((file) => (
                <tr key={file.name}>
                  <td data-label={t("data.export.files.name")}>{file.name}</td>
                  <td data-label={t("data.export.files.bytes")}>
                    <span className="mono">{formatBytes(file.bytes)}</span>
                  </td>
                  <td data-label={t("data.export.files.redactions")}>{file.redactions}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {Object.entries(preview.content).map(([name, text]) => (
            <div className="data-export-content" key={name}>
              <button type="button" className="btn txt sm" onClick={() => copyText(text)}>
                {t("common.button.copy")}
              </button>
              {copyFailed && (
                <p role="alert">{dataErrorText(new ShellApiError(0, "unavailable"))}</p>
              )}
              <pre className="mono">{text}</pre>
            </div>
          ))}

          {renderPathField({
            id: "export-destination",
            label: t("data.export.result.path"),
            value: destination,
            onChange: setDestination,
            error: destinationFieldError,
            onSubmit: runWrite,
          })}
          <button
            type="button"
            className="btn go sm"
            onClick={runWrite}
            aria-busy={writeStatus === "loading"}
          >
            {t("data.button.export_write")}
          </button>
          {writeStatus === "loading" && <Loading>{t("common.state.loading")}</Loading>}
          {panelWriteError}
          {writeStatus === "ready" && writeResult && (
            <>
              <p role="status">
                {writeResult.replayed ? t("data.export.result.replayed") : t("data.export.written")}
              </p>
              <dl className="data-export-result">
                <dt>{t("data.export.result.path")}</dt>
                <dd className="mono">{writeResult.path}</dd>
                <dt>{t("data.export.result.bytes")}</dt>
                <dd className="mono">{formatBytes(writeResult.bytes)}</dd>
                <dt>{t("data.export.result.sha256")}</dt>
                <dd className="mono">{writeResult.sha256}</dd>
              </dl>
            </>
          )}
        </div>
      )}
    </section>
  );
}
