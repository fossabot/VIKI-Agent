import { useEffect, useState } from "react";
import {
  type PreparedProfilePreview,
  type ProfileLaunchApi,
  ProfileLaunchApiError,
  type ProfileSessionIdentity,
} from "../../clients/profiles/profileLaunchApi";
import { tf } from "../../i18n/t";
import { modelLabel, useConnectionLabels } from "./modelLabel";

/** A bound profile session's launch never repairs itself: each blocker code from
 * `/api/v2/profile-sessions/{id}` names why. A session that is already sending keeps
 * doing so regardless of this result (only a fresh *start* is refused), so the way on must stay
 * true whether or not this session is live right now — it never claims Send is blocked. */
const BLOCKER_REASON: Record<string, string> = {
  launch_changed: "This session can no longer start again from its profile launch.",
  capability_changed: "The provider's capabilities changed since this session's profile launch.",
  launch_blocked: "This profile's launch is blocked.",
  session_exists: "This session's profile draft has already started elsewhere.",
  provider_unavailable: "The profile's provider is unavailable.",
  connection_bot_only:
    "A saved model connection works only in a Bot's chat. Start a new draft from the Bot.",
  connection_missing:
    "This profile's saved model connection was removed. Choose another model in the profile's settings.",
  connections_unavailable:
    "Saved model connections cannot be read right now. Check Settings, then use Refresh review.",
  connection_invalid:
    "This profile's saved model connection is no longer valid. Update it in Settings or choose another model.",
};
/** These four reasons already say the way on, so no drift sentence follows them. */
const CONNECTION_CODES = new Set([
  "connection_bot_only",
  "connection_missing",
  "connections_unavailable",
  "connection_invalid",
]);
const CLAUDE_DEFAULT = "Claude Code's default";
/** Every blocker but provider_unavailable is drift: nothing brings the old launch back, so the
 * only way on is a fresh draft. A conversation this session is already sending is unaffected —
 * it keeps going until it ends on its own; only starting it again is refused. */
const DRIFT_WAY_ON =
  "A conversation this session is already sending keeps going until it ends. " +
  "To continue after that, start a new draft from the profile in Agents.";
/** provider_unavailable is not drift — nothing about this launch changed, so a new draft would
 * fail the identical way once the provider is unavailable. The way on is to fix the provider,
 * not to discard this draft. */
const PROVIDER_WAY_ON = "Check Settings, then use Refresh review.";

function blockedReason(blockers: readonly string[]): string {
  const reasons = blockers.map(
    (code) => BLOCKER_REASON[code] ?? `This profile launch is blocked (${code}).`,
  );
  if (blockers.every((code) => CONNECTION_CODES.has(code))) return [...new Set(reasons)].join(" ");
  const wayOn = blockers.includes("provider_unavailable") ? PROVIDER_WAY_ON : DRIFT_WAY_ON;
  return `${[...new Set(reasons)].join(" ")} ${wayOn}`;
}

/** A field's effective value is server JSON of unknown shape; render it as one text node. */
function displayValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  if (typeof value === "boolean" || typeof value === "number") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

const MODEL_FIELDS = new Set(["prefs.model", "runtime.model"]);

