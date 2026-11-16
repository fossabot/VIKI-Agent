import { afterEach, expect, test, vi } from "vitest";
import { createMessagingApi, MESSAGING_STOP_REASONS, MessagingApiError } from "./messagingApi";

const connected = {
  mattermost: { configured: true, state: "connected", restart_required: false, reason: null },
  slack: { configured: true, state: "connected", restart_required: false, reason: null },
  discord: { configured: true, state: "connected", restart_required: false, reason: null },
};

afterEach(() => vi.useRealTimers());

test("test_provider_actions_are_closed_and_authenticated", async () => {
  const fetchImpl = vi.fn(async () => Response.json(connected));
  const api = createMessagingApi("synthetic-token", fetchImpl);
  expect(await api.get()).toEqual(connected);
  expect(await api.disableMattermost()).toEqual(connected);
  for (const provider of ["mattermost", "slack", "discord"] as const) {
    expect(await api.enable(provider)).toEqual(connected);
    expect(await api.disable(provider)).toEqual(connected);
  }
  const routes = [
    "/api/v2/messaging",
    "/api/v2/messaging/mattermost/disable",
    ...["mattermost", "slack", "discord"].flatMap((provider) => [
      `/api/v2/messaging/${provider}/enable`,
      `/api/v2/messaging/${provider}/disable`,
    ]),
  ];
  expect(fetchImpl.mock.calls).toHaveLength(8);
  for (const [index, route] of routes.entries()) {
    expect(fetchImpl.mock.calls[index]).toEqual([
      route,
      {
        method: index === 0 ? "GET" : "POST",
        signal: expect.any(AbortSignal),
        headers: { authorization: "Bearer synthetic-token" },
        cache: "no-store",
        credentials: "omit",
      },
    ]);
  }
});

test.each(["not_configured", "disabled", "connecting", "connected", "stopped"])(
  "the %s state is admitted",
  async (state) => {
    const reading = {
      ...connected,
      mattermost: {
        configured: state !== "not_configured",
        state,
        restart_required: state === "stopped",
        reason: null,
      },
    };
    expect(await createMessagingApi("", async () => Response.json(reading)).get()).toEqual(reading);
  },
);

test.each([
  null,
  { ...connected, bot_token: "unexpected" },
  { ...connected, mattermost: { ...connected.mattermost, url: "unexpected" } },
  { ...connected, mattermost: { ...connected.mattermost, configured: "true" } },
  { ...connected, mattermost: { ...connected.mattermost, state: "enabled" } },
  { ...connected, mattermost: { configured: true, state: "connected" } },
  { ...connected, mattermost: { ...connected.mattermost, restart_required: 0 } },
  { mattermost: connected.mattermost },
  { ...connected, slack: { configured: false, state: "connected", restart_required: false } },
  { ...connected, discord: { configured: true, state: "not_configured", restart_required: false } },
  { ...connected, slack: { configured: false, state: "not_configured", restart_required: true } },
  { ...connected, discord: { configured: true, state: "disabled", restart_required: true } },
])("invalid status shape is refused", async (reading) => {
  const api = createMessagingApi("", async () => Response.json(reading));
  await expect(api.get()).rejects.toMatchObject({ code: "invalid_response" });
  await expect(api.disableMattermost()).rejects.toMatchObject({ code: "invalid_response" });
});

test("stop reasons are fixed codes on Stopped only", async () => {
  const withMattermost = (mattermost: Record<string, unknown>) =>
    createMessagingApi("", async () => Response.json({ ...connected, mattermost })).get();
  for (const reason of MESSAGING_STOP_REASONS)
    await expect(
      withMattermost({ configured: true, state: "stopped", restart_required: false, reason }),
    ).resolves.toBeTruthy();
  await expect(
    withMattermost({ configured: true, state: "stopped", restart_required: false, reason: null }),
  ).resolves.toBeTruthy();
  await expect(
    withMattermost({
      configured: true,
      state: "stopped",
      restart_required: false,
      reason: "not_a_real_code",
    }),
  ).rejects.toMatchObject({ code: "invalid_response" });
  await expect(
    withMattermost({
      configured: true,
      state: "connected",
      restart_required: false,
      reason: "token_rejected",
    }),
  ).rejects.toMatchObject({ code: "invalid_response" });
  await expect(
    withMattermost({
      configured: true,
      state: "stopped",
      restart_required: true,
      reason: "token_rejected",
    }),
  ).rejects.toMatchObject({ code: "invalid_response" });
  await expect(
    withMattermost({ configured: true, state: "stopped", restart_required: false }),
  ).rejects.toMatchObject({ code: "invalid_response" });
});

