/**
 * The shell's own external store: capabilities, the settings document and the
 * workspace listing, read through `src/clients/app/shellApi.ts` and published with `AppStore`'s identity rules
 * (a new snapshot object per commit, the same object until the next), so the existing
 * `useSnapshot` can read it.
 *
 * Rules:
 * - `createShellStore(api, appStore)`. With `api === null` — an App mounted without a
 *   shell, which every frozen suite does — `start`, `stop` and every `refresh*` resolve at once
 *   and the snapshot never changes. `patch` and `runCheck` have no honest value to resolve with
 *   there, so they reject with `unavailable` and still change nothing.
 * - Generation counters per kind, exactly as `rest.ts`: only the newest read of a kind may
 *   commit. A failure keeps the data already on screen, sets that kind's status to `"error"`
 *   and `errors[kind]` to the `ShellApiError` code; the next success clears it. A retry already
 *   in flight commits `"loading"` without clearing that stale code, so a view must render from
 *   `<kind>Status`, never from `errors[kind]` being non-empty. Every rejection is caught inside
 *   the store, so `refresh*` never rejects.
 * - `start()` reads capabilities first, then settings and workspaces once its first successful
 *   normal-mode answer arrives, including from a manual retry. It re-reads **capabilities**
 *   whenever the socket becomes `open` after having been something else (a reconnect).
 *   `stop()` invalidates pending reads and startup ownership. No interval:
 *   capabilities change through this window's own actions, another window's actions (which a
 *   reconnect does not signal — the Workspaces page has a manual refresh for that) or a restart.
 * - `rest.ts`'s `coalesce()` is deliberately not copied. It exists for the rest store's rule that
 *   concurrent explicit callers fold into one request; these reads are triggered by the user's
 *   own actions, which cannot burst faster than the network answers, and the generation rule
 *   already keeps a double click consistent. Folding would import a settlement subtlety to
 *   prevent a duplicate GET nobody can observe.
 * - Appearance is *not* applied here: App owns that effect, because the Appearance
 *   page's live preview must be able to override the committed values.
 * - Nothing is logged or cached, and nothing is retried except `patch`'s one resend after a
 *   conflict that moved only `layout.panes` (see `ShellStore.patch`).
 */
import {
  type Capabilities,
  type CheckName,
  type CheckResult,
  type SettingsPatchResult,
  type SettingsView,
  type ShellApi,
  ShellApiError,
  type WorkspacesListing,
} from "../clients/app/shellApi";
import type { AppStore } from "./store";

export type ReadStatus = "unavailable" | "loading" | "ready" | "error";
export type ReadKind = "capabilities" | "settings" | "workspaces";

export interface ShellSnapshot {
  capabilities: Capabilities | null;
  capabilitiesStatus: ReadStatus;
  settings: SettingsView | null;
  settingsStatus: ReadStatus;
  workspaces: WorkspacesListing | null;
  workspacesStatus: ReadStatus;
  /** The last failure's `ShellApiError` code per kind, "" when the last read succeeded. Stays
   * the stale code while a retry is `"loading"` (below), so this is not itself the failure
   * signal a view renders from -- `<kind>Status === "error"` is (no production view reads
   * this). */
  errors: { capabilities: string; settings: string; workspaces: string };
  recovery: boolean;
  /** The check in flight, so every button that could start one is disabled at once. */
  checkRunning: CheckName | null;
}

export interface ShellStore {
  getSnapshot(): ShellSnapshot;
  subscribe(listener: () => void): () => void;
  refreshCapabilities(): Promise<void>;
  refreshSettings(): Promise<void>;
  refreshWorkspaces(): Promise<void>;
  /**
   * PATCH with the snapshot's revision and a fresh `command_id`; commits the result. A 409
   * `stale_revision` whose document's effective values differ from the snapshot's only in
   * `layout.panes` is sent once more, with a fresh `command_id`, on that document's revision,
   * unless `changes` write `layout.panes` themselves; that resend commits or rejects as the first
   * attempt would, and is never resent. Every other 409 `stale_revision` rejects with the
   * `ShellApiError` — its `current` is NOT committed, because the caller's review panel decides
   * what the user keeps — and everything else, including a `read_only` refusal, rejects with
   * nothing committed.
   */
  patch(changes: Record<string, unknown>): Promise<SettingsPatchResult>;
  /** Commit a document the caller already holds (the 409 panel's "Load the current values and
   * re-enter", an import apply's result, a reset's aftermath). */
  adoptSettings(view: SettingsView): void;
  /** POST a check with a fresh `command_id`, hold `checkRunning`, then re-read capabilities.
   * Rejects with the `ShellApiError` (409 check_in_progress, 422 unsupported) unchanged. */
  runCheck(check: CheckName): Promise<CheckResult>;
  /** After a workspace mutation or an import: capabilities + workspaces (+ settings). */
  afterMutation(kinds: ReadKind[]): Promise<void>;
  start(): void;
  stop(): void;
}

