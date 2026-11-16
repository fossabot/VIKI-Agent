/**
 * The model/effort chip's data before a chat's first `session.started`: a chat's saved or
 * default prefs, the provider's last model list, and a new-chat pick held locally until `commit`
 * writes it.
 *
 * - The prefs read runs while `api && enabled && chatKey !== null`, and again on every change of
 *   `api`, `chatKey`, `enabled` or `refreshKey`, and on `refresh()`. Each run aborts the one
 *   before it; an aborted read's answer is dropped (guarded by its own `AbortController`, so a
 *   key change or a redundant `refresh()` can never let a stale answer land). The slot for the
 *   current key is left untouched while a re-read is in flight, so the last good read keeps
 *   showing; once a (re-)read settles, success or failure, it replaces that slot outright — a
 *   failure clears the chip (null), it does not keep the previous good value.
 * - The model list is fetched once per `chatKey` (not on every `refreshKey`/`refresh()`, to
 *   save the request); a failure, an empty list or no `models` function at all all settle the
 *   list as "none", which is what turns on the `NO_CATALOG_NOTE` fallback row below.
 * - `hold`/`commit` track at most one in-progress pick, independent of the read: `hold` only
 *   moves the chip's own label (no request), `commit` is the one call that writes it.
 */
import { useCallback, useContext, useEffect, useRef, useState } from "react";
import { ModelConnectionsContext } from "../../clients/app/modelConnectionsApi";
import type { ChatPrefs, SessionPrefsApi } from "../../clients/conversations/sessionPrefsApi";
import type { ModelOption } from "../../protocol";

export const NO_CATALOG_NOTE = "More models appear after a chat starts";

export interface PrestartChip {
  chip: { models: readonly ModelOption[]; model: string; effort: string } | null;
  hold(kind: "model" | "effort", value: string): void;
  holds(key: string): boolean;
  commit(key: string): Promise<boolean>;
  refresh(): void;
  preview(key: string): { model: string | null } | null;
}

export interface UsePrestartChipOptions {
  api: SessionPrefsApi | undefined;
  models: ((signal: AbortSignal) => Promise<readonly ModelOption[]>) | undefined;
  chatKey: string | null;
  enabled: boolean;
  refreshKey: unknown;
}

interface ReadSlot {
  key: string;
  /** null means the (re-)read that last settled for this key failed. */
  prefs: ChatPrefs | null;
}

interface ListSlot {
  key: string;
  models: readonly ModelOption[];
}

interface Held {
  key: string;
  model?: string;
  effort?: string;
}

