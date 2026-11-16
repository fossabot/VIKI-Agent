import { renderHook } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import type { PermissionRequestedData, QuestionAskedData } from "../protocol";
import type { NotifyPermission } from "./routineNotifications";
import type { RunGroups, RunRow } from "./runGroups";
import {
  NOTICE_TITLE,
  type RunNotificationOptions,
  type RunSignals,
  runNotices,
  runSignals,
  useCardArrivals,
  useRunNotifications,
} from "./runNotifications";
import type { Card, RoutingBlock, SessionView } from "./view";

const NONE: ReadonlySet<string> = new Set();
const ASSISTANT = "VIKI";

function permission(over: Partial<PermissionRequestedData> = {}): PermissionRequestedData {
  return {
    tool_use_id: "tu1",
    kind: "tool",
    tool: "Bash",
    summary: "",
    question: "",
    reason: "",
    agent: "",
    plan: "",
    ...over,
  };
}

function question(over: Partial<QuestionAskedData> = {}): QuestionAskedData {
  return { tool_use_id: "tu1", questions: [], ...over };
}

function permissionCard(over: Partial<PermissionRequestedData> = {}): Card {
  return { kind: "permission", data: permission(over), nativeRequest: null };
}

function questionCard(over: Partial<QuestionAskedData> = {}): Card {
  return { kind: "question", data: question(over), nativeRequest: null };
}

function block(over: Partial<RoutingBlock> = {}): RoutingBlock {
  return {
    kind: "routing",
    id: "b1",
    agent: "researcher",
    model: "",
    description: "",
    reason: "",
    status: "running",
    tools: [],
    ...over,
  };
}

function runRow(over: Partial<RunRow> = {}): RunRow {
  return {
    key: "s1:b1",
    sessionId: "s1",
    turnId: "t1",
    block: block(),
    background: false,
    live: true,
    ended: false,
    waitingSince: null,
    requests: [],
    ...over,
  };
}

function groups(over: Partial<RunGroups> = {}): RunGroups {
  return { needs: [], working: [], review: [], done: [], unattributed: [], ...over };
}

function sessionsOf(byId: Record<string, Card[]>): Readonly<Record<string, SessionView>> {
  const out: Record<string, { cards: Card[] }> = {};
  for (const [id, cards] of Object.entries(byId)) out[id] = { cards };
  return out as never;
}

