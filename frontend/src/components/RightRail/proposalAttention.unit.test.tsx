import { act, renderHook } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { DecisionInbox, DecisionsApi, VaultProposalItem } from "../../state/decisionsApi";
import { useProposalAttention } from "./proposalAttention";

interface Call {
  signal: AbortSignal | undefined;
}
interface Deferred {
  resolve: (value: DecisionInbox) => void;
  reject: (error: unknown) => void;
}
interface Props {
  api: DecisionsApi | undefined;
  workspaceId: string | null;
  enabled: boolean;
  trigger: string;
}

const WS_A = "ws-a";
const WS_B = "ws-b";

function notUsed(): never {
  throw new Error("not used in this test");
}

function fakeApi(): { api: DecisionsApi; calls: Call[]; pending: Deferred[] } {
  const calls: Call[] = [];
  const pending: Deferred[] = [];
  const api: DecisionsApi = {
    decisions(signal) {
      calls.push({ signal });
      return new Promise<DecisionInbox>((resolve, reject) => pending.push({ resolve, reject }));
    },
    proposalDetail: notUsed,
    proposalAct: notUsed,
    createProposal: notUsed,
  };
  return { api, calls, pending };
}

function proposal(id: string): VaultProposalItem {
  return {
    kind: "vault_proposal",
    id,
    origin: { workspace_id: "ws-1", session_id: null },
    revision: "a".repeat(64),
    state: "open",
    attention: true,
    title: "a.md",
    ref: "a.md",
    origin_kind: "note_edit",
    base_revision: "a".repeat(64),
    text_bytes: 5,
    handed_at: null,
    created_at: "2026-10-02T03:04:05.678Z",
  };
}

function inbox(items: VaultProposalItem[]): DecisionInbox {
  return { items };
}

/** Renders the hook behind a tiny wrapper so every test shares the same typed rerender(). */
function renderAttention(initialProps: Props) {
  return renderHook((p: Props) => useProposalAttention(p), { initialProps });
}

async function settle(): Promise<void> {
  await act(async () => {});
}

test("a disabled hook does not read", async () => {
  const { api, calls } = fakeApi();
  const { result } = renderAttention({ api, workspaceId: WS_A, enabled: false, trigger: "t1" });
  await settle();

  expect(calls).toHaveLength(0);
  expect(result.current).toMatchObject({
    items: null,
    readAt: null,
    workspaceId: null,
    status: "off",
  });
});

test("the same trigger causes no read", async () => {
  const { api, calls } = fakeApi();
  const { rerender } = renderAttention({ api, workspaceId: WS_A, enabled: true, trigger: "t1" });
  await settle();
  expect(calls).toHaveLength(1);

  rerender({ api, workspaceId: WS_A, enabled: true, trigger: "t1" });
  await settle();
  expect(calls).toHaveLength(1);
});

test("refresh reads, keeping a stable identity and the held items while in flight", async () => {
  const { api, calls, pending } = fakeApi();
  const { result, rerender } = renderAttention({
    api,
    workspaceId: WS_A,
    enabled: true,
    trigger: "t1",
  });
  await settle();
  const firstRefresh = result.current.refresh;
  await act(async () => pending[0]?.resolve(inbox([proposal("1")])));
  expect(result.current.status).toBe("ready");

  act(() => result.current.refresh());
  await settle();

  expect(calls).toHaveLength(2);
  expect(result.current.status).toBe("reading");
  expect(result.current.items).toEqual([proposal("1")]);

  rerender({ api, workspaceId: WS_A, enabled: true, trigger: "t1" });
  expect(result.current.refresh).toBe(firstRefresh);
});

test("a scope change drops the items and aborts", async () => {
  const { api, calls, pending } = fakeApi();
  const { result, rerender } = renderAttention({
    api,
    workspaceId: WS_A,
    enabled: true,
    trigger: "t1",
  });
  await settle();
  await act(async () => pending[0]?.resolve(inbox([proposal("1")])));
  expect(result.current.items).toEqual([proposal("1")]);

  rerender({ api, workspaceId: WS_B, enabled: true, trigger: "t1" });
  await settle();

  expect(calls).toHaveLength(2);
  expect(calls[0]?.signal?.aborted).toBe(true);
  expect(result.current.items).toBeNull();
  expect(result.current.readAt).toBeNull();
  expect(result.current.workspaceId).toBe(WS_B);
  expect(result.current.status).toBe("reading");
});

test("a late or old response is dropped", async () => {
  const { api, calls, pending } = fakeApi();
  const { result } = renderAttention({ api, workspaceId: WS_A, enabled: true, trigger: "t1" });
  await settle();

  act(() => result.current.refresh());
  await settle();
  expect(calls).toHaveLength(2);
  expect(calls[0]?.signal?.aborted).toBe(true);

  await act(async () => pending[1]?.resolve(inbox([proposal("new")])));
  expect(result.current.items).toEqual([proposal("new")]);

  await act(async () => pending[0]?.resolve(inbox([proposal("stale")])));
  expect(result.current.items).toEqual([proposal("new")]);
});

test("a failure keeps the items", async () => {
  const { api, pending } = fakeApi();
  const { result } = renderAttention({ api, workspaceId: WS_A, enabled: true, trigger: "t1" });
  await settle();
  await act(async () => pending[0]?.resolve(inbox([proposal("1")])));
  expect(result.current.status).toBe("ready");

  act(() => result.current.refresh());
  await settle();
  await act(async () => pending[1]?.reject(new Error("fixture unavailable")));

  expect(result.current.status).toBe("failed");
  expect(result.current.items).toEqual([proposal("1")]);
});

test("unmount aborts", async () => {
  const { api, calls } = fakeApi();
  const { unmount } = renderAttention({ api, workspaceId: WS_A, enabled: true, trigger: "t1" });
  await settle();
  expect(calls[0]?.signal?.aborted).toBe(false);

  unmount();
  expect(calls[0]?.signal?.aborted).toBe(true);
});

test("StrictMode commits once", async () => {
  const { api, calls, pending } = fakeApi();
  const { result } = renderHook((p: Props) => useProposalAttention(p), {
    initialProps: { api, workspaceId: WS_A, enabled: true, trigger: "t1" },
    reactStrictMode: true,
  });
  await settle();

  expect(calls).toHaveLength(2);
  expect(calls[0]?.signal?.aborted).toBe(true);
  expect(calls[1]?.signal?.aborted).toBe(false);

  await act(async () => pending[1]?.resolve(inbox([proposal("1")])));
  expect(result.current.status).toBe("ready");
  expect(result.current.items).toEqual([proposal("1")]);

  await act(async () => pending[0]?.resolve(inbox([proposal("stale")])));
  expect(result.current.items).toEqual([proposal("1")]);
});

test("readAt comes from a mocked Date.now", async () => {
  const stamp = "2026-10-05T00:00:00.000Z";
  vi.spyOn(Date, "now").mockReturnValue(Date.parse(stamp));
  const { api, pending } = fakeApi();
  const { result } = renderAttention({ api, workspaceId: WS_A, enabled: true, trigger: "t1" });
  await settle();

  await act(async () => pending[0]?.resolve(inbox([])));
  expect(result.current.readAt).toBe(stamp);
});
