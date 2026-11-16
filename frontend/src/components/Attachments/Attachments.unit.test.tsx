import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { type AssetsApi, AssetsApiError } from "../../clients/media/assetsApi";
import type { TurnAttachment } from "../../turnInput";
import {
  AttachmentPicker,
  AttachmentStrip,
  useAssetPreview,
  useAttachmentPicker,
} from "./Attachments";

const file = (name = "screen.png") =>
  new File([new Uint8Array([1, 2])], name, { type: "image/png" });
const asset = {
  asset_id: "123e4567-e89b-12d3-a456-426614174000",
  media_type: "image/png" as const,
  byte_size: 2,
  width: 2,
  height: 1,
  name: "screen.png",
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

test("picker forwards selected files, pasted images and dropped files", () => {
  const onFiles = vi.fn();
  const { container } = render(<AttachmentPicker onFiles={onFiles} />);
  const input = container.querySelector("input[type=file]") as HTMLInputElement;
  const host = screen.getByRole("button", { name: "Add attachment" }).parentElement;
  if (!host) throw new Error("picker host missing");
  fireEvent.change(input, { target: { files: [file()] } });
  expect(onFiles).toHaveBeenCalledWith([expect.any(File)], "picker");
  fireEvent.paste(host, {
    clipboardData: {
      items: [{ kind: "file", type: "image/png", getAsFile: () => file("paste.png") }],
    },
  });
  expect(onFiles).toHaveBeenCalledTimes(2);
  fireEvent.drop(host, {
    dataTransfer: { files: [file("drop.png")] },
  });
  expect(onFiles).toHaveBeenCalledTimes(3);
});

function Harness({ api, destinationId }: { api: AssetsApi; destinationId: string }) {
  const state = useAttachmentPicker({ api, destinationId });
  return (
    <>
      <button type="button" data-testid="add" onClick={() => state.addFiles([file()])}>
        add
      </button>
      <button
        type="button"
        data-testid="cancel-second"
        onClick={() => {
          const item = state.attachments[1];
          if (item) state.cancel(item.localId);
        }}
      >
        cancel second
      </button>
      <AttachmentPicker onFiles={state.addFiles} />
      <AttachmentStrip
        attachments={state.attachments}
        onRemove={state.remove}
        onRetry={state.retry}
        onCancel={state.cancel}
      />
    </>
  );
}

test("ordered uploads keep retry and stale destination results out", async () => {
  let resolve!: (value: typeof asset) => void;
  const api: AssetsApi = {
    upload: vi.fn(
      (_file, _options) =>
        new Promise<typeof asset>((r) => {
          resolve = r;
        }),
    ),
    read: vi.fn(),
  };
  const { rerender } = render(<Harness api={api} destinationId="a" />);
  await act(async () => {
    fireEvent.click(screen.getByTestId("add"));
    await Promise.resolve();
  });
  rerender(<Harness api={api} destinationId="b" />);
  await act(async () => resolve(asset));
  expect(screen.queryByText("Ready")).toBeNull();
});

test("a destination change drops the old batch instead of letting it upload under the new one", async () => {
  let resolveF1!: (value: typeof asset) => void;
  const api: AssetsApi = {
    upload: vi.fn((uploaded: File) => {
      if (uploaded.name === "f1.png")
        return new Promise<typeof asset>((r) => {
          resolveF1 = r;
        });
      return Promise.resolve({ ...asset, asset_id: `${uploaded.name}-id` });
    }),
    read: vi.fn(),
  };
  function DestinationHarness({ destinationId }: { destinationId: string }) {
    const state = useAttachmentPicker({ api, destinationId });
    return (
      <>
        <button
          type="button"
          onClick={() => state.addFiles([file("f1.png"), file("f2.png"), file("f3.png")])}
        >
          add three
        </button>
        <button type="button" onClick={() => state.addFiles([file("f4.png")])}>
          add one
        </button>
        <AttachmentStrip
          attachments={state.attachments}
          onRemove={state.remove}
          onRetry={state.retry}
          onCancel={state.cancel}
        />
      </>
    );
  }
  const { rerender } = render(<DestinationHarness destinationId="a" />);
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "add three" }));
    await flush();
  });
  expect(api.upload).toHaveBeenCalledTimes(1);
  rerender(<DestinationHarness destinationId="b" />);
  expect(screen.queryByText(/upload/i)).toBeNull();
  await act(async () => {
    resolveF1(asset);
    await flush();
  });
  expect(screen.queryByText("Ready")).toBeNull();
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "add one" }));
    await flush();
  });
  expect(api.upload).toHaveBeenCalledTimes(2);
  expect(api.upload).toHaveBeenLastCalledWith(
    expect.objectContaining({ name: "f4.png" }),
    expect.anything(),
  );
  expect(screen.getAllByText("Ready").length).toBe(1);
});

