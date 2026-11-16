/**
 * Loads one workspace's capture listing, polls VIKI's suggestion run while it is active, and
 * starts a new run on request.
 *
 * The listing and the poll share one rule: whenever the known `run.state` is "running", a poll
 * is due `pollMs` later; once a poll (or the list itself) shows it is no longer running, nothing
 * schedules another one. Two effects carry this out and nothing more:
 *  - the list effect (keyed on `workspaceId`, plus a reload generation) fetches the listing,
 *    aborting whatever list call came before it — mount, an id change and reload() all land here;
 *  - the poll effect is keyed on `listing` itself: a single setTimeout re-reads the run, and the
 *    state update it makes (or doesn't) decides whether the next effect run schedules another
 *    one — the same self-scheduling shape runStats.ts uses for its own per-row polling.
 * A poll that finds the run no longer running reads the list once more, then schedules nothing
 * further: the condition the poll effect re-checks on its next run is no longer satisfied.
 *
 * `suggest()` only has to post and merge the run it gets back; merging changes `listing`, which
 * is all the poll effect needs to pick the new run up on its own.
 *
 * A vault switch resets every per-workspace field during render — React's own pattern for
 * resetting state on a prop change — so neither effect ever mixes one workspace's data with
 * another's id. `suggest`'s own POST has no abort signal to cancel (src/clients/vaults/capturesApi.ts), so its
 * continuation checks a mounted flag and the workspace id it was issued for, dropping the answer
 * rather than applying it once either has moved on.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  type CaptureListing,
  type CapturesApi,
  CapturesApiError,
} from "../../clients/vaults/capturesApi";

export const DEFAULT_POLL_MS = 2000;

export interface UseCaptureListingOptions {
  pollMs?: number;
}

export interface CaptureListingResult {
  listing: CaptureListing | null;
  loading: boolean;
  error: string | null;
  starting: boolean;
  startError: string | null;
  reload: () => void;
  suggest: () => void;
}

function errorCode(error: unknown): string {
  return error instanceof CapturesApiError ? error.code : "unavailable";
}

export function useCaptureListing(
  api: CapturesApi,
  workspaceId: string,
  { pollMs = DEFAULT_POLL_MS }: UseCaptureListingOptions = {},
): CaptureListingResult {
  const [listing, setListing] = useState<CaptureListing | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [reloadGen, setReloadGen] = useState(0);

  // A vault switch resets every per-workspace field during render, so neither effect below ever
  // runs a render with one workspace's data sitting next to another's id (see the module doc).
  const [lastId, setLastId] = useState(workspaceId);
  if (workspaceId !== lastId) {
    setLastId(workspaceId);
    setListing(null);
    setLoading(true);
    setError(null);
    setStarting(false);
    setStartError(null);
  }

  const workspaceIdRef = useRef(workspaceId);
  workspaceIdRef.current = workspaceId;

  // Unmount only: suggest()'s own POST has no abort signal (src/clients/vaults/capturesApi.ts), so its continuation
  // checks this instead.
  const mountedRef = useRef(true);
  useEffect(() => {
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // The listing: mount, an id change and reload() all land here, each aborting whatever list
  // call came before it.
  // biome-ignore lint/correctness/useExhaustiveDependencies: reloadGen is a trigger only — reload() bumps it to force a fresh list call; the effect body never reads its value.
  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    api.list(workspaceId, controller.signal).then(
      (result) => {
        if (cancelled) return;
        setListing(result);
        setLoading(false);
      },
      (error: unknown) => {
        if (cancelled) return;
        setLoading(false);
        setError(errorCode(error));
      },
    );
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [api, workspaceId, reloadGen]);

  // The poll: only while the listing's own run is running, one request at a time, pollMs apart.
  // Keyed on `listing` itself, so merging a fresh run into it — here or from suggest() below — is
  // all it takes to pick the loop back up (see the module doc).
  useEffect(() => {
    if (listing?.run?.state !== "running") return;
    let cancelled = false;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      api.run(workspaceId, controller.signal).then(
        (run) => {
          if (cancelled) return;
          if (run?.state !== "running") {
            api.list(workspaceId, controller.signal).then(
              (result) => {
                if (cancelled) return;
                setListing(result);
              },
              (error: unknown) => {
                if (cancelled) return;
                setError(errorCode(error));
              },
            );
            return;
          }
          setListing((prev) => (prev ? { ...prev, run } : prev));
        },
        (error: unknown) => {
          if (cancelled) return;
          setError(errorCode(error));
        },
      );
    }, pollMs);
    return () => {
      cancelled = true;
      controller.abort();
      clearTimeout(timer);
    };
  }, [api, workspaceId, listing, pollMs]);

  const reload = useCallback(() => {
    setError(null);
    setReloadGen((generation) => generation + 1);
  }, []);

  const suggest = useCallback(() => {
    // The id this call was issued for, compared against workspaceIdRef below: a POST already
    // sent cannot be aborted by a later vault switch (src/clients/vaults/capturesApi.ts has no signal for suggest).
    const forId = workspaceId;
    setStarting(true);
    setStartError(null);
    api.suggest(workspaceId).then(
      (run) => {
        if (!mountedRef.current || workspaceIdRef.current !== forId) return;
        setStarting(false);
        setListing((prev) => (prev ? { ...prev, run } : prev));
      },
      (error: unknown) => {
        if (!mountedRef.current || workspaceIdRef.current !== forId) return;
        setStarting(false);
        if (error instanceof CapturesApiError && error.code === "suggestion_running") {
          reload();
        } else {
          setStartError(errorCode(error));
        }
      },
    );
  }, [api, workspaceId, reload]);

  return { listing, loading, error, starting, startError, reload, suggest };
}
