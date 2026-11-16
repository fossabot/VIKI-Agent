import { boundedJson } from "../clients/boundedResponse";
import { validWriteRef } from "./vaultApi";

// The wire format is the server's (src/viki_agent/vaults/vault_ops_api.py, src/viki_agent/vaults/vault_lifecycle.py, src/viki_agent/vaults/vault_publication.py).
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const PHYSICAL_ID = /^[0-9]+:[0-9]+$/;
const SHA256 = /^[0-9a-f]{64}$/;
const MAX = 128 * 1024;
const TIMEOUT = 15_000;
const KINDS = ["rename", "move", "archive", "delete", "restore"] as const;
const ACTIONS = ["create", "move", "remove", "mkdir"] as const;
const STATUSES = ["done", "failed", "conflict", "recovery_required"] as const;
const RECOVERY_KINDS = ["deleted", "preimage"] as const;
const CHOICES = ["forget", "drop_previous"] as const;

export type OperationKind = (typeof KINDS)[number];
export type OperationStatus = (typeof STATUSES)[number];
export interface OperationStep {
  n: number;
  action: (typeof ACTIONS)[number];
  ref: string;
  to: string | null;
}
export interface OperationPlan {
  steps: OperationStep[];
  creates_folders: string[];
  recovery_days: number;
  /** Present only when the server refused; `steps` is then empty. */
  refusal?: string;
}
export interface OperationOutcome {
  operation_id: string;
  status: OperationStatus;
  replayed: boolean;
  steps_applied: number;
  reason: string | null;
  created_folders: string[];
}
export interface RecoveryEntry {
  recovery_id: string;
  kind: (typeof RECOVERY_KINDS)[number];
  ref: string;
  byte_size: number;
  created_at: string;
  expires_at: string;
}
export interface StuckOperation {
  operation_id: string;
  kind: OperationKind;
  state: string;
  paths: string[];
}
export interface RecoveryList {
  entries: RecoveryEntry[];
  stuck: StuckOperation[];
}
export interface ForgottenOperation {
  operation_id: string;
  recovery_ids: string[];
  stage_retained: boolean;
}
export type ResolveChoice = (typeof CHOICES)[number];
export interface ResolveOutcome {
  operation_id: string;
  choice: ResolveChoice;
  stage_retained: boolean;
  backup_retained: boolean;
}
/** The server's typed code, or "unavailable" when the answer carries none. */
export class VaultOpsError extends Error {
  constructor(readonly code: string) {
    super(code);
  }
}
/**
 * Per kind, exactly: rename, move and archive take `ref`, `to` and `expectedSha256`; delete takes
 * `ref` and `expectedSha256`; restore takes `recoveryId` and `to` and no hash.
 */
export interface OperationRequest {
  policyRevision: number;
  rootPhysicalId: string;
  kind: OperationKind;
  ref?: string | undefined;
  to?: string | undefined;
  expectedSha256?: string | undefined;
  recoveryId?: string | undefined;
}
export interface VaultOpsApi {
  preview(id: string, request: OperationRequest, signal?: AbortSignal): Promise<OperationPlan>;
  apply(
    id: string,
    request: OperationRequest,
    commandId: string,
    signal?: AbortSignal,
  ): Promise<OperationOutcome>;
  recovery(id: string, signal?: AbortSignal): Promise<RecoveryList>;
  forgetOperation(operationId: string, signal?: AbortSignal): Promise<ForgottenOperation>;
  resolve(
    operationId: string,
    choice: ResolveChoice,
    signal?: AbortSignal,
  ): Promise<ResolveOutcome>;
}

const record = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
const uuid = (value: unknown): value is string => typeof value === "string" && UUID.test(value);
const count = (value: unknown): value is number =>
  Number.isSafeInteger(value) && (value as number) >= 0;
const text = (value: unknown): value is string =>
  typeof value === "string" && value.length > 0 && value.length <= 512;
const textList = (value: unknown, max: number): value is string[] =>
  Array.isArray(value) && value.length <= max && value.every(text);
const oneOf = <T extends string>(set: readonly T[], value: unknown): value is T =>
  set.includes(value as T);
// A step's ref may name a folder (mkdir), so it is held to a bound, not the note-ref rule.
const validStep = (value: unknown): value is OperationStep => {
  const step = record(value);
  return (
    !!step &&
    count(step.n) &&
    oneOf(ACTIONS, step.action) &&
    text(step.ref) &&
    (step.to === null || text(step.to))
  );
};
const unavailable = () => new VaultOpsError("unavailable");