test("preview creates and revokes an authenticated blob URL", async () => {
  const create = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:preview");
  const revoke = vi.spyOn(URL, "revokeObjectURL");
  const api: AssetsApi = {
    upload: vi.fn(async () => asset),
    read: vi.fn(async () => new Blob(["x"], { type: "image/png" })),
  };
  function Preview(): ReactElement {
    const url = useAssetPreview(api, asset);
    return <span>{url}</span>;
  }
  const { unmount } = render(<Preview />);
  await act(async () => Promise.resolve());
  expect(create).toHaveBeenCalled();
  unmount();
  expect(revoke).toHaveBeenCalledWith("blob:preview");
});

test("strip exposes retry, cancel and remove actions", () => {
  const item: TurnAttachment = {
    localId: "1",
    file: file(),
    name: "x.png",
    byteSize: 2,
    mediaType: "image/png",
    status: "error",
    progress: null,
    asset: null,
    error: null,
    failure: { code: "upload_failed", retryable: true },
    source: "picker",
    modified_at: null,
  };
  const retry = vi.fn(),
    remove = vi.fn(),
    cancel = vi.fn();
  render(
    <AttachmentStrip attachments={[item]} onRetry={retry} onRemove={remove} onCancel={cancel} />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  fireEvent.click(screen.getByRole("button", { name: "Remove" }));
  expect(retry).toHaveBeenCalledWith("1");
  expect(remove).toHaveBeenCalledWith("1");
});

test("invalid files report a rejection instead of disappearing silently", () => {
  const api: AssetsApi = { upload: vi.fn(), read: vi.fn() };
  const onReject = vi.fn();
  function RejectHarness(): ReactElement {
    const state = useAttachmentPicker({ api, destinationId: "a", onReject });
    return (
      <button
        type="button"
        onClick={() => state.addFiles([new File(["x"], "bad.txt", { type: "text/plain" })])}
      >
        reject
      </button>
    );
  }
  render(<RejectHarness />);
  fireEvent.click(screen.getByRole("button", { name: "reject" }));
  expect(onReject).toHaveBeenCalledWith(expect.stringContaining("type"));
  expect(api.upload).not.toHaveBeenCalled();
});

test("cancelling a queued item prevents its upload", async () => {
  let resolveFirst!: (value: typeof asset) => void;
  const api: AssetsApi = {
    upload: vi.fn(
      (_file, _options) =>
        new Promise<typeof asset>((resolve) => {
          if (!resolveFirst) resolveFirst = resolve;
        }),
    ),
    read: vi.fn(),
  };
  function QueueHarness(): ReactElement {
    const state = useAttachmentPicker({ api, destinationId: "a" });
    return (
      <>
        <button type="button" onClick={() => state.addFiles([file("one.png"), file("two.png")])}>
          add two
        </button>
        <button
          type="button"
          onClick={() => {
            const item = state.attachments[1];
            if (item) state.cancel(item.localId);
          }}
        >
          cancel queued
        </button>
      </>
    );
  }
  render(<QueueHarness />);
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "add two" }));
    await Promise.resolve();
  });
  fireEvent.click(screen.getByRole("button", { name: "cancel queued" }));
  await act(async () => resolveFirst(asset));
  await act(async () => Promise.resolve());
  expect(api.upload).toHaveBeenCalledTimes(1);
});

test("unmounting while the first of three queued uploads is in flight sends only that one", async () => {
  let resolveFirst!: (value: typeof asset) => void;
  let calls = 0;
  const api: AssetsApi = {
    upload: vi.fn(
      () =>
        new Promise<typeof asset>((resolve) => {
          calls += 1;
          if (calls === 1) resolveFirst = resolve;
        }),
    ),
    read: vi.fn(),
  };
  function ThreeHarness(): ReactElement {
    const state = useAttachmentPicker({ api, destinationId: "a" });
    return (
      <button
        type="button"
        onClick={() => state.addFiles([file("one.png"), file("two.png"), file("three.png")])}
      >
        add three
      </button>
    );
  }
  const { unmount } = render(<ThreeHarness />);
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "add three" }));
    await flush();
  });
  expect(api.upload).toHaveBeenCalledTimes(1);
  unmount();
  await act(async () => {
    resolveFirst(asset);
    await flush();
  });
  expect(api.upload).toHaveBeenCalledTimes(1);
});

function readyItem(localId: string, assetId: string): TurnAttachment {
  return {
    localId,
    file: file(),
    name: "screen.png",
    byteSize: 2,
    mediaType: "image/png",
    status: "ready",
    progress: 100,
    asset: { ...asset, asset_id: assetId },
    error: null,
    failure: null,
    source: "history",
    modified_at: null,
  };
}

