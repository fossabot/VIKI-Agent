/**
 * The profile editor's MCP server list: local per-field drafts backed by the profile document's
 * tools.mcp_configuration field, round-tripped through mcpServersModel.ts.
 */
import { type KeyboardEvent, type ReactElement, useEffect, useId, useRef, useState } from "react";
import { type ProfileApi, ProfileApiError } from "../../clients/profiles/profileApi";
import { t } from "../../i18n/t";
import {
  emptyDraft,
  fieldIndex,
  MAX_MCP_SERVERS,
  type McpConfigurationV1,
  type McpServerDraft,
  readConfiguration,
  writeConfiguration,
} from "./mcpServersModel";

/** The 400 the profile save answered, when its field names this configuration. */
export interface McpServersError {
  field: string;
  reason: string | null;
}

const ERROR_ID = "mcp-servers-field-error";

/** English copy per server reason code (mcp_config's McpConfigError codes, and the document
 * parser's extra_key and forbidden_key); an unlisted or missing reason falls back to the last
 * line. */
const REASON_TEXT: Record<string, string> = {
  unknown_key: "This key is not allowed.",
  extra_key: "This key is not allowed.",
  forbidden_key: "This key is not allowed.",
  missing_key: "A required value is missing.",
  invalid_type: "The value has the wrong type.",
  invalid_count: "At most 8 servers, and at most 32 arguments each.",
  invalid_name: "Use 1 to 26 lower-case letters, digits, - or _, starting with a letter.",
  duplicate_name: "Another server has this name.",
  invalid_length: "This value is empty or too long.",
  invalid_transport: "Choose stdio or http.",
  invalid_url:
    "Use https://, or http:// to 127.0.0.1, localhost or [::1], with no user name, query or fragment.",
  invalid_combination:
    "stdio needs a command and no URL; http needs a URL and no command or arguments.",
  // biome-ignore lint/suspicious/noTemplateCurlyInString: literal copy, not an interpolation.
  forbidden_text: "Remove ${…} and control characters.",
  secret_value:
    "This looks like a secret, so it is refused. Server definitions are plain text; keep secrets out of them.",
};

function reasonText(reason: string | null): string {
  return (reason !== null && REASON_TEXT[reason]) || "This value is not valid.";
}

export interface McpServersProps {
  value: Record<string, unknown> | null;
  onChange: (value: McpConfigurationV1 | null) => void;
  disabled: boolean;
  error: McpServersError | null;
  evaluation: string;
  onImport?: NonNullable<ProfileApi["importMcpConfiguration"]>;
  groupLabelledBy?: string | undefined;
  groupDescribedBy?: string | undefined;
}

/** Structural equality: an object's key order never matters (a stored document's order can
 * differ from writeConfiguration's canonical order); an array's order does. */
function sameValue(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a) || Array.isArray(b)) {
    return (
      Array.isArray(a) &&
      Array.isArray(b) &&
      a.length === b.length &&
      a.every((item, index) => sameValue(item, b[index]))
    );
  }
  if (typeof a === "object" && a !== null && typeof b === "object" && b !== null) {
    const left = a as Record<string, unknown>;
    const right = b as Record<string, unknown>;
    const leftKeys = Object.keys(left);
    const rightKeys = Object.keys(right);
    return (
      leftKeys.length === rightKeys.length &&
      leftKeys.every((key) => Object.hasOwn(right, key) && sameValue(left[key], right[key]))
    );
  }
  return false;
}

function preventEnterSubmit(event: KeyboardEvent<HTMLFieldSetElement>): void {
  if (event.key === "Enter" && event.target instanceof HTMLInputElement) event.preventDefault();
}

