import { describe, expect, it, vi } from "vitest";
import {
  createSkillProposalsApi,
  isSkillProposalItem,
  SKILL_STATE_WORDS,
  SKILL_TRIGGER_WORDS,
  SkillProposalsApiError,
  skillRefusal,
} from "./skillProposalsApi";

const WS = "00000000-0000-4000-8000-000000000001";
const PID = "00000000-0000-4000-8000-000000000002";
const SID = "00000000-0000-4000-8000-000000000003";
const HASH = "a".repeat(64);
const AT = "2026-10-02T03:04:05.678Z";

const validItem = (over: Record<string, unknown> = {}): Record<string, unknown> => ({
  kind: "skill_proposal",
  id: PID,
  origin: { workspace_id: WS, session_id: null },
  revision: HASH,
  state: "open",
  attention: true,
  title: "New skill: my-skill",
  name: "my-skill",
  relative_id: "skills/my-skill/SKILL.md",
  trigger: "button",
  sessions: [SID],
  base_revision: null,
  text_bytes: 42,
  approved_at: null,
  rejected_at: null,
  reason: null,
  created_at: AT,
  updated_at: AT,
  ...over,
});
const validDetail = (over: Record<string, unknown> = {}): Record<string, unknown> => ({
  ...validItem(),
  text: "---\nname: my-skill\ndescription: does a thing\n---\nbody",
  current_text: null,
  diff: "",
  diff_reason: "no_source",
  description: "does a thing",
  unsupported_fields: [],
  shadowed: null,
  policy: "ordinary",
  ...over,
});
const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