test("the strip's thumbnail read reports a 410 or 404 asset, and no other failure", async () => {
  const gone = "123e4567-e89b-12d3-a456-426614174001";
  const missing = "123e4567-e89b-12d3-a456-426614174002";
  const otherFailure = "123e4567-e89b-12d3-a456-426614174003";
  const api: AssetsApi = {
    upload: vi.fn(),
    read: vi.fn(async (assetId: string) => {
      if (assetId === gone) throw new AssetsApiError(410, "expired");
      if (assetId === missing) throw new AssetsApiError(404, "read_failed");
      throw new AssetsApiError(500, "read_failed");
    }),
  };
  const onUnavailable = vi.fn();
  render(
    <AttachmentStrip
      api={api}
      attachments={[
        readyItem("gone", gone),
        readyItem("missing", missing),
        readyItem("other", otherFailure),
      ]}
      onRemove={vi.fn()}
      onRetry={vi.fn()}
      onCancel={vi.fn()}
      onUnavailable={onUnavailable}
    />,
  );
  await act(async () => Promise.resolve());
  expect(onUnavailable).toHaveBeenCalledWith("gone");
  expect(onUnavailable).toHaveBeenCalledWith("missing");
  expect(onUnavailable).not.toHaveBeenCalledWith("other");
});

test("the chip shows the server's media type, not the browser's", () => {
  const item = readyItem("1", "123e4567-e89b-12d3-a456-426614174009");
  item.mediaType = "application/octet-stream";
  item.asset = {
    ...asset,
    asset_id: "123e4567-e89b-12d3-a456-426614174009",
    media_type: "image/png",
  };
  render(
    <AttachmentStrip
      attachments={[item]}
      onRetry={vi.fn()}
      onRemove={vi.fn()}
      onCancel={vi.fn()}
    />,
  );
  expect(screen.getByText(/image\/png/)).toBeTruthy();
  expect(screen.queryByText(/application\/octet-stream/)).toBeNull();
});

test("an unavailable item shows the fixed label, offers remove and no retry", () => {
  const item: TurnAttachment = {
    localId: "1",
    file: file(),
    name: "x.png",
    byteSize: 2,
    mediaType: "image/png",
    status: "error",
    progress: null,
    asset: null,
    error: "unavailable",
    failure: null,
    source: "history",
    modified_at: null,
  };
  render(
    <AttachmentStrip
      attachments={[item]}
      onRetry={vi.fn()}
      onRemove={vi.fn()}
      onCancel={vi.fn()}
    />,
  );
  expect(screen.getByText("Unavailable")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  expect(screen.getByRole("button", { name: "Remove" })).toBeTruthy();
});

function failedItem(code: string, retryable: boolean): TurnAttachment {
  return {
    localId: "1",
    file: file(),
    name: "x.png",
    byteSize: 2,
    mediaType: "image/png",
    status: "error",
    progress: null,
    asset: null,
    error: null,
    failure: { code, retryable },
    source: "picker",
    modified_at: null,
  };
}

test("capacity shows its copy and no Retry", () => {
  render(
    <AttachmentStrip
      attachments={[failedItem("capacity", false)]}
      onRetry={vi.fn()}
      onRemove={vi.fn()}
      onCancel={vi.fn()}
    />,
  );
  expect(screen.getByText(/Attachment storage is full/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
});

test("decoder_unavailable's copy names uv sync", () => {
  render(
    <AttachmentStrip
      attachments={[failedItem("decoder_unavailable", false)]}
      onRetry={vi.fn()}
      onRemove={vi.fn()}
      onCancel={vi.fn()}
    />,
  );
  expect(screen.getByText(/uv sync/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
});

async function flush(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

test("busy shows Retry and a click re-uploads", async () => {
  let attempt = 0;
  const api: AssetsApi = {
    upload: vi.fn(async () => {
      attempt += 1;
      if (attempt === 1) throw new AssetsApiError(503, "busy");
      return asset;
    }),
    read: vi.fn(),
  };
  function RetryHarness() {
    const state = useAttachmentPicker({ api, destinationId: "a" });
    return (
      <>
        <button type="button" onClick={() => state.addFiles([file()])}>
          add
        </button>
        <AttachmentStrip
          attachments={state.attachments}
          onRemove={state.remove}
          onRetry={state.retry}
          onCancel={state.cancel}
        />
      </>
    );
  }
  render(<RetryHarness />);
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "add" }));
    await flush();
  });
  expect(screen.getByText("The app's data was busy.")).toBeTruthy();
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await flush();
  });
  expect(screen.getByText("Ready")).toBeTruthy();
  expect(api.upload).toHaveBeenCalledTimes(2);
});

