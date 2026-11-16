/**
 * `DataView`'s "Locations" panel: one table per category from
 * `GET /api/v2/data/locations`. `indexes` is always empty (the backend hands back `{items: []}`
 * unconditionally); a zero-row category with no dedicated table (`indexes` and any other unknown
 * id) renders the same empty sentence rather than a table with no body, which is a more honest
 * "nothing to show" than an empty `<table>` a screen reader still announces as a table. `assets`
 * is the one category with a table-less layout that is not that sentence: its retention
 * paragraph and `<dl>` render even with zero items.
 */
import { type ReactElement, type RefObject, useCallback, useEffect, useRef, useState } from "react";
import {
  type AppDataLocation,
  type DataLocations,
  type LocationCategory,
  type ProviderLocation,
  type ShellApi,
  ShellApiError,
  type VaultLocation,
} from "../../clients/app/shellApi";
import { t, tf } from "../../i18n/t";
import { Loading } from "../Loading/Loading";
import { dataErrorText } from "./dataErrorText";
import { formatBytes } from "./formatBytes";

export interface LocationsPanelProps {
  api: ShellApi | null;
  headingRef: RefObject<HTMLHeadingElement | null>;
  /** Called after every successful read (mount and each Refresh click) so a sibling panel that
   * only wants one field (`ResetPanel`'s edge-profile path) never opens its own second
   * `dataLocations()` read. */
  onLoaded?: (locations: DataLocations) => void;
}

type Status = "loading" | "ready" | "error";

function Bytes({ value }: { value: number }): ReactElement {
  return <span className="mono">{formatBytes(value)}</span>;
}

