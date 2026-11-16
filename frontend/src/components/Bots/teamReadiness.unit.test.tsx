import { act, renderHook, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { NativeAssetsApi, NativeCatalog } from "../../clients/profiles/nativeAssetsApi";
import { readinessOf, useTeamReadiness } from "./teamReadiness";

function catalog(assets: [string, string][], partial = false): NativeCatalog {
  return {
    assets: assets.map(([kind, relative_id]) => ({ kind, relative_id, name: "kanban" })),
    partial,
  } as unknown as NativeCatalog;
}
const KANBAN: [string, string] = ["skill", "skills/kanban/SKILL.md"];

function client(result: Promise<NativeCatalog>) {
  const catalogFn = vi.fn((_id: string, _signal?: AbortSignal) => result);
  return { api: { sources: vi.fn(), catalog: catalogFn } as unknown as NativeAssetsApi, catalogFn };
}

test("readinessOf", () => {
  expect(readinessOf(catalog([KANBAN]))).toBe("ready");
  expect(readinessOf(catalog([["skill", "skills/other/kanban/SKILL.md"]]))).toBe("missing");
  expect(readinessOf(catalog([["agent", "skills/kanban/SKILL.md"]]))).toBe("missing");
  expect(readinessOf(catalog([]))).toBe("missing");
  expect(readinessOf(catalog([], true))).toBe("unknown");
  expect(readinessOf(catalog([KANBAN], true))).toBe("ready");
});

test("reads the vault's workspace source and becomes ready", async () => {
  const { api, catalogFn } = client(Promise.resolve(catalog([KANBAN])));
  const { result } = renderHook(() => useTeamReadiness(api, "v1", 0));
  expect(result.current).toBe("loading");
  await waitFor(() => expect(result.current).toBe("ready"));
  expect(catalogFn.mock.calls[0]?.[0]).toBe("claude:workspace:v1");
});

test("rejection is unknown", async () => {
  const { api } = client(Promise.reject(new Error("x")));
  const { result } = renderHook(() => useTeamReadiness(api, "v1", 0));
  await waitFor(() => expect(result.current).toBe("unknown"));
});

test("a previous vault's Kanban skill does not make the active vault ready", async () => {
  let resolvePrevious!: (value: NativeCatalog) => void;
  const { api, catalogFn } = client(
    new Promise((resolve) => {
      resolvePrevious = resolve;
    }),
  );
  const { result, rerender } = renderHook(({ id }) => useTeamReadiness(api, id, 0), {
    initialProps: { id: "previous" },
  });
  const previousSignal = catalogFn.mock.calls[0]?.[1];
  catalogFn.mockResolvedValueOnce(catalog([]));
  rerender({ id: "active" });
  expect(previousSignal?.aborted).toBe(true);
  await waitFor(() => expect(result.current).toBe("missing"));
  await act(async () => resolvePrevious(catalog([KANBAN])));
  expect(result.current).toBe("missing");
  expect(catalogFn.mock.calls.map(([id]) => id)).toEqual([
    "claude:workspace:previous",
    "claude:workspace:active",
  ]);
});

test("no client is unknown", async () => {
  const { result } = renderHook(() => useTeamReadiness(undefined, "v1", 0));
  await waitFor(() => expect(result.current).toBe("unknown"));
});

test("undefined vault is loading and null is no_vault, with no call", () => {
  const { api, catalogFn } = client(Promise.resolve(catalog([KANBAN])));
  expect(renderHook(() => useTeamReadiness(api, undefined, 0)).result.current).toBe("loading");
  expect(renderHook(() => useTeamReadiness(api, null, 0)).result.current).toBe("no_vault");
  expect(catalogFn).not.toHaveBeenCalled();
});

test("a check bump reads again and reads loading meanwhile", async () => {
  const { api, catalogFn } = client(Promise.resolve(catalog([])));
  const { result, rerender } = renderHook(({ n }) => useTeamReadiness(api, "v1", n), {
    initialProps: { n: 0 },
  });
  await waitFor(() => expect(result.current).toBe("missing"));
  catalogFn.mockReturnValueOnce(new Promise(() => {}));
  rerender({ n: 1 });
  expect(result.current).toBe("loading");
  expect(catalogFn).toHaveBeenCalledTimes(2);
});

test("unmount aborts the read", () => {
  const { api, catalogFn } = client(new Promise(() => {}));
  const { unmount } = renderHook(() => useTeamReadiness(api, "v1", 0));
  const signal = catalogFn.mock.calls[0]?.[1];
  expect(signal?.aborted).toBe(false);
  unmount();
  expect(signal?.aborted).toBe(true);
});