export function usePrestartChip({
  api,
  models,
  chatKey,
  enabled,
  refreshKey,
}: UsePrestartChipOptions): PrestartChip {
  const connections = useContext(ModelConnectionsContext);
  const [readSlot, setReadSlot] = useState<ReadSlot | null>(null);
  const [listSlot, setListSlot] = useState<ListSlot | null>(null);
  const [nonce, setNonce] = useState(0);
  const [, bumpHeld] = useState(0);
  const heldRef = useRef<Held | null>(null);
  const listSlotRef = useRef(listSlot);
  listSlotRef.current = listSlot;

  // biome-ignore lint/correctness/useExhaustiveDependencies: refreshKey and nonce (bumped by refresh()) are intentional re-read triggers, not values this effect's body reads.
  useEffect(() => {
    if (!api || !enabled || chatKey === null) return;
    const key = chatKey;
    const controller = new AbortController();
    api.get(key, controller.signal).then(
      (prefs) => {
        if (controller.signal.aborted) return;
        setReadSlot({ key, prefs });
      },
      () => {
        if (controller.signal.aborted) return;
        setReadSlot({ key, prefs: null });
      },
    );
    return () => controller.abort();
  }, [api, chatKey, enabled, refreshKey, nonce]);

  useEffect(() => {
    if (!enabled || chatKey === null) return;
    const key = chatKey;
    if (listSlotRef.current?.key === key) return; // already fetched once for this chat
    if (!models && !connections) {
      setListSlot({ key, models: [] });
      return;
    }
    const controller = new AbortController();
    Promise.all([
      models ? models(controller.signal).catch(() => []) : Promise.resolve([]),
      connections
        ? connections
            .list(controller.signal)
            .then((view) =>
              view.connections.map((row) => ({
                value: row.model_ref,
                label: `${row.name} · ${row.model}`,
                description: row.base_url,
              })),
            )
            .catch(() => [])
        : Promise.resolve([]),
    ]).then(
      (lists) => {
        if (controller.signal.aborted) return;
        const list = lists.flat();
        setListSlot({ key, models: list.length > 0 ? list : [] });
      },
      () => {
        if (controller.signal.aborted) return;
        setListSlot({ key, models: [] });
      },
    );
    return () => controller.abort();
  }, [models, connections, chatKey, enabled]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: api isn't read in this body; listing it only ties refresh's identity to api, the way commit's and holds' dependency lists do below.
  const refresh = useCallback(() => {
    setNonce((n) => n + 1);
  }, [api]);

  const commit = useCallback(
    async (key: string): Promise<boolean> => {
      const held = heldRef.current;
      if (!held || held.key !== key || (held.model === undefined && held.effort === undefined)) {
        return true;
      }
      if (!api) {
        heldRef.current = null;
        return true;
      }
      const changes: { model?: string; effort?: string } = {};
      if (held.model !== undefined) changes.model = held.model;
      if (held.effort !== undefined) changes.effort = held.effort;
      try {
        const prefs = await api.save(key, changes);
        heldRef.current = null;
        setReadSlot({ key, prefs });
        return true;
      } catch {
        heldRef.current = null;
        refresh();
        return false;
      }
    },
    [api, refresh],
  );

  // biome-ignore lint/correctness/useExhaustiveDependencies: api isn't read here either; same identity reason as refresh above.
  const holds = useCallback((key: string): boolean => heldRef.current?.key === key, [api]);

  function hold(kind: "model" | "effort", value: string): void {
    if (chatKey === null) return;
    if (!readSlot || readSlot.key !== chatKey || readSlot.prefs === null) return; // no landed read
    const read = readSlot.prefs;
    const prior = heldRef.current?.key === chatKey ? heldRef.current : null;
    const priorModel = prior?.model;
    const priorEffort = prior?.effort;
    if (kind === "model" && priorModel === value) return;
    const next: Held = { key: chatKey };
    if (kind === "model") {
      if (priorEffort !== undefined) next.effort = priorEffort;
      if (
        value !== (read.model ?? "default") ||
        priorModel === undefined ||
        read.model === null ||
        read.model.startsWith("viki-llm/")
      )
        next.model = value;
    } else {
      if (priorModel !== undefined) next.model = priorModel;
      if (value !== read.effort) next.effort = value;
    }
    heldRef.current = next.model !== undefined || next.effort !== undefined ? next : null;
    bumpHeld((n) => n + 1); // heldRef is a plain ref: force the re-render that reads it
  }

  function currentChip(): PrestartChip["chip"] {
    if (!enabled || chatKey === null) return null;
    if (!readSlot || readSlot.key !== chatKey || readSlot.prefs === null) return null;
    if (!listSlot || listSlot.key !== chatKey) return null;
    const held = heldRef.current?.key === chatKey ? heldRef.current : null;
    const read = readSlot.prefs;
    const model = held?.model ?? read.model ?? "default";
    const effort = held?.effort ?? read.effort;
    const chipModels =
      listSlot.models.length > 0
        ? listSlot.models
        : [{ value: model, label: model, description: NO_CATALOG_NOTE }];
    return { models: chipModels, model, effort };
  }

  function preview(key: string): ReturnType<PrestartChip["preview"]> {
    if (
      !enabled ||
      chatKey !== key ||
      readSlot?.key !== key ||
      !readSlot.prefs ||
      listSlot?.key !== key
    )
      return null;
    const held = heldRef.current?.key === key ? heldRef.current : null;
    const model = held?.model ?? readSlot.prefs.model;
    return { model };
  }

  return { chip: currentChip(), hold, holds, commit, refresh, preview };
}
