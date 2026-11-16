import { useEffect, useState } from "react";
import {
  type NativeAssetsApi,
  NativeAssetsApiError,
  type NativeAssetView,
  type NativeCatalog,
  type NativeSource,
} from "../../clients/profiles/nativeAssetsApi";
import { Loading } from "../Loading/Loading";
import "../Profiles/CapabilityContext.css";
import "./NativeCapabilities.css";

export interface NativeCapabilitiesProps {
  api: NativeAssetsApi;
  onManage?: (() => void) | undefined;
}

type SourcesRead = { api: NativeAssetsApi; items: NativeSource[] | null; error: string | null };
type CatalogRead = {
  api: NativeAssetsApi;
  sourceId: string;
  catalog: NativeCatalog | null;
  error: string | null;
};

const REASONS: Record<string, string> = {
  deadline: "The read stopped at its time limit; entries may be missing.",
  entry_limit: "The entry limit was reached; entries may be missing.",
  byte_limit: "The data limit was reached; entries may be missing.",
  oversized: "Some content was too large to read in full.",
  unsafe_entry: "Some entries were skipped for safety.",
  unreadable: "Some entries could not be read.",
  changed: "Some entries changed during the read.",
  invalid_encoding: "Some text could not be decoded.",
  malformed_metadata: "Some metadata could not be read in the supported format.",
};

function errorCode(error: unknown): string {
  return error instanceof NativeAssetsApiError ? error.code : "unavailable";
}

function identity(asset: NativeAssetView): string {
  return JSON.stringify([asset.source_id, asset.relative_id]);
}

