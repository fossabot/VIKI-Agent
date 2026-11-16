import { describe, expect, it, vi } from "vitest";
import { createVaultOpsApi, VaultOpsError } from "./vaultOpsApi";

const ID = "00000000-0000-4000-8000-000000000001";
const COMMAND = "00000000-0000-4000-8000-000000000002";
const HASH = "a".repeat(64);
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const request = {
  policyRevision: 3,
  rootPhysicalId: "1:2",
  kind: "rename" as const,
  ref: "a/b.md",
  to: "a/c.md",
  expectedSha256: HASH,
};
const restore = {
  policyRevision: 3,
  rootPhysicalId: "1:2",
  kind: "restore" as const,
  recoveryId: COMMAND,
  to: "a/c.md",
};
const plan = {
  steps: [{ n: 0, action: "move", ref: "a/b.md", to: "a/c.md" }],
  creates_folders: [],
  recovery_days: 30,
};
const outcome = {
  operation_id: COMMAND,
  status: "done",
  replayed: false,
  steps_applied: 1,
  reason: null,
  created_folders: [],
};
const apiWith = (reply: () => Response) => {
  const fetcher = vi.fn<typeof fetch>(async () => reply());
  return { api: createVaultOpsApi("tok", fetcher), fetcher };
};
const sent = (fetcher: ReturnType<typeof vi.fn<typeof fetch>>) => {
  const [route, init] = fetcher.mock.calls[0] ?? [];
  return {
    route,
    init: init as RequestInit,
    body: JSON.parse(String((init as RequestInit).body ?? "null")) as Record<string, unknown>,
  };
};

