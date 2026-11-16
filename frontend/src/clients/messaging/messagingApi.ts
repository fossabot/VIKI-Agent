import { boundedJson } from "../boundedResponse";

export const MESSAGING_PROVIDERS = ["mattermost", "slack", "discord"] as const;
export type MessagingProvider = (typeof MESSAGING_PROVIDERS)[number];
export type MessagingState = "not_configured" | "disabled" | "connecting" | "connected" | "stopped";
export type MattermostState = MessagingState;
export const MESSAGING_STOP_REASONS = [
  "token_rejected",
  "app_token_rejected",
  "server_unreachable",
  "account_check_failed",
  "start_failed",
  "connection_lost",
  "limit_reached",
  "conversation_failed",
  "safety_check",
  "internal_error",
] as const;
export type MessagingStopReason = (typeof MESSAGING_STOP_REASONS)[number];
export interface MessagingProviderStatus {
  configured: boolean;
  state: MessagingState;
  restart_required: boolean;
  reason: MessagingStopReason | null;
}
export type MessagingStatus = Record<MessagingProvider, MessagingProviderStatus>;
export type MessagingField = "url" | "owner_id" | "bot_token" | "app_token" | "client_url";
export const MESSAGING_FIELDS: Record<MessagingProvider, readonly MessagingField[]> = {
  mattermost: ["url", "owner_id", "bot_token"],
  slack: ["bot_token", "app_token", "owner_id"],
  discord: ["bot_token", "owner_id", "client_url"],
};
export const MESSAGING_SECRET_FIELDS: readonly MessagingField[] = ["bot_token", "app_token"];
export type MessagingConfigSource = "none" | "saved" | "unreadable";
export interface MessagingProviderConfig {
  source: MessagingConfigSource;
  values: Partial<Record<MessagingField, string>>;
}
export type MessagingConfig = Record<MessagingProvider, MessagingProviderConfig>;
export interface MessagingApi {
  get(signal?: AbortSignal): Promise<MessagingStatus>;
  enable(provider: MessagingProvider, signal?: AbortSignal): Promise<MessagingStatus>;
  disable(provider: MessagingProvider, signal?: AbortSignal): Promise<MessagingStatus>;
  disableMattermost(signal?: AbortSignal): Promise<MessagingStatus>;
  config(signal?: AbortSignal): Promise<MessagingConfig>;
  save(
    provider: MessagingProvider,
    values: Partial<Record<MessagingField, string>>,
    signal?: AbortSignal,
  ): Promise<MessagingStatus>;
  remove(provider: MessagingProvider, signal?: AbortSignal): Promise<MessagingStatus>;
}
export class MessagingApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly retryAfterSeconds?: number,
  ) {
    super("Messaging is unavailable.");
    this.name = "MessagingApiError";
  }
}

const MAX_RESPONSE = 64 << 10;
const TIMEOUT = 15_000;
const CODE = /^[a-z][a-z0-9_.:-]{0,127}$/;
const STATES: readonly MessagingState[] = [
  "not_configured",
  "disabled",
  "connecting",
  "connected",
  "stopped",
];
const CONFIG_SOURCES: readonly MessagingConfigSource[] = ["none", "saved", "unreadable"];
const NON_SECRET_FIELDS: Record<MessagingProvider, readonly MessagingField[]> = Object.fromEntries(
  MESSAGING_PROVIDERS.map((provider) => [
    provider,
    MESSAGING_FIELDS[provider].filter(
      (field) => !(MESSAGING_SECRET_FIELDS as readonly string[]).includes(field),
    ),
  ]),
) as unknown as Record<MessagingProvider, readonly MessagingField[]>;

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function exact(value: unknown, keys: readonly string[]): value is Record<string, unknown> {
  return (
    object(value) && Object.keys(value).length === keys.length && keys.every((key) => key in value)
  );
}
function parseProvider(bot: unknown): MessagingProviderStatus | null {
  if (
    !exact(bot, ["configured", "state", "restart_required", "reason"]) ||
    typeof bot.configured !== "boolean" ||
    typeof bot.restart_required !== "boolean" ||
    typeof bot.state !== "string" ||
    !(STATES as readonly string[]).includes(bot.state) ||
    bot.configured === (bot.state === "not_configured") ||
    (bot.restart_required && bot.state !== "stopped") ||
    (bot.reason !== null && typeof bot.reason !== "string") ||
    (bot.reason !== null &&
      (!(MESSAGING_STOP_REASONS as readonly string[]).includes(bot.reason as string) ||
        bot.state !== "stopped" ||
        bot.restart_required))
  )
    return null;
  return {
    configured: bot.configured,
    state: bot.state as MessagingState,
    restart_required: bot.restart_required,
    reason: bot.reason as MessagingStopReason | null,
  };
}
function parseStatus(value: unknown): MessagingStatus | null {
  if (!exact(value, MESSAGING_PROVIDERS)) return null;
  const mattermost = parseProvider(value.mattermost);
  const slack = parseProvider(value.slack);
  const discord = parseProvider(value.discord);
  return mattermost && slack && discord ? { mattermost, slack, discord } : null;
}
function parseProviderConfig(
  provider: MessagingProvider,
  raw: unknown,
): MessagingProviderConfig | null {
  if (!exact(raw, ["source", "values"])) return null;
  const { source, values } = raw;
  if (typeof source !== "string" || !(CONFIG_SOURCES as readonly string[]).includes(source))
    return null;
  if (!object(values)) return null;
  const expected: readonly string[] = source === "saved" ? NON_SECRET_FIELDS[provider] : [];
  if (!exact(values, expected)) return null;
  for (const field of Object.values(values))
    if (typeof field !== "string" || field.length > 4096) return null;
  return {
    source: source as MessagingConfigSource,
    values: values as Partial<Record<MessagingField, string>>,
  };
}
function parseConfig(value: unknown): MessagingConfig | null {
  if (!exact(value, MESSAGING_PROVIDERS)) return null;
  const mattermost = parseProviderConfig("mattermost", value.mattermost);
  const slack = parseProviderConfig("slack", value.slack);
  const discord = parseProviderConfig("discord", value.discord);
  return mattermost && slack && discord ? { mattermost, slack, discord } : null;
}
function serverError(status: number, body: unknown): MessagingApiError {
  const error = object(body) && object(body.error) ? body.error : null;
  const code = error?.code;
  const retry = error?.retry_after_seconds;
  return new MessagingApiError(
    status,
    typeof code === "string" && CODE.test(code) ? code : "unavailable",
    status === 429 &&
      code === "cooldown" &&
      typeof retry === "number" &&
      Number.isInteger(retry) &&
      retry >= 1 &&
      retry <= 86400
      ? retry
      : undefined,
  );
}

