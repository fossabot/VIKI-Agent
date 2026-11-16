import { useContext, useEffect, useState } from "react";
import { ModelConnectionsContext } from "../../clients/app/modelConnectionsApi";
import type { ProviderModelView } from "../../clients/profiles/profileLaunchApi";

const CONNECTION_MODEL =
  /^viki-llm\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/([^/].*)$/;

export interface ConnectionLabel {
  model_ref: string;
  name: string;
  model: string;
}
export interface ConnectionLabels {
  status: "loading" | "ready" | "failed";
  labels: readonly ConnectionLabel[];
}

// No catalog lookup here: a localized caller passes its own `connected` text.
export function modelLabel(
  value: string,
  models?: ProviderModelView | null,
  connections: readonly ConnectionLabel[] = [],
  connected = "Connected model",
): string {
  if (value.startsWith("viki-llm/")) {
    const row = connections.find((item) => item.model_ref === value);
    if (row) return `${row.name} · ${row.model}`;
    return CONNECTION_MODEL.exec(value)?.[1] ?? connected;
  }
  return models?.models.find((model) => model.value === value)?.label ?? value;
}

const LOADING: ConnectionLabels = { status: "loading", labels: [] };

/** Lists connections only while `enabled`; a failed list is `failed`, never an empty ready. */
export function useConnectionLabels(enabled: boolean, refreshKey?: unknown): ConnectionLabels {
  const api = useContext(ModelConnectionsContext);
  const [state, setState] = useState<ConnectionLabels>(LOADING);
  // biome-ignore lint/correctness/useExhaustiveDependencies: refreshKey only re-triggers the list.
  useEffect(() => {
    setState(LOADING);
    if (!enabled) return;
    if (!api) {
      setState({ status: "failed", labels: [] });
      return;
    }
    const controller = new AbortController();
    api.list(controller.signal).then(
      (listing) => {
        if (controller.signal.aborted) return;
        setState({
          status: "ready",
          labels: listing.connections.map(({ model_ref, name, model }) => ({
            model_ref,
            name,
            model,
          })),
        });
      },
      () => {
        if (!controller.signal.aborted) setState({ status: "failed", labels: [] });
      },
    );
    return () => controller.abort();
  }, [api, enabled, refreshKey]);
  return enabled ? state : LOADING;
}