test.each(["unknown", "../slack", "slack/enable?token=private", null, 123])(
  "invalid provider is refused before network",
  async (provider) => {
    const fetchImpl = vi.fn(async () => Response.json(connected));
    const api = createMessagingApi("", fetchImpl);
    await expect(api.enable(provider as never)).rejects.toMatchObject({ code: "invalid_input" });
    await expect(api.disable(provider as never)).rejects.toMatchObject({ code: "invalid_input" });
    expect(fetchImpl).not.toHaveBeenCalled();
  },
);

test("cooldown retains only its bounded retry interval and original code", async () => {
  const api = createMessagingApi("", async () =>
    Response.json(
      { error: { code: "cooldown", retry_after_seconds: 7, message: "private" } },
      { status: 429 },
    ),
  );
  await expect(api.enable("slack")).rejects.toMatchObject({
    status: 429,
    code: "cooldown",
    retryAfterSeconds: 7,
  });
});

test.each([0, -1, 86401, 1.5, "7", true, null])(
  "invalid cooldown is discarded without changing error identity",
  async (retry) => {
    const api = createMessagingApi("", async () =>
      Response.json({ error: { code: "cooldown", retry_after_seconds: retry } }, { status: 429 }),
    );
    await expect(api.enable("discord")).rejects.toMatchObject({
      code: "cooldown",
      retryAfterSeconds: undefined,
    });
  },
);

test("other refusal codes do not acquire cooldown fields", async () => {
  const api = createMessagingApi("", async () =>
    Response.json({ error: { code: "unavailable", retry_after_seconds: 7 } }, { status: 503 }),
  );
  await expect(api.disable("mattermost")).rejects.toMatchObject({
    status: 503,
    code: "unavailable",
    retryAfterSeconds: undefined,
  });
});

test("existing MessagingApiError identity is preserved", async () => {
  const refusal = new MessagingApiError(503, "unavailable");
  const api = createMessagingApi("", async () => {
    throw refusal;
  });
  await expect(api.get()).rejects.toBe(refusal);
});

test("a server refusal keeps only its bounded typed code", async () => {
  const api = createMessagingApi("", async () =>
    Response.json(
      { error: { code: "unavailable", message: "private server details" } },
      { status: 503 },
    ),
  );
  const error: unknown = await api.get().catch((value: unknown) => value);
  expect(error).toBeInstanceOf(MessagingApiError);
  expect(error).toMatchObject({ status: 503, code: "unavailable" });
  expect(String(error)).not.toContain("private server details");
});

test("oversized and malformed successful responses are refused", async () => {
  for (const body of ["{", " ".repeat(65537)]) {
    const api = createMessagingApi("", async () => new Response(body));
    await expect(api.get()).rejects.toMatchObject({ code: "invalid_response" });
  }
});

const emptyConfig = {
  mattermost: { source: "none", values: {} },
  slack: { source: "none", values: {} },
  discord: { source: "none", values: {} },
};

test("config is fetched from its own route and authenticated", async () => {
  const fetchImpl = vi.fn(async () => Response.json(emptyConfig));
  const api = createMessagingApi("synthetic-token", fetchImpl);
  expect(await api.config()).toEqual(emptyConfig);
  expect(fetchImpl).toHaveBeenCalledWith("/api/v2/messaging/config", {
    method: "GET",
    signal: expect.any(AbortSignal),
    headers: { authorization: "Bearer synthetic-token" },
    cache: "no-store",
    credentials: "omit",
  });
});

test("a saved provider config carries exactly its non-secret fields", async () => {
  const saved = {
    mattermost: { source: "saved", values: { url: "https://mm.example", owner_id: "u1" } },
    slack: { source: "saved", values: { owner_id: "u2" } },
    discord: {
      source: "saved",
      values: { owner_id: "u3", client_url: "https://discord.example" },
    },
  };
  const api = createMessagingApi("", async () => Response.json(saved));
  expect(await api.config()).toEqual(saved);
});

