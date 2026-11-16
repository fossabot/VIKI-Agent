import { afterEach, expect, test, vi } from "vitest";
import { createAssetsApi, uploadName } from "./assetsApi";

function stubUploadResponse(status: number, responseText: string): void {
  class FakeXHR {
    upload = { onprogress: () => {} };
    status = status;
    responseText = responseText;
    onload = () => {};
    onerror = () => {};
    ontimeout = () => {};
    onabort = () => {};
    open = vi.fn();
    setRequestHeader = vi.fn();
    abort = vi.fn();
    send = vi.fn(() => this.onload());
  }
  vi.stubGlobal("XMLHttpRequest", FakeXHR);
}

function uploadedFile(): File {
  return new File([new Uint8Array([1])], "a.png", { type: "image/png" });
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("read authenticates with Bearer and keeps asset id in the path only", async () => {
  const fetchImpl = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    expect(String(url)).toBe("/api/v2/assets/123e4567-e89b-12d3-a456-426614174000");
    expect(init?.headers).toEqual({ authorization: "Bearer secret" });
    return new Response(new Blob(["png"], { type: "image/png" }), { status: 200 });
  });
  const blob = await createAssetsApi("secret", fetchImpl).read(
    "123e4567-e89b-12d3-a456-426614174000",
  );
  expect(blob).toBeInstanceOf(Blob);
});

test("invalid identifiers and pre-aborted reads never reach fetch", async () => {
  const request = vi.fn();
  const api = createAssetsApi("secret", request);
  await expect(api.read("a/b")).rejects.toMatchObject({ code: "invalid_asset" });
  const controller = new AbortController();
  controller.abort();
  await expect(
    api.read("123e4567-e89b-12d3-a456-426614174000", controller.signal),
  ).rejects.toMatchObject({ code: "cancelled" });
  expect(request).not.toHaveBeenCalled();
});

test("upload sends raw bytes, encoded display name and auth header", async () => {
  let instance: FakeXHR | undefined;
  class FakeXHR {
    upload = { onprogress: () => {} };
    status = 201;
    responseText = JSON.stringify({
      asset_id: "123e4567-e89b-12d3-a456-426614174000",
      media_type: "image/png",
      byte_size: 2,
      width: 2,
      height: 1,
      name: "a b.png",
    });
    onload = () => {};
    onerror = () => {};
    onabort = () => {};
    open = vi.fn();
    setRequestHeader = vi.fn();
    abort = vi.fn();
    send = vi.fn(() => this.onload());
    constructor() {
      instance = this;
    }
  }
  vi.stubGlobal("XMLHttpRequest", FakeXHR);
  const api = createAssetsApi("secret");
  const file = new File([new Uint8Array([1, 2])], "a b.png", { type: "image/png" });
  const result = await api.upload(file);
  expect(result.name).toBe("a b.png");
  expect(instance).toBeDefined();
  expect(instance?.open).toHaveBeenCalledWith("POST", "/api/v2/assets");
  expect(instance?.setRequestHeader).toHaveBeenCalledWith("Authorization", "Bearer secret");
  expect(instance?.setRequestHeader).toHaveBeenCalledWith("Content-Type", "image/png");
  expect(instance?.setRequestHeader).toHaveBeenCalledWith("X-File-Name", "a%20b.png");
  expect(instance?.send).toHaveBeenCalledWith(file);
});

function uploadedHeaders(): string[][] {
  const calls: string[][] = [];
  class FakeXHR {
    upload = { onprogress: () => {} };
    status = 201;
    responseText = JSON.stringify({
      asset_id: "123e4567-e89b-12d3-a456-426614174000",
      media_type: "image/png",
      byte_size: 2,
      width: 2,
      height: 1,
      name: "a.png",
    });
    onload = () => {};
    onerror = () => {};
    onabort = () => {};
    open = vi.fn();
    setRequestHeader = vi.fn((name: string, value: string) => calls.push([name, value]));
    abort = vi.fn();
    send = vi.fn(() => this.onload());
  }
  vi.stubGlobal("XMLHttpRequest", FakeXHR);
  return calls;
}

test("upload sets the purpose header only when asked", async () => {
  const withPurpose = uploadedHeaders();
  await createAssetsApi("secret").upload(uploadedFile(), { purpose: "avatar" });
  expect(withPurpose).toContainEqual(["X-Viki-Asset-Purpose", "avatar"]);

  const withoutPurpose = uploadedHeaders();
  await createAssetsApi("secret").upload(uploadedFile());
  expect(withoutPurpose.some(([name]) => name === "X-Viki-Asset-Purpose")).toBe(false);
});