function errorCode(error: unknown): string {
  return error instanceof ShellApiError ? error.code : "unavailable";
}

/** Where the primary window saves its pane layout (`usePanes`). */
const LAYOUT_PANES = "layout.panes";

/**
 * For `patch`: the server's document when a 409 `stale_revision` moved nothing but
 * `layout.panes`. The error carries that document, `changes` leave `layout.panes` alone, and
 * every other effective value equals the one `base` (the document the request was built on)
 * showed: the same keys, values equal as JSON. Else null, and the conflict is the caller's.
 */
function rebaseOnto(
  error: unknown,
  base: SettingsView | null,
  changes: Record<string, unknown>,
): SettingsView | null {
  if (!(error instanceof ShellApiError) || error.status !== 409 || error.code !== "stale_revision")
    return null;
  const current = error.current;
  if (current === null || base === null || Object.hasOwn(changes, LAYOUT_PANES)) return null;
  const keys = Object.keys(base.effective).filter((key) => key !== LAYOUT_PANES);
  const same =
    keys.length === Object.keys(current.effective).filter((key) => key !== LAYOUT_PANES).length &&
    keys.every(
      (key) =>
        Object.hasOwn(current.effective, key) &&
        JSON.stringify(current.effective[key]) === JSON.stringify(base.effective[key]),
    );
  return same ? current : null;
}

/** The three vault surfaces (tiles, Needs you, git chip) draw unless the app KNOWS it runs in the
 * shared scratch root or in a code folder: null → true; `shared_scratch` → false; a code folder
 * active → false; everything else → true. */
export function vaultSurfaces(snapshot: ShellSnapshot): boolean {
  const workspace = snapshot.capabilities?.workspace;
  return workspace?.shared_scratch !== true && workspace?.active?.kind !== "code";
}

/** null → null (unknown, which every caller treats as allowed); else `provider.claude.can_start`. */
export function canStart(snapshot: ShellSnapshot): boolean | null {
  return snapshot.capabilities?.provider.claude.can_start ?? null;
}