function setup(response: Response) {
  const fetchImpl = vi.fn(async () => response);
  return { fetchImpl, api: createSkillProposalsApi("tok", fetchImpl as unknown as typeof fetch) };
}
const sent = (fetchImpl: ReturnType<typeof vi.fn>) => {
  const [route, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
  return { route, init, headers: init.headers as Record<string, string> };
};
const invalid = async (promise: Promise<unknown>) =>
  expect(((await promise.catch((error) => error)) as SkillProposalsApiError).code).toBe(
    "invalid_response",
  );

describe("skill proposals API paths and bodies", () => {
  it("POSTs a create and reads a 201, then a replayed 200", async () => {
    const created = setup(reply({ proposal: validItem() }, 201));
    const result = await created.api.create(WS, "text", "button", [SID]);
    expect(result.replayed).toBe(false);
    expect(result.item?.name).toBe("my-skill");
    const { route, init, headers } = sent(created.fetchImpl);
    expect(route).toBe(`/api/v2/vaults/${WS}/skill-proposals`);
    expect(init.method).toBe("POST");
    expect(headers).toEqual({ authorization: "Bearer tok", "content-type": "application/json" });
    expect(JSON.parse(init.body as string)).toEqual({
      text: "text",
      trigger: "button",
      sessions: [SID],
    });

    const replayed = setup(reply({ proposal: { id: "p9" }, replayed: true }));
    expect(await replayed.api.create(WS, "text", "button", [SID])).toEqual({
      id: "p9",
      item: null,
      replayed: true,
    });
  });

  it("GETs a proposal's detail with a Bearer token and no body", async () => {
    const detail = validDetail();
    const { api, fetchImpl } = setup(reply({ proposal: detail }));
    expect(await api.detail(PID)).toEqual(detail);
    const { route, init, headers } = sent(fetchImpl);
    expect(route).toBe(`/api/v2/decisions/skills/${PID}`);
    expect(init.method).toBe("GET");
    expect(init.body).toBeUndefined();
    expect(headers).toEqual({ authorization: "Bearer tok" });
  });

  it("POSTs an act with its optional text and reason, and a withdraw with neither", async () => {
    const edited = setup(reply({ proposal: validItem({ state: "handed", approved_at: AT }) }));
    const result = await edited.api.act(PID, "edit", HASH, "new text", "because");
    expect(result?.state).toBe("handed");
    const { route, init, headers } = sent(edited.fetchImpl);
    expect(route).toBe(`/api/v2/decisions/skills/${PID}`);
    expect(init.method).toBe("POST");
    expect(headers["content-type"]).toBe("application/json");
    expect(JSON.parse(init.body as string)).toEqual({
      action: "edit",
      revision: HASH,
      text: "new text",
      reason: "because",
    });

    const withdrawn = setup(reply({ withdrawn: true }));
    expect(await withdrawn.api.act(PID, "withdraw", HASH)).toBeNull();
    expect(sent(withdrawn.fetchImpl).route).toBe(`/api/v2/decisions/skills/${PID}`);
    expect(JSON.parse(sent(withdrawn.fetchImpl).init.body as string)).toEqual({
      action: "withdraw",
      revision: HASH,
    });
  });
});

describe("skill proposals API failures", () => {
  it("carries the server's code and details", async () => {
    const { api } = setup(
      reply({ error: { code: "stale_revision", message: "m", details: { revision: HASH } } }, 409),
    );
    const failure = await api.act(PID, "approve", HASH).catch((e) => e);
    expect(failure).toBeInstanceOf(SkillProposalsApiError);
    expect(failure.code).toBe("stale_revision");
    expect(failure.extra).toEqual({ revision: HASH });
  });

  it.each([
    [413, "too_large"],
    [404, "unknown_proposal"],
    [403, "forbidden_origin"],
  ])("maps %i to its code", async (status, code) => {
    const { api } = setup(reply({ error: { code, message: "m" } }, status));
    const failure = await api.create(WS, "t", "button", [SID]).catch((e) => e);
    expect(failure.code).toBe(code);
    expect(failure.extra).toEqual({});
  });

  it("falls back to unavailable for an unreadable error body", async () => {
    const { api } = setup(new Response("<html>", { status: 502 }));
    expect((await api.detail(PID).catch((e) => e)).code).toBe("unavailable");
  });

  it("refuses an aborted call before fetching", async () => {
    const { api, fetchImpl } = setup(reply({}));
    const controller = new AbortController();
    controller.abort();
    expect((await api.detail(PID, controller.signal).catch((e) => e)).code).toBe("cancelled");
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});

describe("skill proposals API reply validation", () => {
  it("refuses a malformed create, detail and act reply", async () => {
    await invalid(
      setup(reply({ proposal: validItem({ revision: "short" }) }, 201)).api.create(
        WS,
        "t",
        "button",
        [SID],
      ),
    );
    await invalid(
      setup(reply({ proposal: { id: "p9" } }, 201)).api.create(WS, "t", "button", [SID]),
    );
    await invalid(setup(reply({ proposal: validDetail({ policy: "locked" }) })).api.detail(PID));
    await invalid(
      setup(reply({ proposal: validItem({ state: "lost" }) })).api.act(PID, "approve", HASH),
    );
    await invalid(setup(reply({ withdrawn: false })).api.act(PID, "withdraw", HASH));
  });

  it("refuses a 200 that is not JSON", async () => {
    await invalid(setup(new Response("nope", { status: 200 })).api.detail(PID));
  });

  it("accepts a valid item", () => {
    expect(isSkillProposalItem(validItem())).toBe(true);
  });

  it.each([
    ["a non-uuid id", { id: "not-a-uuid" }],
    ["a short revision", { revision: "short" }],
    ["an uppercase name", { name: "My-Skill", relative_id: "skills/My-Skill/SKILL.md" }],
    ["a double-hyphen name", { name: "my--skill", relative_id: "skills/my--skill/SKILL.md" }],
    ["a leading-hyphen name", { name: "-my-skill", relative_id: "skills/-my-skill/SKILL.md" }],
    ["a trailing-hyphen name", { name: "my-skill-", relative_id: "skills/my-skill-/SKILL.md" }],
    ["a 65-char name", { name: "a".repeat(65), relative_id: `skills/${"a".repeat(65)}/SKILL.md` }],
    ["a relative_id not matching the name", { relative_id: "skills/other/SKILL.md" }],
    ["a non-hex base revision", { base_revision: "nothex" }],
    ["zero sessions", { sessions: [] }],
    ["11 sessions", { sessions: Array.from({ length: 11 }, () => SID) }],
    ["a non-uuid session", { sessions: ["not-a-uuid"] }],
    ["zero text_bytes", { text_bytes: 0 }],
    ["text_bytes over the cap", { text_bytes: 131073 }],
    ["a fractional text_bytes", { text_bytes: 1.5 }],
    ["a numeric approved_at", { approved_at: 123 }],
    ["an empty reason", { reason: "" }],
    ["a 201-char reason", { reason: "a".repeat(201) }],
    ["an unknown state", { state: "deferred" }],
    ["an unknown trigger", { trigger: "cron" }],
    ["an unknown kind", { kind: "vault_proposal" }],
  ] as [string, Record<string, unknown>][])("rejects %s", (_label, override) => {
    expect(isSkillProposalItem(validItem(override))).toBe(false);
  });

  it.each([
    ["a non-string text", { text: 5 }],
    ["a numeric current_text", { current_text: 5 }],
    ["a non-string diff", { diff: null }],
    ["an unknown diff_reason", { diff_reason: "oops" }],
    ["a non-string description", { description: null }],
    ["a non-array unsupported_fields", { unsupported_fields: "none" }],
    ["a non-string entry in unsupported_fields", { unsupported_fields: [1] }],
    ["a stringly shadowed", { shadowed: "true" }],
    ["an unknown policy", { policy: "locked" }],
  ] as [string, Record<string, unknown>][])(
    "refuses a detail with %s",
    async (_label, override) => {
      await invalid(setup(reply({ proposal: validDetail(override) })).api.detail(PID));
    },
  );
});

describe("skill proposal words", () => {
  it("has the exact state and trigger words", () => {
    expect(SKILL_STATE_WORDS).toEqual({
      open: "Proposed",
      handed: "Approved, not applied yet",
      applied: "Skill file matches the proposal",
      rejected: "Rejected",
      stale: "Skill changed since",
      source_missing: "Skill file not found",
      unknown: "State unknown",
    });
    expect(SKILL_TRIGGER_WORDS).toEqual({
      button: "You asked for it",
      long_session: "A long task finished",
      repeated_request: "The same request in several chats",
      correction: "Your correction in a chat",
    });
  });
});

describe("skillRefusal", () => {
  const CASES: [string, string][] = [
    ["too_large", "This skill is over 128 KiB."],
    ["no_vault", "Open a vault to propose skills."],
    ["not_active", "Open this proposal's vault to act on it."],
    ["catalog_partial", "VIKI Agent could not read this vault's skills in full. Try again."],
    ["no_change", "The vault already has this exact skill."],
    [
      "proposal_conflict",
      "Approvals already holds a proposal for this skill. Open it to edit it, or withdraw it first.",
    ],
    [
      "proposals_full",
      "Approvals already keeps 100 skill proposals for this vault. Withdraw some first.",
    ],
    ["stale_revision", "This proposal changed since you opened it. Open it again."],
    ["not_approvable", "This proposal cannot be approved in its current state."],
    ["already_applied", "The skill file already matches this proposal."],
    ["name_changed", "An edit keeps the skill's name. Propose a new skill for a new name."],
    ["unknown_proposal", "This proposal is gone."],
    ["something_new_and_unmapped", "That did not work. Try again."],
  ];
  it.each(CASES)("maps %s to its line", (code, text) => {
    expect(skillRefusal(new SkillProposalsApiError(code))).toBe(text);
  });

  const REASONS: [string, string][] = [
    ["missing_frontmatter", "it needs frontmatter between --- lines at the top"],
    ["malformed_metadata", "its frontmatter is not in a form VIKI Agent reads"],
    ["missing_name", "its frontmatter has no name"],
    [
      "invalid_name",
      "its name must be lowercase letters, digits and single hyphens, at most 64 characters",
    ],
    ["reserved_name", "Claude Code reserves that name"],
    ["missing_description", "its frontmatter has no description"],
    ["long_description", "its description is over 1,536 characters"],
    ["carriage_return", "it holds characters VIKI Agent does not keep"],
    ["control_character", "it holds characters VIKI Agent does not keep"],
    ["invalid_encoding", "it holds characters VIKI Agent does not keep"],
    ["some_unlisted_reason", "its file is not in the expected form"],
  ];
  it.each(REASONS)("explains invalid_skill reason %s", (reason, words) => {
    expect(skillRefusal(new SkillProposalsApiError("invalid_skill", { reason }))).toBe(
      `This is not a skill VIKI Agent can propose: ${words}.`,
    );
  });

  it("falls back for a non-API error and for a missing reason", () => {
    expect(skillRefusal(new Error("boom"))).toBe("That did not work. Try again.");
    expect(skillRefusal("nope")).toBe("That did not work. Try again.");
    expect(skillRefusal(new SkillProposalsApiError("invalid_skill"))).toBe(
      "This is not a skill VIKI Agent can propose: its file is not in the expected form.",
    );
  });

  it("keeps every refusal line ASCII: no Japanese literal", () => {
    const all = [...CASES.map(([, text]) => text), ...REASONS.map(([, words]) => words)];
    for (const text of all) expect([...text].every((ch) => ch.charCodeAt(0) < 128)).toBe(true);
  });
});
