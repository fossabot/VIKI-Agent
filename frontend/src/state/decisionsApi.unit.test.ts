import { describe, expect, it, vi } from "vitest";
import { createDecisionsApi, DecisionsApiError } from "./decisionsApi";

const HASH = "a".repeat(64);
const ID = "b".repeat(32);
const AT = "2026-10-02T03:04:05.678Z";
const WS = "00000000-0000-4000-8000-000000000001";

const retro = (over: Record<string, unknown> = {}) => ({
  kind: "retro_proposal",
  id: ID,
  origin: { workspace_id: WS, session_id: null },
  revision: HASH,
  state: "decision_drafted",
  attention: true,
  title: "R-1 — item",
  source: "system/retro/2026-10-01.md",
  item_id: "R-1",
  label: "label",
  owner_applies: false,
  choice: "approve",
  snoozed: false,
  handed_at: null,
  mismatch: false,
  outcome: "open",
  ...over,
});
const proposal = (over: Record<string, unknown> = {}) => ({
  kind: "vault_proposal",
  id: "c0c0c0c0-0000-4000-8000-000000000002",
  origin: { workspace_id: WS, session_id: null },
  revision: HASH,
  state: "open",
  attention: true,
  title: "a.md",
  ref: "a.md",
  origin_kind: "note_edit",
  base_revision: HASH,
  text_bytes: 5,
  handed_at: null,
  created_at: AT,
  ...over,
});
const skill = (over: Record<string, unknown> = {}) => ({
  kind: "skill_proposal",
  id: "dddddddd-dddd-dddd-dddd-dddddddddddd",
  origin: { workspace_id: WS, session_id: null },
  revision: HASH,
  state: "open",
  attention: true,
  title: "my-skill",
  name: "my-skill",
  relative_id: "skills/my-skill/SKILL.md",
  trigger: "button",
  sessions: [WS],
  base_revision: null,
  text_bytes: 42,
  approved_at: null,
  rejected_at: null,
  reason: null,
  created_at: AT,
  updated_at: AT,
  ...over,
});
const questionProposal = (over: Record<string, unknown> = {}) => ({
  kind: "question_proposal",
  id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
  origin: { workspace_id: WS, session_id: null },
  revision: HASH,
  state: "open",
  attention: true,
  title: "Question change: Worth a note?",
  name: "worth_note",
  created_at: AT,
  updated_at: AT,
  approved_at: null,
  rejected_at: null,
  reason: null,
  ...over,
});
const changeProposal = (over: Record<string, unknown> = {}) => ({
  kind: "change_proposal",
  id: "ffffffff-ffff-4fff-8fff-ffffffffffff",
  origin: { workspace_id: WS, session_id: null },
  revision: HASH,
  state: "open",
  attention: true,
  title: "Link a.md to b.md",
  change: "note_link",
  ref: "a.md",
  created_at: AT,
  updated_at: AT,
  approved_at: null,
  rejected_at: null,
  reason: null,
  ...over,
});
const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

