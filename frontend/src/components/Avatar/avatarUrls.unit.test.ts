import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import type { AvatarsApi } from "../../clients/media/avatarsApi";
import { type AvatarUrls, createAvatarUrls, useAvatarUrl, variantFor } from "./avatarUrls";

function stubObjectUrls() {
  let next = 0;
  const create = vi.spyOn(URL, "createObjectURL").mockImplementation(() => `blob:${next++}`);
  const revoke = vi.spyOn(URL, "revokeObjectURL");
  return { create, revoke };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const flush = async () => {
  await Promise.resolve();
  await Promise.resolve();
};

afterEach(() => vi.restoreAllMocks());

describe("createAvatarUrls", () => {
  test("two holders of one key share one fetch and url; the last release revokes it once", async () => {
    const { revoke } = stubObjectUrls();
    const variant = vi.fn<AvatarsApi["variant"]>(async () => new Blob(["a"]));
    const urls = createAvatarUrls({ variant });
    const release1 = urls.acquire("id1", 64, () => {});
    const release2 = urls.acquire("id1", 64, () => {});
    await flush();
    expect(variant).toHaveBeenCalledTimes(1);
    const url = urls.get("id1", 64);
    expect(typeof url).toBe("string");

    release1();
    expect(revoke).not.toHaveBeenCalled();
    release2();
    expect(revoke).toHaveBeenCalledTimes(1);
    expect(revoke).toHaveBeenCalledWith(url);
    expect(urls.get("id1", 64)).toBeNull();
  });

  test('a failed read reports "failed"', async () => {
    stubObjectUrls();
    const variant = vi.fn<AvatarsApi["variant"]>(async () => {
      throw new Error("no");
    });
    const urls = createAvatarUrls({ variant });
    urls.acquire("id1", 24, () => {});
    await flush();
    expect(urls.get("id1", 24)).toBe("failed");
  });

  test("a release before the fetch settles still revokes the url it later gets", async () => {
    const { revoke } = stubObjectUrls();
    const pending = deferred<Blob>();
    const variant = vi.fn<AvatarsApi["variant"]>(async () => pending.promise);
    const urls = createAvatarUrls({ variant });
    const release = urls.acquire("id1", 24, () => {});
    release();
    expect(urls.get("id1", 24)).toBeNull();

    pending.resolve(new Blob(["a"]));
    await flush();
    expect(revoke).toHaveBeenCalledTimes(1);
  });

  test("releasing the last holder of a pending fetch aborts it; an earlier release does not", () => {
    stubObjectUrls();
    const signals: AbortSignal[] = [];
    const variant = vi.fn<AvatarsApi["variant"]>(async (_id, _size, signal) => {
      if (signal) signals.push(signal);
      return new Promise<Blob>(() => {});
    });
    const urls = createAvatarUrls({ variant });
    const release1 = urls.acquire("id1", 24, () => {});
    const release2 = urls.acquire("id1", 24, () => {});
    release1();
    expect(signals[0]?.aborted).toBe(false);
    release2();
    expect(signals).toHaveLength(1);
    expect(signals[0]?.aborted).toBe(true);
  });

  test("dispose revokes every loaded url and cancels a fetch still pending", async () => {
    const { revoke } = stubObjectUrls();
    const pending = deferred<Blob>();
    const variant = vi.fn<AvatarsApi["variant"]>(async (id) =>
      id === "loaded" ? new Blob(["a"]) : pending.promise,
    );
    const urls = createAvatarUrls({ variant });
    urls.acquire("loaded", 24, () => {});
    urls.acquire("pending", 24, () => {});
    await flush();
    const loadedUrl = urls.get("loaded", 24);
    expect(typeof loadedUrl).toBe("string");

    urls.dispose();
    expect(revoke).toHaveBeenCalledWith(loadedUrl);
    expect(urls.get("loaded", 24)).toBeNull();
    expect(urls.get("pending", 24)).toBeNull();

    pending.resolve(new Blob(["b"]));
    await flush();
    expect(revoke).toHaveBeenCalledTimes(2);
  });
});

test("variantFor picks the smallest served size at least px, else 256", () => {
  expect(variantFor(1)).toBe(24);
  expect(variantFor(24)).toBe(24);
  expect(variantFor(26)).toBe(32);
  expect(variantFor(128)).toBe(128);
  expect(variantFor(129)).toBe(256);
  expect(variantFor(300)).toBe(256);
});

describe("useAvatarUrl", () => {
  test("a null id acquires nothing and returns the empty state", () => {
    const urls: AvatarUrls = { acquire: vi.fn(), get: vi.fn(), dispose: vi.fn() };
    const { result } = renderHook(() => useAvatarUrl(urls, null, 24));
    expect(result.current).toEqual({ url: null, failed: false });
    expect(urls.acquire).not.toHaveBeenCalled();
  });

  test("acquires on mount, reads the loaded url, releases exactly once on unmount", async () => {
    const { revoke } = stubObjectUrls();
    const variant = vi.fn<AvatarsApi["variant"]>(async () => new Blob(["a"]));
    const urls = createAvatarUrls({ variant });
    const { result, unmount } = renderHook(() => useAvatarUrl(urls, "id1", 24));
    expect(result.current).toEqual({ url: null, failed: false });

    await act(flush);
    expect(typeof result.current.url).toBe("string");
    expect(result.current.failed).toBe(false);

    unmount();
    expect(revoke).toHaveBeenCalledTimes(1);
  });

  test("a failed fetch reports failed", async () => {
    stubObjectUrls();
    const variant = vi.fn<AvatarsApi["variant"]>(async () => {
      throw new Error("no");
    });
    const urls = createAvatarUrls({ variant });
    const { result } = renderHook(() => useAvatarUrl(urls, "id1", 24));
    await act(flush);
    expect(result.current).toEqual({ url: null, failed: true });
  });

  test("changing the id releases the old key and acquires the new one", async () => {
    const { revoke } = stubObjectUrls();
    const variant = vi.fn<AvatarsApi["variant"]>(async () => new Blob(["a"]));
    const urls = createAvatarUrls({ variant });
    const { result, rerender } = renderHook(({ id }) => useAvatarUrl(urls, id, 24), {
      initialProps: { id: "id1" },
    });
    await act(flush);
    const firstUrl = result.current.url;

    rerender({ id: "id2" });
    expect(revoke).toHaveBeenCalledWith(firstUrl);
    await act(flush);
    expect(result.current.url).not.toBe(firstUrl);
  });
});