test("upload keeps a code allowlisted for its own response status", async () => {
  const cases: ReadonlyArray<readonly [number, string]> = [
    [400, "invalid_name"],
    [400, "invalid_body"],
    [400, "empty"],
    [400, "incomplete"],
    [400, "disconnected"],
    [400, "invalid_request"],
    [400, "invalid_purpose"],
    [401, "unauthorized"],
    [403, "forbidden_origin"],
    [408, "intake_timeout"],
    [413, "too_large"],
    [415, "decode_failed"],
    [415, "invalid_representation"],
    [429, "capacity"],
    [429, "overloaded"],
    [503, "decoder_unavailable"],
    [503, "unavailable"],
    [503, "worker_timeout"],
    [503, "worker_unavailable"],
    [503, "corrupt"],
    [503, "protection"],
    [503, "busy"],
  ];
  for (const [status, code] of cases) {
    stubUploadResponse(status, JSON.stringify({ error: { code } }));
    await expect(createAssetsApi("secret").upload(uploadedFile())).rejects.toMatchObject({
      status,
      code,
    });
  }
});

test("upload falls back to upload_failed off-allowlist, unknown, malformed or oversize bodies", async () => {
  stubUploadResponse(500, JSON.stringify({ error: { code: "invalid_name" } }));
  await expect(createAssetsApi("secret").upload(uploadedFile())).rejects.toMatchObject({
    status: 500,
    code: "upload_failed",
  });
  stubUploadResponse(400, JSON.stringify({ error: { code: "not_a_real_code" } }));
  await expect(createAssetsApi("secret").upload(uploadedFile())).rejects.toMatchObject({
    status: 400,
    code: "upload_failed",
  });
  stubUploadResponse(400, "not json");
  await expect(createAssetsApi("secret").upload(uploadedFile())).rejects.toMatchObject({
    status: 400,
    code: "upload_failed",
  });
  stubUploadResponse(400, JSON.stringify({ error: { code: "invalid_name" } }).padEnd(4097, " "));
  await expect(createAssetsApi("secret").upload(uploadedFile())).rejects.toMatchObject({
    status: 400,
    code: "upload_failed",
  });
  stubUploadResponse(400, JSON.stringify({}));
  await expect(createAssetsApi("secret").upload(uploadedFile())).rejects.toMatchObject({
    status: 400,
    code: "upload_failed",
  });
});

test("upload keeps its network and timeout handling outside the response-code path", async () => {
  class NetworkXHR {
    upload = { onprogress: () => {} };
    open = vi.fn();
    setRequestHeader = vi.fn();
    abort = vi.fn();
    onload = () => {};
    onerror = () => {};
    ontimeout = () => {};
    onabort = () => {};
    send = vi.fn(() => this.onerror());
  }
  vi.stubGlobal("XMLHttpRequest", NetworkXHR);
  await expect(createAssetsApi("secret").upload(uploadedFile())).rejects.toMatchObject({
    status: 0,
    code: "network_error",
  });

  class TimeoutXHR {
    upload = { onprogress: () => {} };
    open = vi.fn();
    setRequestHeader = vi.fn();
    abort = vi.fn();
    onload = () => {};
    onerror = () => {};
    ontimeout = () => {};
    onabort = () => {};
    send = vi.fn(() => this.ontimeout());
  }
  vi.stubGlobal("XMLHttpRequest", TimeoutXHR);
  await expect(createAssetsApi("secret").upload(uploadedFile())).rejects.toMatchObject({
    status: 0,
    code: "timeout",
  });

  const controller = new AbortController();
  controller.abort();
  await expect(
    createAssetsApi("secret").upload(uploadedFile(), { signal: controller.signal }),
  ).rejects.toMatchObject({ status: 0, code: "cancelled" });
});

test("uploadName trims to 512 UTF-8 bytes, keeps a short extension, and repairs lone surrogates", () => {
  expect(uploadName("a\uD800b.png")).toBe("a�b.png");
  const trimmed = uploadName(`${"あ".repeat(171)}.png`);
  expect(new TextEncoder().encode(trimmed).byteLength).toBeLessThanOrEqual(512);
  expect(trimmed.endsWith(".png")).toBe(true);
});

test("upload encodes a lone-surrogate file name into a well-formed header", async () => {
  let instance: { setRequestHeader: ReturnType<typeof vi.fn> } | undefined;
  class FakeXHR {
    upload = { onprogress: () => {} };
    status = 201;
    responseText = JSON.stringify({
      asset_id: "123e4567-e89b-12d3-a456-426614174000",
      media_type: "image/png",
      byte_size: 2,
      width: 2,
      height: 1,
      name: "a.png",
    });
    onload = () => {};
    onerror = () => {};
    onabort = () => {};
    open = vi.fn();
    setRequestHeader = vi.fn();
    abort = vi.fn();
    send = vi.fn(() => this.onload());
    constructor() {
      instance = this;
    }
  }
  vi.stubGlobal("XMLHttpRequest", FakeXHR);
  const file = new File([new Uint8Array([1, 2])], "a\uD800b.png", { type: "image/png" });
  await createAssetsApi("secret").upload(file);
  expect(instance?.setRequestHeader).toHaveBeenCalledWith(
    "X-File-Name",
    encodeURIComponent("a�b.png"),
  );
});