describe("runSignals", () => {
  test("a permission card joined to a needs row reads as that row's agent and block id", () => {
    const needRow = runRow({
      block: block({ id: "b9", agent: "coder" }),
      requests: [permission({ tool_use_id: "tu1" })],
    });
    const next = runSignals(
      groups({ needs: [needRow] }),
      sessionsOf({ s1: [permissionCard({ tool_use_id: "tu1", agent: "coder" })] }),
      NONE,
      ASSISTANT,
    );
    expect(next.cards.get("s1:tu1")).toEqual({
      body: "coder needs your approval",
      sessionId: "s1",
      blockId: "b9",
      toolUseId: "tu1",
    });
  });

  test("the generic subagent fallback with no needs match reads as a generic agent", () => {
    const next = runSignals(
      groups(),
      sessionsOf({ s1: [permissionCard({ agent: "subagent" })] }),
      NONE,
      ASSISTANT,
    );
    expect(next.cards.get("s1:tu1")?.body).toBe("An agent needs your approval");
    expect(next.cards.get("s1:tu1")?.blockId).toBeNull();
  });

  test("a named agent with no needs match names that agent, with no block id", () => {
    const next = runSignals(
      groups(),
      sessionsOf({ s1: [permissionCard({ agent: "coder" })] }),
      NONE,
      ASSISTANT,
    );
    expect(next.cards.get("s1:tu1")).toEqual({
      body: "coder needs your approval",
      sessionId: "s1",
      blockId: null,
      toolUseId: "tu1",
    });
  });

  test("a main-thread tool card names the assistant", () => {
    const next = runSignals(
      groups(),
      sessionsOf({ s1: [permissionCard({ kind: "tool", agent: "" })] }),
      NONE,
      ASSISTANT,
    );
    expect(next.cards.get("s1:tu1")?.body).toBe("VIKI needs your approval");
  });

  test("a main-thread plan card offers the plan", () => {
    const next = runSignals(
      groups(),
      sessionsOf({ s1: [permissionCard({ kind: "plan", agent: "" })] }),
      NONE,
      ASSISTANT,
    );
    expect(next.cards.get("s1:tu1")?.body).toBe("VIKI has a plan for you to approve");
  });

  test("a question names the assistant", () => {
    const next = runSignals(groups(), sessionsOf({ s1: [questionCard()] }), NONE, ASSISTANT);
    expect(next.cards.get("s1:tu1")?.body).toBe("VIKI has a question for you");
  });

  test("a card of an excluded session is dropped", () => {
    const next = runSignals(
      groups(),
      sessionsOf({ s1: [permissionCard({ agent: "coder" })] }),
      new Set(["s1"]),
      ASSISTANT,
    );
    expect(next.cards.size).toBe(0);
    expect(next.sessions.has("s1")).toBe(false);
  });

  test("a run is tracked only while live and not ended, across every bucket", () => {
    const live = runRow({ key: "s1:live" });
    const history = runRow({ key: "s1:history", live: false });
    const endedRow = runRow({ key: "s1:ended", ended: true });
    const next = runSignals(
      groups({ needs: [live], working: [history], done: [endedRow] }),
      sessionsOf({}),
      NONE,
      ASSISTANT,
    );
    expect([...next.runs.keys()]).toEqual(["s1:live"]);
  });

  test("a run of an excluded session is dropped", () => {
    const next = runSignals(
      groups({ working: [runRow({ sessionId: "s1" })] }),
      sessionsOf({}),
      new Set(["s1"]),
      ASSISTANT,
    );
    expect(next.runs.size).toBe(0);
  });

  test("a session not yet past its openSession placeholder (generation null) is not seen", () => {
    const next = runSignals(
      groups(),
      { s1: { cards: [], generation: null } } as never,
      NONE,
      ASSISTANT,
    );
    expect(next.sessions.has("s1")).toBe(false);
  });

  test("a session whose view has synced (any generation) is seen", () => {
    const next = runSignals(
      groups(),
      { s1: { cards: [], generation: "g1" } } as never,
      NONE,
      ASSISTANT,
    );
    expect(next.sessions.has("s1")).toBe(true);
  });
});

