import { describe, expect, it, vi } from "vitest";
import { createVaultKnowledgeApi, KnowledgeError, type MemoryDraft } from "./vaultKnowledgeApi";

const WORKSPACE = "00000000-0000-4000-8000-000000000001";
const COMMAND = "00000000-0000-4000-8000-000000000002";
const ASSET = "00000000-0000-4000-8000-000000000003";
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const request = {
  policyRevision: 3,
  rootPhysicalId: "1:2",
  assetId: ASSET,
  name: "photo",
};
const kept = { ref: "attachments/photo.png", reused: false, byte_size: 12 };
const preview = {
  files: 2,
  bytes: 30,
  instruction_files: ["CLAUDE.md"],
  instruction_files_total: 1,
  skipped: [{ ref: "x.md", reason: "unreadable" }],
  skipped_total: 1,
  local_recovery: true,
};
const result = {
  folder: "Vault backup 20261002",
  files: 2,
  bytes: 30,
  skipped: [],
  skipped_total: 0,
  replayed: false,
};
const apiWith = (reply: () => Response) => {
  const fetcher = vi.fn<typeof fetch>(async () => reply());
  return { api: createVaultKnowledgeApi("tok", fetcher), fetcher };
};
const sent = (fetcher: ReturnType<typeof vi.fn<typeof fetch>>) => {
  const [route, init] = fetcher.mock.calls[0] ?? [];
  return {
    route,
    init: init as RequestInit,
    body: JSON.parse(String((init as RequestInit).body ?? "null")) as Record<string, unknown>,
  };
};
const codeOf = async (promise: Promise<unknown>) => {
  try {
    await promise;
  } catch (error) {
    return error instanceof KnowledgeError ? error.code : "other";
  }
  return "resolved";
};