function VaultTable({ items }: { items: VaultLocation[] }): ReactElement {
  if (items.length === 0) return <p className="data-empty">{t("data.locations.empty")}</p>;
  return (
    <table className="data-table">
      <thead>
        <tr>
          <th>{t("data.table.name")}</th>
          <th>{t("data.table.path")}</th>
          <th>{t("data.table.policy")}</th>
          <th>{t("data.table.bytes")}</th>
        </tr>
      </thead>
      <tbody>
        {items.map((row) => (
          <tr key={row.workspace_id}>
            <td data-label={t("data.table.name")}>
              {row.name}
              {!row.available && (
                <span className="chip dim data-inline-chip">{t("common.chip.not_found")}</span>
              )}
            </td>
            <td data-label={t("data.table.path")} className="mono">
              {row.path}
            </td>
            <td data-label={t("data.table.policy")}>
              <span className="chip dim">
                {t(`workspaces.policy.${row.policy}` as `workspaces.policy.${typeof row.policy}`)}
              </span>
            </td>
            <td data-label={t("data.table.bytes")}>
              <Bytes value={row.bytes} />
              {row.partial && <span className="data-partial">{t("data.locations.partial")}</span>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function appDataDetails(row: AppDataLocation): ReactElement | null {
  const parts: ReactElement[] = [];
  if (row.schema !== undefined)
    parts.push(<span key="schema">{tf("data.location.schema", { version: row.schema })}</span>);
  if (row.count !== undefined)
    parts.push(<span key="count">{tf("data.location.count", { count: row.count })}</span>);
  if (row.in_use === true)
    parts.push(
      <span key="in_use" className="chip dim">
        {t("data.chip.in_use")}
      </span>,
    );
  if (row.shared === true)
    parts.push(
      <span key="shared" className="chip dim">
        {t("data.chip.shared")}
      </span>,
    );
  if (parts.length === 0) return null;
  return <span className="data-details">{parts}</span>;
}

function AppDataTable({ items }: { items: AppDataLocation[] }): ReactElement {
  if (items.length === 0) return <p className="data-empty">{t("data.locations.empty")}</p>;
  return (
    <table className="data-table">
      <thead>
        <tr>
          <th>{t("data.table.kind")}</th>
          <th>{t("data.table.path")}</th>
          <th>{t("data.table.bytes")}</th>
        </tr>
      </thead>
      <tbody>
        {items.map((row) => (
          <tr key={row.kind}>
            <td data-label={t("data.table.kind")}>
              {t(`data.location.${row.kind}` as `data.location.${typeof row.kind}`)}
            </td>
            <td data-label={t("data.table.path")} className="mono">
              {row.path}
            </td>
            <td data-label={t("data.table.bytes")}>
              <Bytes value={row.bytes} />
              {row.partial === true && (
                <span className="data-partial">{t("data.locations.partial")}</span>
              )}
              {appDataDetails(row)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ProviderTable({ items }: { items: ProviderLocation[] }): ReactElement {
  if (items.length === 0) return <p className="data-empty">{t("data.locations.empty")}</p>;
  return (
    <table className="data-table">
      <thead>
        <tr>
          <th>{t("data.table.kind")}</th>
          <th>{t("data.table.path")}</th>
          <th>{t("data.table.contains")}</th>
        </tr>
      </thead>
      <tbody>
        {items.map((row) => (
          <tr key={row.kind}>
            <td data-label={t("data.table.kind")}>
              {t(`data.provider.${row.kind}` as `data.provider.${typeof row.kind}`)}
            </td>
            <td data-label={t("data.table.path")} className="mono">
              {row.path ?? t("data.provider.hidden")}
              {!row.read_by_app && (
                <span className="data-not-read">{t("data.provider.not_read")}</span>
              )}
            </td>
            <td data-label={t("data.table.contains")}>{(row.contains ?? []).join(", ")}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function CategoryTable({ category }: { category: LocationCategory }): ReactElement {
  if (category.id === "vaults") return <VaultTable items={category.items} />;
  if (category.id === "app_data") return <AppDataTable items={category.items} />;
  if (category.id === "provider") return <ProviderTable items={category.items} />;
  if (category.id === "assets")
    return (
      <>
        <p>
          Unused uploads expire after 24 hours, and sent attachment copies after 30 days; drafts
          keep their files. This runs hourly for every workspace with images, not only the active
          one. Resetting Drafts or Send history frees their images sooner than that schedule. Chat
          originals retain image metadata. Removing a local copy does not remove content already
          sent to a provider.
        </p>
        <dl>
          {category.items.map((item) => (
            <div key={item.path}>
              <dt className="mono">{item.path}</dt>
              <dd>
                <Bytes value={item.bytes} />
                {item.partial ? (
                  <span className="data-partial">{t("data.locations.partial")}</span>
                ) : null}
              </dd>
            </div>
          ))}
        </dl>
      </>
    );
  return <p className="data-empty">{t("data.locations.empty")}</p>;
}

export function LocationsPanel({ api, headingRef, onLoaded }: LocationsPanelProps): ReactElement {
  const [status, setStatus] = useState<Status>("loading");
  const [locations, setLocations] = useState<DataLocations | null>(null);
  const [error, setError] = useState<ShellApiError | null>(null);

  // Refresh can be clicked again before an earlier read has settled. Only the newest read may
  // commit its result — state/shell.ts's `read()` implements the same rule for the shell store
  // ("only the newest read of a kind may commit") — so a slow, now-superseded read can never
  // overwrite what a faster later read already painted. The opening `setStatus("loading")` below
  // is never guarded: it always belongs to the read that just started, which by definition is
  // the current generation.
  const generation = useRef(0);

  const load = useCallback(() => {
    if (api === null) {
      setStatus("error");
      setError(new ShellApiError(0, "unavailable"));
      return;
    }
    const mine = ++generation.current;
    setStatus("loading");
    api
      .dataLocations()
      .then((value) => {
        if (mine !== generation.current) return;
        setLocations(value);
        setStatus("ready");
        onLoaded?.(value);
      })
      .catch((err: unknown) => {
        if (mine !== generation.current) return;
        setStatus("error");
        setError(err as ShellApiError);
      });
  }, [api, onLoaded]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <section className="data-panel" aria-labelledby="data-panel-locations-h">
      <h2 id="data-panel-locations-h" ref={headingRef} tabIndex={-1}>
        {t("data.panel.locations.title")}
      </h2>
      <button type="button" className="btn alt sm" onClick={load} aria-busy={status === "loading"}>
        {t("common.button.refresh")}
      </button>
      {status === "loading" && <Loading>{t("common.state.loading")}</Loading>}
      {status === "error" && error && <p role="alert">{dataErrorText(error)}</p>}
      {/* Not gated on status === "ready": a Refresh in flight, or one that failed, must not
       * blank a table that already loaded correctly (the same "never empties what is on
       * screen" rule state/shell.ts's read() follows for the store). */}
      {locations?.categories.map((category) => (
        <div className="data-category" key={category.id}>
          <h3>{t(`data.category.${category.id}` as `data.category.${typeof category.id}`)}</h3>
          <CategoryTable category={category} />
        </div>
      ))}
    </section>
  );
}
