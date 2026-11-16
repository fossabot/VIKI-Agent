/**
 * Per-chat open tool-permission requests for the right rail's run groups (runGroups.ts's optional
 * `requests` argument; AgentRuns.tsx is the eventual caller). A card only carries its own
 * `tool_use_id`; this hook is the one place that asks the server (src/clients/conversations/runsApi.ts's `requests`) which
 * run each open card belongs to and when it was raised.
 *
 * Scheduling: a chat (keyed by session id) with at least one open card is identified by the
 * ordered list of its cards' `tool_use_id`s — its "signature". A first or changed signature
 * aborts that chat's earlier read, if any, and starts a new one; an unchanged signature leaves
 * whatever read is already running or already settled alone. A chat with no open cards —
 * including one whose session has gone — loses its entry and has its read aborted. Every chat
 * reads independently and concurrently: there is no single in-flight slot to share (contrast
 * runStats.ts, which reads at most one row at a time).
 *
 * A settled read only acts while its own controller is still unaborted — a newer read for the
 * same chat aborts the old one first, so a stale success or failure never lands. A live success
 * replaces that chat's entries with the response, dropping any item whose `tool_use_id` is no
 * longer one of the chat's current cards. A live failure leaves the chat's existing entries
 * untouched; there is no retry timer, so the same signature is only read again once it changes.
 */
import { useEffect, useRef, useState } from "react";
import type { RunsApi } from "../../clients/conversations/runsApi";
import type { RequestInfo, RunRequests } from "../../state/runGroups";
import type { SessionView } from "../../state/view";

interface ChatRead {
  signature: string;
  controller: AbortController;
}

export function useRunRequests(
  api: RunsApi | undefined,
  sessions: Readonly<Record<string, SessionView>>,
): RunRequests {
  const readsRef = useRef<Map<string, ChatRead>>(new Map());
  const [entries, setEntries] = useState<RunRequests>(new Map());

  // Fresh on mount, and again on StrictMode's development-only remount: a discarded mount's
  // aborted reads are never mistaken for live ones by the effect below.
  useEffect(() => {
    const reads = new Map<string, ChatRead>();
    readsRef.current = reads;
    return () => {
      for (const read of reads.values()) read.controller.abort();
    };
  }, []);

  useEffect(() => {
    if (!api) return;
    const reads = readsRef.current;

    const cardIds = new Map<string, string[]>();
    for (const [sessionId, view] of Object.entries(sessions)) {
      if (view.cards.length > 0) {
        cardIds.set(
          sessionId,
          view.cards.map((card) => card.data.tool_use_id),
        );
      }
    }

    // A chat that lost its cards, or whose session is gone, loses its read and its entry.
    for (const [sessionId, read] of reads) {
      if (cardIds.has(sessionId)) continue;
      read.controller.abort();
      reads.delete(sessionId);
    }
    setEntries((prev) => {
      if (prev.size === 0) return prev;
      let next: Map<string, ReadonlyMap<string, RequestInfo>> | undefined;
      for (const sessionId of prev.keys()) {
        if (!cardIds.has(sessionId)) {
          next ??= new Map(prev);
          next.delete(sessionId);
        }
      }
      return next ?? prev;
    });

    for (const [sessionId, ids] of cardIds) {
      const signature = ids.join(",");
      const existing = reads.get(sessionId);
      if (existing?.signature === signature) continue;
      existing?.controller.abort();
      const controller = new AbortController();
      reads.set(sessionId, { signature, controller });
      const current = new Set(ids);

      api.requests(sessionId, controller.signal).then(
        (result) => {
          if (controller.signal.aborted) return;
          setEntries((prev) => {
            const filtered = new Map<string, RequestInfo>();
            for (const item of result) {
              if (current.has(item.toolUseId)) {
                filtered.set(item.toolUseId, { run: item.run, raisedAt: item.raisedAt });
              }
            }
            const next = new Map(prev);
            next.set(sessionId, filtered);
            return next;
          });
        },
        () => {
          // A genuine failure, or our own abort: either way the chat's previous entries stand.
        },
      );
    }
  }, [api, sessions]);

  return entries;
}
