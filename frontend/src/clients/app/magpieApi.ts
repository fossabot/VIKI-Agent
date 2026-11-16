import { boundedJson } from "../boundedResponse";

export const MAGPIE_BASE_URL = "http://127.0.0.1:3426/v1";

export interface MagpieStatus {
  available: boolean;
  running: boolean;
  version: string;
  desktop: boolean;
  base_url: string | null;
}

export interface MagpieView {
  id: string;
  bounds: { x: number; y: number; width: number; height: number };
  appearance: {
    theme: "dark" | "light";
    density: "comfortable" | "compact";
    scale: number;
    reduce_motion: boolean;
    locale: "en";
    palette: Record<
      | "bg"
      | "panel"
      | "raised"
      | "text"
      | "secondary"
      | "muted"
      | "faint"
      | "line"
      | "hair"
      | "accent"
      | "onAccent"
      | "popover",
      string
    >;
  };
  visible: boolean;
}

export interface MagpieApi {
  status(signal?: AbortSignal): Promise<MagpieStatus>;
  open(ui: string | null, signal?: AbortSignal, view?: MagpieView): Promise<MagpieStatus>;
  view(ui: string | null, view: MagpieView, signal?: AbortSignal): Promise<MagpieStatus>;
  close(ui: string | null, id?: string, signal?: AbortSignal): Promise<MagpieStatus>;
}

export class MagpieError extends Error {
  constructor(readonly code: string) {
    super("Magpie request failed.");
  }
}

function parseStatus(value: unknown): MagpieStatus {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new MagpieError("invalid_response");
  const row = value as Record<string, unknown>;
  if (
    Object.keys(row).sort().join(",") !== "available,base_url,desktop,running,version" ||
    typeof row.available !== "boolean" ||
    typeof row.running !== "boolean" ||
    typeof row.desktop !== "boolean" ||
    typeof row.version !== "string" ||
    !/^\d+\.\d+\.\d+$/.test(row.version) ||
    !(row.base_url === null || row.base_url === MAGPIE_BASE_URL)
  )
    throw new MagpieError("invalid_response");
  return row as unknown as MagpieStatus;
}

export function createMagpieApi(token: string, fetchImpl: typeof fetch = fetch): MagpieApi {
  async function call(path: string, body?: object, signal?: AbortSignal): Promise<MagpieStatus> {
    const response = await fetchImpl(`/api/v2/llm/magpie${path}`, {
      method: body === undefined ? "GET" : "POST",
      signal: signal ?? null,
      headers: {
        authorization: `Bearer ${token}`,
        ...(body === undefined ? {} : { "content-type": "application/json" }),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    const value: unknown = await boundedJson(response, 4096);
    if (!response.ok) {
      const error = (value as { error?: { code?: unknown } } | null)?.error;
      throw new MagpieError(
        typeof error?.code === "string" && /^[a-z_]{1,64}$/.test(error.code)
          ? error.code
          : "unavailable",
      );
    }
    return parseStatus(value);
  }
  return {
    status: (signal) => call("", undefined, signal),
    open: (ui, signal, view) => call("/open", { ui, ...(view ? { view } : {}) }, signal),
    view: (ui, view, signal) => call("/view", { ui, view }, signal),
    close: (ui, id, signal) =>
      call("/close", { ui, ...(id === undefined ? {} : { view_id: id }) }, signal),
  };
}