describe("runNotices", () => {
  test("a new card in a session already known notifies", () => {
    const prev = runSignals(groups(), sessionsOf({ s1: [] }), NONE, ASSISTANT);
    const next = runSignals(
      groups(),
      sessionsOf({ s1: [permissionCard({ agent: "coder" })] }),
      NONE,
      ASSISTANT,
    );
    expect(runNotices(prev, next)).toEqual([
      {
        tag: "viki-card-s1:tu1",
        body: "coder needs your approval",
        sessionId: "s1",
        blockId: null,
        toolUseId: "tu1",
      },
    ]);
  });

  test("a card in a session seen for the first time is silent", () => {
    const prev: RunSignals = { sessions: new Set(), cards: new Map(), runs: new Map() };
    const next = runSignals(
      groups(),
      sessionsOf({ s1: [permissionCard({ agent: "coder" })] }),
      NONE,
      ASSISTANT,
    );
    expect(runNotices(prev, next)).toEqual([]);
  });

  test("a card in a session's first real snapshot is silent when it was seen only as its empty, not-yet-synced placeholder", () => {
    const prev = runSignals(
      groups(),
      { s1: { cards: [], generation: null } } as never,
      NONE,
      ASSISTANT,
    );
    const next = runSignals(
      groups(),
      { s1: { cards: [permissionCard({ agent: "coder" })], generation: "g1" } } as never,
      NONE,
      ASSISTANT,
    );
    expect(runNotices(prev, next)).toEqual([]);
  });

  test("an unchanged card does not notify again", () => {
    const cards = sessionsOf({ s1: [permissionCard({ agent: "coder" })] });
    const prev = runSignals(groups(), cards, NONE, ASSISTANT);
    const next = runSignals(groups(), cards, NONE, ASSISTANT);
    expect(runNotices(prev, next)).toEqual([]);
  });

  test("a run that was running and completed notifies as finished", () => {
    const prev = runSignals(groups({ needs: [runRow()] }), sessionsOf({}), NONE, ASSISTANT);
    const next = runSignals(
      groups({ done: [runRow({ block: block({ status: "completed" }) })] }),
      sessionsOf({}),
      NONE,
      ASSISTANT,
    );
    expect(runNotices(prev, next)).toEqual([
      {
        tag: "viki-run-s1:b1-completed",
        body: "researcher finished",
        sessionId: "s1",
        toolUseId: null,
        blockId: "b1",
      },
    ]);
  });

  test("a run that was running and failed notifies as failed", () => {
    const prev = runSignals(groups({ needs: [runRow()] }), sessionsOf({}), NONE, ASSISTANT);
    const next = runSignals(
      groups({ done: [runRow({ block: block({ status: "failed" }) })] }),
      sessionsOf({}),
      NONE,
      ASSISTANT,
    );
    expect(runNotices(prev, next)).toEqual([
      {
        tag: "viki-run-s1:b1-failed",
        body: "researcher failed",
        sessionId: "s1",
        blockId: "b1",
        toolUseId: null,
      },
    ]);
  });

  test("a run that was running and stopped does not notify", () => {
    const prev = runSignals(groups({ needs: [runRow()] }), sessionsOf({}), NONE, ASSISTANT);
    const next = runSignals(
      groups({ done: [runRow({ block: block({ status: "stopped" }), live: false })] }),
      sessionsOf({}),
      NONE,
      ASSISTANT,
    );
    expect(runNotices(prev, next)).toEqual([]);
  });

  test("a run first tracked after the baseline is silent", () => {
    const prev: RunSignals = { sessions: new Set(), cards: new Map(), runs: new Map() };
    const next = runSignals(groups({ needs: [runRow()] }), sessionsOf({}), NONE, ASSISTANT);
    expect(runNotices(prev, next)).toEqual([]);
  });
});

function fakeNotifier(permissionState: NotifyPermission = "granted") {
  const shown: { title: string; body: string; tag: string; onClick: () => void }[] = [];
  return {
    shown,
    notifier: {
      permission: () => permissionState,
      request: async () => permissionState,
      show: (title: string, body: string, tag: string, onClick: () => void) =>
        shown.push({ title, body, tag, onClick }),
    },
  };
}

function mount(over: Partial<RunNotificationOptions> = {}) {
  const fake = fakeNotifier();
  const onOpen = vi.fn();
  const base: RunNotificationOptions = {
    groups: groups(),
    sessions: sessionsOf({ s1: [] }),
    exclude: NONE,
    assistantName: ASSISTANT,
    enabled: true,
    quietStart: null,
    quietEnd: null,
    notifier: fake.notifier,
    isFocused: () => false,
    now: () => new Date(2026, 9, 3, 12, 0),
    onOpen,
    ...over,
  };
  const view = renderHook((props: RunNotificationOptions) => useRunNotifications(props), {
    initialProps: base,
  });
  let props = base;
  return {
    ...fake,
    onOpen,
    update(next: Partial<RunNotificationOptions>) {
      props = { ...props, ...next };
      view.rerender(props);
    },
  };
}