// Every code the strip's copy table lists, split by whether the card marks it retryable.
const RETRYABLE_REFUSAL_CODES = [
  "overloaded",
  "busy",
  "worker_timeout",
  "intake_timeout",
  "worker_unavailable",
  "incomplete",
  "disconnected",
  "unavailable",
  "network_error",
  "timeout",
  "invalid_response",
  "upload_failed",
];
const NON_RETRYABLE_REFUSAL_CODES = [
  "capacity",
  "decode_failed",
  "decoder_unavailable",
  "invalid_name",
  "too_large",
  "empty",
  "protection",
  "corrupt",
  "unauthorized",
  "forbidden_origin",
  "invalid_request",
  "invalid_body",
  "invalid_representation",
];

function CodeHarness({ api }: { api: AssetsApi }): ReactElement {
  const state = useAttachmentPicker({ api, destinationId: "a" });
  const first = state.attachments[0];
  return (
    <>
      <button type="button" onClick={() => state.addFiles([file()])}>
        add
      </button>
      <button type="button" onClick={() => first && state.retry(first.localId)}>
        force retry
      </button>
      <AttachmentStrip
        attachments={state.attachments}
        onRemove={state.remove}
        onRetry={state.retry}
        onCancel={state.cancel}
      />
    </>
  );
}

test("Retry shows for exactly the retryable codes, and retry() refuses a non-retryable item", async () => {
  for (const code of [...RETRYABLE_REFUSAL_CODES, ...NON_RETRYABLE_REFUSAL_CODES]) {
    const api: AssetsApi = {
      upload: vi.fn(async () => {
        throw new AssetsApiError(500, code);
      }),
      read: vi.fn(),
    };
    const { unmount } = render(<CodeHarness api={api} />);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "add" }));
      await flush();
    });
    const retryButton = screen.queryByRole("button", { name: "Retry" });
    if (RETRYABLE_REFUSAL_CODES.includes(code)) expect(retryButton, code).toBeTruthy();
    else expect(retryButton, code).toBeNull();
    unmount();
  }
});

test("retry() on a capacity item uploads nothing", async () => {
  const api: AssetsApi = {
    upload: vi.fn(async () => {
      throw new AssetsApiError(429, "capacity");
    }),
    read: vi.fn(),
  };
  render(<CodeHarness api={api} />);
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "add" }));
    await flush();
  });
  expect(api.upload).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "force retry" }));
  await act(async () => flush());
  expect(api.upload).toHaveBeenCalledTimes(1);
});

test("two quick addFiles batches never have two uploads in flight", async () => {
  let inFlight = 0;
  let maxInFlight = 0;
  const pending: Array<() => void> = [];
  const api: AssetsApi = {
    upload: vi.fn(
      () =>
        new Promise<typeof asset>((resolve) => {
          inFlight += 1;
          maxInFlight = Math.max(maxInFlight, inFlight);
          pending.push(() => {
            inFlight -= 1;
            resolve(asset);
          });
        }),
    ),
    read: vi.fn(),
  };
  function BatchHarness() {
    const state = useAttachmentPicker({ api, destinationId: "a" });
    return (
      <button type="button" onClick={() => state.addFiles([file()])}>
        add
      </button>
    );
  }
  render(<BatchHarness />);
  const button = screen.getByRole("button", { name: "add" });
  fireEvent.click(button);
  fireEvent.click(button);
  await act(async () => {
    await flush();
    pending.shift()?.();
    await flush();
    pending.shift()?.();
    await flush();
  });
  expect(maxInFlight).toBe(1);
  expect(api.upload).toHaveBeenCalledTimes(2);
});

test("the keep control renders only when keepInVault is given", () => {
  const row = {
    localId: "a",
    file: file(),
    name: "screen.png",
    byteSize: 2,
    mediaType: "image/png",
    status: "ready",
    progress: 100,
    asset,
    error: null,
    failure: null,
    source: "picker",
    modified_at: null,
  } as TurnAttachment;
  const props = { attachments: [row], onRemove: vi.fn(), onRetry: vi.fn(), onCancel: vi.fn() };
  const { rerender } = render(<AttachmentStrip {...props} />);
  expect(screen.queryByRole("button", { name: "Save to vault" })).toBeNull();
  rerender(
    <AttachmentStrip
      {...props}
      keepInVault={{
        api: { keepAttachment: vi.fn() },
        workspaceId: "223e4567-e89b-12d3-a456-426614174000",
        policyRevision: 1,
        rootPhysicalId: "1:2",
      }}
    />,
  );
  expect(screen.getByRole("button", { name: "Save to vault" })).toBeTruthy();
});