export function createVaultOpsApi(token: string, fetchImpl: typeof fetch = fetch): VaultOpsApi {
  async function call(
    route: string,
    init: RequestInit,
    signal?: AbortSignal,
  ): Promise<Record<string, unknown>> {
    if (signal?.aborted) throw new Error("cancelled");
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(abort, TIMEOUT);
    try {
      const response = await fetchImpl(route, {
        ...init,
        signal: controller.signal,
        headers: { authorization: `Bearer ${token}`, ...(init.headers ?? {}) },
      });
      const body = record(await boundedJson(response, MAX).catch(() => null));
      if (response.ok && body) return body;
      // Every refusal carries {error: {code, message}}; the code alone is acted on.
      const error = record(body?.error);
      throw error && text(error.code) ? new VaultOpsError(error.code) : unavailable();
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    }
  }
  const post = (route: string, body: unknown, signal?: AbortSignal) =>
    call(
      route,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      },
      signal,
    );
  // The server's strict body: exactly these keys per kind, so a stray one is refused here.
  function operationBody(id: string, request: OperationRequest) {
    const { policyRevision, rootPhysicalId, kind, ref, to, expectedSha256, recoveryId } = request;
    const common = {
      policy_revision: policyRevision,
      root_physical_id: rootPhysicalId,
      kind,
    };
    const valid =
      uuid(id) &&
      Number.isSafeInteger(policyRevision) &&
      policyRevision >= 1 &&
      PHYSICAL_ID.test(rootPhysicalId) &&
      rootPhysicalId.length <= 80 &&
      oneOf(KINDS, kind);
    if (!valid) throw new VaultOpsError("invalid_operation");
    if (kind === "restore") {
      if (
        !uuid(recoveryId) ||
        !validWriteRef(to) ||
        ref !== undefined ||
        expectedSha256 !== undefined
      )
        throw new VaultOpsError("invalid_operation");
      return { ...common, recovery_id: recoveryId, to };
    }
    if (
      !validWriteRef(ref) ||
      typeof expectedSha256 !== "string" ||
      !SHA256.test(expectedSha256) ||
      recoveryId !== undefined ||
      (kind === "delete" ? to !== undefined : !validWriteRef(to))
    )
      throw new VaultOpsError("invalid_operation");
    return {
      ...common,
      ref,
      ...(kind === "delete" ? {} : { to }),
      expected_sha256: expectedSha256,
    };
  }
  return {
    async preview(id, request, signal) {
      const body = await post(
        `/api/v2/vaults/${id}/operations/preview`,
        operationBody(id, request),
        signal,
      );
      if (
        !Array.isArray(body.steps) ||
        body.steps.length > 64 ||
        !body.steps.every(validStep) ||
        !textList(body.creates_folders, 64) ||
        !count(body.recovery_days) ||
        (body.refusal !== undefined && !text(body.refusal))
      )
        throw unavailable();
      return body as unknown as OperationPlan;
    },
    async apply(id, request, commandId, signal) {
      if (!uuid(commandId)) throw new VaultOpsError("invalid_command");
      const body = await post(
        `/api/v2/vaults/${id}/operations`,
        { ...operationBody(id, request), command_id: commandId },
        signal,
      );
      if (
        !uuid(body.operation_id) ||
        !oneOf(STATUSES, body.status) ||
        typeof body.replayed !== "boolean" ||
        !count(body.steps_applied) ||
        (body.reason !== null && !text(body.reason)) ||
        !textList(body.created_folders, 64)
      )
        throw unavailable();
      return body as unknown as OperationOutcome;
    },
    async recovery(id, signal) {
      if (!uuid(id)) throw new VaultOpsError("invalid_workspace");
      const body = await call(`/api/v2/vaults/${id}/recovery`, { method: "GET" }, signal);
      const { entries, stuck } = body;
      if (
        !Array.isArray(entries) ||
        entries.length > 256 ||
        !entries.every((row) => {
          const entry = record(row);
          return (
            !!entry &&
            uuid(entry.recovery_id) &&
            oneOf(RECOVERY_KINDS, entry.kind) &&
            text(entry.ref) &&
            count(entry.byte_size) &&
            text(entry.created_at) &&
            text(entry.expires_at)
          );
        }) ||
        !Array.isArray(stuck) ||
        stuck.length > 64 ||
        !stuck.every((row) => {
          const item = record(row);
          return (
            !!item &&
            uuid(item.operation_id) &&
            oneOf(KINDS, item.kind) &&
            text(item.state) &&
            textList(item.paths, 8)
          );
        })
      )
        throw unavailable();
      return { entries, stuck } as RecoveryList;
    },
    async forgetOperation(operationId, signal) {
      if (!uuid(operationId)) throw new VaultOpsError("invalid_operation");
      const body = await post(
        `/api/v2/vault-operations/${operationId}/resolve`,
        { choice: "forget" },
        signal,
      );
      if (
        !uuid(body.operation_id) ||
        !Array.isArray(body.recovery_ids) ||
        body.recovery_ids.length > 64 ||
        !body.recovery_ids.every(uuid) ||
        typeof body.stage_retained !== "boolean"
      )
        throw unavailable();
      return body as unknown as ForgottenOperation;
    },
    async resolve(operationId, choice, signal) {
      if (!uuid(operationId) || !oneOf(CHOICES, choice))
        throw new VaultOpsError("invalid_publication");
      const body = await post(
        `/api/v2/vault-publications/${operationId}/resolve`,
        { choice },
        signal,
      );
      if (
        !uuid(body.operation_id) ||
        !oneOf(CHOICES, body.choice) ||
        typeof body.stage_retained !== "boolean" ||
        typeof body.backup_retained !== "boolean"
      )
        throw unavailable();
      return body as unknown as ResolveOutcome;
    },
  };
}