/** The binding is re-read after reload; session-local UI state is never launch authority. */
export function ProfileSessionReview({
  api,
  sessionId,
  refreshKey,
  onProfileSession,
  onUnboundSession,
}: {
  api: ProfileLaunchApi;
  sessionId: string;
  /** The selected view's current error object (or undefined), so a review that was opened
   * before a start refusal (or never opened) re-inspects without a manual Refresh review. Keyed
   * on the error itself, not a running counter: the reducer keeps the same object across every
   * later event that leaves it set, so only a genuinely new refusal changes this reference. */
  refreshKey?: unknown;
  /** Told once a successful inspection proves this session is bound to a profile, so the
   * composer can hide the model chip a profile session's server always refuses (`profile_locked`).
   * Carries the bound identity, or null only when the server's answer omits it. */
  onProfileSession?: (sessionId: string, identity: ProfileSessionIdentity | null) => void;
  /** Told when the inspection answers 404 `not_found`: this session has no profile binding.
   * Any other failure proves nothing either way, so neither callback hears of it. */
  onUnboundSession?: (sessionId: string) => void;
}) {
  const [refresh, setRefresh] = useState(0);
  const [result, setResult] = useState<{
    api: ProfileLaunchApi;
    sessionId: string;
    preview?: PreparedProfilePreview;
    failed?: boolean;
  } | null>(null);
  // biome-ignore lint/correctness/useExhaustiveDependencies(refresh): a manual re-run trigger.
  // biome-ignore lint/correctness/useExhaustiveDependencies(refreshKey): App's re-run trigger.
  useEffect(() => {
    const controller = new AbortController();
    void api.inspect(sessionId, controller.signal).then(
      (preview) => {
        if (controller.signal.aborted) return;
        setResult({ api, sessionId, preview });
        onProfileSession?.(sessionId, preview.identity ?? null);
      },
      (error: unknown) => {
        if (controller.signal.aborted) return;
        const unbound = error instanceof ProfileLaunchApiError && error.code === "not_found";
        setResult({ api, sessionId, failed: !unbound });
        if (unbound) onUnboundSession?.(sessionId);
      },
    );
    return () => controller.abort();
  }, [api, sessionId, refresh, refreshKey, onProfileSession, onUnboundSession]);
  const shown = result?.api === api && result.sessionId === sessionId ? result.preview : undefined;
  const connections = useConnectionLabels(
    shown?.prefs.model?.startsWith("viki-llm/") === true ||
      shown?.fields.some(
        (field) =>
          MODEL_FIELDS.has(field.field) &&
          typeof field.effective_value === "string" &&
          field.effective_value.startsWith("viki-llm/"),
      ) === true,
    refresh,
  );
  if (result?.api !== api || result.sessionId !== sessionId) return null;
  // A manual retry only: a failed read leaves the chat's binding unknown until one answers.
  if (result.failed)
    return (
      <>
        <p role="status">Profile details are unavailable.</p>
        <button type="button" onClick={() => setRefresh((value) => value + 1)}>
          Refresh review
        </button>
      </>
    );
  const preview = result.preview;
  if (!preview) return null;
  const name = preview.fields.find((field) => field.field === "identity.name")?.effective_value;
  return (
    <details open={preview.launchable ? undefined : true}>
      <summary>
        Profile instructions · {typeof name === "string" ? name : "Profile"} · revision{" "}
        {preview.profile_revision} · {preview.runtime}
        {preview.launchable ? "" : " · cannot start again"}
      </summary>
      {preview.launchable ? (
        <p>
          {preview.bot_memory
            ? "These instructions and VIKI Agent's memory rule and Bot rules go to Claude Code as its system prompt each time this chat starts. Type your message and Send."
            : "The instructions are editable in the composer. Use Send when you are ready."}
        </p>
      ) : null}
      {preview.code_copy ? (
        <p>
          {preview.code_copy.branch === null
            ? tf("bots.code_folder.review_pending", { name: preview.code_copy.name })
            : tf("bots.code_folder.review_existing", {
                name: preview.code_copy.name,
                branch: preview.code_copy.branch,
              })}
        </p>
      ) : null}
      <button type="button" onClick={() => setRefresh((value) => value + 1)}>
        Refresh review
      </button>
      {!preview.launchable ? <p role="alert">{blockedReason(preview.blockers)}</p> : null}
      <p>
        Workspace policy: {preview.policy}. Model:{" "}
        {preview.prefs.model === null
          ? CLAUDE_DEFAULT
          : modelLabel(preview.prefs.model, null, connections.labels)}
        . Effort: {preview.prefs.effort}. Permission mode: {preview.prefs.mode}.
      </p>
      <table>
        <thead>
          <tr>
            <th>Field</th>
            <th>Effective value</th>
            <th>Effect</th>
            <th>Source</th>
            <th>Support</th>
          </tr>
        </thead>
        <tbody>
          {preview.fields.map((field) => (
            <tr key={field.field}>
              <td>{field.field}</td>
              <td>
                {MODEL_FIELDS.has(field.field) && typeof field.effective_value === "string"
                  ? modelLabel(field.effective_value, null, connections.labels)
                  : displayValue(field.effective_value)}
              </td>
              <td>{field.preview_class}</td>
              <td>{field.source}</td>
              <td>
                {field.status}
                {field.reason ? ` (${field.reason})` : ""}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {preview.mcp_config ? (
        <section className="mcp-preview">
          <h3>App MCP servers</h3>
          <p>Generated file</p>
          <pre>{preview.mcp_config.json}</pre>
          <p>Argument</p>
          <code>{preview.mcp_config.argv_element}</code>
        </section>
      ) : null}
    </details>
  );
}
