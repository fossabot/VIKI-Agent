/** Resets selected categories after typed confirmation and reports each result. The running app profile cannot be reset from inside that profile. */
import { type ReactElement, type RefObject, useRef, useState } from "react";
import type {
  ResetCategory,
  ResetOutcome,
  ResetResult,
  ShellApi,
  ShellApiError,
} from "../../clients/app/shellApi";
import { commandFor } from "../../commands";
import { en, type Key } from "../../i18n/index";
import { t, tf } from "../../i18n/t";
import type { ShellStore } from "../../state/shell";
import { useSnapshot } from "../../state/viki";
import { dataErrorText } from "./dataErrorText";
import { ResetDialog } from "./ResetDialog";

export interface ResetPanelProps {
  api: ShellApi | null;
  store: ShellStore;
  headingRef: RefObject<HTMLHeadingElement | null>;
  /** `LocationsPanel`'s own read, not a second `dataLocations()` call: this panel only decorates
   * the always-disabled edge-profile row with the path `DataView` already has in hand. */
  edgeProfilePath: string | null;
  onReset?: ((result: ResetResult) => void) | undefined;
}

const CATEGORIES: ResetCategory[] = ["settings", "drafts", "commands", "approvals", "scratch"];

/** `error.reset.<reason>` when the catalog has a row of its own for that reason (`in_use`,
 * `read_only`, `unknown_category`), else the shared storage rows of `errors.common.ts` under
 * `error.<reason>` (a forwarded `SettingsError.code`), else the generic reset row with the code
 * in mono. */
function resetReasonText(reason: string): string {
  const specific = `error.reset.${reason}` as Key;
  if (specific in en) return t(specific);
  const shared = `error.${reason}` as Key;
  if (shared in en) return t(shared);
  return tf("error.reset.generic" as Key, { code: reason });
}

export function ResetPanel({
  api,
  store,
  headingRef,
  edgeProfilePath,
  onReset,
}: ResetPanelProps): ReactElement {
  const snapshot = useSnapshot(store);
  const [checked, setChecked] = useState<Set<ResetCategory>>(new Set());
  const [dialogOpen, setDialogOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const [result, setResult] = useState<ResetResult | null>(null);
  const [error, setError] = useState<ShellApiError | null>(null);

  const command = commandFor("data.reset", snapshot);

  function toggle(category: ResetCategory): void {
    if (!command.enabled) return;
    setChecked((current) => {
      const next = new Set(current);
      if (next.has(category)) next.delete(category);
      else next.add(category);
      return next;
    });
  }

  const selected = [...checked];
  const sentences = selected.map((category) => t(`data.reset.category.${category}` as Key));

  function runReset(): void {
    if (inFlight.current || api === null || selected.length === 0) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    api
      .reset(crypto.randomUUID(), selected)
      .then((value) => {
        setResult(value);
        onReset?.(value);
        // The reset returns no settings document, unlike a patch or an import apply, so there is
        // nothing for `adoptSettings` to commit here: a fresh read is the only way to learn what
        // "settings: done" actually left behind.
        if (value.results.settings?.status === "done")
          void store.afterMutation(["settings", "capabilities"]);
      })
      .catch((err: unknown) => {
        setError(err as ShellApiError);
      })
      .finally(() => {
        inFlight.current = false;
        setBusy(false);
        setDialogOpen(false);
      });
  }

  function cancelReset(): void {
    if (!inFlight.current) setDialogOpen(false);
  }

  return (
    <section className="data-panel" aria-labelledby="data-panel-reset-h">
      <h2 id="data-panel-reset-h" ref={headingRef} tabIndex={-1}>
        {t("data.panel.reset.title")}
      </h2>
      <ul className="data-reset-categories">
        {CATEGORIES.map((category) => (
          <li key={category}>
            <label>
              <input
                type="checkbox"
                checked={checked.has(category)}
                aria-disabled={!command.enabled}
                {...(!command.enabled && command.reason !== null
                  ? { "aria-describedby": "data-reset-reason", title: t(command.reason) }
                  : {})}
                onChange={() => toggle(category)}
              />
              {t(`data.reset.label.${category}` as Key)}
            </label>
            <p className="data-reset-scope">{t(`data.reset.category.${category}` as Key)}</p>
          </li>
        ))}
        <li>
          <label>
            <input
              type="checkbox"
              checked={false}
              aria-disabled="true"
              aria-describedby="data-reset-edge-scope"
              title={tf("data.reset.category.edge_profile" as Key, {
                path: edgeProfilePath ?? t("data.provider.hidden"),
              })}
              onChange={(e) => e.preventDefault()}
            />
            {t("data.reset.label.edge_profile")}
          </label>
          <p className="data-reset-scope" id="data-reset-edge-scope">
            {tf("data.reset.category.edge_profile" as Key, {
              path: edgeProfilePath ?? t("data.provider.hidden"),
            })}
          </p>
        </li>
      </ul>
      {!command.enabled && command.reason && (
        <p className="data-reset-reason" id="data-reset-reason">
          {t(command.reason)}
        </p>
      )}
      <button
        type="button"
        className="btn go sm"
        aria-disabled={!command.enabled || selected.length === 0}
        {...(!command.enabled && command.reason !== null
          ? { "aria-describedby": "data-reset-reason", title: t(command.reason) }
          : command.enabled && selected.length === 0
            ? { "aria-describedby": "data-reset-opener-reason", title: t("data.reset.select_none") }
            : {})}
        onClick={() => {
          if (command.enabled && selected.length > 0) setDialogOpen(true);
        }}
      >
        {t("data.reset.dialog.confirm")}
      </button>
      {command.enabled && selected.length === 0 && (
        <span id="data-reset-opener-reason" className="sr-only">
          {t("data.reset.select_none")}
        </span>
      )}
      {error && <p role="alert">{dataErrorText(error)}</p>}

      {result && (
        <>
          <p role="status">{t("data.reset.done")}</p>
          <table className="data-table">
            <thead>
              <tr>
                <th>{t("data.table.name")}</th>
                <th>{t("data.reset.result.status")}</th>
                <th>{t("data.reset.result.reason")}</th>
                <th>{t("data.reset.result.count")}</th>
                <th>{t("data.reset.result.kept")}</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(result.results).map(([category, outcome]: [string, ResetOutcome]) => (
                <tr key={category}>
                  <td data-label={t("data.table.name")}>{category}</td>
                  <td data-label={t("data.reset.result.status")}>
                    {t(`data.reset.status.${outcome.status}` as Key)}
                  </td>
                  <td data-label={t("data.reset.result.reason")}>
                    {outcome.reason ? resetReasonText(outcome.reason) : ""}
                  </td>
                  <td data-label={t("data.reset.result.count")}>{outcome.count}</td>
                  {/* A kept row is a turn the store refused to delete because this launch had
                   * admitted it and not finished it; without the number, `partial` would not say
                   * what stayed behind. */}
                  <td data-label={t("data.reset.result.kept")}>{outcome.kept ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {dialogOpen && (
        <ResetDialog
          sentences={sentences}
          busy={busy}
          onConfirm={runReset}
          onCancel={cancelReset}
        />
      )}
    </section>
  );
}