export function NativeCapabilities({ api, onManage }: NativeCapabilitiesProps) {
  const [sourcesRead, setSourcesRead] = useState<SourcesRead>({ api, items: null, error: null });
  const [sourceId, setSourceId] = useState<string | null>(null);
  const [catalogRead, setCatalogRead] = useState<CatalogRead | null>(null);
  const [sourcesRefresh, setSourcesRefresh] = useState(0);
  const [catalogRefresh, setCatalogRefresh] = useState(0);
  const [query, setQuery] = useState("");
  const [selection, setSelection] = useState<{ api: NativeAssetsApi; key: string } | null>(null);

  useEffect(() => {
    void sourcesRefresh;
    const controller = new AbortController();
    setSourcesRead({ api, items: null, error: null });
    api.sources(controller.signal).then(
      (items) => {
        if (controller.signal.aborted) return;
        setSourcesRead({ api, items, error: null });
        // Keep an exact prior choice even when it disappears; let the user choose its replacement.
        setSourceId(
          (current) =>
            current ??
            (items.find((item) => item.scope === "workspace") ?? items[0])?.source_id ??
            null,
        );
      },
      (error: unknown) => {
        if (!controller.signal.aborted)
          setSourcesRead({ api, items: null, error: errorCode(error) });
      },
    );
    return () => controller.abort();
  }, [api, sourcesRefresh]);

  useEffect(() => {
    void catalogRefresh;
    if (
      sourceId === null ||
      sourcesRead.api !== api ||
      !sourcesRead.items?.some((source) => source.source_id === sourceId)
    )
      return;
    const controller = new AbortController();
    setCatalogRead({ api, sourceId, catalog: null, error: null });
    api.catalog(sourceId, controller.signal).then(
      (catalog) => {
        if (!controller.signal.aborted) setCatalogRead({ api, sourceId, catalog, error: null });
      },
      (error: unknown) => {
        if (!controller.signal.aborted)
          setCatalogRead({ api, sourceId, catalog: null, error: errorCode(error) });
      },
    );
    return () => controller.abort();
  }, [api, sourceId, sourcesRead, catalogRefresh]);

  const sources = sourcesRead.api === api ? sourcesRead.items : null;
  const source = sources?.find((item) => item.source_id === sourceId);
  const read =
    source && catalogRead?.api === api && catalogRead.sourceId === sourceId ? catalogRead : null;
  const catalog = read?.catalog;
  const skills = catalog?.assets.filter((asset) => asset.kind === "skill");
  const needle = query.trim().toLowerCase();
  const shown = skills?.filter((asset) =>
    [asset.name, asset.description ?? "", asset.source_id, asset.relative_id].some((value) =>
      value.toLowerCase().includes(needle),
    ),
  );
  const selected =
    selection?.api === api ? skills?.find((asset) => identity(asset) === selection.key) : null;
  const sourcesError = sourcesRead.api === api ? sourcesRead.error : null;
  const error = sourcesError ?? read?.error;
  const pending = sources === null && !sourcesError;
  const unavailable = sources !== null && sources.length > 0 && !source;

  return (
    <section className="native-capabilities" aria-label="Native skills">
      <header className="capability-context">
        <h3>Native skills</h3>
        <p>
          Read-only catalog. Claude manages these capabilities; discovery does not enable them or
          change provider permissions.
        </p>
        {onManage && (
          <button type="button" className="btn txt sm" onClick={onManage}>
            Manage native capabilities
          </button>
        )}
      </header>
      <div className="native-capabilities-controls">
        <label>
          Native source
          <select
            value={sourceId ?? ""}
            disabled={pending || sources === null}
            onChange={(event) => {
              setSourceId(event.target.value);
              setSelection(null);
            }}
          >
            {!source && <option value={sourceId ?? ""}>{sourceId ?? "Choose a source"}</option>}
            {sources?.map((item) => (
              <option key={item.source_id} value={item.source_id}>
                {item.scope === "workspace" ? "Workspace" : "User"} · {item.source_id}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="btn txt sm"
          disabled={pending}
          onClick={() => setSourcesRefresh((value) => value + 1)}
        >
          Refresh sources
        </button>
        <label>
          Search native skills
          <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} />
        </label>
      </div>
      {source && <p className="capability-context-ref">Source: {source.source_id}</p>}
      {pending && <Loading>Loading native sources…</Loading>}
      {source && !catalog && !error && <Loading>Loading native catalog…</Loading>}
      {sources?.length === 0 && <p>No native sources are available.</p>}
      {unavailable && <p role="status">Selected source is unavailable. Choose another source.</p>}
      {error && (
        <div role="alert">
          <p>
            {sourcesError ? "Native sources could not be read" : "Native catalog could not be read"}
            : {error}
          </p>
          <button
            type="button"
            className="btn txt sm"
            onClick={() =>
              sourcesError
                ? setSourcesRefresh((value) => value + 1)
                : setCatalogRefresh((value) => value + 1)
            }
          >
            Retry
          </button>
        </div>
      )}
      <p>
        {shown
          ? `Skills shown: ${shown.length} of ${skills?.length} returned`
          : "Skills shown: unknown"}
      </p>
      {catalog && (
        <div className="capability-context">
          <p>Catalog coverage: {catalog.partial ? "partial" : "complete"}</p>
          {(catalog.partial || catalog.omitted > 0 || catalog.partial_reasons.length > 0) && (
            <details>
              <summary>Catalog read details</summary>
              <p>Omitted catalog entries: {catalog.omitted}</p>
              {catalog.partial_reasons.map((reason) => (
                <p key={reason}>
                  {REASONS[reason] ?? "The catalog read is incomplete."} ({reason})
                </p>
              ))}
            </details>
          )}
          {catalog.partial && (
            <button
              type="button"
              className="btn txt sm"
              onClick={() => setCatalogRefresh((value) => value + 1)}
            >
              Refresh catalog
            </button>
          )}
        </div>
      )}
      {skills?.length === 0 && (
        <p>
          {catalog?.partial
            ? "No skills were returned; this catalog is partial."
            : "No skills were returned by this complete catalog."}
        </p>
      )}
      {shown?.length === 0 && skills?.length !== 0 && <p>No returned skills match this search.</p>}
      <div className="native-capabilities-layout">
        {!!shown?.length && (
          <ul aria-label="Returned native skills">
            {shown.map((asset) => (
              <li key={identity(asset)}>
                <button
                  type="button"
                  aria-pressed={selected === asset}
                  onClick={() => setSelection({ api, key: identity(asset) })}
                >
                  <strong>{asset.name}</strong>
                  {asset.description && <span>{asset.description}</span>}
                  <span className="capability-context-ref">Ref: {asset.relative_id}</span>
                  <span className="capability-context-ref">Source: {asset.source_id}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
        {selected && (
          <section className="capability-context" aria-label="Selected skill">
            <h3>{selected.name}</h3>
            <p>{selected.description ?? "Description not reported."}</p>
            <p className="capability-context-ref">Ref: {selected.relative_id}</p>
            <p className="capability-context-ref">Source: {selected.source_id}</p>
            <p>
              Provider: {selected.provider} · {selected.source_scope}
            </p>
            <p>Model: {selected.model ?? "inherited"}</p>
            <p>Tools: {selected.tools.length ? selected.tools.join(", ") : "Not reported"}</p>
            <p>Observed: {selected.observed_at}</p>
            <p className="capability-context-ref">Digest: {selected.digest}</p>
            <p>Supported fields: {selected.supported_fields.join(", ") || "None reported"}</p>
            {selected.unsupported_fields.length > 0 && (
              <p>Unsupported fields: {selected.unsupported_fields.join(", ")}</p>
            )}
            <p>Launch from this catalog is unsupported.</p>
          </section>
        )}
      </div>
    </section>
  );
}
