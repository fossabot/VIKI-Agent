import { useEffect, useRef, useState } from "react";
import {
  type ProfileApi,
  ProfileApiError,
  type ProfileDefinition,
} from "../../clients/profiles/profileApi";
import {
  type ProfileLaunchApi,
  ProfileLaunchApiError,
  type ProfilePreview,
  type ProfileSessionIdentity,
} from "../../clients/profiles/profileLaunchApi";
import { readConfiguration, writeConfiguration } from "../Profiles/mcpServersModel";

export interface CapabilityLaunchProps {
  profileApi: ProfileApi;
  launchApi?: ProfileLaunchApi | undefined;
  workspaceId?: string | undefined;
  source: ProfileDefinition | null;
  included: readonly boolean[];
  onPrepared?: ((id: string, identity: ProfileSessionIdentity | null) => void) | undefined;
}

const EMPTY = { pending: false, ready: false, error: "", preview: null as ProfilePreview | null };

function failure(error: unknown): string {
  const code =
    error instanceof ProfileApiError || error instanceof ProfileLaunchApiError
      ? error.code
      : "unavailable";
  const messages: Record<string, string> = {
    conflict: "The profile or workspace changed. Reload before trying again.",
    command_conflict: "This retry no longer matches the original request.",
    workspace_restart_required: "Restart in the selected workspace before preparing this draft.",
    capability_unverified: "Provider capability evidence is incomplete.",
    not_found: "The saved profile revision was not found.",
    capacity: "The local profile or draft limit has been reached.",
    unauthorized: "Authentication is required.",
  };
  return (
    messages[code] ??
    "The connector review could not be completed. Your selection is retained; retry manually."
  );
}

export function useCapabilityLaunch({
  profileApi,
  launchApi,
  workspaceId,
  source,
  included,
  onPrepared,
}: CapabilityLaunchProps) {
  const configuration = source?.document.tools.mcp_configuration;
  const rows = configuration === null ? [] : readConfiguration(configuration);
  const valid =
    source !== null && !source.archived && rows !== null && included.length === rows.length;
  const changed = valid && included.some((value) => !value);
  const document =
    valid && rows && source
      ? changed
        ? {
            ...source.document,
            tools: {
              ...source.document.tools,
              mcp_configuration: writeConfiguration(rows.filter((_row, index) => included[index])),
            },
          }
        : source.document
      : null;
  const key = JSON.stringify([source?.profile_id, source?.revision, document, workspaceId]);
  const context = useRef({ key, profileApi, launchApi });
  context.current = { key, profileApi, launchApi };
  const controller = useRef<AbortController | null>(null);
  const attempt = useRef<{
    commandId: string;
    sessionId: string;
    copyId: string;
    copyCommandId: string;
    copied: { profileId: string; revision: number } | null;
  } | null>(null);
  const base = { ...EMPTY, key, profileApi, launchApi };
  const [state, setState] = useState(base);
  const visible =
    state.key === key && state.profileApi === profileApi && state.launchApi === launchApi
      ? state
      : EMPTY;

  // biome-ignore lint/correctness/useExhaustiveDependencies: Each input context owns its pending request and retry identifiers.
  useEffect(() => {
    attempt.current = null;
    controller.current = null;
    return () => controller.current?.abort();
  }, [key, profileApi, launchApi]);

  async function prepare(confirmCopy = false): Promise<void> {
    if (
      !source ||
      !document ||
      !launchApi ||
      !workspaceId ||
      (changed && !confirmCopy) ||
      visible.ready ||
      (controller.current && !controller.current.signal.aborted)
    )
      return;
    attempt.current ??= {
      commandId: crypto.randomUUID(),
      sessionId: crypto.randomUUID(),
      copyId: crypto.randomUUID(),
      copyCommandId: crypto.randomUUID(),
      copied: null,
    };
    const ids = attempt.current;
    const abort = new AbortController();
    controller.current = abort;
    const current = (): boolean =>
      !abort.signal.aborted &&
      context.current.key === key &&
      context.current.profileApi === profileApi &&
      context.current.launchApi === launchApi;
    setState({ ...base, pending: true });
    try {
      let target = { profileId: source.profile_id, revision: source.revision };
      if (changed) {
        if (!ids.copied) {
          const receipt = await profileApi.create(
            ids.copyId,
            ids.copyCommandId,
            document,
            abort.signal,
          );
          if (!current()) return;
          ids.copied = { profileId: receipt.profile_id, revision: receipt.revision };
        }
        target = ids.copied;
      }
      const result = await launchApi.prepare(
        target.profileId,
        ids.commandId,
        ids.sessionId,
        target.revision,
        workspaceId,
        abort.signal,
      );
      if (!current()) return;
      setState({ ...base, pending: true, preview: result });
      if (!result.launchable || result.blockers.length || !("session_id" in result)) return;
      const inspected = await launchApi.inspect(result.session_id, abort.signal);
      if (!current()) return;
      setState({ ...base, pending: true, preview: inspected });
      if (!inspected.launchable || inspected.blockers.length) return;
      if (inspected.started === true) {
        setState({
          ...base,
          preview: inspected,
          error: "This session has already started. Open it from Sessions instead.",
        });
        return;
      }
      if (inspected.started !== false) {
        setState({
          ...base,
          preview: inspected,
          error:
            "This session's never-started state could not be confirmed. Retry review manually.",
        });
        return;
      }
      if (
        inspected.session_id !== ids.sessionId ||
        inspected.draft.key !== ids.sessionId ||
        inspected.profile_id !== target.profileId ||
        inspected.profile_revision !== target.revision ||
        inspected.workspace_id !== workspaceId
      ) {
        throw new ProfileLaunchApiError(0, "invalid_response");
      }
      setState({ ...base, ready: true, preview: inspected });
      onPrepared?.(inspected.session_id, inspected.identity ?? null);
    } catch (error) {
      if (current()) setState((prior) => ({ ...prior, error: failure(error) }));
    } finally {
      if (current()) {
        controller.current = null;
        setState((prior) => ({ ...prior, pending: false }));
      }
    }
  }

  return { ...visible, changed, valid, prepare };
}
