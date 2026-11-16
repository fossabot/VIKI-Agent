/**
 * Per-row run stats for the right rail's agent runs list (AgentRuns.tsx is its consumer). A
 * run's elapsed time and token count live in its own transcript
 * (src/clients/conversations/subagentsApi.ts), a whole-answer read, so this hook keeps at most one such read in flight and
 * never refreshes one row more often than ROW_REFRESH_MS.
 *
 * Scheduling: on every change of `api`, the runs' keys, `nowMs` or the stats collected so far,
 * and only while no read is in flight, the first run (in `runs`' own order) that has no entry
 * yet, or whose entry is at least ROW_REFRESH_MS old, is read. A run dropped from `runs` keeps
 * whatever entry it already has; it is simply never read again. Settling (success or failure)
 * clears the in-flight flag and stores an entry stamped with the latest `nowMs`, which is what
 * lets the next stale run be picked up right away rather than waiting for a fresh tick.
 */
import { useEffect, useRef, useState } from "react";
import type { SubagentsApi } from "../../clients/conversations/subagentsApi";

export const ROW_REFRESH_MS = 15_000;

export interface RunStats {
  startedAt: number | null;
  tokens: number | null;
  at: number;
}

export function useRunStats(
  api: SubagentsApi | undefined,
  runs: readonly { key: string; sessionId: string; blockId: string }[],
  nowMs: number,
): ReadonlyMap<string, RunStats> {
  const [stats, setStats] = useState<ReadonlyMap<string, RunStats>>(new Map());
  const inFlightRef = useRef(false);
  const nowMsRef = useRef(nowMs);
  nowMsRef.current = nowMs;
  const controllerRef = useRef<AbortController | null>(null);

  // Unmount only. The scheduling effect below never returns a cleanup of its own: a clock tick
  // or a settled read retriggering it must not cancel a read that is still in flight.
  useEffect(() => {
    return () => controllerRef.current?.abort();
  }, []);

  const keys = runs.map((run) => run.key).join(",");

  // biome-ignore lint/correctness/useExhaustiveDependencies: keys stands in for runs' own contents (same keys ⇒ same rows), so an equal-keys array does not retrigger this; runs is read below but deliberately left off this list.
  useEffect(() => {
    if (!api) return;
    if (inFlightRef.current) return;
    const target = runs.find((run) => {
      const entry = stats.get(run.key);
      return !entry || nowMs - entry.at >= ROW_REFRESH_MS;
    });
    if (!target) return;
    const controller = new AbortController();
    controllerRef.current = controller;
    inFlightRef.current = true;
    // A whole transcript answer is read to learn two numbers (startedAt, tokens); switch to a
    // counts-only route if a profile ever shows this costing more than the rail can spare.
    api.read(target.sessionId, target.blockId, controller.signal).then(
      (result) => {
        inFlightRef.current = false;
        if (controller.signal.aborted) return;
        setStats((prev) => {
          const next = new Map(prev);
          next.set(target.key, {
            startedAt: result.startedAt,
            tokens: result.tokens,
            at: nowMsRef.current,
          });
          return next;
        });
      },
      () => {
        inFlightRef.current = false;
        if (controller.signal.aborted) return;
        setStats((prev) => {
          const earlier = prev.get(target.key);
          const next = new Map(prev);
          next.set(target.key, {
            startedAt: earlier?.startedAt ?? null,
            tokens: earlier?.tokens ?? null,
            at: nowMsRef.current,
          });
          return next;
        });
      },
    );
  }, [api, keys, nowMs, stats]);

  return stats;
}