describe("useRunNotifications", () => {
  test("the first pass is silent, even with a card already open", () => {
    const { shown } = mount({ sessions: sessionsOf({ s1: [permissionCard({ agent: "coder" })] }) });
    expect(shown).toEqual([]);
  });

  test("a new card notifies once, with the fixed title", () => {
    const { shown, update } = mount();
    update({ sessions: sessionsOf({ s1: [permissionCard({ agent: "coder" })] }) });
    expect(shown).toHaveLength(1);
    expect(shown[0]).toMatchObject({
      title: NOTICE_TITLE,
      body: "coder needs your approval",
      tag: "viki-card-s1:tu1",
    });
  });

  test("a click opens the session and block the notice named", () => {
    const { shown, update, onOpen } = mount();
    const needRow = runRow({
      block: block({ id: "b9", agent: "coder" }),
      requests: [permission()],
    });
    update({
      groups: groups({ needs: [needRow] }),
      sessions: sessionsOf({ s1: [permissionCard({ agent: "coder" })] }),
    });
    expect(shown).toHaveLength(1);
    shown[0]?.onClick();
    expect(onOpen).toHaveBeenCalledExactlyOnceWith("s1", "b9");
  });

  test("a click re-resolves the block id against the latest groups when two same-type runs left it unnamed", () => {
    const { shown, update, onOpen } = mount();
    // Two running rows of the same agent type: the card cannot yet be told apart between them.
    update({
      groups: groups({
        working: [
          runRow({ key: "s1:r1", block: block({ id: "r1" }) }),
          runRow({ key: "s1:r2", block: block({ id: "r2" }) }),
        ],
      }),
      sessions: sessionsOf({ s1: [permissionCard({ agent: "researcher" })] }),
    });
    expect(shown).toHaveLength(1);
    expect(shown[0]).toMatchObject({ body: "researcher needs your approval" });

    // useRunRequests' own read lands: tu1 is r2's own request now.
    update({
      groups: groups({
        needs: [runRow({ key: "s1:r2", block: block({ id: "r2" }), requests: [permission()] })],
        working: [runRow({ key: "s1:r1", block: block({ id: "r1" }) })],
      }),
    });
    expect(shown).toHaveLength(1); // the card key is unchanged: no second notice

    shown[0]?.onClick();
    expect(onOpen).toHaveBeenCalledExactlyOnceWith("s1", "r2");
  });

  describe.each([
    ["the page is focused", { isFocused: () => true }, { isFocused: () => false }],
    [
      "in quiet hours",
      { quietStart: "11:00", quietEnd: "13:00" },
      { quietStart: null, quietEnd: null },
    ],
    ["notifications are disabled", { enabled: false }, { enabled: true }],
    ["permission is not granted", { notifier: fakeNotifier("denied").notifier }, null],
  ] as [string, Partial<RunNotificationOptions>, Partial<RunNotificationOptions> | null][])(
    "%s",
    (_label, hold, release) => {
      test("the notice is dropped and not replayed once the hold ends", () => {
        const { shown, notifier, update } = mount(hold);
        update({ sessions: sessionsOf({ s1: [permissionCard({ agent: "coder" })] }) });
        expect(shown).toEqual([]);
        // the hold ends; the same state arrives again
        update({ ...(release ?? { notifier }) });
        update({ sessions: sessionsOf({ s1: [permissionCard({ agent: "coder" })] }) });
        expect(shown).toEqual([]);
        // a genuinely new card after the hold notifies
        update({
          sessions: sessionsOf({
            s1: [
              permissionCard({ agent: "coder" }),
              permissionCard({ tool_use_id: "tu2", agent: "coder" }),
            ],
          }),
        });
        expect(shown.map((s) => s.tag)).toEqual(["viki-card-s1:tu2"]);
      });
    },
  );
});

