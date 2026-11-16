import type { ReactNode } from "react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type {
  NativeAssetsApi,
  NativeCatalog,
  NativeSource,
} from "../../clients/profiles/nativeAssetsApi";
import { Loading } from "../Loading/Loading";
import { RailSection } from "./RailSection";
import "./SkillsSummary.css";

export interface SkillsSummaryProps {
  api: NativeAssetsApi;
  contextKey: string;
  onOpen?: () => void;
  compact?: boolean;
  /** A stable consumer-owned destination; moving it does not start another read. */
  detailsHost?: HTMLElement | null;
  activity?: ReactNode;
}

interface ReadState {
  api: NativeAssetsApi;
  contextKey: string;
  stage: "sources" | "catalog" | "empty" | "error" | "ready";
  source?: NativeSource | undefined;
  catalog?: NativeCatalog | undefined;
}

export function SkillsSummary({
  api,
  contextKey,
  onOpen,
  compact = false,
  detailsHost,
  activity,
}: SkillsSummaryProps) {
  const [state, setState] = useState<ReadState>({ api, contextKey, stage: "sources" });
  const latest = useRef(state);
  latest.current = state;
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    const owner = { api, contextKey };
    const prior = latest.current;
    const reuse = retry > 0 && prior.api === api && prior.contextKey === contextKey;
    const retained = reuse ? prior.catalog : undefined;
    let source = reuse ? prior.source : undefined;
    setState({ ...owner, source, catalog: retained, stage: source ? "catalog" : "sources" });
    async function load() {
      try {
        if (!source) {
          const sources = await api.sources(controller.signal);
          if (controller.signal.aborted) return;
          source = sources.find((item) => item.scope === "workspace") ?? sources[0];
          if (!source) {
            setState({ ...owner, stage: "empty" });
            return;
          }
          setState({ ...owner, source, stage: "catalog" });
        }
        const catalog = await api.catalog(source.source_id, controller.signal);
        if (!controller.signal.aborted) setState({ ...owner, source, catalog, stage: "ready" });
      } catch {
        if (!controller.signal.aborted)
          setState({ ...owner, source, catalog: retained, stage: "error" });
      }
    }
    void load();
    return () => controller.abort();
  }, [api, contextKey, retry]);

  const current: ReadState =
    state.api === api && state.contextKey === contextKey
      ? state
      : { api, contextKey, stage: "sources" };
  const [query, setQuery] = useState("");
  const catalog = current.catalog;
  const source = catalog
    ? { source_id: catalog.source_id, scope: catalog.source_scope }
    : current.source;
  const skills = catalog?.assets.filter((item) => item.kind === "skill");
  const count = skills?.length;
  const needle = query.toLowerCase();
  const shown = skills?.filter((item) =>
    [item.name, item.description ?? "", item.relative_id, item.source_id].some((value) =>
      value.toLowerCase().includes(needle),
    ),
  );
  const overview = compact || !!detailsHost;
  const pending = current.stage === "sources" || current.stage === "catalog";
  const canRetry = current.stage === "error" || catalog?.partial || (pending && retry > 0);
  const readStatus = catalog && current.stage !== "ready" && (
    <p>
      {current.stage === "error"
        ? "Last successful read; current catalog read failed."
        : "Last successful read; current catalog read is pending."}
    </p>
  );
  const readDetails = (
    <>
      {source && (
        <p style={{ overflowWrap: "anywhere" }}>
          {source.scope === "workspace" ? "Workspace" : "User"} source: {source.source_id}
        </p>
      )}
      {current.stage === "sources" && <Loading announce={false}>Loading skill sources…</Loading>}
      {current.stage === "catalog" && !catalog && (
        <Loading announce={false}>Loading Skills catalog…</Loading>
      )}
      {current.stage === "empty" && <p>No native sources are available.</p>}
      {current.stage === "error" && (
        <p role="alert">
          {source ? "Skills catalog could not be read." : "Skill sources could not be read."}
        </p>
      )}
      {catalog && (
        <>
          {!overview && (
            <>
              <p>Skills returned: {count}</p>
              <p>Catalog coverage: {catalog.partial ? "partial" : "complete"}</p>
              {readStatus}
            </>
          )}
          {(catalog.partial || catalog.omitted > 0 || catalog.partial_reasons.length > 0) && (
            <details>
              <summary>Catalog read details</summary>
              {(catalog.partial || catalog.omitted > 0) && (
                <p>Omitted catalog entries: {catalog.omitted}</p>
              )}
              {catalog.partial_reasons.map((reason) => (
                <p key={reason}>{reason}</p>
              ))}
            </details>
          )}
          {count === 0 && (
            <p>
              {catalog.partial
                ? "No skills were returned; this catalog is partial."
                : "No skills were returned by this complete catalog."}
            </p>
          )}
        </>
      )}
    </>
  );
  return (
    <>
      <RailSection title="Skills" className={compact ? "skills-summary" : ""}>
        {overview ? (
          <>
            <p>
              <span>Skills returned: {count ?? "unknown"}</span> ·{" "}
              {catalog ? (
                <>
                  {source?.scope === "workspace" ? "Workspace" : "User"} ·{" "}
                  <span>Catalog coverage: {catalog.partial ? "partial" : "complete"}</span>
                </>
              ) : pending ? (
                "Read pending."
              ) : current.stage === "error" ? (
                "Read failed."
              ) : (
                <span>No sources available.</span>
              )}
            </p>
            {readStatus}
            {!detailsHost && (
              <details>
                <summary>Skills details</summary>
                {readDetails}
              </details>
            )}
            {compact && !detailsHost && catalog && (
              <>
                <ul className="skills-preview" aria-label="Skill previews">
                  {skills?.slice(0, 3).map((item) => (
                    <li
                      className="skills-preview-row"
                      key={JSON.stringify([item.source_id, item.relative_id])}
                      title={item.name}
                    >
                      <span className="skills-preview-dot" aria-hidden="true" />
                      <span className="skills-preview-name">{item.name}</span>
                    </li>
                  ))}
                </ul>
                <details className="skills-browser">
                  <summary>Browse Skills ({count})</summary>
                  <div className="skills-browser-body">
                    <label>
                      Search Skills
                      <input
                        type="search"
                        className="skills-browser-search"
                        value={query}
                        onChange={(event) => setQuery(event.target.value)}
                      />
                    </label>
                    <p className="skills-browser-count">
                      Skills shown: {shown?.length ?? 0} of {count} returned
                    </p>
                    {shown?.length === 0 ? (
                      count === 0 ? null : (
                        <p>No returned Skills match this search.</p>
                      )
                    ) : (
                      <ul aria-label="Returned native Skills">
                        {shown?.map((item) => (
                          <li
                            className="skills-browser-row"
                            key={JSON.stringify([item.source_id, item.relative_id])}
                          >
                            <p>{item.name}</p>
                            {item.description && <p>{item.description}</p>}
                            <p>Ref: {item.relative_id}</p>
                            <p>Source: {item.source_id}</p>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                </details>
              </>
            )}
          </>
        ) : (
          readDetails
        )}
        {activity}
        <button type="button" className="btn txt sm" disabled={!onOpen} onClick={onOpen}>
          Open Skills
        </button>
        {!onOpen && <p>Skills browser unavailable.</p>}
        {canRetry && (
          <button
            type="button"
            className="btn txt sm"
            disabled={pending}
            onClick={() => setRetry((value) => value + 1)}
          >
            Retry
          </button>
        )}
      </RailSection>
      {detailsHost &&
        createPortal(
          <section aria-label="Native Skills details">
            {readDetails}
            <label>
              Search Skills
              <input
                type="search"
                style={{ width: "100%", boxSizing: "border-box" }}
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            </label>
            <p>
              {shown ? (
                <>
                  Skills shown: {shown.length} of {count} returned
                </>
              ) : (
                "Skills shown: unknown"
              )}
            </p>
            {shown && shown.length === 0 && count !== 0 && (
              <p>No returned Skills match this search.</p>
            )}
            {shown && shown.length > 0 && (
              <ul aria-label="Returned native Skills">
                {shown.map((item) => (
                  <li key={JSON.stringify([item.source_id, item.relative_id])}>
                    <p>{item.name}</p>
                    {item.description && <p>{item.description}</p>}
                    <p style={{ overflowWrap: "anywhere" }}>Ref: {item.relative_id}</p>
                    <p style={{ overflowWrap: "anywhere" }}>Source: {item.source_id}</p>
                  </li>
                ))}
              </ul>
            )}
          </section>,
          detailsHost,
        )}
    </>
  );
}
