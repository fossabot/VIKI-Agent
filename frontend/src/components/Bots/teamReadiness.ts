/**
 * Optional Kanban workflow availability, read from the active vault's native skill catalog.
 * Team conversations do not require it. Skills in another source do not count.
 */
import { useEffect, useState } from "react";
import type { NativeAssetsApi, NativeCatalog } from "../../clients/profiles/nativeAssetsApi";

export type TeamReadiness = "loading" | "ready" | "missing" | "unknown" | "no_vault";

export const READINESS_TEXT: Record<Exclude<TeamReadiness, "ready">, string> = {
  loading: "Checking optional Kanban skills…",
  missing:
    "This vault has no Kanban skill. Team conversations can work without it. To add the Kanban workflow, see Help → Bots and messaging.",
  unknown:
    "Optional Kanban skills could not be checked. You can open team conversations; Refresh Bots checks the skills again.",
  no_vault: "Open a vault to start your team.",
};

export function readinessOf(catalog: NativeCatalog): "ready" | "missing" | "unknown" {
  if (catalog.assets.some((a) => a.kind === "skill" && a.relative_id === "skills/kanban/SKILL.md"))
    return "ready";
  return catalog.partial ? "unknown" : "missing";
}

interface Read {
  api: NativeAssetsApi | undefined;
  vaultId: string;
  check: number;
  result: "ready" | "missing" | "unknown";
}

export function useTeamReadiness(
  api: NativeAssetsApi | undefined,
  vaultId: string | null | undefined,
  check: number,
): TeamReadiness {
  const [read, setRead] = useState<Read | null>(null);
  useEffect(() => {
    if (typeof vaultId !== "string") return;
    const done = (result: Read["result"]): void => setRead({ api, vaultId, check, result });
    if (!api) {
      done("unknown");
      return;
    }
    const controller = new AbortController();
    api
      .catalog(`claude:workspace:${vaultId}`, controller.signal)
      .then(
        (catalog) => readinessOf(catalog),
        () => "unknown" as const,
      )
      .then((result) => {
        if (!controller.signal.aborted) done(result);
      });
    return () => controller.abort();
  }, [api, vaultId, check]);
  if (vaultId === undefined) return "loading";
  if (vaultId === null) return "no_vault";
  return read && read.api === api && read.vaultId === vaultId && read.check === check
    ? read.result
    : "loading";
}
