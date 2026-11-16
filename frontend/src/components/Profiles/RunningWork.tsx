/**
 * The Agents view's running-work list: every session open right now, with Open, Stop and Close,
 * plus a read-only list of sessions other claude processes have open (a terminal session, for
 * example). A row bound to a profile launch also shows that identity, once its own inspection
 * resolves.
 */
import { type ReactElement, useEffect, useRef, useState } from "react";
import {
  type PreparedProfilePreview,
  ProfileLaunchApiError,
} from "../../clients/profiles/profileLaunchApi";
import { t } from "../../i18n/t";
import type { OtherSession } from "../../protocol";
import { timeLabel } from "../../state/rows";
import { Avatar } from "../Avatar/Avatar";
import { BotAvatar } from "../Bots/BotAvatar";
import { modelLabel, useConnectionLabels } from "./modelLabel";

export interface RunningRow {
  sessionId: string;
  title: string;
  busy: boolean;
  since: string;
}

export interface RunningWorkProps {
  rows: readonly RunningRow[];
  /** Bounds current rows selected for inspection; reads are serialized separately so a burst
   * does not exhaust the profile routes' shared limiter. Departed ids free selection slots
   * without aborting the active read. */
  maxLiveSessions?: number;
  others: readonly OtherSession[];
  inspect: (sessionId: string, signal: AbortSignal) => Promise<PreparedProfilePreview>;
  nowMs: number;
  onOpen: (sessionId: string) => void;
  onStop: (sessionId: string) => void;
  onClose: (sessionId: string) => void;
  /** The Bot profile id of a Bot's chat, from App's Bot binds. */
  botProfileIdOf?: (sessionId: string) => string | undefined;
}

type Inspection = { preview: PreparedProfilePreview } | { failed: true };

export function RunningWork({
  rows,
  maxLiveSessions = 3,
  others,
  inspect,
  nowMs,
  onOpen,
  onStop,
  onClose,
  botProfileIdOf,
}: RunningWorkProps): ReactElement {
  const inspectionRef = useRef<{
    signal: AbortSignal;
    ids: Set<string>;
    queue: string[];
    activeId: string | null;
  } | null>(null);
  const [results, setResults] = useState<Record<string, Inspection>>({});

  // Created fresh on mount, and again on StrictMode's development-only remount, so its aborted
  // controller and already-inspected ids from a discarded mount are never reused.
  useEffect(() => {
    const controller = new AbortController();
    inspectionRef.current = {
      signal: controller.signal,
      ids: new Set(),
      queue: [],
      activeId: null,
    };
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const run = inspectionRef.current;
    if (!run) return;
    // Count toward the cap only ids still live: one that left `rows` frees a slot for a new one.
    const liveIds = new Set(rows.map((r) => r.sessionId));
    run.ids = new Set(Array.from(run.ids).filter((id) => liveIds.has(id)));
    run.queue = run.queue.filter((id) => liveIds.has(id));
    for (const row of rows) {
      if (run.ids.size >= maxLiveSessions) break;
      if (run.ids.has(row.sessionId)) continue;
      run.ids.add(row.sessionId);
      if (run.activeId !== row.sessionId) run.queue.push(row.sessionId);
    }
    if (run.activeId !== null) return;
    void (async () => {
      while (!run.signal.aborted) {
        const id = run.queue.shift();
        if (id === undefined) break;
        run.activeId = id;
        try {
          const preview = await inspect(id, run.signal);
          if (run.signal.aborted || !run.ids.has(id) || !preview.identity) continue;
          setResults((prev) => ({ ...prev, [id]: { preview } }));
        } catch (error: unknown) {
          if (
            run.signal.aborted ||
            !run.ids.has(id) ||
            (error instanceof ProfileLaunchApiError && error.code === "not_found")
          )
            continue;
          setResults((prev) => ({ ...prev, [id]: { failed: true } }));
        }
      }
      run.activeId = null;
    })();
  }, [rows, inspect, maxLiveSessions]);

  const connections = useConnectionLabels(
    Object.values(results).some(
      (item) => "preview" in item && item.preview.prefs.model?.startsWith("viki-llm/"),
    ),
  );
  const modelText = (model: string | null): string =>
    model === null
      ? t("profiles.model.claudeDefault")
      : modelLabel(model, null, connections.labels, t("profiles.model.connected"));

  if (rows.length === 0 && others.length === 0) {
    return <p>No running work</p>;
  }

  return (
    <div>
      {rows.length > 0 ? (
        <ul>
          {rows.map((row) => {
            const inspection = results[row.sessionId];
            const preview = inspection && "preview" in inspection ? inspection.preview : null;
            const identity = preview?.identity;
            const failed = inspection !== undefined && "failed" in inspection;
            const since = timeLabel(row.since, nowMs);
            const botProfileId = botProfileIdOf?.(row.sessionId);
            return (
              <li key={row.sessionId} className="session-organizer">
                <p>{row.title}</p>
                <p>
                  {row.busy ? "busy" : "idle"}
                  {since ? ` · since ${since}` : ""}
                </p>
                {identity ? (
                  <p>
                    {botProfileId ? (
                      <BotAvatar
                        profileId={botProfileId}
                        identity={{
                          name: identity.name,
                          purpose: "",
                          avatar_asset_id: identity.avatar_asset_id,
                          accent: identity.accent,
                          shape: identity.shape,
                        }}
                        name={identity.name}
                        size={24}
                      />
                    ) : (
                      <Avatar
                        name={identity.name}
                        avatarId={identity.avatar_asset_id}
                        accent={identity.accent}
                        shape={identity.shape}
                        size={24}
                      />
                    )}
                    <span>{identity.name}</span>
                    <span className="chip">{`r${identity.profile_revision}`}</span>
                    <span className="chip">Profile</span>
                  </p>
                ) : null}
                {identity && preview ? (
                  <details>
                    <summary>Profile details</summary>
                    <p>
                      {`Revision r${identity.profile_revision}. Workspace ${preview.workspace_id}.`}
                    </p>
                    <p>
                      {`Policy: ${preview.policy}. Model: ${modelText(preview.prefs.model)}. Effort: ${preview.prefs.effort}. Permission mode: ${preview.prefs.mode}.`}
                    </p>
                  </details>
                ) : null}
                {failed ? <p role="status">Profile details are unavailable.</p> : null}
                <div className="session-organizer-actions">
                  <button type="button" onClick={() => onOpen(row.sessionId)}>
                    Open
                  </button>
                  <button type="button" disabled={!row.busy} onClick={() => onStop(row.sessionId)}>
                    Stop
                  </button>
                  <button type="button" onClick={() => onClose(row.sessionId)}>
                    Close
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}
      {others.length > 0 ? (
        <section aria-label="Running elsewhere">
          <h3>Running elsewhere</h3>
          <ul>
            {others.map((other) => (
              <li key={other.session_id}>
                <p>{other.title}</p>
                <p>{other.entrypoint}</p>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
