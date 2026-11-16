/**
 * Loads one workspace's vault overview (`VaultApi.overview`). A module-level `WeakMap<object,
 * Map<workspaceId, VaultOverview>>`, keyed by the `api` object, remembers the last `ready` answer
 * for the window's life (never persisted — see the note on `remembered` below); the initial render
 * and any owner change (a different `api` or `workspaceId`) seed `data` from it at once, phase
 * "loading", and an error that follows keeps that same-owner data instead of clearing it. The
 * mount run — the first request this hook instance ever makes — fires on the next tick
 * (`setTimeout(…, 0)`, so StrictMode's discarded run never sends); every later run (an owner
 * change or a `refresh` bump) debounces 250 ms first, same as VaultBrowser's own list effect. A
 * read that fails with `overloaded` (the server's single overview slot is busy) is retried after
 * 1,000 ms, up to 4 attempts in all (a window over 3 s, longer than the server's own 3 s hold on
 * that single slot); the phase stays "loading" across retries. The returned
 * `api`/`workspaceId` double as the "owner" the state belongs to. A generation counter dropped on
 * cleanup discards any answer that arrives after a newer effect run (or unmount) has moved on.
 */
import { useEffect, useRef, useState } from "react";
import type { VaultOverview, VaultReadApi } from "./vaultApi";

export interface OverviewState {
  api: object;
  workspaceId: string;
  phase: "none" | "loading" | "ready" | "error";
  data: VaultOverview | null;
  error: string;
}

const DEBOUNCE_MS = 250;
const RETRY_DELAY_MS = 1000;
const MAX_ATTEMPTS = 4;

// In-memory only, keyed by the api object, for the window's life — never persisted. A
// new client (a fresh api object, or a reload) starts empty; so does a workspace never fetched.
const remembered = new WeakMap<object, Map<string, VaultOverview>>();

function recall(api: object, workspaceId: string): VaultOverview | null {
  return remembered.get(api)?.get(workspaceId) ?? null;
}

function remember(api: object, workspaceId: string, data: VaultOverview): void {
  let forApi = remembered.get(api);
  if (!forApi) {
    forApi = new Map();
    remembered.set(api, forApi);
  }
  forApi.set(workspaceId, data);
}

export function useVaultOverview(
  api: Pick<VaultReadApi, "overview">,
  workspaceId: string,
  refresh: number,
): OverviewState {
  const [state, setState] = useState<OverviewState>(() =>
    api.overview
      ? { api, workspaceId, phase: "loading", data: recall(api, workspaceId), error: "" }
      : { api, workspaceId, phase: "none", data: null, error: "" },
  );
  const generation = useRef(0);
  const startedOnce = useRef(false);

  useEffect(() => {
    void refresh;
    const method = api.overview;
    if (!method) {
      setState({ api, workspaceId, phase: "none", data: null, error: "" });
      return;
    }
    const mine = ++generation.current;
    const controller = new AbortController();
    const isMountRun = !startedOnce.current;
    setState((previous) => ({
      api,
      workspaceId,
      phase: "loading",
      data:
        previous.api === api && previous.workspaceId === workspaceId
          ? previous.data
          : recall(api, workspaceId),
      error: "",
    }));
    let timer: number;
    const attempt = (count: number) => {
      method(workspaceId, controller.signal).then(
        (data) => {
          if (mine !== generation.current) return;
          remember(api, workspaceId, data);
          setState({ api, workspaceId, phase: "ready", data, error: "" });
        },
        (error: unknown) => {
          if (controller.signal.aborted || mine !== generation.current) return;
          if (error instanceof Error && error.message === "overloaded" && count < MAX_ATTEMPTS) {
            timer = window.setTimeout(() => attempt(count + 1), RETRY_DELAY_MS);
            return;
          }
          setState((previous) => ({
            api,
            workspaceId,
            phase: "error",
            data:
              previous.api === api && previous.workspaceId === workspaceId ? previous.data : null,
            error: error instanceof Error ? error.message : "unavailable",
          }));
        },
      );
    };
    timer = window.setTimeout(
      () => {
        startedOnce.current = true;
        attempt(1);
      },
      isMountRun ? 0 : DEBOUNCE_MS,
    );
    return () => {
      window.clearTimeout(timer);
      controller.abort();
      if (generation.current === mine) ++generation.current;
    };
  }, [api, workspaceId, refresh]);

  return state;
}
