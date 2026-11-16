import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { type AssetsApi, AssetsApiError } from "../../clients/media/assetsApi";
import { MediaContext } from "../../context/mediaContext";
import type { Attachment, UnavailableAttachment } from "../../turnInput";
import { UserTurn } from "./Conversation";

afterEach(cleanup);

const attachment: Attachment = {
  asset_id: "123e4567-e89b-12d3-a456-426614174000",
  media_type: "image/png",
  byte_size: 2048,
  width: 320,
  height: 200,
  name: "screen.png",
  modified_at: null,
  source: "paste",
};
const user = { text: "", command: "", at: "2026-09-12T10:00:00.000Z", attachments: [attachment] };

test("image-only user turns preserve attachment metadata and render an authenticated preview", async () => {
  let resolve!: (blob: Blob) => void;
  const read = vi.fn(
    (_id: string, signal?: AbortSignal) =>
      new Promise<Blob>((r) => {
        resolve = r;
        signal?.addEventListener("abort", () => undefined);
      }),
  );
  const api: AssetsApi = { upload: vi.fn(), read };
  const create = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:screen");
  render(
    <MediaContext.Provider value={api}>
      <UserTurn user={user} nowMs={Date.parse("2026-09-12T10:00:00.000Z")} />
    </MediaContext.Provider>,
  );
  expect(screen.getByText("screen.png · 320 × 200")).toBeTruthy();
  expect(read).toHaveBeenCalledWith(attachment.asset_id, expect.any(AbortSignal));
  await act(async () => resolve(new Blob(["png"], { type: "image/png" })));
  const preview = screen.getByRole("button", { name: "Preview: screen.png" });
  expect(preview.querySelector("img")?.getAttribute("src")).toBe("blob:screen");
  expect(create).toHaveBeenCalled();
});