function setup(response: Response) {
  const fetchImpl = vi.fn(async () => response);
  return { fetchImpl, api: createDecisionsApi("tok", fetchImpl as unknown as typeof fetch) };
}
const sent = (fetchImpl: ReturnType<typeof vi.fn>) => {
  const [route, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
  return { route, init, headers: init.headers as Record<string, string> };
};
const invalid = async (promise: Promise<unknown>) =>
  expect(((await promise.catch((error) => error)) as DecisionsApiError).code).toBe(
    "invalid_response",
  );

describe("decisions API paths and bodies", () => {
  it("GETs the inbox with a Bearer token and no body or Origin", async () => {
    const { api, fetchImpl } = setup(reply({ items: [proposal(), skill()] }));
    const inbox = await api.decisions();
    expect(inbox.items).toHaveLength(2);
    const { route, init, headers } = sent(fetchImpl);
    expect(route).toBe("/api/v2/decisions");
    expect(init.method).toBe("GET");
    expect(init.body).toBeUndefined();
    expect(headers).toEqual({ authorization: "Bearer tok" });
  });

  it("the inbox accepts a skill proposal item and still refuses a malformed one", async () => {
    const { api } = setup(reply({ items: [proposal(), skill()] }));
    const inbox = await api.decisions();
    expect(inbox.items).toHaveLength(2);
    await invalid(
      setup(reply({ items: [skill({ relative_id: "skills/other/SKILL.md" })] })).api.decisions(),
    );
  });

  it("an inbox holding a question proposal still loads", async () => {
    const { api } = setup(reply({ items: [proposal(), skill(), questionProposal()] }));
    const inbox = await api.decisions();
    expect(inbox.items).toHaveLength(3);
  });

  it("accepts a change item", async () => {
    const { api } = setup(reply({ items: [changeProposal()] }));
    const inbox = await api.decisions();
    expect(inbox.items).toEqual([changeProposal()]);
  });

  it("an inbox holding a change proposal alongside the others still loads", async () => {
    const { api } = setup(
      reply({ items: [proposal(), skill(), questionProposal(), changeProposal()] }),
    );
    const inbox = await api.decisions();
    expect(inbox.items).toHaveLength(4);
  });

  it("GETs a proposal's detail", async () => {
    const detail = {
      ...proposal(),
      current_revision: null,
      proposed_text: "x",
      diff: "",
      diff_reason: "no_source",
    };
    const { api, fetchImpl } = setup(reply({ proposal: detail }));
    expect(await api.proposalDetail("p1")).toEqual(detail);
    expect(sent(fetchImpl).route).toBe("/api/v2/decisions/proposals/p1");
  });

  it("POSTs a handoff and a withdraw", async () => {
    const handed = setup(reply({ proposal: proposal({ state: "handed", handed_at: AT }) }));
    expect((await handed.api.proposalAct("p1", "handoff"))?.state).toBe("handed");
    expect(JSON.parse(sent(handed.fetchImpl).init.body as string)).toEqual({ action: "handoff" });
    const withdrawn = setup(reply({ withdrawn: true }));
    expect(await withdrawn.api.proposalAct("p1", "withdraw")).toBeNull();
    expect(sent(withdrawn.fetchImpl).route).toBe("/api/v2/decisions/proposals/p1");
  });

  it("POSTs a create and reads a 201 and a replayed 200", async () => {
    const created = setup(reply({ proposal: proposal() }, 201));
    const result = await created.api.createProposal(WS, "a.md", "text", HASH);
    expect(result.replayed).toBe(false);
    expect(result.item?.ref).toBe("a.md");
    const { route, init } = sent(created.fetchImpl);
    expect(route).toBe(`/api/v2/vaults/${WS}/proposals`);
    expect(JSON.parse(init.body as string)).toEqual({
      ref: "a.md",
      text: "text",
      base_revision: HASH,
    });
    const replayed = setup(reply({ proposal: { id: "p9" }, replayed: true }));
    expect(await replayed.api.createProposal(WS, "a.md", "text", HASH)).toEqual({
      id: "p9",
      item: null,
      replayed: true,
    });
  });
});

describe("decisions API failures", () => {
  it("carries the server's code and details", async () => {
    const { api } = setup(
      reply({ error: { code: "stale_proposals", message: "m", details: { stale: [ID] } } }, 409),
    );
    const failure = await api.proposalAct("p1", "handoff").catch((e) => e);
    expect(failure).toBeInstanceOf(DecisionsApiError);
    expect(failure.code).toBe("stale_proposals");
    expect(failure.extra).toEqual({ stale: [ID] });
  });

  it.each([
    [413, "too_large"],
    [415, "invalid_representation"],
    [403, "forbidden_origin"],
  ])("maps %i to its code", async (status, code) => {
    const { api } = setup(reply({ error: { code, message: "m" } }, status));
    const failure = await api.createProposal(WS, "a.md", "t", HASH).catch((e) => e);
    expect(failure.code).toBe(code);
    expect(failure.extra).toEqual({});
  });

  it("falls back to unavailable for an unreadable error body", async () => {
    const { api } = setup(new Response("<html>", { status: 502 }));
    expect((await api.decisions().catch((e) => e)).code).toBe("unavailable");
  });

  it("refuses an aborted call before fetching", async () => {
    const { api, fetchImpl } = setup(reply({}));
    const controller = new AbortController();
    controller.abort();
    expect((await api.decisions(controller.signal).catch((e) => e)).code).toBe("cancelled");
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});

describe("decisions API reply validation", () => {
  it("refuses a reply with no items array", async () => {
    await invalid(setup(reply({})).api.decisions());
    await invalid(setup(reply({ items: null })).api.decisions());
  });

  it("refuses a numeric timestamp, which the server never sends", async () => {
    await invalid(
      setup(reply({ items: [proposal({ created_at: 1759374245678 })] })).api.decisions(),
    );
    await invalid(
      setup(reply({ items: [proposal({ handed_at: 1759374245678 })] })).api.decisions(),
    );
  });

  it("refuses an unknown kind or state, and a session origin", async () => {
    for (const items of [
      [proposal({ kind: "permission" })],
      [proposal({ state: "bogus" })],
      [proposal({ origin: { workspace_id: WS, session_id: "s" } })],
      [proposal({ revision: "short" })],
    ])
      await invalid(setup(reply({ items })).api.decisions());
  });

  it("refuses a retro_proposal item", async () => {
    await invalid(setup(reply({ items: [retro()] })).api.decisions());
  });

  it("refuses a malformed question proposal", async () => {
    for (const items of [
      [questionProposal({ name: "Worth-Note" })],
      [questionProposal({ name: "a".repeat(33) })],
      [questionProposal({ state: "handed" })],
      [questionProposal({ reason: "" })],
      [questionProposal({ reason: "x".repeat(201) })],
      [questionProposal({ id: "not-a-uuid" })],
      [questionProposal({ updated_at: null })],
      [questionProposal({ attention: "false" })],
    ])
      await invalid(setup(reply({ items })).api.decisions());
  });

  it("refuses a malformed change proposal", async () => {
    for (const items of [
      [changeProposal({ change: "note_edit" })],
      [changeProposal({ state: "handed" })],
      [changeProposal({ reason: 1 })],
      [changeProposal({ id: "not-a-uuid" })],
      [changeProposal({ ref: 1 })],
      [changeProposal({ updated_at: null })],
      [changeProposal({ attention: "false" })],
    ])
      await invalid(setup(reply({ items })).api.decisions());
  });

  it("refuses a change item with an extra or a missing key", async () => {
    await invalid(setup(reply({ items: [{ ...changeProposal(), extra: "x" }] })).api.decisions());
    const missing = changeProposal() as Record<string, unknown>;
    delete missing.reason;
    await invalid(setup(reply({ items: [missing] })).api.decisions());
  });

  it("ignores an unknown key such as retro in the inbox reply", async () => {
    const { api } = setup(reply({ items: [proposal()], retro: "unexpected" }));
    const inbox = await api.decisions();
    expect(inbox.items).toHaveLength(1);
  });

  it("refuses a malformed detail and act reply", async () => {
    await invalid(setup(reply({ proposal: proposal() })).api.proposalDetail("p1"));
    await invalid(setup(reply({ withdrawn: false })).api.proposalAct("p1", "withdraw"));
    await invalid(
      setup(reply({ proposal: { id: "p9" } }, 201)).api.createProposal(WS, "a.md", "t", HASH),
    );
  });

  it("loads a memory promotion's create-only item, which has no base revision", async () => {
    const created = proposal({ origin_kind: "memory_promotion", base_revision: null });
    const inbox = await setup(reply({ items: [skill(), created] })).api.decisions();
    expect(inbox.items).toHaveLength(2);
  });

  it("refuses a null base revision on any other origin", async () => {
    for (const origin_kind of ["note_edit", "other"])
      await invalid(
        setup(reply({ items: [proposal({ origin_kind, base_revision: null })] })).api.decisions(),
      );
  });

  it("reads a create-only proposal's detail and refuses a null base on a note edit", async () => {
    const detail = (over: Record<string, unknown>) => ({
      ...proposal(over),
      current_revision: null,
      proposed_text: "x",
      diff: "",
      diff_reason: "no_source",
    });
    const created = detail({ origin_kind: "memory_promotion", base_revision: null });
    expect(await setup(reply({ proposal: created })).api.proposalDetail("p1")).toEqual(created);
    await invalid(
      setup(reply({ proposal: detail({ base_revision: null }) })).api.proposalDetail("p1"),
    );
  });

  it("refuses a 200 that is not JSON", async () => {
    await invalid(setup(new Response("nope", { status: 200 })).api.decisions());
  });
});
