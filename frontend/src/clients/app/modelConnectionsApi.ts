import { createContext } from "react";
import { boundedJson } from "../boundedResponse";
import { createMagpieApi, type MagpieApi } from "./magpieApi";

export type ModelApiMode = "chat_completions" | "responses" | "anthropic";
export interface ModelConnectionDraft {
  name: string;
  base_url: string;
  api_mode: ModelApiMode;
  model: string;
  api_key: string;
}
export interface ModelConnection extends Omit<ModelConnectionDraft, "api_key"> {
  id: string;
  model_ref: string;
  has_api_key: boolean;
  is_default: boolean;
}
export interface ModelConnectionsListing {
  revision: number;
  connections: ModelConnection[];
  default_model: string | null;
}
export interface ModelConnectionsApi {
  readonly magpie?: MagpieApi;
  list(signal?: AbortSignal): Promise<ModelConnectionsListing>;
  save(revision: number, connection: ModelConnectionDraft): Promise<ModelConnectionsListing>;
  remove(revision: number, id: string): Promise<ModelConnectionsListing>;
  updateKey(
    id: string,
    revision: number,
    key: string,
    signal?: AbortSignal,
  ): Promise<ModelConnectionsListing>;
  test(connection: ModelConnectionDraft, signal?: AbortSignal): Promise<string[]>;
  discover(connection: ModelConnectionDraft, signal?: AbortSignal): Promise<string[]>;
}

export class ModelConnectionError extends Error {
  constructor(readonly code: string) {
    super("Model connection request failed.");
  }
}

export const ModelConnectionsContext = createContext<ModelConnectionsApi | null>(null);
const modes = ["chat_completions", "responses", "anthropic"] as const;
const record = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
const invalid = () => new ModelConnectionError("invalid_response");
const str = (value: unknown, max: number): value is string =>
  typeof value === "string" && value.length > 0 && [...value].length <= max && value.isWellFormed();
const modelId = (value: unknown): value is string =>
  str(value, 512) &&
  ![...value].some((char) => {
    const code = char.charCodeAt(0);
    return code <= 32 || (code >= 127 && code <= 159);
  }) &&
  !/\p{White_Space}/u.test(value) &&
  !value.startsWith("viki-llm/") &&
  new TextEncoder().encode(value).length <= 978;

function parseListing(value: unknown): ModelConnectionsListing {
  const row = record(value);
  if (
    !row ||
    !Number.isSafeInteger(row.revision) ||
    (row.revision as number) < 0 ||
    !Array.isArray(row.connections) ||
    row.connections.length > 8 ||
    !(row.default_model === null || str(row.default_model, 1024))
  )
    throw invalid();
  const connections = row.connections.map((value): ModelConnection => {
    const item = record(value);
    if (
      !item ||
      !str(item.id, 36) ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(item.id) ||
      !str(item.name, 80) ||
      [...item.name].some((char) => char.charCodeAt(0) < 32) ||
      !str(item.base_url, 2048) ||
      !modelId(item.model) ||
      !modes.includes(item.api_mode as ModelApiMode) ||
      item.model_ref !== `viki-llm/${item.id}/${item.model}` ||
      typeof item.has_api_key !== "boolean" ||
      typeof item.is_default !== "boolean" ||
      "api_key" in item
    )
      throw invalid();
    return {
      id: item.id,
      name: item.name,
      base_url: item.base_url,
      model: item.model,
      api_mode: item.api_mode as ModelApiMode,
      model_ref: item.model_ref as string,
      has_api_key: item.has_api_key,
      is_default: item.is_default,
    };
  });
  if (new Set(connections.map((item) => item.id)).size !== connections.length) throw invalid();
  return {
    revision: row.revision as number,
    connections,
    default_model: row.default_model as string | null,
  };
}

function parseModels(answer: unknown): string[] {
  const value = record(answer);
  if (
    value?.ok !== true ||
    !Array.isArray(value.models) ||
    value.models.length > 257 ||
    !value.models.every(modelId)
  )
    throw invalid();
  return value.models as string[];
}

export function createModelConnectionsApi(
  token: string,
  fetchImpl: typeof fetch = fetch,
): ModelConnectionsApi {
  async function call(
    method: string,
    path: string,
    body?: unknown,
    signal?: AbortSignal,
  ): Promise<unknown> {
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (signal?.aborted) throw new ModelConnectionError("cancelled");
    signal?.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(abort, 30_000);
    try {
      const response = await fetchImpl(`/api/v2/llm/connections${path}`, {
        method,
        signal: controller.signal,
        headers: {
          authorization: `Bearer ${token}`,
          ...(body === undefined ? {} : { "content-type": "application/json" }),
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      const value: unknown = await boundedJson(response, 1024 * 1024);
      if (!response.ok) {
        const code = record(record(value)?.error)?.code;
        throw new ModelConnectionError(
          typeof code === "string" && /^[a-z_]{1,64}$/.test(code) ? code : "unavailable",
        );
      }
      return value;
    } catch (error) {
      if (error instanceof ModelConnectionError) throw error;
      throw new ModelConnectionError("unavailable");
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    }
  }
  return {
    magpie: createMagpieApi(token, fetchImpl),
    async list(signal) {
      return parseListing(await call("GET", "", undefined, signal));
    },
    async save(revision, connection) {
      return parseListing(await call("POST", "", { expected_revision: revision, connection }));
    },
    async remove(revision, id) {
      if (!/^[0-9a-f-]{36}$/.test(id)) throw invalid();
      return parseListing(await call("DELETE", `/${id}`, { expected_revision: revision }));
    },
    async updateKey(id, revision, key, signal) {
      if (!/^[0-9a-f-]{36}$/.test(id)) throw invalid();
      return parseListing(
        await call("PATCH", `/${id}`, { expected_revision: revision, api_key: key }, signal),
      );
    },
    async test(connection, signal) {
      return parseModels(await call("POST", "/test", { connection }, signal));
    },
    async discover(connection, signal) {
      return parseModels(await call("POST", "/models", { connection }, signal));
    },
  };
}