test("preview dialog traps focus, closes on Escape, and revokes the object URL", async () => {
  let resolve!: (blob: Blob) => void;
  const api: AssetsApi = {
    upload: vi.fn(),
    read: vi.fn(
      () =>
        new Promise<Blob>((r) => {
          resolve = r;
        }),
    ),
  };
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:screen");
  const revoke = vi.spyOn(URL, "revokeObjectURL");
  render(
    <MediaContext.Provider value={api}>
      <UserTurn user={user} nowMs={0} />
    </MediaContext.Provider>,
  );
  await act(async () => resolve(new Blob(["png"], { type: "image/png" })));
  fireEvent.click(screen.getByRole("button", { name: "Preview: screen.png" }));
  expect(screen.getByRole("dialog")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Close preview" })).toBe(document.activeElement);
  fireEvent.keyDown(window, { key: "Escape" });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(revoke).not.toHaveBeenCalled();
  cleanup();
  expect(revoke).toHaveBeenCalledWith("blob:screen");
});

test("an aborted preview read cannot publish a late object URL", async () => {
  let resolve!: (blob: Blob) => void;
  let signal: AbortSignal | undefined;
  const api: AssetsApi = {
    upload: vi.fn(),
    read: vi.fn((_id, passed) => {
      signal = passed;
      return new Promise<Blob>((r) => {
        resolve = r;
      });
    }),
  };
  const create = vi.spyOn(URL, "createObjectURL");
  const { unmount } = render(
    <MediaContext.Provider value={api}>
      <UserTurn user={user} nowMs={0} />
    </MediaContext.Provider>,
  );
  unmount();
  expect(signal?.aborted).toBe(true);
  await act(async () => resolve(new Blob(["late"])));
  expect(create).not.toHaveBeenCalled();
});

test("unmatched history media is explained without fetching or inserting image URLs", () => {
  const api: AssetsApi = { upload: vi.fn(), read: vi.fn() };
  const unavailable: UnavailableAttachment[] = [
    {
      name: "history-image.png",
      media_type: "image/png",
      byte_size: 2048,
      reason: "not_in_local_assets",
    },
    {
      name: "history-image.webp",
      media_type: "image/webp",
      byte_size: 0,
      reason: "invalid_native_media",
    },
  ];
  const { container } = render(
    <MediaContext.Provider value={api}>
      <UserTurn
        user={{
          text: "Image history",
          command: "",
          at: user.at,
          unavailable_attachments: unavailable,
        }}
        nowMs={0}
      />
    </MediaContext.Provider>,
  );
  expect(screen.getByText("This image has no copy in local attachments.")).toBeTruthy();
  expect(screen.getByText("The image data in this history could not be read.")).toBeTruthy();
  expect(api.read).not.toHaveBeenCalled();
  expect(container.querySelector("img, a")).toBeNull();
});

test("a retained history reference whose local copy expired keeps its metadata and explains 410", async () => {
  const api: AssetsApi = {
    upload: vi.fn(),
    read: vi.fn().mockRejectedValue(new AssetsApiError(410, "expired")),
  };
  render(
    <MediaContext.Provider value={api}>
      <UserTurn user={{ ...user, attachments: [{ ...attachment, source: "history" }] }} nowMs={0} />
    </MediaContext.Provider>,
  );
  expect(await screen.findByText("Attachment no longer stored locally")).toBeTruthy();
  expect(screen.getByText("screen.png · 320 × 200")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Preview: screen.png" }).hasAttribute("disabled")).toBe(
    true,
  );
});

test("choosing the same file for an expired attachment restores it and re-reads the preview", async () => {
  let reads = 0;
  const read = vi.fn(() => {
    reads += 1;
    return reads === 1
      ? Promise.reject(new AssetsApiError(410, "expired"))
      : Promise.resolve(new Blob(["png"], { type: "image/png" }));
  });
  const relink = vi.fn().mockResolvedValue(undefined);
  const api: AssetsApi = { upload: vi.fn(), read, relink };
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:restored");
  const { container } = render(
    <MediaContext.Provider value={api}>
      <UserTurn user={user} nowMs={0} />
    </MediaContext.Provider>,
  );
  expect(await screen.findByText("Attachment no longer stored locally")).toBeTruthy();
  const input = container.querySelector("input[type=file]") as HTMLInputElement;
  const file = new File([new Uint8Array([1])], "screen.png", { type: "image/png" });
  await act(async () => {
    fireEvent.change(input, { target: { files: [file] } });
  });
  expect(relink).toHaveBeenCalledWith(attachment.asset_id, file);
  await screen.findByRole("button", { name: "Preview: screen.png" });
  expect(reads).toBe(2);
  expect(screen.queryByText("Attachment no longer stored locally")).toBeNull();
});

test("a mismatched relink keeps the expired state and names the mismatch; no relink means no button", async () => {
  const relink = vi.fn().mockRejectedValue(new AssetsApiError(409, "asset_mismatch"));
  const api: AssetsApi = {
    upload: vi.fn(),
    read: vi.fn().mockRejectedValue(new AssetsApiError(410, "expired")),
    relink,
  };
  const { container } = render(
    <MediaContext.Provider value={api}>
      <UserTurn user={user} nowMs={0} />
    </MediaContext.Provider>,
  );
  expect(await screen.findByText("Attachment no longer stored locally")).toBeTruthy();
  const input = container.querySelector("input[type=file]") as HTMLInputElement;
  const file = new File([new Uint8Array([1])], "wrong.png", { type: "image/png" });
  await act(async () => {
    fireEvent.change(input, { target: { files: [file] } });
  });
  const status = await screen.findByRole("status");
  expect(status.textContent).toBe("The chosen file does not match the sent image.");

  cleanup();
  const noRelinkApi: AssetsApi = {
    upload: vi.fn(),
    read: vi.fn().mockRejectedValue(new AssetsApiError(410, "expired")),
  };
  render(
    <MediaContext.Provider value={noRelinkApi}>
      <UserTurn user={user} nowMs={0} />
    </MediaContext.Provider>,
  );
  expect(await screen.findByText("Attachment no longer stored locally")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Choose the same file to restore" })).toBeNull();
});

test("a relink rejected with 404 shows the generic restore failure, announced with role status", async () => {
  const relink = vi.fn().mockRejectedValue(new AssetsApiError(404, "not_found"));
  const api: AssetsApi = {
    upload: vi.fn(),
    read: vi.fn().mockRejectedValue(new AssetsApiError(410, "expired")),
    relink,
  };
  const { container } = render(
    <MediaContext.Provider value={api}>
      <UserTurn user={user} nowMs={0} />
    </MediaContext.Provider>,
  );
  expect(await screen.findByText("Attachment no longer stored locally")).toBeTruthy();
  const input = container.querySelector("input[type=file]") as HTMLInputElement;
  const file = new File([new Uint8Array([1])], "screen.png", { type: "image/png" });
  await act(async () => {
    fireEvent.change(input, { target: { files: [file] } });
  });
  const status = await screen.findByRole("status");
  expect(status.textContent).toBe("The image could not be restored.");
});

test("the relink button disables while a relink is pending", async () => {
  let resolveRelink!: () => void;
  const relink = vi.fn(
    () =>
      new Promise<void>((resolve) => {
        resolveRelink = resolve;
      }),
  );
  const api: AssetsApi = {
    upload: vi.fn(),
    read: vi.fn().mockRejectedValue(new AssetsApiError(410, "expired")),
    relink,
  };
  const { container } = render(
    <MediaContext.Provider value={api}>
      <UserTurn user={user} nowMs={0} />
    </MediaContext.Provider>,
  );
  expect(await screen.findByText("Attachment no longer stored locally")).toBeTruthy();
  const input = container.querySelector("input[type=file]") as HTMLInputElement;
  const file = new File([new Uint8Array([1])], "screen.png", { type: "image/png" });
  const button = screen.getByRole("button", {
    name: "Choose the same file to restore",
  }) as HTMLButtonElement;
  expect(button.disabled).toBe(false);
  fireEvent.change(input, { target: { files: [file] } });
  expect(button.disabled).toBe(true);
  await act(async () => resolveRelink());
});

const previewWitness = vi.hoisted(() => ({
  retries: [] as (() => void)[],
  views: [] as { api: AssetsApi; id: string | null; url: string | null; state: string }[],
}));
vi.mock("../Attachments/Attachments", async (importOriginal) => {
  const original = await importOriginal<typeof import("../Attachments/Attachments")>();
  return {
    ...original,
    useAssetPreviewState: (...args: Parameters<typeof original.useAssetPreviewState>) => {
      const result = original.useAssetPreviewState(...args);
      if ("retry" in result && typeof result.retry === "function")
        previewWitness.retries.push(result.retry as () => void);
      previewWitness.views.push({
        api: args[0],
        id: args[1]?.asset_id ?? null,
        url: result.url,
        state: result.state,
      });
      return result;
    },
  };
});

function previewReads() {
  const calls: {
    id: string;
    signal: AbortSignal | undefined;
    resolve: (blob: Blob) => void;
    reject: (error: unknown) => void;
  }[] = [];
  const api: AssetsApi = {
    upload: vi.fn(),
    relink: vi.fn(),
    read: vi.fn(
      (id, signal) =>
        new Promise<Blob>((resolve, reject) => {
          calls.push({ id, signal, resolve, reject });
        }),
    ),
  };
  return { api, calls };
}

const retryAsset = { ...attachment, name: "<img src=x onerror=alert(1)>notes.png" };
function previewFixture(asset: Attachment | null = retryAsset) {
  previewWitness.retries.length = 0;
  previewWitness.views.length = 0;
  let urls = 0;
  const create = vi
    .spyOn(URL, "createObjectURL")
    .mockImplementation(() => `blob:preview-${++urls}`);
  const revoke = vi.spyOn(URL, "revokeObjectURL");
  create.mockClear();
  revoke.mockClear();
  const reads = previewReads();
  const draw = (
    api: AssetsApi,
    current: Attachment | null,
    unavailable: UnavailableAttachment[] = [],
  ) => (
    <MediaContext.Provider value={api}>
      <UserTurn
        user={{
          ...user,
          text: "See this",
          attachments: current ? [current] : [],
          unavailable_attachments: unavailable,
        }}
        nowMs={0}
      />
    </MediaContext.Provider>
  );
  const view = render(draw(reads.api, asset));
  return {
    ...reads,
    ...view,
    draw,
    create,
    revoke,
    restore: () => {
      cleanup();
      create.mockRestore();
      revoke.mockRestore();
    },
  };
}
function capturedRetry() {
  const retry = previewWitness.retries.at(-1);
  if (!retry) throw new Error("The real preview did not expose its retry callback");
  return retry;
}
function fixtureAt<T>(items: readonly T[], index: number): T {
  const item = items[index];
  if (item === undefined) throw new Error(`Missing fixture item at index ${index}`);
  return item;
}
const previewBlob = () => new Blob(["png"], { type: "image/png" });

test("a transient preview failure needs one explicit retry and publishes only the current blob", async () => {
  const f = previewFixture();
  try {
    expect(f.calls).toHaveLength(1);
    expect(fixtureAt(f.calls, 0).id).toBe(retryAsset.asset_id);
    expect(fixtureAt(f.calls, 0).signal).toBeInstanceOf(AbortSignal);
    expect(f.create).not.toHaveBeenCalled();
    await act(async () => fixtureAt(f.calls, 0).reject(new AssetsApiError(0, "timeout")));
    expect(screen.getByText("Attachment could not be loaded")).toBeTruthy();
    expect(screen.getByText("See this")).toBeTruthy();
    expect(f.container.querySelector("img, a")).toBeNull();
    const retry = capturedRetry();
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "Retry preview" }));
      retry();
    });
    expect(f.calls).toHaveLength(2);
    expect(fixtureAt(f.calls, 0).signal?.aborted).toBe(true);
    expect(fixtureAt(f.calls, 1).id).toBe(retryAsset.asset_id);
    expect(fixtureAt(f.calls, 1).signal?.aborted).toBe(false);
    expect(fixtureAt(f.calls, 1).signal).not.toBe(fixtureAt(f.calls, 0).signal);
    expect(screen.getByRole("status").textContent).toBe("Loading preview…");
    expect(screen.queryByText("Attachment could not be loaded")).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry preview" })).toBeNull();
    act(() => retry());
    expect(f.calls).toHaveLength(2);
    await act(async () => fixtureAt(f.calls, 1).resolve(previewBlob()));
    const preview = screen.getByRole("button", { name: `Preview: ${retryAsset.name}` });
    expect(preview.querySelector("img")?.getAttribute("src")).toBe("blob:preview-1");
    expect(preview.querySelector("img")?.getAttribute("alt")).toBe(retryAsset.name);
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry preview" })).toBeNull();
    preview.focus();
    fireEvent.click(preview);
    expect(screen.getByRole("button", { name: "Close preview" })).toBe(document.activeElement);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(preview);
    expect(f.api.upload).not.toHaveBeenCalled();
    expect(f.api.relink).not.toHaveBeenCalled();
    expect(f.revoke).not.toHaveBeenCalled();
    f.unmount();
    expect(fixtureAt(f.calls, 1).signal?.aborted).toBe(true);
    expect(f.revoke).toHaveBeenCalledExactlyOnceWith("blob:preview-1");
  } finally {
    f.restore();
  }
});

