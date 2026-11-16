/**
 * `usePrestartChip`: the chat-prefs read, the provider model list, a new-chat pick held locally
 * until `commit`, and the `NO_CATALOG_NOTE` fallback row.
 */
import { act, renderHook } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { ChatPrefs, SessionPrefsApi } from "../../clients/conversations/sessionPrefsApi";
import type { ModelOption } from "../../protocol";
import { NO_CATALOG_NOTE, usePrestartChip } from "./prestartChip";

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function fakeApi(): {
  api: SessionPrefsApi;
  get: ReturnType<typeof vi.fn<SessionPrefsApi["get"]>>;
  save: ReturnType<typeof vi.fn<SessionPrefsApi["save"]>>;
} {
  const get = vi.fn<SessionPrefsApi["get"]>();
  const save = vi.fn<SessionPrefsApi["save"]>();
  return { api: { get, save }, get, save };
}

const MODELS: ModelOption[] = [
  { value: "sonnet", label: "Sonnet", description: "Efficient for routine tasks" },
  { value: "opus", label: "Opus", description: "Most capable" },
];

/** Flushes the microtask queue inside `act`, the way modelChoice.unit.test.tsx's own tests
 * settle a resolved/rejected promise without a real wait. */
async function flush(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

test.each([null, "viki-llm/11111111-1111-4111-8111-111111111111/model-a"])(
  "the send preview captures raw prefs, including an effort-only pick (%s)",
  async (model) => {
    const { api, get } = fakeApi();
    get.mockResolvedValue({ model, effort: "high" });
    const { result } = renderHook(() =>
      usePrestartChip({
        api,
        models: undefined,
        chatKey: "k1",
        enabled: true,
        refreshKey: null,
      }),
    );
    expect(result.current.preview("k1")).toBeNull();
    await flush();
    expect(result.current.preview("other")).toBeNull();
    expect(result.current.preview("k1")).toEqual({ model });
    act(() => result.current.hold("effort", "max"));
    const captured = result.current.preview("k1");
    expect(captured).toEqual({ model });
    act(() => result.current.hold("model", "model-b"));
    expect(captured).toEqual({ model });
    expect(result.current.preview("k1")).toEqual({ model: "model-b" });
    act(() => result.current.hold("model", "default"));
    expect(result.current.preview("k1")).toEqual({ model: "default" });
  },
);

test.each([null, "sonnet", "viki-llm/11111111-1111-4111-8111-111111111111/model"])(
  "an explicit current default pick is held and saved (%s)",
  async (model) => {
    const { api, get, save } = fakeApi();
    get.mockResolvedValue({ model, effort: "high" });
    save.mockResolvedValue({ model, effort: "high" });
    const { result } = renderHook(() =>
      usePrestartChip({
        api,
        models: undefined,
        chatKey: "k1",
        enabled: true,
        refreshKey: null,
      }),
    );
    await flush();
    act(() => result.current.hold("model", model ?? "default"));
    act(() => result.current.hold("model", model ?? "default")); // a duplicate is still explicit
    await act(async () => {
      await result.current.commit("k1");
    });
    expect(save).toHaveBeenCalledExactlyOnceWith("k1", { model: model ?? "default" });
  },
);

test("a null model reads the catalog's default label once both the read and the list land", async () => {
  const { api, get } = fakeApi();
  get.mockResolvedValue({ model: null, effort: "high" });
  const models = vi.fn(async () => MODELS);
  const { result } = renderHook(() =>
    usePrestartChip({ api, models, chatKey: "k1", enabled: true, refreshKey: null }),
  );
  expect(result.current.chip).toBeNull(); // neither the read nor the list has landed yet
  await flush();
  expect(result.current.chip).toEqual({ models: MODELS, model: "default", effort: "high" });
  expect(get).toHaveBeenCalledWith("k1", expect.anything());
});

test("stored prefs show as the chip once landed", async () => {
  const { api, get } = fakeApi();
  get.mockResolvedValue({ model: "sonnet", effort: "max" });
  const models = vi.fn(async () => MODELS);
  const { result } = renderHook(() =>
    usePrestartChip({ api, models, chatKey: "k1", enabled: true, refreshKey: null }),
  );
  await flush();
  expect(result.current.chip).toEqual({ models: MODELS, model: "sonnet", effort: "max" });
});

test("a failed read leaves the chip null", async () => {
  const { api, get } = fakeApi();
  get.mockRejectedValue(new Error("unavailable"));
  const models = vi.fn(async () => MODELS);
  const { result } = renderHook(() =>
    usePrestartChip({ api, models, chatKey: "k1", enabled: true, refreshKey: null }),
  );
  await flush();
  expect(result.current.chip).toBeNull();
});

test("an empty model list falls back to one row noted with NO_CATALOG_NOTE", async () => {
  const { api, get } = fakeApi();
  get.mockResolvedValue({ model: "sonnet", effort: "high" });
  const models = vi.fn(async () => []);
  const { result } = renderHook(() =>
    usePrestartChip({ api, models, chatKey: "k1", enabled: true, refreshKey: null }),
  );
  await flush();
  expect(result.current.chip).toEqual({
    models: [{ value: "sonnet", label: "sonnet", description: NO_CATALOG_NOTE }],
    model: "sonnet",
    effort: "high",
  });
});

test("a failing models() and no models function at all both fall back to the noted row", async () => {
  const { api: api1, get: get1 } = fakeApi();
  get1.mockResolvedValue({ model: "sonnet", effort: "high" });
  const failing = vi.fn(() => Promise.reject(new Error("nope")));
  const first = renderHook(() =>
    usePrestartChip({ api: api1, models: failing, chatKey: "k1", enabled: true, refreshKey: null }),
  );
  await flush();
  expect(first.result.current.chip?.models).toEqual([
    { value: "sonnet", label: "sonnet", description: NO_CATALOG_NOTE },
  ]);

  const { api: api2, get: get2 } = fakeApi();
  get2.mockResolvedValue({ model: "sonnet", effort: "high" });
  const second = renderHook(() =>
    usePrestartChip({
      api: api2,
      models: undefined,
      chatKey: "k1",
      enabled: true,
      refreshKey: null,
    }),
  );
  await flush();
  expect(second.result.current.chip?.models).toEqual([
    { value: "sonnet", label: "sonnet", description: NO_CATALOG_NOTE },
  ]);
});

test("hold moves the label at once with no request, and reverting to the read value drops it", async () => {
  const { api, get } = fakeApi();
  get.mockResolvedValue({ model: "sonnet", effort: "high" });
  const models = vi.fn(async () => MODELS);
  const { result } = renderHook(() =>
    usePrestartChip({ api, models, chatKey: "k1", enabled: true, refreshKey: null }),
  );
  await flush();
  act(() => result.current.hold("model", "opus"));
  expect(result.current.chip?.model).toBe("opus");
  expect(result.current.holds("k1")).toBe(true);
  expect(get).toHaveBeenCalledTimes(1); // holding never re-reads or goes over a socket
  act(() => result.current.hold("model", "sonnet")); // back to the read's own value: a revert
  expect(result.current.chip?.model).toBe("sonnet");
  expect(result.current.holds("k1")).toBe(false); // nothing left held
});

test("holds reports false before any hold and for a different key", async () => {
  const { api, get } = fakeApi();
  get.mockResolvedValue({ model: "sonnet", effort: "high" });
  const models = vi.fn(async () => MODELS);
  const { result } = renderHook(() =>
    usePrestartChip({ api, models, chatKey: "k1", enabled: true, refreshKey: null }),
  );
  await flush();
  expect(result.current.holds("k1")).toBe(false);
  act(() => result.current.hold("effort", "max"));
  expect(result.current.holds("k1")).toBe(true);
  expect(result.current.holds("k2")).toBe(false);
});

test("hold is a no-op before any read has landed for the chat", async () => {
  const { api, get } = fakeApi();
  get.mockReturnValue(new Promise<ChatPrefs>(() => {})); // never settles
  const models = vi.fn(async () => MODELS);
  const { result } = renderHook(() =>
    usePrestartChip({ api, models, chatKey: "k1", enabled: true, refreshKey: null }),
  );
  act(() => result.current.hold("model", "opus"));
  expect(result.current.holds("k1")).toBe(false);
  expect(result.current.chip).toBeNull();
});

test("commit saves only the held fields and the saved answer becomes the chip", async () => {
  const { api, get, save } = fakeApi();
  get.mockResolvedValue({ model: "sonnet", effort: "high" });
  save.mockResolvedValue({ model: "opus", effort: "high" });
  const models = vi.fn(async () => MODELS);
  const { result } = renderHook(() =>
    usePrestartChip({ api, models, chatKey: "k1", enabled: true, refreshKey: null }),
  );
  await flush();
  act(() => result.current.hold("model", "opus"));
  let outcome: boolean | undefined;
  await act(async () => {
    outcome = await result.current.commit("k1");
  });
  expect(outcome).toBe(true);
  expect(save).toHaveBeenCalledExactlyOnceWith("k1", { model: "opus" });
  expect(result.current.chip).toEqual({ models: MODELS, model: "opus", effort: "high" });
  expect(result.current.holds("k1")).toBe(false);
});

test("commit holding both fields saves both", async () => {
  const { api, get, save } = fakeApi();
  get.mockResolvedValue({ model: "sonnet", effort: "high" });
  save.mockResolvedValue({ model: "opus", effort: "max" });
  const models = vi.fn(async () => MODELS);
  const { result } = renderHook(() =>
    usePrestartChip({ api, models, chatKey: "k1", enabled: true, refreshKey: null }),
  );
  await flush();
  act(() => result.current.hold("model", "opus"));
  act(() => result.current.hold("effort", "max"));
  await act(async () => {
    await result.current.commit("k1");
  });
  expect(save).toHaveBeenCalledExactlyOnceWith("k1", { model: "opus", effort: "max" });
});

test("a commit failure drops the pick, reports false, and reads again", async () => {
  const { api, get, save } = fakeApi();
  get.mockResolvedValue({ model: "sonnet", effort: "high" });
  save.mockRejectedValue(new Error("unavailable"));
  const models = vi.fn(async () => MODELS);
  const { result } = renderHook(() =>
    usePrestartChip({ api, models, chatKey: "k1", enabled: true, refreshKey: null }),
  );
  await flush();
  act(() => result.current.hold("model", "opus"));
  let outcome: boolean | undefined;
  await act(async () => {
    outcome = await result.current.commit("k1");
  });
  expect(outcome).toBe(false);
  expect(result.current.holds("k1")).toBe(false);
  await flush();
  expect(get).toHaveBeenCalledTimes(2); // refresh() re-read
  expect(result.current.chip?.model).toBe("sonnet"); // unmoved: the dropped pick, re-read
});

test("commit with nothing held saves nothing and reports true", async () => {
  const { api, get, save } = fakeApi();
  get.mockResolvedValue({ model: "sonnet", effort: "high" });
  const models = vi.fn(async () => MODELS);
  const { result } = renderHook(() =>
    usePrestartChip({ api, models, chatKey: "k1", enabled: true, refreshKey: null }),
  );
  await flush();
  let outcome: boolean | undefined;
  await act(async () => {
    outcome = await result.current.commit("k1");
  });
  expect(outcome).toBe(true);
  expect(save).not.toHaveBeenCalled();
});

test("a key change ignores the old key's late answer", async () => {
  const { api, get } = fakeApi();
  const first = deferred<ChatPrefs>();
  const second = deferred<ChatPrefs>();
  get.mockImplementationOnce(() => first.promise).mockImplementationOnce(() => second.promise);
  const models = vi.fn(async () => MODELS);
  const { result, rerender } = renderHook(
    (props: { chatKey: string }) =>
      usePrestartChip({ api, models, enabled: true, refreshKey: null, chatKey: props.chatKey }),
    { initialProps: { chatKey: "k1" } },
  );
  rerender({ chatKey: "k2" });
  await act(async () => {
    second.resolve({ model: "opus", effort: "max" });
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(result.current.chip?.model).toBe("opus");
  await act(async () => {
    first.resolve({ model: "sonnet", effort: "high" }); // the abandoned k1 read, landing late
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(result.current.chip?.model).toBe("opus"); // unmoved by the stale answer
  expect(get).toHaveBeenCalledTimes(2);
});

test("enabled false shows no chip and reads nothing", async () => {
  const { api, get } = fakeApi();
  const models = vi.fn(async () => MODELS);
  const { result } = renderHook(() =>
    usePrestartChip({ api, models, chatKey: "k1", enabled: false, refreshKey: null }),
  );
  await flush();
  expect(result.current.chip).toBeNull();
  expect(get).not.toHaveBeenCalled();
  expect(models).not.toHaveBeenCalled();
});

test("a refreshKey change and an explicit refresh() both re-read the same chat", async () => {
  const { api, get } = fakeApi();
  get.mockResolvedValue({ model: "sonnet", effort: "high" });
  const models = vi.fn(async () => MODELS);
  const { result, rerender } = renderHook(
    (props: { refreshKey: unknown }) =>
      usePrestartChip({ api, models, chatKey: "k1", enabled: true, refreshKey: props.refreshKey }),
    { initialProps: { refreshKey: 1 } },
  );
  await flush();
  expect(get).toHaveBeenCalledTimes(1);
  rerender({ refreshKey: 2 });
  await flush();
  expect(get).toHaveBeenCalledTimes(2);
  act(() => result.current.refresh());
  await flush();
  expect(get).toHaveBeenCalledTimes(3);
  expect(models).toHaveBeenCalledTimes(1); // the list itself is fetched once per chatKey, not per refresh
});

test("a re-read keeps the last good read of the same key until the new one lands", async () => {
  const { api, get } = fakeApi();
  const first = deferred<ChatPrefs>();
  get.mockImplementationOnce(() => first.promise);
  const models = vi.fn(async () => MODELS);
  const { result, rerender } = renderHook(
    (props: { refreshKey: unknown }) =>
      usePrestartChip({ api, models, chatKey: "k1", enabled: true, refreshKey: props.refreshKey }),
    { initialProps: { refreshKey: 1 } },
  );
  await act(async () => {
    first.resolve({ model: "sonnet", effort: "high" });
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(result.current.chip).toEqual({ models: MODELS, model: "sonnet", effort: "high" });

  const second = deferred<ChatPrefs>();
  get.mockImplementationOnce(() => second.promise);
  rerender({ refreshKey: 2 }); // a re-read of the same key starts, and does not land yet
  expect(result.current.chip).toEqual({ models: MODELS, model: "sonnet", effort: "high" }); // unmoved

  await act(async () => {
    second.resolve({ model: "opus", effort: "max" });
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(result.current.chip).toEqual({ models: MODELS, model: "opus", effort: "max" });
  expect(get).toHaveBeenCalledTimes(2);
});