export function createMessagingApi(token: string, fetchImpl: typeof fetch = fetch): MessagingApi {
  async function request<T>(
    path: string,
    method: "GET" | "POST" | "PUT" | "DELETE",
    parse: (value: unknown) => T | null,
    signal?: AbortSignal,
    body?: Record<string, string>,
  ): Promise<T> {
    if (signal?.aborted) throw new MessagingApiError(0, "cancelled");
    const controller = new AbortController();
    const relay = () => controller.abort();
    signal?.addEventListener("abort", relay, { once: true });
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, TIMEOUT);
    try {
      const response = await fetchImpl(path, {
        method,
        signal: controller.signal,
        headers: {
          authorization: `Bearer ${token}`,
          ...(body !== undefined ? { "content-type": "application/json" } : {}),
        },
        cache: "no-store",
        credentials: "omit",
        ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
      });
      if (controller.signal.aborted)
        throw new MessagingApiError(0, signal?.aborted ? "cancelled" : "timeout");
      let responseBody: unknown;
      try {
        responseBody = await boundedJson(response, MAX_RESPONSE);
      } catch (error) {
        if (controller.signal.aborted) throw error;
        throw response.ok
          ? new MessagingApiError(0, "invalid_response")
          : serverError(response.status, null);
      }
      if (controller.signal.aborted)
        throw new MessagingApiError(0, signal?.aborted ? "cancelled" : "timeout");
      if (!response.ok) throw serverError(response.status, responseBody);
      const parsed = parse(responseBody);
      if (!parsed) throw new MessagingApiError(0, "invalid_response");
      return parsed;
    } catch (error) {
      if (error instanceof MessagingApiError) throw error;
      if (signal?.aborted) throw new MessagingApiError(0, "cancelled");
      if (timedOut) throw new MessagingApiError(0, "timeout");
      throw new MessagingApiError(0, "unavailable");
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", relay);
    }
  }
  function validProvider(provider: unknown): provider is MessagingProvider {
    return (MESSAGING_PROVIDERS as readonly unknown[]).includes(provider);
  }
  async function change(
    provider: MessagingProvider,
    action: "enable" | "disable",
    signal?: AbortSignal,
  ): Promise<MessagingStatus> {
    if (!validProvider(provider)) throw new MessagingApiError(0, "invalid_input");
    return request(`/api/v2/messaging/${provider}/${action}`, "POST", parseStatus, signal);
  }
  async function save(
    provider: MessagingProvider,
    values: Partial<Record<MessagingField, string>>,
    signal?: AbortSignal,
  ): Promise<MessagingStatus> {
    if (!validProvider(provider) || !object(values))
      throw new MessagingApiError(0, "invalid_input");
    const allowed = MESSAGING_FIELDS[provider] as readonly string[];
    for (const [key, fieldValue] of Object.entries(values))
      if (!allowed.includes(key) || typeof fieldValue !== "string")
        throw new MessagingApiError(0, "invalid_input");
    return request(
      `/api/v2/messaging/${provider}/config`,
      "PUT",
      parseStatus,
      signal,
      values as Record<string, string>,
    );
  }
  async function remove(
    provider: MessagingProvider,
    signal?: AbortSignal,
  ): Promise<MessagingStatus> {
    if (!validProvider(provider)) throw new MessagingApiError(0, "invalid_input");
    return request(`/api/v2/messaging/${provider}/config`, "DELETE", parseStatus, signal);
  }
  return {
    get: (signal) => request("/api/v2/messaging", "GET", parseStatus, signal),
    enable: (provider, signal) => change(provider, "enable", signal),
    disable: (provider, signal) => change(provider, "disable", signal),
    disableMattermost: (signal) => change("mattermost", "disable", signal),
    config: (signal) => request("/api/v2/messaging/config", "GET", parseConfig, signal),
    save,
    remove,
  };
}
