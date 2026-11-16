import { useEffect, useId, useRef, useState } from "react";
import type {
  ProfileApi,
  ProfileDefinition,
  ProfileSummary,
} from "../../clients/profiles/profileApi";
import type {
  ProfileLaunchApi,
  ProfileSessionIdentity,
} from "../../clients/profiles/profileLaunchApi";
import { setUnsavedWork, useReportUnsavedWork } from "../../state/unsavedWork";
import { Loading } from "../Loading/Loading";
import { readConfiguration } from "../Profiles/mcpServersModel";
import { problemText } from "../Profiles/profileCopy";
import { useCapabilityLaunch } from "./useCapabilityLaunch";
import "./ConnectorSelection.css";

export interface ConnectorSelectionProps {
  profileApi: ProfileApi;
  launchApi?: ProfileLaunchApi | undefined;
  workspaceId?: string | undefined;
  onPrepared?: ((id: string, identity: ProfileSessionIdentity | null) => void) | undefined;
  onManageProfiles?: (() => void) | undefined;
}

export function ConnectorSelection({
  profileApi,
  launchApi,
  workspaceId,
  onPrepared,
  onManageProfiles,
}: ConnectorSelectionProps) {
  const selectId = useId();
  const [reload, setReload] = useState(0);
  const [list, setList] = useState<{
    api: ProfileApi;
    rows: ProfileSummary[];
    loading: boolean;
    error: string;
  }>({ api: profileApi, rows: [], loading: true, error: "" });
  const [selection, setSelection] = useState<{ api: ProfileApi; profile: ProfileSummary } | null>(
    null,
  );
  const selected = selection?.api === profileApi ? selection.profile : null;
  const selectedKey = selected ? `${selected.profile_id}:${selected.revision}` : "";
  const [detail, setDetail] = useState<{
    api: ProfileApi;
    key: string;
    source: ProfileDefinition;
    included: boolean[];
  } | null>(null);
  const [detailError, setDetailError] = useState("");
  const [reviewKey, setReviewKey] = useState<string | null>(null);
  const owner = useRef({ profileApi, selectedKey });
  owner.current = { profileApi, selectedKey };
  const draft = detail?.api === profileApi && detail.key === selectedKey ? detail : null;
  const source = draft?.source ?? null;
  const rows =
    source?.document.tools.mcp_configuration === null
      ? []
      : readConfiguration(source?.document.tools.mcp_configuration);
  const included = draft?.included ?? [];
  const launch = useCapabilityLaunch({
    profileApi,
    launchApi,
    workspaceId,
    source,
    included,
    onPrepared: (id, identity) => {
      // The confirmed copy is saved; navigation reads this store before React commits ready.
      setUnsavedWork("profile", false);
      onPrepared?.(id, identity);
    },
  });
  const dirty = launch.changed && !launch.ready;
  useReportUnsavedWork("profile", dirty);
  const currentReviewKey = JSON.stringify([selectedKey, workspaceId, included]);
  const reviewing = reviewKey === currentReviewKey;
  const previewPermissionMode =
    launch.preview && "prefs" in launch.preview ? launch.preview.prefs.mode : null;
  const shownList = list.api === profileApi ? list : { rows: [], loading: true, error: "" };

  // biome-ignore lint/correctness/useExhaustiveDependencies: Reload is the explicit manual list retry.
  useEffect(() => {
    const controller = new AbortController();
    setSelection(null);
    setDetail(null);
    setDetailError("");
    setReviewKey(null);
    setList({ api: profileApi, rows: [], loading: true, error: "" });
    profileApi.list(false, controller.signal).then(
      (result) => {
        if (!controller.signal.aborted && owner.current.profileApi === profileApi)
          setList({ api: profileApi, rows: result, loading: false, error: "" });
      },
      () => {
        if (!controller.signal.aborted && owner.current.profileApi === profileApi)
          setList({
            api: profileApi,
            rows: [],
            loading: false,
            error: "Saved app profiles could not be loaded. Retry manually.",
          });
      },
    );
    return () => controller.abort();
  }, [profileApi, reload]);

  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    setDetail(null);
    setDetailError("");
    setReviewKey(null);
    profileApi.get(selected.profile_id, selected.revision, controller.signal).then(
      (result) => {
        if (
          controller.signal.aborted ||
          owner.current.profileApi !== profileApi ||
          owner.current.selectedKey !== selectedKey
        )
          return;
        const configuration = result.document.tools.mcp_configuration;
        const parsed = configuration === null ? [] : readConfiguration(configuration);
        setDetail({
          api: profileApi,
          key: selectedKey,
          source: result,
          included: parsed?.map(() => true) ?? [],
        });
      },
      () => {
        if (
          !controller.signal.aborted &&
          owner.current.profileApi === profileApi &&
          owner.current.selectedKey === selectedKey
        )
          setDetailError(
            "The selected profile revision could not be loaded. Reload profiles to retry.",
          );
      },
    );
    return () => controller.abort();
  }, [profileApi, selected, selectedKey]);

  function resetInclusion(): void {
    if (!draft) return;
    setDetail({ ...draft, included: included.map(() => true) });
    setReviewKey(null);
  }

  return (
    <section className="connector-selection" aria-label="App-profile connectors">
      <div className="connector-selection-heading">
        <div>
          <h2>Connectors for your next session</h2>
          <p>
            Select the app-profile MCP servers to include. These controls leave native provider
            configuration and permissions unchanged.
          </p>
        </div>
        {onManageProfiles ? (
          <button type="button" onClick={onManageProfiles} disabled={launch.pending}>
            Manage profiles
          </button>
        ) : null}
      </div>
      <label htmlFor={selectId}>Saved app profile</label>
      <div className="connector-selection-picker">
        <select
          id={selectId}
          value={selectedKey}
          disabled={shownList.loading || dirty || launch.pending}
          onChange={(event) => {
            const profile = shownList.rows.find(
              (row) => `${row.profile_id}:${row.revision}` === event.target.value,
            );
            setSelection(profile ? { api: profileApi, profile } : null);
          }}
        >
          <option value="">Choose a saved profile</option>
          {shownList.rows.map((profile) => (
            <option key={profile.profile_id} value={`${profile.profile_id}:${profile.revision}`}>
              {profile.identity.name} · revision {profile.revision} · {profile.runtime.provider}
            </option>
          ))}
        </select>
        <button
          type="button"
          disabled={dirty || launch.pending}
          onClick={() => setReload((value) => value + 1)}
        >
          Reload profiles
        </button>
      </div>
      {shownList.loading ? <Loading>Loading saved app profiles…</Loading> : null}
      {shownList.error ? <p role="alert">{shownList.error}</p> : null}
      {!shownList.loading && !shownList.error && !shownList.rows.length ? (
        <p>No saved app profiles. Create one in Profiles to configure connectors.</p>
      ) : null}
      {selected && !source && !detailError ? <Loading>Loading selected revision…</Loading> : null}
      {detailError ? <p role="alert">{detailError}</p> : null}
      {source ? (
        <>
          <div className="connector-selection-source">
            <strong>{source.document.identity.name}</strong>
            <span>Source: saved app profile · revision {source.revision}</span>
            <code>{source.profile_id}</code>
          </div>
          {rows === null ? (
            <p>
              This MCP configuration cannot be selected here. Manage the saved profile to review it.
            </p>
          ) : (
            <fieldset disabled={launch.pending || launch.ready || source.archived}>
              <legend>Include in next session</legend>
              {rows.length ? (
                rows.map((row, index) => (
                  // biome-ignore lint/suspicious/noArrayIndexKey: The exact saved revision's server order is immutable; duplicate names still need distinct rows.
                  <label className="connector-selection-row" key={`${index}-${row.name}`}>
                    <input
                      type="checkbox"
                      checked={included[index] ?? false}
                      aria-label={`Include ${row.name} in next session`}
                      onChange={(event) => {
                        if (!draft) return;
                        setDetail({
                          ...draft,
                          included: included.map((value, position) =>
                            position === index ? event.target.checked : value,
                          ),
                        });
                        setReviewKey(null);
                      }}
                    />
                    <span>
                      {row.name}
                      <small>App-profile MCP · {row.transport}</small>
                    </span>
                  </label>
                ))
              ) : (
                <p>This profile has no app-profile MCP servers.</p>
              )}
            </fieldset>
          )}
          <div className="connector-selection-actions">
            <button
              type="button"
              disabled={
                !launch.valid || !workspaceId || !launchApi || launch.pending || launch.ready
              }
              onClick={() => setReviewKey(currentReviewKey)}
            >
              Review next session
            </button>
            {dirty ? (
              <button type="button" disabled={launch.pending} onClick={resetInclusion}>
                Reset inclusion
              </button>
            ) : null}
          </div>
          {!workspaceId || !launchApi ? (
            <p>
              Select an active workspace with profile launch review available to prepare a draft.
            </p>
          ) : null}
          {reviewing ? (
            <section className="connector-selection-review" aria-label="Next session review">
              <h3>Next session review</h3>
              <p>
                Source revision {source.revision}. Workspace: <code>{workspaceId}</code>
              </p>
              <p>
                Included app-profile connectors:{" "}
                {rows
                  ?.filter((_row, index) => included[index])
                  .map((row) => row.name)
                  .join(", ") || "None"}
                .
              </p>
              <p>
                {launch.changed
                  ? "This selection will save a new profile copy before preparing a draft. The source profile is kept."
                  : "This selection uses the saved profile revision."}{" "}
                Other profile fields and launch blockers are preserved.
              </p>
              <button
                type="button"
                disabled={
                  launch.pending || launch.ready || !launch.valid || !workspaceId || !launchApi
                }
                onClick={() => void launch.prepare(launch.changed)}
              >
                {launch.changed
                  ? "Save new profile copy and prepare draft"
                  : "Prepare draft from saved revision"}
              </button>
              {launch.pending ? <Loading>Reviewing and preparing…</Loading> : null}
              {launch.error ? <p role="alert">{launch.error}</p> : null}
              {launch.preview ? (
                <div role="status">
                  <p>
                    {launch.ready
                      ? "Draft is ready. Send a message in the conversation to start the provider."
                      : launch.preview.launchable
                        ? "Draft preparation needs further review."
                        : "Draft is blocked."}
                  </p>
                  {launch.preview.blockers.length ? (
                    <ul>
                      {launch.preview.blockers.map((blocker) => {
                        const field = launch.preview?.fields.find((item) => item.field === blocker);
                        return (
                          <li key={blocker}>
                            {problemText(blocker, {
                              ...(field?.reason ? { reason: field.reason } : {}),
                              permissionMode: previewPermissionMode,
                            })}
                          </li>
                        );
                      })}
                    </ul>
                  ) : null}
                  {launch.preview.fields
                    .filter((field) => field.status !== "supported")
                    .map((field) => (
                      <p key={field.field}>
                        {problemText(field.field, {
                          ...(field.reason ? { reason: field.reason } : {}),
                          permissionMode: previewPermissionMode,
                        })}
                      </p>
                    ))}
                </div>
              ) : null}
            </section>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