test("a rejected current retry stays recoverable without starting another read", async () => {
  const f = previewFixture();
  try {
    await act(async () =>
      fixtureAt(f.calls, 0).reject(new TypeError("synthetic transport failure")),
    );
    fireEvent.click(screen.getByRole("button", { name: "Retry preview" }));
    await act(async () => fixtureAt(f.calls, 1).reject(new AssetsApiError(0, "timeout")));
    expect(screen.getByText("Attachment could not be loaded")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry preview" })).toBeTruthy();
    expect(screen.queryByRole("status")).toBeNull();
    expect(f.calls).toHaveLength(2);
    expect(f.create).not.toHaveBeenCalled();
    expect(f.api.upload).not.toHaveBeenCalled();
    expect(f.api.relink).not.toHaveBeenCalled();
  } finally {
    f.restore();
  }
});

test.each(["pending", "ready", "error"] as const)(
  "same-ID API replacement masks the previous %s result on its first render",
  async (state) => {
    const f = previewFixture();
    const next = previewReads();
    try {
      if (state === "ready") await act(async () => fixtureAt(f.calls, 0).resolve(previewBlob()));
      if (state === "error")
        await act(async () => fixtureAt(f.calls, 0).reject(new AssetsApiError(0, "timeout")));
      const oldRetry = capturedRetry();
      previewWitness.views.length = 0;
      f.rerender(f.draw(next.api, retryAsset));
      const nextViews = previewWitness.views.filter((view) => view.api === next.api);
      expect(nextViews.length).toBeGreaterThan(0);
      expect(nextViews.every((view) => view.state === "loading" && view.url === null)).toBe(true);
      expect(fixtureAt(f.calls, 0).signal?.aborted).toBe(true);
      expect(next.calls).toHaveLength(1);
      expect(fixtureAt(next.calls, 0).id).toBe(retryAsset.asset_id);
      expect(fixtureAt(next.calls, 0).signal?.aborted).toBe(false);
      act(() => oldRetry());
      expect(next.calls).toHaveLength(1);
      if (state === "pending") await act(async () => fixtureAt(f.calls, 0).resolve(previewBlob()));
      expect(screen.getByRole("status").textContent).toBe("Loading preview…");
      await act(async () => fixtureAt(next.calls, 0).resolve(previewBlob()));
      expect(f.create).toHaveBeenCalledTimes(state === "ready" ? 2 : 1);
      expect(f.revoke).toHaveBeenCalledTimes(state === "ready" ? 1 : 0);
      const src = screen
        .getByRole("button", { name: `Preview: ${retryAsset.name}` })
        .querySelector("img")
        ?.getAttribute("src");
      expect(src).toBe(state === "ready" ? "blob:preview-2" : "blob:preview-1");
      expect(f.api.upload).not.toHaveBeenCalled();
      expect(next.api.upload).not.toHaveBeenCalled();
      expect(f.api.relink).not.toHaveBeenCalled();
      expect(next.api.relink).not.toHaveBeenCalled();
    } finally {
      f.restore();
    }
  },
);

test.each(["resolve", "reject"] as const)(
  "asset A to B to A rejects obsolete held-retry %s and captured callbacks",
  async (settlement) => {
    const f = previewFixture();
    const other = { ...retryAsset, asset_id: "123e4567-e89b-12d3-a456-426614174001" };
    try {
      await act(async () => fixtureAt(f.calls, 0).reject(new AssetsApiError(0, "timeout")));
      const staleRetry = capturedRetry();
      fireEvent.click(screen.getByRole("button", { name: "Retry preview" }));
      f.rerender(f.draw(f.api, other));
      f.rerender(f.draw(f.api, retryAsset));
      expect(f.calls.map((call) => call.id)).toEqual([
        retryAsset.asset_id,
        retryAsset.asset_id,
        other.asset_id,
        retryAsset.asset_id,
      ]);
      expect(f.calls.slice(0, 3).every((call) => call.signal?.aborted)).toBe(true);
      expect(fixtureAt(f.calls, 3).signal?.aborted).toBe(false);
      act(() => staleRetry());
      expect(f.calls).toHaveLength(4);
      await act(async () => {
        for (const call of f.calls.slice(1, 3)) {
          if (settlement === "resolve") call.resolve(previewBlob());
          else call.reject(new TypeError("obsolete transport failure"));
        }
      });
      expect(f.create).not.toHaveBeenCalled();
      expect(screen.queryByRole("button", { name: "Retry preview" })).toBeNull();
      expect(screen.getByRole("status").textContent).toBe("Loading preview…");
      await act(async () => fixtureAt(f.calls, 3).resolve(previewBlob()));
      expect(f.create).toHaveBeenCalledTimes(1);
      expect(f.revoke).not.toHaveBeenCalled();
      expect(f.api.upload).not.toHaveBeenCalled();
      expect(f.api.relink).not.toHaveBeenCalled();
    } finally {
      f.restore();
    }
  },
);

test.each(["resolve", "reject"] as const)(
  "API A to B to A rejects obsolete held-retry %s and captured callbacks",
  async (settlement) => {
    const f = previewFixture();
    const other = previewReads();
    try {
      await act(async () => fixtureAt(f.calls, 0).reject(new AssetsApiError(0, "timeout")));
      const staleRetry = capturedRetry();
      fireEvent.click(screen.getByRole("button", { name: "Retry preview" }));
      f.rerender(f.draw(other.api, retryAsset));
      f.rerender(f.draw(f.api, retryAsset));
      expect(f.calls).toHaveLength(3);
      expect(other.calls).toHaveLength(1);
      expect(f.calls.slice(0, 2).every((call) => call.signal?.aborted)).toBe(true);
      expect(fixtureAt(other.calls, 0).signal?.aborted).toBe(true);
      expect(fixtureAt(f.calls, 2).signal?.aborted).toBe(false);
      expect([...f.calls, ...other.calls].every((call) => call.id === retryAsset.asset_id)).toBe(
        true,
      );
      act(() => staleRetry());
      expect(f.calls).toHaveLength(3);
      expect(other.calls).toHaveLength(1);
      await act(async () => {
        for (const call of [fixtureAt(f.calls, 1), fixtureAt(other.calls, 0)]) {
          if (settlement === "resolve") call.resolve(previewBlob());
          else call.reject(new TypeError("obsolete transport failure"));
        }
      });
      expect(f.create).not.toHaveBeenCalled();
      expect(screen.queryByRole("button", { name: "Retry preview" })).toBeNull();
      expect(screen.getByRole("status").textContent).toBe("Loading preview…");
      await act(async () => fixtureAt(f.calls, 2).resolve(previewBlob()));
      expect(f.create).toHaveBeenCalledTimes(1);
      expect(f.revoke).not.toHaveBeenCalled();
      expect(f.api.upload).not.toHaveBeenCalled();
      expect(other.api.upload).not.toHaveBeenCalled();
      expect(f.api.relink).not.toHaveBeenCalled();
      expect(other.api.relink).not.toHaveBeenCalled();
    } finally {
      f.restore();
    }
  },
);

test.each(["resolve", "reject"] as const)(
  "unmount blocks a held retry's late %s and its captured callback",
  async (settlement) => {
    const f = previewFixture();
    try {
      await act(async () => fixtureAt(f.calls, 0).reject(new AssetsApiError(0, "timeout")));
      const staleRetry = capturedRetry();
      fireEvent.click(screen.getByRole("button", { name: "Retry preview" }));
      f.unmount();
      expect(fixtureAt(f.calls, 1).signal?.aborted).toBe(true);
      act(() => staleRetry());
      await act(async () => {
        if (settlement === "resolve") fixtureAt(f.calls, 1).resolve(previewBlob());
        else fixtureAt(f.calls, 1).reject(new TypeError("late transport failure"));
      });
      expect(f.calls).toHaveLength(2);
      expect(f.create).not.toHaveBeenCalled();
      expect(f.revoke).not.toHaveBeenCalled();
      expect(f.api.upload).not.toHaveBeenCalled();
      expect(f.api.relink).not.toHaveBeenCalled();
    } finally {
      f.restore();
    }
  },
);

test.each([
  [404, "read_failed"],
  [410, "expired"],
  [401, "read_failed"],
  [403, "read_failed"],
  [400, "invalid_asset"],
  [413, "read_failed"],
  [415, "read_failed"],
  [500, "read_failed"],
  [503, "read_failed"],
  [0, "cancelled"],
  [0, "network_error"],
  [0, "invalid_response"],
] as const)("read refusal %s/%s has no transient retry action", async (status, code) => {
  const f = previewFixture();
  try {
    await act(async () => fixtureAt(f.calls, 0).reject(new AssetsApiError(status, code)));
    expect(
      screen.getByText(
        status === 410 ? "Attachment no longer stored locally" : "Attachment could not be loaded",
      ),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: `Preview: ${retryAsset.name}` }).hasAttribute("disabled"),
    ).toBe(true);
    expect(screen.queryByRole("button", { name: "Retry preview" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Choose the same file to restore" }) !== null).toBe(
      status === 410,
    );
    expect(f.calls).toHaveLength(1);
    expect(f.create).not.toHaveBeenCalled();
    expect(f.api.upload).not.toHaveBeenCalled();
    expect(f.api.relink).not.toHaveBeenCalled();
  } finally {
    f.restore();
  }
});

test.each(["response_too_large", "empty_response", "unsupported", "unknown"])(
  "bounded or unsupported read error %s does not offer retry",
  async (message) => {
    const f = previewFixture();
    try {
      await act(async () => fixtureAt(f.calls, 0).reject(new Error(message)));
      expect(screen.getByText("Attachment could not be loaded")).toBeTruthy();
      expect(screen.queryByRole("button", { name: "Retry preview" })).toBeNull();
      expect(f.calls).toHaveLength(1);
      expect(f.create).not.toHaveBeenCalled();
      expect(f.api.upload).not.toHaveBeenCalled();
      expect(f.api.relink).not.toHaveBeenCalled();
    } finally {
      f.restore();
    }
  },
);

test("an unsupported object URL failure after a successful read is not transport recovery", async () => {
  const f = previewFixture();
  try {
    f.create.mockImplementation(() => {
      throw new TypeError("object URLs unsupported");
    });
    await act(async () => fixtureAt(f.calls, 0).resolve(previewBlob()));
    expect(screen.getByText("Attachment could not be loaded")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retry preview" })).toBeNull();
    expect(f.calls).toHaveLength(1);
    expect(f.revoke).not.toHaveBeenCalled();
    expect(f.api.upload).not.toHaveBeenCalled();
    expect(f.api.relink).not.toHaveBeenCalled();
  } finally {
    f.restore();
  }
});

test("bounded history refusals keep literal metadata without acquiring a reader or retry", () => {
  const f = previewFixture(null);
  try {
    const reasons = ["not_in_local_assets", "too_large", "limit", "invalid_native_media"] as const;
    f.rerender(
      f.draw(
        f.api,
        null,
        reasons.map((reason, index) => ({
          name: `${retryAsset.name}-${index}`,
          media_type: "image/png",
          byte_size: 2048,
          reason,
        })),
      ),
    );
    expect(screen.getByText("This image has no copy in local attachments.")).toBeTruthy();
    expect(screen.getAllByText("This image exceeds the history preview limits.")).toHaveLength(2);
    expect(screen.getByText("The image data in this history could not be read.")).toBeTruthy();
    reasons.forEach((_, index) => {
      expect(screen.getByText(`${retryAsset.name}-${index}`)).toBeTruthy();
    });
    expect(f.container.querySelector("img, a, input, button")).toBeNull();
    expect(f.calls).toHaveLength(0);
    expect(f.create).not.toHaveBeenCalled();
    expect(f.api.upload).not.toHaveBeenCalled();
    expect(f.api.relink).not.toHaveBeenCalled();
  } finally {
    f.restore();
  }
});

test("the real authenticated reader preserves transport failure for explicit preview retry", async () => {
  const { createAssetsApi } = await import("../../clients/media/assetsApi");
  const fetchImpl = vi
    .fn<typeof fetch>()
    .mockRejectedValueOnce(new TypeError("synthetic fetch transport failure"))
    .mockResolvedValueOnce(new Response(previewBlob(), { status: 200 }));
  const api = createAssetsApi("preview-test", fetchImpl);
  const upload = vi.spyOn(api, "upload");
  const relink = vi.spyOn(api, "relink");
  const f = previewFixture(null);
  try {
    await act(async () => f.rerender(f.draw(api, retryAsset)));
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Retry preview" })).toBeTruthy();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry preview" })));
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    for (const [path, init] of fetchImpl.mock.calls) {
      expect(path).toBe(`/api/v2/assets/${retryAsset.asset_id}`);
      expect(init?.method).toBe("GET");
      expect(init?.headers).toEqual({ authorization: "Bearer preview-test" });
      expect(init?.signal).toBeInstanceOf(AbortSignal);
    }
    expect(fixtureAt(fetchImpl.mock.calls, 0)[1]?.signal).not.toBe(
      fixtureAt(fetchImpl.mock.calls, 1)[1]?.signal,
    );
    expect(fixtureAt(fetchImpl.mock.calls, 1)[1]?.signal?.aborted).toBe(false);
    expect(
      screen
        .getByRole("button", { name: `Preview: ${retryAsset.name}` })
        .querySelector("img")
        ?.getAttribute("src"),
    ).toBe("blob:preview-1");
    expect(f.calls).toHaveLength(0);
    expect(upload).not.toHaveBeenCalled();
    expect(relink).not.toHaveBeenCalled();
    expect(f.create).toHaveBeenCalledTimes(1);
  } finally {
    f.restore();
    upload.mockRestore();
    relink.mockRestore();
  }
});