test("upload trims a 600-byte file name before encoding the X-File-Name header", async () => {
  let instance: { setRequestHeader: ReturnType<typeof vi.fn> } | undefined;
  class FakeXHR {
    upload = { onprogress: () => {} };
    status = 201;
    responseText = JSON.stringify({
      asset_id: "123e4567-e89b-12d3-a456-426614174000",
      media_type: "image/png",
      byte_size: 2,
      width: 2,
      height: 1,
      name: "a.png",
    });
    onload = () => {};
    onerror = () => {};
    onabort = () => {};
    open = vi.fn();
    setRequestHeader = vi.fn();
    abort = vi.fn();
    send = vi.fn(() => this.onload());
    constructor() {
      instance = this;
    }
  }
  vi.stubGlobal("XMLHttpRequest", FakeXHR);
  const longName = `${"あ".repeat(200)}.png`;
  expect(new TextEncoder().encode(longName).byteLength).toBeGreaterThanOrEqual(600);
  const file = new File([new Uint8Array([1, 2])], longName, { type: "image/png" });
  await createAssetsApi("secret").upload(file);
  const call = instance?.setRequestHeader.mock.calls.find(([key]) => key === "X-File-Name");
  const decoded = decodeURIComponent(String(call?.[1]));
  expect(new TextEncoder().encode(decoded).byteLength).toBe(
    new TextEncoder().encode(uploadName(longName)).byteLength,
  );
  expect(new TextEncoder().encode(decoded).byteLength).toBeLessThanOrEqual(512);
});

test("relink posts to the asset's route with auth and content type, and resolves on 204", async () => {
  let instance: FakeXHR | undefined;
  class FakeXHR {
    status = 204;
    responseText = "";
    onload = () => {};
    onerror = () => {};
    ontimeout = () => {};
    onabort = () => {};
    open = vi.fn();
    setRequestHeader = vi.fn();
    abort = vi.fn();
    send = vi.fn(() => this.onload());
    constructor() {
      instance = this;
    }
  }
  vi.stubGlobal("XMLHttpRequest", FakeXHR);
  const file = new File([new Uint8Array([1, 2])], "a.png", { type: "image/png" });
  await createAssetsApi("secret").relink?.("123e4567-e89b-12d3-a456-426614174000", file);
  expect(instance?.open).toHaveBeenCalledWith(
    "POST",
    "/api/v2/assets/123e4567-e89b-12d3-a456-426614174000/relink",
  );
  expect(instance?.setRequestHeader).toHaveBeenCalledWith("Authorization", "Bearer secret");
  expect(instance?.setRequestHeader).toHaveBeenCalledWith("Content-Type", "image/png");
  expect(instance?.setRequestHeader).not.toHaveBeenCalledWith("X-File-Name", expect.anything());
  expect(instance?.setRequestHeader).toHaveBeenCalledTimes(2);
});

test("relink keeps not_found and asset_mismatch/asset_changed alongside the upload allowlist", async () => {
  const cases: ReadonlyArray<readonly [number, string]> = [
    [404, "not_found"],
    [409, "asset_mismatch"],
    [409, "asset_changed"],
    [415, "decode_failed"],
  ];
  for (const [status, code] of cases) {
    stubUploadResponse(status, JSON.stringify({ error: { code } }));
    await expect(
      createAssetsApi("secret").relink?.("123e4567-e89b-12d3-a456-426614174000", uploadedFile()),
    ).rejects.toMatchObject({ status, code });
  }
  stubUploadResponse(400, JSON.stringify({ error: { code: "not_found" } }));
  await expect(
    createAssetsApi("secret").relink?.("123e4567-e89b-12d3-a456-426614174000", uploadedFile()),
  ).rejects.toMatchObject({ status: 400, code: "upload_failed" });
});

test("non-2xx reads fail with a stable error", async () => {
  const fetchImpl = vi.fn(async () => new Response("", { status: 413 }));
  await expect(
    createAssetsApi("secret", fetchImpl).read("123e4567-e89b-12d3-a456-426614174000"),
  ).rejects.toMatchObject({ status: 413 });
  expect(fetchImpl).toHaveBeenCalledOnce();
});

test("provider attachment capabilities authenticate and reject unsafe or malformed bounds", async () => {
  const valid = {
    provider: "claude",
    media_types: ["image/png", "image/jpeg", "image/webp"],
    max_files: 10,
    max_file_bytes: 5 << 20,
    max_turn_bytes: 20 << 20,
    max_dimension: 8000,
  };
  const request = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
    expect(init?.headers).toEqual({ authorization: "Bearer secret" });
    return Response.json(valid);
  });
  expect(await createAssetsApi("secret", request).capabilities?.()).toEqual({
    maxAttachments: 10,
    maxFileBytes: 5 << 20,
    maxTurnBytes: 20 << 20,
    maxDimension: 8000,
  });
  expect(request.mock.calls[0]?.[0]).toBe("/api/v2/attachment-capabilities");
  const unsafe = createAssetsApi(
    "secret",
    vi.fn(async () => Response.json({ ...valid, max_files: 10000 })),
  );
  await expect(unsafe.capabilities?.()).rejects.toMatchObject({ code: "invalid_response" });
});