export function createShellStore(api: ShellApi | null, appStore: AppStore): ShellStore {
  let snapshot: ShellSnapshot = {
    capabilities: null,
    capabilitiesStatus: "unavailable",
    settings: null,
    settingsStatus: "unavailable",
    workspaces: null,
    workspacesStatus: "unavailable",
    errors: { capabilities: "", settings: "", workspaces: "" },
    recovery: false,
    checkRunning: null,
  };
  const listeners = new Set<() => void>();
  const generations: Record<ReadKind, number> = { capabilities: 0, settings: 0, workspaces: 0 };

  function commit(patch: Partial<ShellSnapshot>): void {
    snapshot = { ...snapshot, ...patch };
    for (const listener of [...listeners]) listener();
  }

  function statusPatch(kind: ReadKind, status: ReadStatus, code: string): Partial<ShellSnapshot> {
    const errors = { ...snapshot.errors, [kind]: code };
    if (kind === "capabilities") return { capabilitiesStatus: status, errors };
    if (kind === "settings") return { settingsStatus: status, errors };
    return { workspacesStatus: status, errors };
  }

  /** One read of one kind: newest-wins, never rejects, never empties what is on screen. */
  async function read<T>(
    kind: ReadKind,
    load: (client: ShellApi) => Promise<T>,
    apply: (value: T) => void,
  ): Promise<void> {
    if (api === null) return;
    generations[kind] += 1;
    const generation = generations[kind];
    commit(statusPatch(kind, "loading", snapshot.errors[kind]));
    try {
      const value = await load(api);
      if (generation === generations[kind]) apply(value);
    } catch (error) {
      if (generation === generations[kind]) commit(statusPatch(kind, "error", errorCode(error)));
    }
  }

  function refreshCapabilities(): Promise<void> {
    const currentLifecycle = lifecycle;
    return read(
      "capabilities",
      (client) => client.capabilities(),
      (value) => {
        commit({
          capabilities: value,
          recovery: value.app.mode === "recovery",
          ...statusPatch("capabilities", "ready", ""),
        });
        // Recovery serves neither settings nor workspaces; a failed read admits neither.
        if (
          started &&
          lifecycle === currentLifecycle &&
          !initialized &&
          value.app.mode === "normal"
        ) {
          initialized = true;
          void refreshSettings();
          void refreshWorkspaces();
        }
      },
    );
  }

  function refreshSettings(): Promise<void> {
    return read(
      "settings",
      (client) => client.settings(),
      (value) => commit({ settings: value, ...statusPatch("settings", "ready", "") }),
    );
  }

  function refreshWorkspaces(): Promise<void> {
    return read(
      "workspaces",
      (client) => client.workspaces(),
      (value) => commit({ workspaces: value, ...statusPatch("workspaces", "ready", "") }),
    );
  }

  function refreshOf(kind: ReadKind): Promise<void> {
    if (kind === "capabilities") return refreshCapabilities();
    if (kind === "settings") return refreshSettings();
    return refreshWorkspaces();
  }

  function afterMutation(kinds: ReadKind[]): Promise<void> {
    return Promise.all(kinds.map(refreshOf)).then(() => undefined);
  }

  function adoptSettings(view: SettingsView): void {
    // The caller's document is the newest settings truth, so a GET already in flight must not
    // land on top of it: the same generation rule, claimed by this commit.
    generations.settings += 1;
    commit({ settings: view, ...statusPatch("settings", "ready", "") });
  }

  let started = false;
  let initialized = false;
  let lifecycle = 0;
  let unsubscribe: (() => void) | undefined;

  return {
    getSnapshot: () => snapshot,
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    refreshCapabilities,
    refreshSettings,
    refreshWorkspaces,
    async patch(changes) {
      if (api === null) throw new ShellApiError(0, "unavailable");
      const base = snapshot.settings;
      let result: SettingsPatchResult;
      try {
        result = await api.patchSettings(crypto.randomUUID(), base?.revision ?? 0, changes);
      } catch (error) {
        // The primary window's layout saves move the one revision every write is checked
        // against. When that is all that moved, the same changes go once more on the server's
        // revision; every other conflict, and a failed resend, reaches the caller unchanged.
        const current = rebaseOnto(error, base, changes);
        if (current === null) throw error;
        result = await api.patchSettings(crypto.randomUUID(), current.revision, changes);
      }
      adoptSettings(result);
      // The session defaults are recomputed server-side from `chat.*`, so what the next session
      // would start with has changed; cheap to re-read and harmless when nothing moved.
      if (Object.keys(changes).some((key) => key.startsWith("chat.")))
        await afterMutation(["capabilities"]);
      return result;
    },
    adoptSettings,
    async runCheck(check) {
      if (api === null) throw new ShellApiError(0, "unavailable");
      if (snapshot.checkRunning !== null) throw new ShellApiError(409, "check_in_progress");
      const currentLifecycle = lifecycle;
      commit({ checkRunning: check });
      let result: CheckResult;
      try {
        result = await api.runCheck(crypto.randomUUID(), check);
      } catch (error) {
        // A refused check ran nothing, so there is no new readiness to read.
        commit({ checkRunning: null });
        throw error;
      }
      commit({ checkRunning: null });
      if (lifecycle === currentLifecycle) await refreshCapabilities();
      return result;
    },
    afterMutation,
    start() {
      if (started || api === null) return;
      started = true;
      initialized = false;
      void refreshCapabilities();
      let previous = appStore.getSnapshot().status;
      unsubscribe = appStore.subscribe(() => {
        const status = appStore.getSnapshot().status;
        if (status === "open" && previous !== "open") void refreshCapabilities();
        previous = status;
      });
    },
    stop() {
      started = false;
      lifecycle += 1;
      generations.capabilities += 1;
      generations.settings += 1;
      generations.workspaces += 1;
      unsubscribe?.();
      unsubscribe = undefined;
    },
  };
}