test.each(["pending", "ready", "error"] as const)(
  "asset replacement discards the previous %s result and its callback",
  async (state) => {
    const f = previewFixture();
    const next = { ...retryAsset, asset_id: "123e4567-e89b-12d3-a456-426614174001" };
    try {
      if (state === "ready") await act(async () => fixtureAt(f.calls, 0).resolve(previewBlob()));
      if (state === "error")
        await act(async () => fixtureAt(f.calls, 0).reject(new AssetsApiError(0, "timeout")));
      const staleRetry = capturedRetry();
      previewWitness.views.length = 0;
      f.rerender(f.draw(f.api, next));
      expect(previewWitness.views.length).toBeGreaterThan(0);
      expect(
        previewWitness.views.every(
          (view) => view.id === next.asset_id && view.state === "loading" && view.url === null,
        ),
      ).toBe(true);
      expect(fixtureAt(f.calls, 0).signal?.aborted).toBe(true);
      expect(fixtureAt(f.calls, 1).id).toBe(next.asset_id);
      expect(fixtureAt(f.calls, 1).signal?.aborted).toBe(false);
      act(() => staleRetry());
      expect(f.calls).toHaveLength(2);
      if (state === "pending")
        await act(async () => fixtureAt(f.calls, 0).reject(new TypeError("obsolete failure")));
      expect(screen.getByRole("status").textContent).toBe("Loading preview…");
      await act(async () => fixtureAt(f.calls, 1).resolve(previewBlob()));
      expect(f.create).toHaveBeenCalledTimes(state === "ready" ? 2 : 1);
      expect(f.revoke).toHaveBeenCalledTimes(state === "ready" ? 1 : 0);
      expect(f.api.upload).not.toHaveBeenCalled();
      expect(f.api.relink).not.toHaveBeenCalled();
    } finally {
      f.restore();
    }
  },
);

