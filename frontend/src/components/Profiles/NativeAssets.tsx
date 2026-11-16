/**
 * The Agents view's Native section: the read-only agents and skills this app can read from the
 * Claude CLI's own folders, grouped by source (the user's Claude folder, and the active vault when
 * it holds one). Loading, catalog and error states for both the source list and the chosen
 * source's catalog; every value from the server renders as a text node.
 */
import { type ReactElement, useEffect, useRef, useState } from "react";
import type {
  NativeAssetsApi,
  NativeAssetView,
  NativeCatalog,
  NativeSource,
} from "../../clients/profiles/nativeAssetsApi";
import {
  type ProfileApi,
  ProfileApiError,
  type ProfileDocumentV1,
} from "../../clients/profiles/profileApi";
import { t } from "../../i18n/t";
import { Loading } from "../Loading/Loading";
import { profileFromAgent } from "./nativeProfile";
import "./CapabilityContext.css";

export interface NativeAssetsProps {
  api: NativeAssetsApi;
  profileApi: ProfileApi;
  onCreated: (profileId: string) => void;
  /** Seeds the kind filter on mount; later changes leave manual choices intact. */
  initialKind?: "all" | NativeAssetView["kind"];
}

type CreateState = { status: "pending" } | { status: "error"; message: string };

type SourcesState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; sources: readonly NativeSource[] };

type CatalogState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; catalog: NativeCatalog };

function sourceLabel(source: Pick<NativeSource, "scope">): string {
  return source.scope === "user" ? "Your Claude folder" : "This vault";
}

/**
 * English subject for a reason's line. `omitted` counts entries left out one by one; a whole
 * folder left out, or a description cut short, counts none; a stop at the deadline or the entry
 * limit counts one.
 */
function omittedWords(omitted: number): { subject: string; verb: string; possessive: string } {
  if (omitted > 0) {
    const entryWord = omitted === 1 ? "entry" : "entries";
    return {
      subject: `${omitted} ${entryWord}`,
      verb: omitted === 1 ? "was" : "were",
      possessive: omitted === 1 ? `${omitted} entry's` : `${omitted} entries'`,
    };
  }
  return { subject: "Some entries", verb: "were", possessive: "Some entries'" };
}

/** One line per catalog reason (src/viki_agent/profiles/native_assets.py's `NativeAssetError` codes). */
function reasonText(reason: string, omitted: number): string {
  const amount = omittedWords(omitted);
  switch (reason) {
    case "deadline":
      return "Reading took too long; some entries may be missing.";
    case "entry_limit":
      return "Too many entries; some were left out.";
    case "byte_limit":
      return "Too much data; some entries were left out.";
    case "oversized":
      return `${amount.subject} ${amount.verb} too large to read in full.`;
    case "unsafe_entry":
      return `${amount.subject} ${amount.verb} skipped for safety.`;
    case "unreadable":
      return `${amount.subject} could not be read.`;
    case "changed":
      return `${amount.subject} changed while reading.`;
    case "invalid_encoding":
      return `${amount.subject} had text this app could not decode.`;
    case "malformed_metadata":
      return `${amount.possessive} details were not in the expected format.`;
    default:
      return "Some entries were left out.";
  }
}

/** Identifies one agent across catalog reloads, so a retry reuses the same minted ids. */
function createKey(asset: NativeAssetView): string {
  return `${asset.source_id}:${asset.relative_id}`;
}

function createErrorMessage(error: unknown): string {
  const code = error instanceof ProfileApiError ? error.code : "unavailable";
  switch (code) {
    case "conflict":
      return "A profile with this id already exists.";
    case "command_conflict":
      return "This retry no longer matches the original request.";
    case "capacity":
      return "The profile limit has been reached.";
    case "unauthorized":
      return "Authentication is required.";
    case "forbidden_origin":
      return "This window cannot create profiles.";
    default:
      return "The profile could not be created.";
  }
}

interface AssetRowProps {
  asset: NativeAssetView;
  create?: { pending: boolean; error: string | null; onClick: () => void };
}