describe("vault operations API", () => {
  it("previews with the exact route, auth and body", async () => {
    const { api, fetcher } = apiWith(() => json(plan));
    await expect(api.preview(ID, request)).resolves.toEqual(plan);
    const call = sent(fetcher);
    expect(call.route).toBe(`/api/v2/vaults/${ID}/operations/preview`);
    expect(call.init.method).toBe("POST");
    expect(call.init.headers).toMatchObject({
      authorization: "Bearer tok",
      "content-type": "application/json",
    });
    expect(call.body).toEqual({
      policy_revision: 3,
      root_physical_id: "1:2",
      kind: "rename",
      ref: "a/b.md",
      to: "a/c.md",
      expected_sha256: HASH,
    });
  });

  it("sends the exact keys per kind, and apply adds the command id", async () => {
    for (const kind of ["move", "archive"] as const) {
      const { api, fetcher } = apiWith(() => json(outcome));
      await api.apply(ID, { ...request, kind }, COMMAND);
      expect(Object.keys(sent(fetcher).body).sort()).toEqual(
        [
          "command_id",
          "expected_sha256",
          "kind",
          "policy_revision",
          "ref",
          "root_physical_id",
          "to",
        ].sort(),
      );
    }
    const del = apiWith(() => json(outcome));
    await del.api.apply(ID, { ...request, kind: "delete", to: undefined }, COMMAND);
    expect(sent(del.fetcher).route).toBe(`/api/v2/vaults/${ID}/operations`);
    expect(sent(del.fetcher).body).toEqual({
      policy_revision: 3,
      root_physical_id: "1:2",
      kind: "delete",
      ref: "a/b.md",
      expected_sha256: HASH,
      command_id: COMMAND,
    });
    const res = apiWith(() => json(outcome));
    await res.api.apply(ID, restore, COMMAND);
    expect(sent(res.fetcher).body).toEqual({
      policy_revision: 3,
      root_physical_id: "1:2",
      kind: "restore",
      recovery_id: COMMAND,
      to: "a/c.md",
      command_id: COMMAND,
    });
  });

  it("refuses a bad request before any fetch", async () => {
    const { api, fetcher } = apiWith(() => json(plan));
    const bad = [
      { ...request, ref: "../x.md" },
      { ...request, ref: "a.txt" },
      { ...request, ref: undefined },
      { ...request, ref: "CLAUDE.md" },
      { ...request, to: "a/.git/x.md" },
      { ...request, to: undefined },
      { ...request, kind: "delete" as const },
      { ...request, kind: "copy" as never },
      { ...request, policyRevision: 0 },
      { ...request, rootPhysicalId: "x" },
      { ...request, expectedSha256: undefined },
      { ...request, expectedSha256: "A".repeat(64) },
      { ...request, expectedSha256: "a".repeat(63) },
      { ...request, kind: "delete" as const, to: undefined, expectedSha256: undefined },
      { ...request, recoveryId: COMMAND },
      { ...restore, ref: "a/b.md" },
      { ...restore, expectedSha256: HASH },
      { ...restore, recoveryId: undefined },
      { ...restore, recoveryId: "x" },
      { ...restore, to: undefined },
    ];
    for (const item of bad)
      await expect(api.preview(ID, item)).rejects.toMatchObject({ code: "invalid_operation" });
    await expect(api.preview("nope", request)).rejects.toBeInstanceOf(VaultOpsError);
    await expect(api.apply(ID, request, "nope")).rejects.toMatchObject({
      code: "invalid_command",
    });
    await expect(api.resolve(ID, "delete" as never)).rejects.toBeInstanceOf(VaultOpsError);
    await expect(api.recovery("nope")).rejects.toBeInstanceOf(VaultOpsError);
    await expect(api.forgetOperation("nope")).rejects.toBeInstanceOf(VaultOpsError);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("carries the typed code of any non-ok body", async () => {
    for (const [status, code] of [
      [400, "invalid_operation"],
      [404, "recovery_missing"],
      [409, "conflict"],
      [409, "command_conflict"],
      [409, "vault_locked"],
      [413, "too_large"],
      [423, "target_busy"],
      [429, "overloaded"],
      [429, "recovery_full"],
      [503, "unavailable"],
    ] as const) {
      const { api } = apiWith(() =>
        json({ error: { code, message: "The vault operation did not run." } }, status),
      );
      await expect(api.apply(ID, request, COMMAND)).rejects.toMatchObject({ code });
    }
  });

  it("rejects an untyped or non-ok answer as unavailable", async () => {
    for (const reply of [
      () => json({ error: {} }, 500),
      () => json(outcome, 500),
      () => json(outcome, 409),
      () => json(outcome, 404),
      () => new Response("<html>", { status: 502 }),
      () => json([], 200),
    ]) {
      const { api } = apiWith(reply);
      await expect(api.apply(ID, request, COMMAND)).rejects.toMatchObject({ code: "unavailable" });
    }
  });

  it("accepts every apply status and reason, and rejects a fifth status", async () => {
    for (const status of ["done", "failed", "conflict", "recovery_required"]) {
      const body = { ...outcome, status, reason: status === "done" ? null : "target_busy" };
      const { api } = apiWith(() => json(body));
      await expect(api.apply(ID, request, COMMAND)).resolves.toEqual(body);
    }
    for (const body of [
      { ...outcome, status: "planned" },
      { ...outcome, status: undefined },
      { ...outcome, operation_id: "x" },
      { ...outcome, replayed: "no" },
      { ...outcome, steps_applied: -1 },
      { ...outcome, reason: 4 },
      { ...outcome, reason: undefined },
      { ...outcome, created_folders: [1] },
      { ...outcome, created_folders: undefined },
    ]) {
      const { api } = apiWith(() => json(body));
      await expect(api.apply(ID, request, COMMAND)).rejects.toMatchObject({ code: "unavailable" });
    }
  });

  it("rejects a malformed preview body as unavailable", async () => {
    for (const body of [
      { ...plan, steps: [{ n: 0, action: "chmod", ref: "a.md", to: null }] },
      { ...plan, steps: "x" },
      { ...plan, creates_folders: [1] },
      { ...plan, recovery_days: "30" },
      { ...plan, refusal: 4 },
    ]) {
      const { api } = apiWith(() => json(body));
      await expect(api.preview(ID, request)).rejects.toMatchObject({ code: "unavailable" });
    }
  });

  it("keeps a refusal with empty steps, and a created folder, in the plan", async () => {
    const refused = { steps: [], creates_folders: [], recovery_days: 30, refusal: "conflict" };
    const first = apiWith(() => json(refused));
    await expect(first.api.preview(ID, request)).resolves.toEqual(refused);
    const folders = { ...plan, creates_folders: ["x"] };
    const second = apiWith(() => json(folders));
    await expect(second.api.preview(ID, request)).resolves.toEqual(folders);
  });

  it("lists recovery entries and stuck rows by GET and validates each row", async () => {
    const entry = {
      recovery_id: COMMAND,
      kind: "deleted",
      ref: "a/b.md",
      byte_size: 10,
      created_at: "2026-10-02T00:00:00Z",
      expires_at: "2026-11-01T00:00:00Z",
    };
    const stuck = {
      operation_id: COMMAND,
      kind: "delete",
      state: "recovery_required",
      paths: ["a.md"],
    };
    const { api, fetcher } = apiWith(() => json({ entries: [entry], stuck: [stuck] }));
    await expect(api.recovery(ID)).resolves.toEqual({ entries: [entry], stuck: [stuck] });
    expect(sent(fetcher).route).toBe(`/api/v2/vaults/${ID}/recovery`);
    expect(sent(fetcher).init.method).toBe("GET");
    for (const body of [
      { entries: [{ ...entry, kind: "x" }], stuck: [] },
      { entries: [{ ...entry, recovery_id: "x" }], stuck: [] },
      { entries: [{ ...entry, byte_size: -1 }], stuck: [] },
      { entries: [entry] },
      { entries: [entry], stuck: "x" },
      { entries: [entry], stuck: [{ ...stuck, kind: "copy" }] },
      { entries: [entry], stuck: [{ ...stuck, operation_id: "x" }] },
      { entries: [entry], stuck: [{ ...stuck, paths: [1] }] },
      { entries: [entry], stuck: [{ ...stuck, state: undefined }] },
    ]) {
      const { api: other } = apiWith(() => json(body));
      await expect(other.recovery(ID)).rejects.toMatchObject({ code: "unavailable" });
    }
  });

  it("forgets a stuck operation and types the answer", async () => {
    const answer = { operation_id: COMMAND, recovery_ids: [ID], stage_retained: true };
    const { api, fetcher } = apiWith(() => json(answer));
    await expect(api.forgetOperation(COMMAND)).resolves.toEqual(answer);
    const call = sent(fetcher);
    expect(call.route).toBe(`/api/v2/vault-operations/${COMMAND}/resolve`);
    expect(call.body).toEqual({ choice: "forget" });
    for (const body of [
      { ...answer, recovery_ids: ["x"] },
      { ...answer, stage_retained: "no" },
      { ...answer, operation_id: "x" },
    ]) {
      const { api: other } = apiWith(() => json(body));
      await expect(other.forgetOperation(COMMAND)).rejects.toMatchObject({ code: "unavailable" });
    }
  });

  it("resolves a publication row with the choice and types the answer", async () => {
    const answer = {
      operation_id: COMMAND,
      choice: "forget",
      stage_retained: false,
      backup_retained: true,
    };
    const { api, fetcher } = apiWith(() => json(answer));
    await expect(api.resolve(COMMAND, "forget")).resolves.toEqual(answer);
    const call = sent(fetcher);
    expect(call.route).toBe(`/api/v2/vault-publications/${COMMAND}/resolve`);
    expect(call.body).toEqual({ choice: "forget" });
    for (const body of [
      { ...answer, choice: "other" },
      { ...answer, backup_retained: undefined },
    ]) {
      const { api: other } = apiWith(() => json(body));
      await expect(other.resolve(COMMAND, "forget")).rejects.toMatchObject({
        code: "unavailable",
      });
    }
  });

  it("does not fetch when the signal is already aborted and passes its own signal on", async () => {
    const aborted = new AbortController();
    aborted.abort();
    const first = apiWith(() => json(plan));
    await expect(first.api.preview(ID, request, aborted.signal)).rejects.toThrow("cancelled");
    expect(first.fetcher).not.toHaveBeenCalled();
    const live = new AbortController();
    const second = apiWith(() => json(plan));
    await second.api.preview(ID, request, live.signal);
    const forwarded = sent(second.fetcher).init.signal as AbortSignal;
    expect(forwarded.aborted).toBe(false);
  });
});