test.each(["success", "retry"] as const)(
  "an open preview releases removed modal ownership and captures API successor %s",
  async (outcome) => {
    const f = previewFixture();
    const next = previewReads();
    const outside = document.createElement("button");
    outside.textContent = "Outside preview";
    document.body.append(outside);
    const retained = document.createElement("button");
    retained.setAttribute("inert", "retained");
    document.body.append(retained);
    const windowAdd = vi.spyOn(window, "addEventListener");
    const windowRemove = vi.spyOn(window, "removeEventListener");
    const documentAdd = vi.spyOn(document, "addEventListener");
    const documentRemove = vi.spyOn(document, "removeEventListener");
    const keyListeners = () =>
      windowAdd.mock.calls.filter(([type, , capture]) => type === "keydown" && capture === true);
    const focusListeners = () =>
      documentAdd.mock.calls.filter(([type, , capture]) => type === "focusin" && capture === true);
    try {
      await act(async () => fixtureAt(f.calls, 0).resolve(previewBlob()));
      const opener = screen.getByRole("button", { name: `Preview: ${retryAsset.name}` });
      opener.focus();
      fireEvent.click(opener);
      const oldDialog = screen.getByRole("dialog");
      const oldClose = screen.getByRole("button", { name: "Close preview" });
      expect(document.activeElement).toBe(oldClose);
      expect(oldDialog.querySelector("img")?.getAttribute("src")).toBe("blob:preview-1");
      expect(outside.hasAttribute("inert")).toBe(true);
      expect(keyListeners()).toHaveLength(1);
      expect(focusListeners()).toHaveLength(1);
      const oldKey = fixtureAt(keyListeners(), 0)[1];
      const oldFocus = fixtureAt(focusListeners(), 0)[1];
      f.rerender(f.draw(next.api, retryAsset));
      expect(screen.queryByRole("dialog")).toBeNull();
      expect(outside.hasAttribute("inert")).toBe(false);
      expect(retained.getAttribute("inert")).toBe("retained");
      expect(windowRemove).toHaveBeenCalledWith("keydown", oldKey, true);
      expect(documentRemove).toHaveBeenCalledWith("focusin", oldFocus, true);
      expect(oldDialog.isConnected).toBe(false);
      expect(oldClose.isConnected).toBe(false);
      expect(fixtureAt(f.calls, 0).signal?.aborted).toBe(true);
      expect(next.calls).toHaveLength(1);
      expect(fixtureAt(next.calls, 0).id).toBe(retryAsset.asset_id);
      expect(fixtureAt(next.calls, 0).signal?.aborted).toBe(false);
      expect(f.revoke).toHaveBeenCalledExactlyOnceWith("blob:preview-1");
      outside.focus();
      expect(document.activeElement).toBe(outside);
      for (const key of ["Tab", "Escape"]) {
        const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
        window.dispatchEvent(event);
        expect(event.defaultPrevented).toBe(false);
      }
      expect(screen.getByRole("status").textContent).toBe("Loading preview…");
      if (outcome === "retry") {
        await act(async () => fixtureAt(next.calls, 0).reject(new AssetsApiError(0, "timeout")));
        expect(screen.getByText("Attachment could not be loaded")).toBeTruthy();
        expect(screen.queryByRole("dialog")).toBeNull();
        expect(outside.hasAttribute("inert")).toBe(false);
        expect(retained.getAttribute("inert")).toBe("retained");
        expect(keyListeners()).toHaveLength(1);
        expect(focusListeners()).toHaveLength(1);
        fireEvent.click(screen.getByRole("button", { name: "Retry preview" }));
        expect(next.calls).toHaveLength(2);
        expect(screen.queryByRole("dialog")).toBeNull();
        expect(outside.hasAttribute("inert")).toBe(false);
        outside.focus();
      }
      await act(async () =>
        fixtureAt(next.calls, outcome === "retry" ? 1 : 0).resolve(previewBlob()),
      );
      const currentDialog = screen.getByRole("dialog");
      const currentClose = screen.getByRole("button", { name: "Close preview" });
      expect(currentDialog).not.toBe(oldDialog);
      expect(currentDialog.querySelector("img")?.getAttribute("src")).toBe("blob:preview-2");
      expect(currentDialog.querySelector("img")?.getAttribute("alt")).toBe(retryAsset.name);
      expect(document.activeElement).toBe(currentClose);
      expect(outside.hasAttribute("inert")).toBe(true);
      expect(keyListeners()).toHaveLength(2);
      expect(focusListeners()).toHaveLength(2);
      outside.focus();
      expect(document.activeElement).toBe(currentClose);
      fireEvent.keyDown(window, { key: "Tab", shiftKey: true });
      expect(document.activeElement).toBe(currentClose);
      if (outcome === "success") fireEvent.keyDown(window, { key: "Escape" });
      else fireEvent.click(currentClose);
      expect(screen.queryByRole("dialog")).toBeNull();
      expect(outside.hasAttribute("inert")).toBe(false);
      expect(retained.getAttribute("inert")).toBe("retained");
      expect(document.activeElement).toBe(outside);
      expect(windowRemove).toHaveBeenCalledWith("keydown", fixtureAt(keyListeners(), 1)[1], true);
      expect(documentRemove).toHaveBeenCalledWith(
        "focusin",
        fixtureAt(focusListeners(), 1)[1],
        true,
      );
      expect(f.calls).toHaveLength(1);
      expect(next.calls).toHaveLength(outcome === "retry" ? 2 : 1);
      expect(f.api.upload).not.toHaveBeenCalled();
      expect(f.api.relink).not.toHaveBeenCalled();
      expect(next.api.upload).not.toHaveBeenCalled();
      expect(next.api.relink).not.toHaveBeenCalled();
      expect(f.create).toHaveBeenCalledTimes(2);
      expect(f.revoke).toHaveBeenCalledExactlyOnceWith("blob:preview-1");
      f.unmount();
      expect(fixtureAt(next.calls, outcome === "retry" ? 1 : 0).signal?.aborted).toBe(true);
      expect(f.revoke.mock.calls.map(([url]) => url)).toEqual(["blob:preview-1", "blob:preview-2"]);
    } finally {
      f.restore();
      outside.remove();
      retained.remove();
      windowAdd.mockRestore();
      windowRemove.mockRestore();
      documentAdd.mockRestore();
      documentRemove.mockRestore();
    }
  },
);
