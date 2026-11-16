import { act, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { expect, test, vi } from "vitest";
import {
  type ModelConnectionsApi,
  ModelConnectionsContext,
  type ModelConnectionsListing,
} from "../../clients/app/modelConnectionsApi";
import type { ProviderModelView } from "../../clients/profiles/profileLaunchApi";
import { modelLabel, useConnectionLabels } from "./modelLabel";

const models: ProviderModelView = {
  provider: "claude",
  adapter_version: "1",
  native_version: "2.1.281",
  observed_at: null,
  catalog_key: null,
  status: "supported",
  models: [{ value: "claude-sonnet", label: "Claude Sonnet", description: "" }],
};
const ID = "11111111-1111-4111-8111-111111111111";
const REF = `viki-llm/${ID}/vendor/model-x`;
const rows = [{ model_ref: REF, name: "Work endpoint", model: "vendor/model-x" }];

test("uses observed labels and preserves unknown native IDs", () => {
  expect(modelLabel("claude-sonnet", models)).toBe("Claude Sonnet");
  expect(modelLabel("local-model", models)).toBe("local-model");
  expect(modelLabel("local-model")).toBe("local-model");
});

test("shows only model segment for a valid connection ref without a row", () => {
  expect(modelLabel(REF, models)).toBe("vendor/model-x");
});

test("shows the saved name and model for an exact row, never the ref UUID", () => {
  const label = modelLabel(REF, models, rows);
  expect(label).toBe("Work endpoint · vendor/model-x");
  expect(label).not.toContain(ID);
  // A near-miss ref does not borrow another row's name.
  expect(modelLabel(`viki-llm/${ID}/other`, models, rows)).toBe("other");
});

test.each(["viki-llm/not-a-uuid/model-x", `viki-llm/${ID}`, `viki-llm/${ID}/`])(
  "uses the generic label for malformed connection ref %s",
  (value) => {
    expect(modelLabel(value)).toBe("Connected model");
    expect(modelLabel(value, null, rows, "Linked model")).toBe("Linked model");
    expect(modelLabel(value)).not.toContain(ID);
  },
);

function listing(): ModelConnectionsListing {
  return {
    revision: 1,
    default_model: null,
    connections: [
      {
        id: ID,
        name: "Work endpoint",
        base_url: "https://example.invalid/v1",
        api_mode: "chat_completions",
        model: "vendor/model-x",
        model_ref: REF,
        has_api_key: true,
        is_default: false,
      },
    ],
  };
}
function harness(list: ModelConnectionsApi["list"]) {
  const api = { list } as ModelConnectionsApi;
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(ModelConnectionsContext.Provider, { value: api }, children);
  return { wrapper };
}

test("lists only while enabled and keeps just ref, name and model", async () => {
  const list = vi.fn(async () => listing());
  const { wrapper } = harness(list);
  const { result, rerender } = renderHook(({ on }) => useConnectionLabels(on), {
    wrapper,
    initialProps: { on: false },
  });
  expect(list).not.toHaveBeenCalled();
  expect(result.current).toEqual({ status: "loading", labels: [] });
  rerender({ on: true });
  await waitFor(() => expect(result.current.status).toBe("ready"));
  expect(result.current.labels).toEqual(rows);
  expect(JSON.stringify(result.current)).not.toContain("example.invalid");
});

test("a failed list is failed, not an empty ready", async () => {
  const { wrapper } = harness(async () => {
    throw new Error("down");
  });
  const { result } = renderHook(() => useConnectionLabels(true), { wrapper });
  await waitFor(() => expect(result.current.status).toBe("failed"));
  expect(result.current.labels).toEqual([]);
});

test("a missing provider reads as failed", async () => {
  const { result } = renderHook(() => useConnectionLabels(true));
  await waitFor(() => expect(result.current.status).toBe("failed"));
});

test("aborts the stale call and ignores its late answer on refresh", async () => {
  const signals: AbortSignal[] = [];
  const resolvers: ((value: ModelConnectionsListing) => void)[] = [];
  const { wrapper } = harness(
    (signal) =>
      new Promise((resolve) => {
        if (signal) signals.push(signal);
        resolvers.push(resolve);
      }),
  );
  const { result, rerender } = renderHook(({ key }) => useConnectionLabels(true, key), {
    wrapper,
    initialProps: { key: 1 },
  });
  rerender({ key: 2 });
  await waitFor(() => expect(resolvers).toHaveLength(2));
  expect(signals[0]?.aborted).toBe(true);
  expect(signals[1]?.aborted).toBe(false);
  resolvers[0]?.(listing());
  await Promise.resolve();
  expect(result.current.status).toBe("loading");
  resolvers[1]?.({ ...listing(), connections: [] });
  await waitFor(() => expect(result.current.status).toBe("ready"));
  expect(result.current.labels).toEqual([]);
});

test("a stale answer that arrives after the newer one cannot replace it", async () => {
  const resolvers: ((value: ModelConnectionsListing) => void)[] = [];
  const { wrapper } = harness(
    () =>
      new Promise((resolve) => {
        resolvers.push(resolve);
      }),
  );
  const { result, rerender } = renderHook(({ key }) => useConnectionLabels(true, key), {
    wrapper,
    initialProps: { key: 1 },
  });
  rerender({ key: 2 });
  await waitFor(() => expect(resolvers).toHaveLength(2));
  await act(async () => resolvers[1]?.({ ...listing(), connections: [] }));
  expect(result.current).toEqual({ status: "ready", labels: [] });
  await act(async () => resolvers[0]?.(listing()));
  expect(result.current).toEqual({ status: "ready", labels: [] });
});