describe("keepAttachment", () => {
  it("sends the exact route, auth and body", async () => {
    const { api, fetcher } = apiWith(() => json(kept, 201));
    await expect(api.keepAttachment(WORKSPACE, request, COMMAND)).resolves.toEqual(kept);
    const call = sent(fetcher);
    expect(call.route).toBe(`/api/v2/vaults/${WORKSPACE}/attachments`);
    expect(call.init.method).toBe("POST");
    expect(call.init.headers).toMatchObject({
      authorization: "Bearer tok",
      "content-type": "application/json",
    });
    expect(call.body).toEqual({
      command_id: COMMAND,
      policy_revision: 3,
      root_physical_id: "1:2",
      asset_id: ASSET,
      name: "photo",
    });
  });

  it("never sends a sha256, even when the caller's object carries one", async () => {
    const { api, fetcher } = apiWith(() => json(kept, 201));
    const stray = { ...request, sha256: "a".repeat(64) };
    await api.keepAttachment(WORKSPACE, stray, COMMAND);
    expect("sha256" in sent(fetcher).body).toBe(false);
  });

  it("accepts 200 for a reuse or a replayed command", async () => {
    const reused = { ...kept, reused: true };
    const { api } = apiWith(() => json(reused, 200));
    await expect(api.keepAttachment(WORKSPACE, request, COMMAND)).resolves.toEqual(reused);
  });

  it("refuses a malformed argument before any fetch", async () => {
    const { api, fetcher } = apiWith(() => json(kept, 201));
    const bad = [
      api.keepAttachment("nope", request, COMMAND),
      api.keepAttachment(WORKSPACE, request, "nope"),
      api.keepAttachment(WORKSPACE, { ...request, policyRevision: 0 }, COMMAND),
      api.keepAttachment(WORKSPACE, { ...request, rootPhysicalId: "x" }, COMMAND),
      api.keepAttachment(WORKSPACE, { ...request, assetId: "../x" }, COMMAND),
      api.keepAttachment(WORKSPACE, { ...request, name: "x".repeat(256) }, COMMAND),
    ];
    for (const attempt of bad) expect(await codeOf(attempt)).toMatch(/^invalid_/);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("rejects an answer whose ref is not exactly attachments/<leaf>", async () => {
    for (const ref of [
      "a.png",
      "attachments/x/y.png",
      "../attachments/a.png",
      "/etc/passwd",
      "attachments/a.png",
    ]) {
      const { api } = apiWith(() => json({ ...kept, ref }, 201));
      expect(await codeOf(api.keepAttachment(WORKSPACE, request, COMMAND))).toBe("unavailable");
    }
    const { api } = apiWith(() => json({ ...kept, reused: "no" }, 201));
    expect(await codeOf(api.keepAttachment(WORKSPACE, request, COMMAND))).toBe("unavailable");
  });
});

describe("backup", () => {
  it("previews with the exact route and a body of only the destination", async () => {
    const { api, fetcher } = apiWith(() => json(preview));
    await expect(api.backupPreview(WORKSPACE, "D:\\Backups")).resolves.toEqual(preview);
    const call = sent(fetcher);
    expect(call.route).toBe(`/api/v2/vaults/${WORKSPACE}/backup/preview`);
    expect(call.init.headers).toMatchObject({ authorization: "Bearer tok" });
    expect(call.body).toEqual({ destination: "D:\\Backups" });
  });

  it("runs with command_id and destination, and no policy, root or scope", async () => {
    const { api, fetcher } = apiWith(() => json(result, 201));
    await expect(api.backup(WORKSPACE, "D:\\Backups", COMMAND)).resolves.toEqual(result);
    const call = sent(fetcher);
    expect(call.route).toBe(`/api/v2/vaults/${WORKSPACE}/backup`);
    expect(call.body).toEqual({ command_id: COMMAND, destination: "D:\\Backups" });
  });

  it("accepts 200 with replayed: true for a replay", async () => {
    const replay = { ...result, replayed: true };
    const { api } = apiWith(() => json(replay, 200));
    await expect(api.backup(WORKSPACE, "D:\\Backups", COMMAND)).resolves.toEqual(replay);
  });

  it("refuses a malformed argument before any fetch", async () => {
    const { api, fetcher } = apiWith(() => json(result, 201));
    const bad = [
      api.backupPreview("nope", "D:\\B"),
      api.backupPreview(WORKSPACE, ""),
      api.backupPreview(WORKSPACE, "x".repeat(4097)),
      api.backup("nope", "D:\\B", COMMAND),
      api.backup(WORKSPACE, "", COMMAND),
      api.backup(WORKSPACE, "D:\\B", "nope"),
    ];
    for (const attempt of bad) expect(await codeOf(attempt)).toMatch(/^invalid_/);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("rejects a malformed answer", async () => {
    const a = apiWith(() => json({ ...preview, local_recovery: 1 }));
    expect(await codeOf(a.api.backupPreview(WORKSPACE, "D:\\B"))).toBe("unavailable");
    const b = apiWith(() => json({ ...result, replayed: undefined }, 201));
    expect(await codeOf(b.api.backup(WORKSPACE, "D:\\B", COMMAND))).toBe("unavailable");
    const c = apiWith(() => json({ ...result, skipped: [{ ref: "a" }] }, 201));
    expect(await codeOf(c.api.backup(WORKSPACE, "D:\\B", COMMAND))).toBe("unavailable");
  });

  it("keeps code-point-bounded instruction and skipped refs in backup answers", async () => {
    const folder = Array.from({ length: 5 }, () => "😀".repeat(60)).join("/");
    const instruction = `${folder}/CLAUDE.md`;
    const skipped = [{ ref: `${folder}/note.md`, reason: "unreadable" }];
    expect(instruction.length).toBeGreaterThan(512);
    const body = { ...preview, instruction_files: [instruction], skipped };
    const first = apiWith(() => json(body));
    await expect(first.api.backupPreview(WORKSPACE, "D:\\B")).resolves.toEqual(body);
    const backedUp = { ...result, skipped, skipped_total: 1 };
    const second = apiWith(() => json(backedUp, 201));
    await expect(second.api.backup(WORKSPACE, "D:\\B", COMMAND)).resolves.toEqual(backedUp);
    const invalid = { ...body, instruction_files: ["x".repeat(513)] };
    const third = apiWith(() => json(invalid));
    await expect(third.api.backupPreview(WORKSPACE, "D:\\B")).rejects.toMatchObject({
      code: "unavailable",
    });
  });
});

describe("errors", () => {
  it("reads a typed code from any non-ok status and never the message", async () => {
    for (const status of [403, 404, 409, 413, 423, 429, 503]) {
      const { api } = apiWith(() =>
        json({ error: { code: "denied_destination", message: "C:\\secret" } }, status),
      );
      const error = await api.backup(WORKSPACE, "D:\\B", COMMAND).catch((e: unknown) => e);
      expect(error).toBeInstanceOf(KnowledgeError);
      expect((error as KnowledgeError).code).toBe("denied_destination");
      expect((error as KnowledgeError).message).toBe("denied_destination");
    }
  });

  it("falls back to unavailable for an untyped, unreadable or unexpected-status answer", async () => {
    const replies = [
      () => json({ detail: "x" }, 500),
      () => new Response("<html>", { status: 502 }),
      () => json({ error: { code: "" } }, 409),
      () => json(result, 202),
    ];
    for (const reply of replies) {
      const { api } = apiWith(reply);
      expect(await codeOf(api.backup(WORKSPACE, "D:\\B", COMMAND))).toBe("unavailable");
    }
  });

  it("does not fetch when already aborted", async () => {
    const { api, fetcher } = apiWith(() => json(result, 201));
    const controller = new AbortController();
    controller.abort();
    await expect(api.backup(WORKSPACE, "D:\\B", COMMAND, controller.signal)).rejects.toThrow(
      "cancelled",
    );
    expect(fetcher).not.toHaveBeenCalled();
  });
});

const MEMORY = "00000000-0000-4000-8000-000000000004";
const SESSION = "00000000-0000-4000-8000-000000000005";
const HASH = "a".repeat(64);
const OTHER = "b".repeat(64);
const MEMORIES = `/api/v2/vaults/${WORKSPACE}/memories`;
const fields = {
  id: MEMORY,
  kind: "owner",
  scope: "vault",
  status: "approved",
  created: "2026-10-02T00:00:00.000Z",
  reviewed: null,
  creator: null,
  sources: [{ note: "notes/tea.md", sha256: HASH }, { session: SESSION }],
  supersedes: null,
  retired_by: null,
};
const item = {
  ref: `memory/${MEMORY}.md`,
  sha256: HASH,
  state: "pending",
  fields,
  claim: "Tea.",
  superseded_by: null,
};
const listing = {
  items: [item],
  invalid: [
    { ref: "memory/broken.md", reason: "duplicate_key", text: "---\n", state: "invalid" },
    { ref: "memory/locked.md", reason: "read_failed", text: null, state: "invalid" },
  ],
  partial: true,
};
const written = {
  memory_id: MEMORY,
  ref: `memory/${MEMORY}.md`,
  sha256: OTHER,
  operation_id: ASSET,
  created_folders: ["memory"],
  outcome: "written",
};
const proposed = {
  memory_id: MEMORY,
  ref: `memory/${MEMORY}.md`,
  proposal_id: ASSET,
  proposed_sha256: OTHER,
  outcome: "proposed",
};
const reviewed = { memory_id: MEMORY, sha256: HASH, outcome: "reviewed" };
const corrected = { outcome: "corrected", record: written, retired: written };
const draft: MemoryDraft = {
  kind: "owner",
  scope: "vault",
  claim: "Tea.",
  sources: [{ note: "notes/tea.md", sha256: HASH }, { session: SESSION }],
};
const create = { policyRevision: 3, rootPhysicalId: "1:2", ...draft };
const patch = { policyRevision: 3, rootPhysicalId: "1:2", sha256: HASH };
const admitted = { policy_revision: 3, root_physical_id: "1:2" };

describe("memories", () => {
  it("lists with GET, auth and no body, and keeps the invalid entries and partial", async () => {
    const { api, fetcher } = apiWith(() => json(listing));
    await expect(api.memories(WORKSPACE)).resolves.toEqual(listing);
    const [route, init] = fetcher.mock.calls[0] ?? [];
    expect(route).toBe(MEMORIES);
    expect((init as RequestInit).method).toBe("GET");
    expect((init as RequestInit).body).toBeUndefined();
    expect((init as RequestInit).headers).toEqual({ authorization: "Bearer tok" });
  });

  it("refuses a bad workspace before any fetch and rejects malformed answers", async () => {
    const { api, fetcher } = apiWith(() => json(listing));
    expect(await codeOf(api.memories("nope"))).toBe("invalid_workspace");
    expect(fetcher).not.toHaveBeenCalled();
    const bad = [
      { ...listing, partial: "yes" },
      { ...listing, items: [{ ...item, state: "invalid" }] },
      { ...listing, items: [{ ...item, sha256: "x" }] },
      { ...listing, items: [{ ...item, fields: { ...fields, kind: "boss" } }] },
      { ...listing, items: [{ ...item, fields: { ...fields, sources: [{ note: "a" }] } }] },
      { ...listing, invalid: [{ ref: "m", reason: "r", text: 1, state: "invalid" }] },
      { ...listing, invalid: [{ ref: "m", reason: "r", text: null, state: "pending" }] },
    ];
    for (const body of bad) {
      const a = apiWith(() => json(body));
      expect(await codeOf(a.api.memories(WORKSPACE))).toBe("unavailable");
    }
  });
});

const createDefault = (api: ReturnType<typeof createVaultKnowledgeApi>) =>
  api.createMemory(WORKSPACE, create, COMMAND);

describe("createMemory", () => {
  it("sends the exact route and body with command_id, admission and the draft", async () => {
    const { api, fetcher } = apiWith(() => json({ ...written, replayed: false }, 201));
    await api.createMemory(WORKSPACE, create, COMMAND);
    const call = sent(fetcher);
    expect(call.route).toBe(MEMORIES);
    expect(call.init.method).toBe("POST");
    expect(call.body).toEqual({ command_id: COMMAND, ...admitted, ...draft });
  });

  it("tells a written record from a proposal, on 201 and on a 200 replay", async () => {
    const first = apiWith(() => json({ ...written, replayed: false }, 201));
    const w = await first.api.createMemory(WORKSPACE, create, COMMAND);
    expect(w).toEqual({ ...written, replayed: false });
    expect(w.outcome).toBe("written");
    const retry = apiWith(() => json({ ...proposed, replayed: true }, 200));
    const p = await retry.api.createMemory(WORKSPACE, create, COMMAND);
    expect(p).toEqual({ ...proposed, replayed: true });
    expect(p.outcome).toBe("proposed");
  });

  it("rejects an answer without replayed, of another outcome, or a mixed shape", async () => {
    const bad = [
      written,
      { ...written, replayed: 1 },
      { ...written, outcome: "reviewed", replayed: false },
      { ...proposed, outcome: "written", replayed: false },
      { ...proposed, proposed_sha256: "x", replayed: false },
      { ...written, created_folders: "memory", replayed: false },
    ];
    for (const body of bad) {
      const { api } = apiWith(() => json(body, 201));
      expect(await codeOf(api.createMemory(WORKSPACE, create, COMMAND))).toBe("unavailable");
    }
  });

  it("accepts a null operation_id only on a replay", async () => {
    const replay = { ...written, operation_id: null, replayed: true };
    const a = apiWith(() => json(replay, 200));
    await expect(createDefault(a.api)).resolves.toEqual(replay);
    const b = apiWith(() => json({ ...replay, replayed: false }, 201));
    expect(await codeOf(createDefault(b.api))).toBe("unavailable");
  });

  it("refuses a malformed argument before any fetch", async () => {
    const { api, fetcher } = apiWith(() => json({ ...written, replayed: false }, 201));
    const bad = [
      api.createMemory("nope", create, COMMAND),
      api.createMemory(WORKSPACE, create, "nope"),
      api.createMemory(WORKSPACE, { ...create, policyRevision: 0 }, COMMAND),
      api.createMemory(WORKSPACE, { ...create, rootPhysicalId: "x" }, COMMAND),
      api.createMemory(WORKSPACE, { ...create, kind: "boss" as "owner" }, COMMAND),
      api.createMemory(WORKSPACE, { ...create, scope: "all" as "vault" }, COMMAND),
      api.createMemory(WORKSPACE, { ...create, claim: "a\u0001b" }, COMMAND),
      api.createMemory(WORKSPACE, { ...create, claim: "x".repeat(16_385) }, COMMAND),
      api.createMemory(WORKSPACE, { ...create, sources: [{ note: "a" }] as never }, COMMAND),
      api.createMemory(WORKSPACE, { ...create, sources: [{ session: "s" }] }, COMMAND),
      api.createMemory(
        WORKSPACE,
        { ...create, sources: Array.from({ length: 17 }, () => ({ session: SESSION })) },
        COMMAND,
      ),
    ];
    for (const attempt of bad) expect(await codeOf(attempt)).toMatch(/^invalid_/);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("allows tab, LF and CR in a claim", async () => {
    const { api } = apiWith(() => json({ ...written, replayed: false }, 201));
    await expect(
      api.createMemory(WORKSPACE, { ...create, claim: "a\tb\nc\r" }, COMMAND),
    ).resolves.toBeDefined();
  });
});

describe("composeMemory", () => {
  it("posts only the draft to compose and returns the text", async () => {
    const { api, fetcher } = apiWith(() => json({ text: "---\n" }));
    await expect(api.composeMemory(WORKSPACE, draft)).resolves.toEqual({ text: "---\n" });
    const call = sent(fetcher);
    expect(call.route).toBe(`${MEMORIES}/compose`);
    expect(call.body).toEqual(draft);
  });

  it("refuses a bad draft before any fetch and rejects a malformed answer", async () => {
    const { api, fetcher } = apiWith(() => json({ text: 1 }));
    expect(await codeOf(api.composeMemory("nope", draft))).toBe("invalid_workspace");
    expect(await codeOf(api.composeMemory(WORKSPACE, { ...draft, claim: "\u0000" }))).toBe(
      "invalid_request",
    );
    expect(fetcher).not.toHaveBeenCalled();
    expect(await codeOf(api.composeMemory(WORKSPACE, draft))).toBe("unavailable");
  });
});

describe("patchMemory", () => {
  const route = `${MEMORIES}/${MEMORY}`;

  it("sends each action with its own exact body", async () => {
    const cases = [
      [{ ...patch, action: "approve" }, { action: "approve" }, reviewed],
      [{ ...patch, action: "narrow_scope" }, { action: "narrow_scope" }, written],
      [
        { ...patch, action: "retire", retiredBy: null },
        { action: "retire", retired_by: null },
        written,
      ],
      [
        { ...patch, action: "retire", retiredBy: SESSION },
        { action: "retire", retired_by: SESSION },
        written,
      ],
      [
        { ...patch, action: "correct", claim: "Coffee." },
        { action: "correct", claim: "Coffee." },
        corrected,
      ],
    ] as const;
    for (const [request, extra, answer] of cases) {
      const { api, fetcher } = apiWith(() => json(answer));
      await api.patchMemory(WORKSPACE, MEMORY, request as never);
      const call = sent(fetcher);
      expect(call.route).toBe(route);
      expect(call.init.method).toBe("PATCH");
      expect(call.body).toEqual({ ...admitted, sha256: HASH, ...extra });
    }
  });

  it("drops a stray key carried by the request object", async () => {
    const { api, fetcher } = apiWith(() => json(reviewed));
    const stray = { ...patch, action: "approve", claim: "x", retiredBy: null } as never;
    await api.patchMemory(WORKSPACE, MEMORY, stray);
    expect(Object.keys(sent(fetcher).body).sort()).toEqual([
      "action",
      "policy_revision",
      "root_physical_id",
      "sha256",
    ]);
  });

  it("returns reviewed and corrected as the server sent them", async () => {
    const a = apiWith(() => json(reviewed));
    await expect(
      a.api.patchMemory(WORKSPACE, MEMORY, { ...patch, action: "approve" }),
    ).resolves.toEqual(reviewed);
    const b = apiWith(() => json(corrected));
    await expect(
      b.api.patchMemory(WORKSPACE, MEMORY, { ...patch, action: "correct", claim: "C" }),
    ).resolves.toEqual(corrected);
  });

  it("rejects an outcome the action cannot answer, or a malformed one", async () => {
    const cases = [
      [{ ...patch, action: "retire", retiredBy: null }, reviewed],
      [{ ...patch, action: "narrow_scope" }, reviewed],
      [{ ...patch, action: "approve" }, corrected],
      [{ ...patch, action: "correct", claim: "C" }, written],
      [
        { ...patch, action: "correct", claim: "C" },
        { ...corrected, retired: reviewed },
      ],
      [
        { ...patch, action: "approve" },
        { ...reviewed, sha256: "x" },
      ],
      [
        { ...patch, action: "retire", retiredBy: null },
        { ...written, operation_id: 1 },
      ],
      [
        { ...patch, action: "retire", retiredBy: null },
        { ...written, operation_id: null },
      ],
    ] as const;
    for (const [request, answer] of cases) {
      const { api } = apiWith(() => json(answer));
      expect(await codeOf(api.patchMemory(WORKSPACE, MEMORY, request as never))).toBe(
        "unavailable",
      );
    }
  });

  it("refuses a malformed argument before any fetch", async () => {
    const { api, fetcher } = apiWith(() => json(written));
    const retire = { ...patch, action: "retire", retiredBy: null } as const;
    const bad = [
      api.patchMemory("nope", MEMORY, retire),
      api.patchMemory(WORKSPACE, "nope", retire),
      api.patchMemory(WORKSPACE, MEMORY, { ...retire, sha256: "x" }),
      api.patchMemory(WORKSPACE, MEMORY, { ...retire, policyRevision: 0 }),
      api.patchMemory(WORKSPACE, MEMORY, { ...retire, rootPhysicalId: "x" }),
      api.patchMemory(WORKSPACE, MEMORY, { ...retire, retiredBy: "nope" }),
      api.patchMemory(WORKSPACE, MEMORY, { ...retire, retiredBy: undefined as never }),
      api.patchMemory(WORKSPACE, MEMORY, { ...patch, action: "correct", claim: "\u0001" }),
      api.patchMemory(WORKSPACE, MEMORY, { ...patch, action: "correct" } as never),
      api.patchMemory(WORKSPACE, MEMORY, { ...patch, action: "delete" } as never),
    ];
    for (const attempt of bad) expect(await codeOf(attempt)).toMatch(/^invalid_/);
    expect(fetcher).not.toHaveBeenCalled();
  });
});

const restorePreview = {
  source_name: "Vault",
  source_policy: "protected",
  protected: true,
  files: 2,
  bytes: 30,
  instruction_files: ["CLAUDE.md"],
  instruction_files_total: 1,
  problems: [],
  problems_total: 0,
  backup_skipped: 1,
};
const restoreResult = {
  workspace: { workspace_id: ASSET, name: "Restored copy", policy: "protected" },
  files: 1,
  bytes: 10,
  verified: false,
  problems: [{ ref: "notes/gone.md", reason: "missing" }],
  problems_total: 1,
  replayed: false,
};
const restoreRequest = { backup: "D:\\Backups\\b", parent: "D:\\Restore", folder: "Copy" };

describe("backup restore", () => {
  it("previews with the exact route and only the backup path", async () => {
    const { api, fetcher } = apiWith(() => json(restorePreview));
    await expect(api.backupRestorePreview("D:\\Backups\\b")).resolves.toEqual(restorePreview);
    const call = sent(fetcher);
    expect(call.route).toBe("/api/v2/backup-restore/preview");
    expect(call.init.method).toBe("POST");
    expect(call.body).toEqual({ backup: "D:\\Backups\\b" });
  });

  it("keeps code-point-bounded instruction and problem refs in restore answers", async () => {
    const folder = Array.from({ length: 5 }, () => "😀".repeat(60)).join("/");
    const problems = [{ ref: `${folder}/note.md`, reason: "missing" }];
    const body = {
      ...restorePreview,
      instruction_files: [`${folder}/CLAUDE.md`],
      problems,
      problems_total: 1,
    };
    const first = apiWith(() => json(body));
    await expect(first.api.backupRestorePreview(restoreRequest.backup)).resolves.toEqual(body);
    const restored = { ...restoreResult, problems };
    const second = apiWith(() => json(restored, 201));
    await expect(second.api.backupRestore(COMMAND, restoreRequest)).resolves.toEqual(restored);
  });

  it("applies with command_id, backup, parent and folder; 201 and 200 both succeed", async () => {
    const first = apiWith(() => json(restoreResult, 201));
    await expect(first.api.backupRestore(COMMAND, restoreRequest)).resolves.toEqual(restoreResult);
    expect(sent(first.fetcher).route).toBe("/api/v2/backup-restore");
    expect(sent(first.fetcher).body).toEqual({ command_id: COMMAND, ...restoreRequest });
    const replay = { ...restoreResult, replayed: true };
    const again = apiWith(() => json(replay, 200));
    await expect(again.api.backupRestore(COMMAND, restoreRequest)).resolves.toEqual(replay);
  });

  it("sends only the three request fields", async () => {
    const { api, fetcher } = apiWith(() => json(restoreResult, 201));
    await api.backupRestore(COMMAND, { ...restoreRequest, policyRevision: 3 } as never);
    expect(Object.keys(sent(fetcher).body).sort()).toEqual([
      "backup",
      "command_id",
      "folder",
      "parent",
    ]);
  });

  it("refuses a malformed or relative argument before any fetch", async () => {
    const { api, fetcher } = apiWith(() => json(restoreResult, 201));
    const bad = [
      api.backupRestorePreview("backups\\b"),
      api.backupRestorePreview("./b"),
      api.backupRestorePreview(""),
      api.backupRestorePreview("D:\\b\u0000"),
      api.backupRestorePreview("x".repeat(4097)),
      api.backupRestore("nope", restoreRequest),
      api.backupRestore(COMMAND, { ...restoreRequest, backup: "b" }),
      api.backupRestore(COMMAND, { ...restoreRequest, parent: "Restore" }),
      api.backupRestore(COMMAND, { ...restoreRequest, parent: "D:\\p\u007f" }),
      api.backupRestore(COMMAND, { ...restoreRequest, folder: "" }),
      api.backupRestore(COMMAND, { ...restoreRequest, folder: "x".repeat(256) }),
      api.backupRestore(COMMAND, { ...restoreRequest, folder: "a\tb" }),
    ];
    for (const attempt of bad) expect(await codeOf(attempt)).toMatch(/^invalid_/);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("accepts a drive, a UNC and a rooted path", async () => {
    for (const backup of ["C:/b", "\\\\host\\share\\b", "/home/me/b"]) {
      const { api } = apiWith(() => json(restorePreview));
      await expect(api.backupRestorePreview(backup)).resolves.toBeDefined();
    }
  });

  it("rejects malformed answers", async () => {
    const previews = [
      { ...restorePreview, protected: "yes" },
      { ...restorePreview, backup_skipped: -1 },
      { ...restorePreview, problems: [{ ref: "a" }] },
      { ...restorePreview, source_name: 1 },
    ];
    for (const body of previews) {
      const { api } = apiWith(() => json(body));
      expect(await codeOf(api.backupRestorePreview("D:\\b"))).toBe("unavailable");
    }
    const results = [
      { ...restoreResult, replayed: undefined },
      { ...restoreResult, verified: 1 },
      { ...restoreResult, workspace: { ...restoreResult.workspace, workspace_id: "x" } },
      { ...restoreResult, workspace: null },
      { ...restoreResult, problems: "none" },
    ];
    for (const body of results) {
      const { api } = apiWith(() => json(body, 201));
      expect(await codeOf(api.backupRestore(COMMAND, restoreRequest))).toBe("unavailable");
    }
  });

  it("reads the typed code on a refusal and never the message", async () => {
    const reply = () => json({ error: { code: "invalid_backup", message: "C:\\x" } }, 400);
    const { api } = apiWith(reply);
    const error = await api.backupRestore(COMMAND, restoreRequest).catch((e: unknown) => e);
    expect((error as KnowledgeError).code).toBe("invalid_backup");
    expect((error as KnowledgeError).message).toBe("invalid_backup");
  });
});

describe("relative backup path", () => {
  it("refuses a relative destination before any fetch", async () => {
    const { api, fetcher } = apiWith(() => json(result, 201));
    expect(await codeOf(api.backupPreview(WORKSPACE, "Backups"))).toBe("invalid_path");
    expect(await codeOf(api.backup(WORKSPACE, "..\\B", COMMAND))).toBe("invalid_path");
    expect(fetcher).not.toHaveBeenCalled();
  });
});

describe("backup diagnostic coverage", () => {
  it("accepts legacy full lists and supplies their displayed lengths as totals", async () => {
    const legacy = (body: object) =>
      Object.fromEntries(Object.entries(body).filter(([key]) => !key.endsWith("_total")));
    const a = apiWith(() => json(legacy(preview)));
    await expect(a.api.backupPreview(WORKSPACE, "D:\\B")).resolves.toMatchObject({
      instruction_files_total: 1,
      skipped_total: 1,
    });
    const b = apiWith(() => json(legacy(result)));
    await expect(b.api.backup(WORKSPACE, "D:\\B", COMMAND)).resolves.toMatchObject({
      skipped_total: 0,
    });
    const c = apiWith(() => json(legacy(restorePreview)));
    await expect(c.api.backupRestorePreview("D:\\B")).resolves.toMatchObject({
      instruction_files_total: 1,
      problems_total: 0,
    });
    const d = apiWith(() => json(legacy(restoreResult)));
    await expect(d.api.backupRestore(COMMAND, restoreRequest)).resolves.toMatchObject({
      problems_total: 1,
    });
  });

  it("preserves independent totals and mutation outcomes when no diagnostic rows fit", async () => {
    const bodies = [
      {
        ...preview,
        instruction_files: [],
        instruction_files_total: 30,
        skipped: [],
        skipped_total: 2001,
      },
      { ...result, skipped: [], skipped_total: 2001 },
      {
        ...restorePreview,
        instruction_files: [],
        instruction_files_total: 30,
        problems: [],
        problems_total: 2000,
      },
      { ...restoreResult, problems: [], problems_total: 2000 },
    ];
    const calls = [
      (api: ReturnType<typeof createVaultKnowledgeApi>) => api.backupPreview(WORKSPACE, "D:\\B"),
      (api: ReturnType<typeof createVaultKnowledgeApi>) => api.backup(WORKSPACE, "D:\\B", COMMAND),
      (api: ReturnType<typeof createVaultKnowledgeApi>) => api.backupRestorePreview("D:\\B"),
      (api: ReturnType<typeof createVaultKnowledgeApi>) =>
        api.backupRestore(COMMAND, restoreRequest),
    ];
    for (const [index, body] of bodies.entries()) {
      const { api } = apiWith(() => json(body));
      await expect(calls[index]?.(api)).resolves.toEqual(body);
    }
  });

  it.each([-1, 0, 0.5, Number.MAX_SAFE_INTEGER + 1, null, "1"])(
    "rejects the provided invalid or below-displayed total %s on every route",
    async (total) => {
      const a = apiWith(() => json({ ...preview, skipped_total: total }));
      expect(await codeOf(a.api.backupPreview(WORKSPACE, "D:\\B"))).toBe("unavailable");
      const b = apiWith(() => json({ ...result, skipped: preview.skipped, skipped_total: total }));
      expect(await codeOf(b.api.backup(WORKSPACE, "D:\\B", COMMAND))).toBe("unavailable");
      const c = apiWith(() => json({ ...restorePreview, instruction_files_total: total }));
      expect(await codeOf(c.api.backupRestorePreview("D:\\B"))).toBe("unavailable");
      const d = apiWith(() => json({ ...restoreResult, problems_total: total }));
      expect(await codeOf(d.api.backupRestore(COMMAND, restoreRequest))).toBe("unavailable");
    },
  );

  it("retains the 2000-row and 256 KiB response ceilings", async () => {
    const skips = Array.from({ length: 2001 }, (_, index) => ({
      ref: `${index}.md`,
      reason: "missing",
    }));
    const a = apiWith(() => json({ ...preview, skipped: skips, skipped_total: skips.length }));
    expect(await codeOf(a.api.backupPreview(WORKSPACE, "D:\\B"))).toBe("unavailable");
    const b = apiWith(() => json({ ...preview, padding: "x".repeat(256 * 1024) }));
    expect(await codeOf(b.api.backupPreview(WORKSPACE, "D:\\B"))).toBe("unavailable");
  });
});
