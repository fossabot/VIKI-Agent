import { expect, test, vi } from "vitest";
import { createMagpieApi, MagpieError, type MagpieView } from "./magpieApi";

const state = {
  available: true,
  running: false,
  version: "0.1.3357",
  desktop: true,
  base_url: "http://127.0.0.1:3426/v1",
};

const view: MagpieView = {
  id: "b".repeat(32),
  bounds: { x: 272, y: 49, width: 900, height: 700 },
  appearance: {
    theme: "dark",
    density: "compact",
    scale: 1.15,
    reduce_motion: true,
    locale: "en",
    palette: {
      bg: "#0b0b0b",
      panel: "#111111",
      raised: "#161616",
      text: "#ededed",
      secondary: "#d4d4d4",
      muted: "#b3b3b3",
      faint: "#a3a3a3",
      line: "#262626",
      hair: "#1c1c1c",
      accent: "#f8f8ff",
      onAccent: "#000000",
      popover: "#141414",
    },
  },
  visible: true,
};

test("Magpie status and explicit open use bearer authentication and keep credentials out of URLs", async () => {
  const fetcher = vi.fn<typeof fetch>(async () => Response.json(state));
  const api = createMagpieApi("synthetic-token", fetcher);
  expect(await api.status()).toEqual(state);
  expect(fetcher.mock.calls[0]?.[0]).toBe("/api/v2/llm/magpie");
  expect(fetcher.mock.calls[0]?.[1]?.method).toBe("GET");
  expect(await api.open("a".repeat(32))).toEqual(state);
  const [path, init] = fetcher.mock.calls[1] ?? [];
  expect(path).toBe("/api/v2/llm/magpie/open");
  expect(init?.headers).toEqual({
    authorization: "Bearer synthetic-token",
    "content-type": "application/json",
  });
  expect(JSON.parse(init?.body as string)).toEqual({ ui: "a".repeat(32) });
  expect(fetcher).toHaveBeenCalledTimes(2);
});

test.each([
  { ...state, base_url: "http://192.0.2.1/v1" },
  { ...state, dashboard_key: "synthetic-key" },
  { ...state, available: "yes" },
  { ...state, version: "<script>" },
])("Magpie status rejects unexpected endpoint or privileged response fields", async (value) => {
  const api = createMagpieApi("synthetic-token", async () => Response.json(value));
  await expect(api.status()).rejects.toBeInstanceOf(MagpieError);
});

test("an open failure returns only a bounded error code", async () => {
  const api = createMagpieApi("synthetic-token", async () =>
    Response.json(
      { error: { code: "start_failed", message: "synthetic secret" } },
      { status: 503 },
    ),
  );
  await expect(api.open(null)).rejects.toMatchObject({
    code: "start_failed",
    message: "Magpie request failed.",
  });
});

test("embedded opens, view updates and stale closes preserve their own generation", async () => {
  const fetcher = vi.fn<typeof fetch>(async () => Response.json(state));
  const api = createMagpieApi("synthetic-token", fetcher);
  const controller = new AbortController();
  const ui = "a".repeat(32);
  const next = { ...view, id: "c".repeat(32) };
  await api.open(ui, controller.signal, view);
  await api.view(ui, { ...view, visible: false }, controller.signal);
  await api.open(ui, undefined, next);
  controller.abort();
  await api.close(ui, view.id);
  expect(fetcher.mock.calls.map(([path]) => path)).toEqual([
    "/api/v2/llm/magpie/open",
    "/api/v2/llm/magpie/view",
    "/api/v2/llm/magpie/open",
    "/api/v2/llm/magpie/close",
  ]);
  expect(JSON.parse(fetcher.mock.calls[0]?.[1]?.body as string)).toEqual({ ui, view });
  expect(JSON.parse(fetcher.mock.calls[1]?.[1]?.body as string)).toEqual({
    ui,
    view: { ...view, visible: false },
  });
  expect(JSON.parse(fetcher.mock.calls[3]?.[1]?.body as string)).toEqual({
    ui,
    view_id: view.id,
  });
  expect(fetcher.mock.calls[3]?.[1]?.signal).toBeNull();
  for (const [, init] of fetcher.mock.calls)
    expect(init?.headers).toEqual({
      authorization: "Bearer synthetic-token",
      "content-type": "application/json",
    });
});
