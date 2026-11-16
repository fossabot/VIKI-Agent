/**
 * Shared mechanics between CreateVaultForm and OpenVaultForm:
 * - the select-after-success POST every successful create/open must make — the backend
 *   registers a workspace without selecting it, so without this the chat keeps
 *   saying no knowledge base is attached — followed by a capabilities+workspaces refresh
 *   regardless of whether the select itself lands;
 * - the per-field error text a create/open refusal renders on the field it concerns.
 */
import { useCallback, useState } from "react";
import { type ShellApi, ShellApiError } from "../../clients/app/shellApi";
import type { Key } from "../../i18n/index";
import { errorText, t } from "../../i18n/t";
import { type PlatformKind, platformKind } from "../../platform";
import type { ShellStore } from "../../state/shell";

/** `mount_point` reads a different sentence per OS: Windows has its own drive-letter text,
 * macOS and Linux (no drive letters) share one POSIX sentence. A per-OS map like platform.ts's
 * `EXAMPLE_PATH_KEY`. */
const MOUNT_POINT_KEY: Readonly<Record<PlatformKind, Key>> = {
  windows: "field.unsupported_location.mount_point",
  mac: "field.unsupported_location.mount_point_posix",
  linux: "field.unsupported_location.mount_point_posix",
};

export type SelectOutcome =
  | { kind: "pending" }
  | { kind: "done"; restartRequired: boolean }
  | { kind: "failed"; error: ShellApiError };

/** Runs after a successful createVault/openVault. The registration already happened by the time
 * this runs, so capabilities and workspaces are refreshed whether the select call lands or not. */
export function useSelectAfterVaultAction(
  api: ShellApi,
  store: ShellStore,
): { outcome: SelectOutcome | null; run: (workspaceId: string) => void } {
  const [outcome, setOutcome] = useState<SelectOutcome | null>(null);

  const run = useCallback(
    (workspaceId: string) => {
      setOutcome({ kind: "pending" });
      void (async () => {
        try {
          const result = await api.selectWorkspace(crypto.randomUUID(), workspaceId);
          await store.afterMutation(["capabilities", "workspaces"]);
          setOutcome({ kind: "done", restartRequired: result.restart_required });
        } catch (error) {
          // afterMutation is documented as never rejecting (its own refreshes never throw).
          await store.afterMutation(["capabilities", "workspaces"]);
          setOutcome({
            kind: "failed",
            error: error instanceof ShellApiError ? error : new ShellApiError(0, "unavailable"),
          });
        }
      })();
    },
    [api, store],
  );

  return { outcome, run };
}

/** Whether the client genuinely cannot tell if the write landed: a dropped connection, a client
 * timeout, an unreadable body, a 408, or any 5xx (including a plain-text crash response, which
 * still surfaces as `http_error` at the response's own status). Same shape as state/editors.ts's
 * `ambiguous`, kept separate because it reads a `ShellApiError`, not a `DurableApiError`.
 * `invalid_input` is status 0 too, but it is the client's own refusal of a value the user
 * typed — never ambiguous, always a plain field problem. */
export function ambiguousFailure(error: ShellApiError): boolean {
  return (
    error.code !== "invalid_input" &&
    (error.status === 0 || error.status >= 500 || error.status === 408)
  );
}

/** The text a create/open error shows on the field it concerns: `field.<code>` when the catalog
 * has a field row for the code, else the plain `error.<code>` row. `unsupported_location` reads
 * its cause from `extra.reason` first, falling back to the generic
 * `field.unsupported_location` row for an unknown reason.
 * Reason `mount_point` reads its sentence from `MOUNT_POINT_KEY[platformKind()]` instead of the
 * reason string, since Windows and POSIX need different wording (there are no drive letters off
 * Windows). `denied_location` reads its cause from `extra.denied` the same way, falling back to
 * the generic `field.denied_location` row for a category with no row of its own (e.g. `temp`). */
export function fieldMessage(error: ShellApiError): string {
  if (error.code === "unsupported_location") {
    const reason = error.extra.reason;
    if (typeof reason === "string") {
      const specific =
        reason === "mount_point"
          ? MOUNT_POINT_KEY[platformKind()]
          : (`field.unsupported_location.${reason}` as Key);
      const value = t(specific);
      if (value !== specific) return value;
    }
  }
  if (error.code === "denied_location") {
    const denied = error.extra.denied;
    if (typeof denied === "string") {
      const specific = `field.denied_location.${denied}` as Key;
      const value = t(specific);
      if (value !== specific) return value;
    }
  }
  const key = `field.${error.code}` as Key;
  const value = t(key);
  return value === key ? errorText(error) : value;
}