function AssetRow({ asset, create }: AssetRowProps): ReactElement {
  return (
    <li className="native-asset">
      <p>{asset.name}</p>
      {asset.description ? <p>{asset.description}</p> : null}
      <p style={{ overflowWrap: "anywhere" }}>{asset.relative_id}</p>
      <p>{sourceLabel({ scope: asset.source_scope })}</p>
      <p>Model: {asset.model ?? "inherited"}</p>
      {asset.tools.length > 0 ? <p>Tools: {asset.tools.join(", ")}</p> : null}
      <details>
        <summary>Details</summary>
        <p className="capability-context-ref">Source ID: {asset.source_id}</p>
        <p>Digest: {asset.digest}</p>
        <p>Observed: {asset.observed_at}</p>
        {asset.unsupported_fields.length > 0 ? (
          <p>Unsupported fields: {asset.unsupported_fields.join(", ")}</p>
        ) : null}
      </details>
      <p>launch unsupported</p>
      {create ? (
        <p>
          <button type="button" onClick={create.onClick} disabled={create.pending}>
            Create an app profile from this agent
          </button>
          {create.error ? <span role="status">{create.error}</span> : null}
        </p>
      ) : null}
    </li>
  );
}

export function NativeAssets({
  api,
  profileApi,
  onCreated,
  initialKind = "all",
}: NativeAssetsProps): ReactElement {
  const [sourcesState, setSourcesState] = useState<SourcesState>({ status: "loading" });
  const [selectedSourceId, setSelectedSourceId] = useState<string | null>(null);
  const [catalogState, setCatalogState] = useState<CatalogState>({ status: "loading" });
  const [kind, setKind] = useState<"all" | NativeAssetView["kind"]>(initialKind);
  const [query, setQuery] = useState("");
  const [sourcesRefresh, setSourcesRefresh] = useState(0);
  const [catalogRefresh, setCatalogRefresh] = useState(0);
  const explicitSource = useRef(false);
  // Minted once per agent (by source and relative id), document included, and reused on retry: a
  // catalog reload between attempts must not change the body sent under the same command id.
  const createIds = useRef(
    new Map<string, { profileId: string; commandId: string; document: ProfileDocumentV1 }>(),
  );
  const [createState, setCreateState] = useState<Record<string, CreateState>>({});

  async function handleCreate(asset: NativeAssetView): Promise<void> {
    const key = createKey(asset);
    let ids = createIds.current.get(key);
    if (!ids) {
      ids = {
        profileId: crypto.randomUUID(),
        commandId: crypto.randomUUID(),
        document: profileFromAgent(asset),
      };
      createIds.current.set(key, ids);
    }
    setCreateState((state) => ({ ...state, [key]: { status: "pending" } }));
    try {
      const receipt = await profileApi.create(ids.profileId, ids.commandId, ids.document);
      setCreateState((state) => {
        const next = { ...state };
        delete next[key];
        return next;
      });
      onCreated(receipt.profile_id);
    } catch (error) {
      setCreateState((state) => ({
        ...state,
        [key]: { status: "error", message: createErrorMessage(error) },
      }));
    }
  }

  // Created fresh on mount, and again on StrictMode's development-only remount, as RunningWork's
  // mount effect does.
  useEffect(() => {
    void sourcesRefresh;
    const controller = new AbortController();
    setSourcesState({ status: "loading" });
    api.sources(controller.signal).then(
      (sources) => {
        if (controller.signal.aborted) return;
        setSourcesState({ status: "ready", sources });
        setSelectedSourceId((current) =>
          explicitSource.current && sources.some((source) => source.source_id === current)
            ? current
            : ((sources.find((source) => source.scope === "workspace") ?? sources[0])?.source_id ??
              null),
        );
      },
      () => {
        if (controller.signal.aborted) return;
        setSourcesState({ status: "error" });
      },
    );
    return () => controller.abort();
  }, [api, sourcesRefresh]);

  useEffect(() => {
    void catalogRefresh;
    if (selectedSourceId === null) return;
    const controller = new AbortController();
    setCatalogState({ status: "loading" });
    api.catalog(selectedSourceId, controller.signal).then(
      (catalog) => {
        if (controller.signal.aborted) return;
        setCatalogState({ status: "ready", catalog });
      },
      () => {
        if (controller.signal.aborted) return;
        setCatalogState({ status: "error" });
      },
    );
    return () => controller.abort();
  }, [api, selectedSourceId, catalogRefresh]);

  if (sourcesState.status === "loading") {
    return (
      <Loading className="native-assets" announce={false}>
        Loading…
      </Loading>
    );
  }
  if (sourcesState.status === "error") {
    return (
      <p className="native-assets" role="status">
        Native assets are unavailable.{" "}
        <button type="button" onClick={() => setSourcesRefresh((value) => value + 1)}>
          Retry
        </button>
      </p>
    );
  }
  if (sourcesState.sources.length === 0) {
    return <p className="native-assets">No native sources</p>;
  }

  const catalog =
    catalogState.status === "ready" && catalogState.catalog.source_id === selectedSourceId
      ? catalogState.catalog
      : null;
  const search = query.trim().toLocaleLowerCase();
  const shown =
    catalog?.assets.filter(
      (asset) =>
        (kind === "all" || asset.kind === kind) &&
        [asset.name, asset.description ?? "", asset.relative_id].some((value) =>
          value.toLocaleLowerCase().includes(search),
        ),
    ) ?? [];
  const agents = shown.filter((asset) => asset.kind === "agent");
  const skills = shown.filter((asset) => asset.kind === "skill");
  const selectedSource = sourcesState.sources.find(
    (source) => source.source_id === selectedSourceId,
  );

  return (
    <div className="native-assets">
      <section className="capability-context" aria-label="Native catalog context">
        <h3>Read-only native catalog</h3>
        <p>Discovery does not enable an asset or change provider permissions.</p>
        <p>{t("bots.subagents.help")}</p>
        {selectedSource ? (
          <p>
            {sourceLabel(selectedSource)}
            {" · "}
            <span className="capability-context-ref">{selectedSource.source_id}</span>
          </p>
        ) : null}
        {catalog ? (
          <p className="capability-context-coverage">
            {catalog.partial ? "Partial catalog" : "Catalog read complete"}
          </p>
        ) : null}
      </section>
      <label>
        Source
        <select
          value={selectedSourceId ?? ""}
          onChange={(event) => {
            explicitSource.current = true;
            setSelectedSourceId(event.target.value);
          }}
        >
          {sourcesState.sources.map((source) => (
            <option key={source.source_id} value={source.source_id}>
              {sourceLabel(source)}
            </option>
          ))}
        </select>
      </label>
      <fieldset>
        <legend>Asset kind</legend>
        {(
          [
            ["all", "All"],
            ["agent", t("bots.subagents")],
            ["skill", "Skills"],
          ] as const
        ).map(([value, label]) => (
          <button
            key={value}
            type="button"
            aria-pressed={kind === value}
            onClick={() => setKind(value)}
          >
            {label}
          </button>
        ))}
      </fieldset>
      <label>
        Search native assets
        <input value={query} maxLength={256} onChange={(event) => setQuery(event.target.value)} />
      </label>
      {catalogState.status === "loading" ? <Loading announce={false}>Loading…</Loading> : null}
      {catalogState.status === "error" ? (
        <p role="status">
          This source is unavailable.{" "}
          <button type="button" onClick={() => setCatalogRefresh((value) => value + 1)}>
            Retry
          </button>
        </p>
      ) : null}
      {catalog ? (
        <>
          <p>
            {`${shown.length} shown / ${catalog.assets.length} returned`}
            {" · "}
            {sourceLabel({ scope: catalog.source_scope })}
            {catalog.partial ? " · Partial catalog" : ""}
          </p>
          {catalog.partial ? (
            <ul>
              {/* omitted is one shared count over every reason: attribute it to a line only when
                  its reason is the sole one, else every per-entry line stays "Some entries". */}
              {catalog.partial_reasons.map((reason) => (
                <li key={reason}>
                  {reasonText(reason, catalog.partial_reasons.length === 1 ? catalog.omitted : 0)}
                </li>
              ))}
            </ul>
          ) : null}
          {agents.length === 0 && skills.length === 0 ? (
            <p>
              {catalog.assets.length > 0
                ? "No matching native assets"
                : catalog.partial
                  ? "No entries returned in this partial catalog."
                  : "No native assets"}
            </p>
          ) : (
            <>
              {agents.length > 0 ? (
                <>
                  <h3>{t("bots.subagents")}</h3>
                  <ul>
                    {agents.map((asset) => {
                      const state = createState[createKey(asset)];
                      return (
                        <AssetRow
                          key={asset.relative_id}
                          asset={asset}
                          create={{
                            pending: state?.status === "pending",
                            error: state?.status === "error" ? state.message : null,
                            onClick: () => {
                              void handleCreate(asset);
                            },
                          }}
                        />
                      );
                    })}
                  </ul>
                </>
              ) : null}
              {skills.length > 0 ? (
                <>
                  <h3>Skills</h3>
                  <ul>
                    {skills.map((asset) => (
                      <AssetRow key={asset.relative_id} asset={asset} />
                    ))}
                  </ul>
                </>
              ) : null}
            </>
          )}
        </>
      ) : null}
    </div>
  );
}