test.each([
  null,
  { ...emptyConfig, extra: "unexpected" },
  { mattermost: emptyConfig.mattermost },
  { ...emptyConfig, mattermost: { source: "enabled", values: {} } },
  { ...emptyConfig, mattermost: { source: "none", values: { url: "x" } } },
  { ...emptyConfig, mattermost: { source: "saved", values: {} } },
  { ...emptyConfig, mattermost: { source: "saved", values: { url: "x" } } },
  {
    ...emptyConfig,
    mattermost: { source: "saved", values: { url: "x", bot_token: "y" } },
  },
  {
    ...emptyConfig,
    mattermost: { source: "saved", values: { url: 1, owner_id: "y" } },
  },
  {
    ...emptyConfig,
    mattermost: { source: "saved", values: { url: "x".repeat(4097), owner_id: "y" } },
  },
  { ...emptyConfig, mattermost: { source: "none", values: [] } },
  { ...emptyConfig, mattermost: { source: "none" } },
])("invalid config shape is refused", async (reading) => {
  const api = createMessagingApi("", async () => Response.json(reading));
  await expect(api.config()).rejects.toMatchObject({ code: "invalid_response" });
});

test("save sends a PUT with a JSON body and returns the resulting status", async () => {
  const fetchImpl = vi.fn(async () => Response.json(connected));
  const api = createMessagingApi("synthetic-token", fetchImpl);
  const values = { url: "https://mm.example", owner_id: "u1" };
  expect(await api.save("mattermost", values)).toEqual(connected);
  expect(fetchImpl).toHaveBeenCalledWith("/api/v2/messaging/mattermost/config", {
    method: "PUT",
    signal: expect.any(AbortSignal),
    headers: {
      authorization: "Bearer synthetic-token",
      "content-type": "application/json",
    },
    cache: "no-store",
    credentials: "omit",
    body: JSON.stringify(values),
  });
});

test("save accepts a partial field update, e.g. only the secret", async () => {
  const fetchImpl = vi.fn(async () => Response.json(connected));
  const api = createMessagingApi("", fetchImpl);
  await api.save("slack", { bot_token: "xoxb-1" });
  expect(fetchImpl).toHaveBeenCalledWith(
    "/api/v2/messaging/slack/config",
    expect.objectContaining({ body: JSON.stringify({ bot_token: "xoxb-1" }) }),
  );
});

test("remove sends a DELETE and returns the resulting status", async () => {
  const fetchImpl = vi.fn(async () => Response.json(connected));
  const api = createMessagingApi("synthetic-token", fetchImpl);
  expect(await api.remove("slack")).toEqual(connected);
  expect(fetchImpl).toHaveBeenCalledWith("/api/v2/messaging/slack/config", {
    method: "DELETE",
    signal: expect.any(AbortSignal),
    headers: { authorization: "Bearer synthetic-token" },
    cache: "no-store",
    credentials: "omit",
  });
});

test.each(["unknown", "../slack", null, 123])(
  "remove refuses an invalid provider before network",
  async (provider) => {
    const fetchImpl = vi.fn(async () => Response.json(connected));
    const api = createMessagingApi("", fetchImpl);
    await expect(api.remove(provider as never)).rejects.toMatchObject({ code: "invalid_input" });
    expect(fetchImpl).not.toHaveBeenCalled();
  },
);

test.each([
  ["unknown", { owner_id: "u" }],
  ["slack", { client_url: "x" }],
  ["mattermost", { url: 1 }],
  ["mattermost", null],
  ["mattermost", ["url"]],
])(
  "save refuses an unknown provider, unknown keys, non-strings or non-objects before network",
  async (provider, values) => {
    const fetchImpl = vi.fn(async () => Response.json(connected));
    const api = createMessagingApi("", fetchImpl);
    await expect(api.save(provider as never, values as never)).rejects.toMatchObject({
      code: "invalid_input",
    });
    expect(fetchImpl).not.toHaveBeenCalled();
  },
);

test("network failures, pre-aborted calls, external cancellation and timeouts are typed", async () => {
  const failing = vi.fn(async () => {
    throw new Error("private network details");
  });
  await expect(createMessagingApi("", failing).get()).rejects.toMatchObject({
    code: "unavailable",
  });
  const cancelled = new AbortController();
  cancelled.abort();
  await expect(createMessagingApi("", failing).get(cancelled.signal)).rejects.toMatchObject({
    code: "cancelled",
  });
  expect(failing).toHaveBeenCalledTimes(1);

  vi.useFakeTimers();
  const waiting = (_url: RequestInfo | URL, init?: RequestInit) =>
    new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new Error("aborted")));
    });
  const controller = new AbortController();
  const api = createMessagingApi("", waiting);
  const external = api.get(controller.signal).catch((value: unknown) => value);
  controller.abort();
  expect(await external).toMatchObject({ code: "cancelled" });
  const timeout = api.disableMattermost().catch((value: unknown) => value);
  await vi.advanceTimersByTimeAsync(15_000);
  expect(await timeout).toMatchObject({ code: "timeout" });
});
