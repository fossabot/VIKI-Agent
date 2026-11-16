/**
 * Background read of the proposals needing attention for the right rail (feeds `needsYou`'s
 * `proposals` input and the overview's Inbox summary). At most one read is in flight per active
 * scope (`api` + `workspaceId`); starting a new read always aborts whichever one preceded it.
 *
 * Inactive (`enabled` false, or `api`/`workspaceId` missing) means no read: everything is null,
 * status "off", and any in-flight read is aborted. Active, a read starts on activation, on an
 * `api` or `workspaceId` change (which drops the held `items`/`readAt` first), on a `trigger`
 * change, and on `refresh()` (stable identity). Only a `trigger` change or `refresh()` keep the
 * held items visible while the new read is in flight. A read whose controller has since been
 * aborted never commits, so a late or superseded response is silently dropped: this is also what
 * makes a React StrictMode double-invoke commit only once, from its second, non-aborted read. A
 * failure that was not an abort sets status "failed" and keeps whatever items were already held.
 * There are no timers.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { DecisionsApi } from "../../state/decisionsApi";
import type { ProposalNeed } from "../../state/needsYou";

export interface ProposalAttention {
  items: readonly ProposalNeed[] | null;
  readAt: string | null;
  workspaceId: string | null;
  status: "off" | "reading" | "ready" | "failed";
  refresh(): void;
}

type ReadState = Omit<ProposalAttention, "refresh">;

const OFF_STATE: ReadState = { items: null, readAt: null, workspaceId: null, status: "off" };

export function useProposalAttention(i: {
  api: DecisionsApi | undefined;
  workspaceId: string | null;
  enabled: boolean;
  trigger: string;
}): ProposalAttention {
  const { api, workspaceId, enabled, trigger } = i;
  const [state, setState] = useState<ReadState>(OFF_STATE);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const scopeRef = useRef<{ api: DecisionsApi; workspaceId: string } | null>(null);

  // biome-ignore lint/correctness/useExhaustiveDependencies: refreshNonce is a trigger-only signal for refresh(); it is never read in the body.
  useEffect(() => {
    if (!enabled || !api || !workspaceId) {
      scopeRef.current = null;
      setState(OFF_STATE);
      return;
    }
    const sameScope =
      scopeRef.current?.api === api && scopeRef.current?.workspaceId === workspaceId;
    scopeRef.current = { api, workspaceId };
    setState((prev) =>
      sameScope
        ? { ...prev, workspaceId, status: "reading" }
        : { items: null, readAt: null, workspaceId, status: "reading" },
    );
    const controller = new AbortController();
    api.decisions(controller.signal).then(
      (inbox) => {
        if (controller.signal.aborted) return;
        setState({
          items: inbox.items,
          readAt: new Date(Date.now()).toISOString(),
          workspaceId,
          status: "ready",
        });
      },
      () => {
        if (controller.signal.aborted) return;
        setState((prev) => ({ ...prev, status: "failed" }));
      },
    );
    return () => controller.abort();
  }, [enabled, api, workspaceId, trigger, refreshNonce]);

  const refresh = useCallback(() => setRefreshNonce((n) => n + 1), []);

  return { ...state, refresh };
}