export function McpServers({
  value,
  onChange,
  disabled,
  error,
  evaluation,
  onImport,
  groupLabelledBy,
  groupDescribedBy,
}: McpServersProps): ReactElement {
  const configured = readConfiguration(value);
  const [drafts, setDrafts] = useState<McpServerDraft[]>(() => configured ?? []);
  const [source, setSource] = useState("");
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [imported, setImported] = useState(false);
  const importController = useRef<AbortController | null>(null);
  const importHelpId = useId();

  // biome-ignore lint/correctness/useExhaustiveDependencies: a changed draft or editor owner must cancel the old import before it can add rows.
  useEffect(() => {
    setImporting(false);
    return () => importController.current?.abort();
  }, [value, disabled, onImport]);

  // biome-ignore lint/correctness/useExhaustiveDependencies(drafts): only an incoming value change re-seeds; a typed edit never should, or a typed empty Arguments line would be read back out from under the user.
  useEffect(() => {
    if (!sameValue(writeConfiguration(drafts), value)) {
      setDrafts(readConfiguration(value) ?? []);
    }
  }, [value]);

  function commit(next: McpServerDraft[]): void {
    setDrafts(next);
    setImportError(null);
    setImported(false);
    onChange(writeConfiguration(next));
  }

  function editAt(index: number, patch: Partial<McpServerDraft>): void {
    commit(drafts.map((draft, at) => (at === index ? { ...draft, ...patch } : draft)));
  }

  async function importJson(): Promise<void> {
    if (disabled || importing || !onImport) return;
    const controller = new AbortController();
    importController.current = controller;
    setImporting(true);
    setImportError(null);
    setImported(false);
    try {
      const result = await onImport(source, controller.signal);
      if (controller.signal.aborted) return;
      const incoming = readConfiguration(result);
      if (incoming === null) throw new Error("invalid_response");
      if (incoming.length + drafts.length > MAX_MCP_SERVERS) {
        setImportError(t("mcp.import.count"));
        return;
      }
      const names = new Set(drafts.map((draft) => draft.name));
      if (incoming.some((draft) => names.has(draft.name))) {
        setImportError(t("mcp.import.duplicate"));
        return;
      }
      commit([...drafts, ...incoming]);
      setSource("");
      setImported(true);
    } catch (error) {
      if (controller.signal.aborted) return;
      const reason = error instanceof ProfileApiError ? error.reason : null;
      setImportError(
        reason === "invalid_json"
          ? t("mcp.import.invalid_json")
          : reason === "unsupported_import" || reason === "unknown_key"
            ? t("mcp.import.unsupported")
            : reason !== null
              ? reasonText(reason)
              : t("mcp.import.failed"),
      );
    } finally {
      if (!controller.signal.aborted) setImporting(false);
    }
  }

  const location = error === null ? null : fieldIndex(error.field);
  const clearButton = (
    <button type="button" disabled={disabled || value === null} onClick={() => commit([])}>
      Clear
    </button>
  );

  // A stored value the model cannot parse still blocks launch (the server's stricter schema
  // refuses it too), so it is shown, not silently dropped, and only Clear touches it.
  if (value !== null && configured === null) {
    return (
      <fieldset
        className="mcp-servers"
        disabled={disabled}
        onKeyDown={preventEnterSubmit}
        aria-labelledby={groupLabelledBy}
        aria-describedby={groupDescribedBy}
      >
        <legend>MCP configuration</legend>
        <p>{evaluation}</p>
        <p role="alert" className="mcp-field-error">
          This stored configuration does not match the server schema, so it blocks launch. Clear it
          to save this profile.
        </p>
        <pre>{JSON.stringify(value, null, 2)}</pre>
        {clearButton}
      </fieldset>
    );
  }

  function fieldAttrs(
    index: number,
    key: string,
  ): { "aria-invalid"?: true; "aria-describedby"?: string } {
    if (location === null || location.index !== index || location.key !== key) return {};
    return { "aria-invalid": true, "aria-describedby": ERROR_ID };
  }

  function fieldError(index: number, key: string): ReactElement | null {
    if (error === null || location === null || location.index !== index || location.key !== key) {
      return null;
    }
    return (
      <p id={ERROR_ID} role="alert" className="mcp-field-error">
        {reasonText(error.reason)}
      </p>
    );
  }

  return (
    <fieldset
      className="mcp-servers"
      disabled={disabled || importing}
      onKeyDown={preventEnterSubmit}
      aria-labelledby={groupLabelledBy}
      aria-describedby={groupDescribedBy}
    >
      <legend>MCP configuration</legend>
      <p>{evaluation}</p>
      <p>{t("mcp.profile.help")}</p>
      <p>{t("mcp.import.auth_help")}</p>
      {error !== null && location === null ? (
        <p role="alert" className="mcp-field-error">
          {reasonText(error.reason)}
        </p>
      ) : null}
      {drafts.map((draft, index) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: a row has no stable id; only Add and Remove change the list's length, never its order.
        <div className="mcp-server" key={index}>
          <label>
            Name{" "}
            <input
              disabled={disabled}
              value={draft.name}
              onChange={(event) => editAt(index, { name: event.target.value })}
              {...fieldAttrs(index, "name")}
            />
          </label>
          {fieldError(index, "name")}
          <label>
            Transport{" "}
            <select
              disabled={disabled}
              value={draft.transport}
              onChange={(event) =>
                editAt(index, { transport: event.target.value as McpServerDraft["transport"] })
              }
              {...fieldAttrs(index, "transport")}
            >
              <option value="stdio">stdio</option>
              <option value="http">http</option>
            </select>
          </label>
          {fieldError(index, "transport")}
          {draft.transport === "stdio" ? (
            <>
              <label>
                Command{" "}
                <input
                  disabled={disabled}
                  value={draft.command}
                  onChange={(event) => editAt(index, { command: event.target.value })}
                  {...fieldAttrs(index, "command")}
                />
              </label>
              {fieldError(index, "command")}
              <label>
                Arguments, one per line{" "}
                <textarea
                  disabled={disabled}
                  value={draft.args}
                  onChange={(event) => editAt(index, { args: event.target.value })}
                  {...fieldAttrs(index, "args")}
                />
              </label>
              {fieldError(index, "args")}
            </>
          ) : (
            <>
              <label>
                URL{" "}
                <input
                  disabled={disabled}
                  value={draft.url}
                  onChange={(event) => editAt(index, { url: event.target.value })}
                  {...fieldAttrs(index, "url")}
                />
              </label>
              {fieldError(index, "url")}
            </>
          )}
          <label>
            Note{" "}
            <input
              disabled={disabled}
              value={draft.note}
              onChange={(event) => editAt(index, { note: event.target.value })}
              {...fieldAttrs(index, "note")}
            />
          </label>
          {fieldError(index, "note")}
          <button
            type="button"
            disabled={disabled}
            onClick={() => commit(drafts.filter((_draft, at) => at !== index))}
          >
            Remove
          </button>
        </div>
      ))}
      <button
        type="button"
        disabled={disabled || drafts.length >= MAX_MCP_SERVERS}
        onClick={() => commit([...drafts, emptyDraft()])}
      >
        Add server
      </button>
      {clearButton}
      {onImport ? (
        <details>
          <summary>{t("mcp.import.title")}</summary>
          <p id={importHelpId}>{t("mcp.import.help")}</p>
          <label>
            {t("mcp.import.json")}
            <textarea
              value={source}
              rows={8}
              maxLength={64 << 10}
              spellCheck={false}
              autoComplete="off"
              aria-describedby={importHelpId}
              onChange={(event) => {
                setSource(event.target.value);
                setImportError(null);
                setImported(false);
              }}
            />
          </label>
          <button
            type="button"
            disabled={disabled || importing || source.trim() === ""}
            onClick={() => void importJson()}
          >
            {importing ? t("mcp.import.pending") : t("mcp.import.add")}
          </button>
          {importError ? <p role="alert">{importError}</p> : null}
          {imported ? <p role="status">{t("mcp.import.success")}</p> : null}
        </details>
      ) : null}
    </fieldset>
  );
}