describe("useCardArrivals", () => {
  function mountArrivals(initialCards: Card[] = []) {
    const options = {
      groups: groups(),
      sessions: sessionsOf({ s1: initialCards }),
      assistantName: ASSISTANT,
      whereOf: (sessionId: string) => sessionId,
    };
    const view = renderHook((props: typeof options) => useCardArrivals(props), {
      initialProps: options,
    });
    return {
      ...view,
      update(next: Partial<typeof options>) {
        view.rerender({ ...options, ...next });
      },
    };
  }

  test("first pass and unchanged cards stay silent", () => {
    const card = permissionCard();
    const { result, update } = mountArrivals([card]);
    expect(result.current).toEqual({ serial: 0, text: "" });
    update({ sessions: sessionsOf({ s1: [] }) });
    update({ sessions: sessionsOf({ s1: [permissionCard()] }) });
    expect(result.current.serial).toBe(1);
    update({ sessions: sessionsOf({ s1: [card] }) });
    expect(result.current.serial).toBe(1);
  });

  test("first synced sighting after reload stays silent", () => {
    const options = {
      groups: groups(),
      sessions: { s1: { cards: [], generation: null } as never },
      assistantName: ASSISTANT,
      whereOf: (sessionId: string) => sessionId,
    };
    const { result, rerender } = renderHook((props) => useCardArrivals(props), {
      initialProps: options,
    });
    rerender({
      ...options,
      sessions: { s1: { cards: [permissionCard()], generation: "g1" } as never },
    });
    expect(result.current).toEqual({ serial: 0, text: "" });
  });

  test("one arrival uses localized card body and location", () => {
    const { result, update } = mountArrivals();
    update({ sessions: sessionsOf({ s1: [] }) });
    update({ sessions: sessionsOf({ s1: [permissionCard({ agent: "coder" })] }) });
    expect(result.current).toEqual({
      serial: 1,
      text: "coder needs your approval, in s1",
    });
  });

  test("simultaneous arrivals coalesce into one count message", () => {
    const { result, update } = mountArrivals();
    update({ sessions: sessionsOf({ s1: [] }) });
    update({
      sessions: sessionsOf({
        s1: [permissionCard({ tool_use_id: "tu1" }), questionCard({ tool_use_id: "tu2" })],
      }),
    });
    expect(result.current).toEqual({ serial: 1, text: "2 new requests need you" });
  });

  test("a later arrival with same text increments serial", () => {
    const { result, update } = mountArrivals();
    update({ sessions: sessionsOf({ s1: [] }) });
    const card = permissionCard({ agent: "coder" });
    update({ sessions: sessionsOf({ s1: [card] }) });
    const first = result.current.text;
    update({ sessions: sessionsOf({ s1: [] }) });
    update({ sessions: sessionsOf({ s1: [card] }) });
    expect(result.current).toEqual({ serial: 2, text: first });
  });

  test("the assistant's own permission, plan and question arrivals name it and the session", () => {
    const { result, update } = mountArrivals();
    update({ sessions: sessionsOf({ s1: [] }) });
    update({
      sessions: sessionsOf({
        s1: [
          permissionCard({ tool_use_id: "tool", kind: "tool" }),
          permissionCard({ tool_use_id: "plan", kind: "plan" }),
          questionCard({ tool_use_id: "question" }),
        ],
      }),
    });
    expect(result.current).toEqual({ serial: 1, text: "3 new requests need you" });

    update({ sessions: sessionsOf({ s1: [] }) });
    update({
      sessions: sessionsOf({
        s1: [permissionCard({ tool_use_id: "tool", kind: "tool" })],
      }),
    });
    expect(result.current.text).toBe("VIKI needs your approval, in s1");
    update({ sessions: sessionsOf({ s1: [] }) });
    update({
      sessions: sessionsOf({
        s1: [permissionCard({ tool_use_id: "plan", kind: "plan" })],
      }),
    });
    expect(result.current.text).toBe("VIKI has a plan for you to approve, in s1");
    update({ sessions: sessionsOf({ s1: [] }) });
    update({ sessions: sessionsOf({ s1: [questionCard({ tool_use_id: "question" })] }) });
    expect(result.current.text).toBe("VIKI has a question for you, in s1");
  });
});
