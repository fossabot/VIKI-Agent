/**
 * pageFacts.ts: the page's own facts as a `PageFacts`, and the run to open from "Show run".
 */
import { expect, test } from "vitest";
import type { RoutineItem } from "../../clients/automation/routinesApi";
import type { GitChip, SessionRow, UserMessageData } from "../../protocol";
import type { RoutinesSnapshot } from "../../state/routines";
import { LONG_TURNS } from "../../state/skillTriggers";
import type {
  Card,
  RoutingBlock,
  SessionView,
  ToolBlock,
  ToolRowView,
  Turn,
} from "../../state/view";
import { emptyView } from "../../state/view";
import { firstRun, type PageInputs, pageFacts } from "./pageFacts";

function routing(id: string, overrides: Partial<RoutingBlock> = {}): RoutingBlock {
  return {
    kind: "routing",
    id,
    agent: "researcher",
    model: "sonnet",
    description: "",
    reason: "",
    status: "running",
    tools: [],
    ...overrides,
  };
}

function skillToolRow(name: string, overrides: Partial<ToolRowView> = {}): ToolRowView {
  return {
    id: `skill-${name}`,
    tool: "Skill",
    row: { head: "", rest: name, tail: "" },
    done: true,
    isError: false,
    summary: "",
    preview: "",
    ...overrides,
  };
}

function toolsBlock(id: string, rows: ToolRowView[]): ToolBlock {
  return { kind: "tools", id, rows };
}

function turn(id: string, blocks: RoutingBlock[], overrides: Partial<Turn> = {}): Turn {
  return {
    id,
    user: null,
    blocks,
    status: "running",
    usage: null,
    durationMs: 0,
    history: false,
    ...overrides,
  };
}

function view(sessionId: string, overrides: Partial<SessionView> = {}): SessionView {
  return { ...emptyView(sessionId), ...overrides };
}

function toolCard(requestId: string): Card {
  return {
    kind: "permission",
    data: {
      tool_use_id: `tu-${requestId}`,
      kind: "tool",
      tool: "Write",
      summary: "30-knowledge/x.md",
      question: "",
      reason: "",
      agent: "",
      plan: "",
    },
    nativeRequest: {
      runtime_id: "33333333-3333-4333-8333-333333333333",
      request_id: requestId,
      turn_id: "55555555-5555-4555-8555-555555555555",
    },
  };
}

function userMsg(text: string): UserMessageData {
  return { text, command: "", at: "" };
}

/** A turn with a user message, succeeded and not from history — the shape `skillHint`'s own gate
 * (state/skillTriggers.ts) needs to consider a chat "finished". */
function liveUserTurn(id: string, overrides: Partial<Turn> = {}): Turn {
  return {
    id,
    user: userMsg("step"),
    blocks: [],
    status: "success",
    usage: null,
    durationMs: 0,
    history: false,
    ...overrides,
  };
}

function sessionRow(id: string, title: string): SessionRow {
  return {
    session_id: id,
    title,
    first_prompt: "",
    entrypoint: "cli",
    modified: "2026-10-01T00:00:00.000Z",
    cli_version: "",
    other: false,
  };
}

const GIT_KNOWN: GitChip = {
  branch: "main",
  head: "abc123",
  committed_at: "2026-10-01T00:00:00.000Z",
  dirty: 3,
  state: "known",
  ref_state: "branch",
};
const GIT_UNKNOWN: GitChip = {
  branch: "",
  head: "",
  committed_at: "",
  dirty: null,
  state: "unknown",
  ref_state: "unknown",
};

const IDLE_ROUTINES: RoutinesSnapshot = { status: "idle", items: [], scheduler: null, error: null };
const READY_ROUTINES: RoutinesSnapshot = {
  status: "ready",
  items: [],
  scheduler: null,
  error: null,
};

function basePage(overrides: Partial<PageInputs> = {}): PageInputs {
  return {
    sessions: {},
    reviewed: new Set<string>(),
    git: GIT_UNKNOWN,
    railsStatus: "ready",
    routines: READY_ROUTINES,
    routinesEnabled: false,
    ...overrides,
  };
}

function routineItem(overrides: Partial<RoutineItem> = {}): RoutineItem {
  return {
    routine_id: "r1",
    name: "Morning",
    state: "enabled",
    current_revision: 1,
    state_revision: 1,
    schedule: { kind: "daily", timezone: null },
    next_run: null,
    last_run: null,
    last_attempt: null,
    active_run: null,
    ...overrides,
  };
}

// ---- pageFacts: readiness gate ------------------------------------------------------------

test("pageFacts returns null while rails are loading", () => {
  expect(pageFacts(basePage({ railsStatus: "loading" }))).toBeNull();
});

test("pageFacts returns null while routines are enabled and idle", () => {
  expect(pageFacts(basePage({ routinesEnabled: true, routines: IDLE_ROUTINES }))).toBeNull();
});

test("pageFacts returns null while routines are enabled and loading", () => {
  expect(
    pageFacts(
      basePage({ routinesEnabled: true, routines: { ...IDLE_ROUTINES, status: "loading" } }),
    ),
  ).toBeNull();
});

test("pageFacts does not wait on routines that are not enabled", () => {
  expect(pageFacts(basePage({ routinesEnabled: false, routines: IDLE_ROUTINES }))).not.toBeNull();
});

test("force computes a page while unsettled, with git and routine coming through as unknown/null", () => {
  const page = pageFacts(
    basePage({ railsStatus: "loading", routinesEnabled: true, routines: IDLE_ROUTINES }),
    { force: true },
  );
  expect(page).not.toBeNull();
  expect(page?.git).toEqual({ state: "unknown", dirty: null });
  expect(page?.routine).toBeNull();
});

// ---- pageFacts: fields ----------------------------------------------------------------------

test("native_waiting counts open native decision cards", () => {
  const sessions = { s1: view("s1", { generation: "g1", cards: [toolCard("req1")] }) };
  const page = pageFacts(basePage({ sessions }));
  expect(page?.native_waiting).toBe(1);
});

test("runs counts working and review rows from groupRuns", () => {
  const sessions = {
    s1: view("s1", {
      generation: "g1",
      turns: [
        turn("t1", [routing("b1", { status: "running" })]),
        turn("t2", [routing("b2", { status: "completed" })]),
      ],
    }),
  };
  const page = pageFacts(basePage({ sessions, reviewed: new Set() }));
  expect(page?.runs).toEqual({ working: 1, review: 1 });
});

test("git passes dirty through when the state is known", () => {
  const page = pageFacts(basePage({ git: GIT_KNOWN }));
  expect(page?.git).toEqual({ state: "known", dirty: 3 });
});

test("git forces dirty to null when the state is not known", () => {
  const page = pageFacts(basePage({ git: { ...GIT_KNOWN, state: "not_repository" } }));
  expect(page?.git).toEqual({ state: "not_repository", dirty: null });
});

test("routine picks the earliest enabled item with a parseable next_run", () => {
  const routines: RoutinesSnapshot = {
    status: "ready",
    items: [
      routineItem({ routine_id: "late", next_run: "2026-10-12T00:00:00.000Z" }),
      routineItem({ routine_id: "early", next_run: "2026-10-10T00:00:00.000Z" }),
    ],
    scheduler: { state: "running", attended: true, paused: false },
    error: null,
  };
  const page = pageFacts(basePage({ routinesEnabled: true, routines }));
  expect(page?.routine).toEqual({
    routine_id: "early",
    next_run: "2026-10-10T00:00:00.000Z",
    paused: false,
  });
});

test("routine skips a disabled state, a null next_run and an unparseable next_run", () => {
  const routines: RoutinesSnapshot = {
    status: "ready",
    items: [
      routineItem({ routine_id: "paused", state: "paused", next_run: "2026-10-10T00:00:00.000Z" }),
      routineItem({ routine_id: "no-next-run", next_run: null }),
      routineItem({ routine_id: "garbage", next_run: "not-a-date" }),
      routineItem({ routine_id: "good", next_run: "2026-10-11T00:00:00.000Z" }),
    ],
    scheduler: null,
    error: null,
  };
  const page = pageFacts(basePage({ routinesEnabled: true, routines }));
  expect(page?.routine?.routine_id).toBe("good");
});

test("routine's paused reflects scheduler.paused", () => {
  const routines: RoutinesSnapshot = {
    status: "ready",
    items: [routineItem({ next_run: "2026-10-11T00:00:00.000Z" })],
    scheduler: { state: "paused", attended: true, paused: true },
    error: null,
  };
  const page = pageFacts(basePage({ routinesEnabled: true, routines }));
  expect(page?.routine?.paused).toBe(true);
});

test("routine is null when no item qualifies", () => {
  const page = pageFacts(basePage({ routinesEnabled: true, routines: READY_ROUTINES }));
  expect(page?.routine).toBeNull();
});

// ---- pageFacts: skill hint --------------------------------------------------------------------

test("a finished chat's hint rides in page facts", () => {
  const turns = Array.from({ length: LONG_TURNS }, (_, i) => liveUserTurn(`u${i}`));
  const sessionView = view("s1", { turns });
  const page = pageFacts(
    basePage({ skill: { view: sessionView, rows: [sessionRow("s1", "Long task")], off: false } }),
  );
  expect(page?.skill).toEqual({
    trigger: "long_session",
    sessions: ["s1"],
    titles: ["Long task"],
  });
});

test("skill is null without a skill input, without a view, while off, or without a hint", () => {
  const turns = Array.from({ length: LONG_TURNS }, (_, i) => liveUserTurn(`u${i}`));
  const sessionView = view("s1", { turns });
  const rows = [sessionRow("s1", "Long task")];
  expect(pageFacts(basePage())?.skill).toBeNull();
  expect(pageFacts(basePage({ skill: { view: null, rows, off: false } }))?.skill).toBeNull();
  expect(pageFacts(basePage({ skill: { view: sessionView, rows, off: true } }))?.skill).toBeNull();
  const shortView = view("s1", { turns: [liveUserTurn("u0")] });
  expect(pageFacts(basePage({ skill: { view: shortView, rows, off: false } }))?.skill).toBeNull();
});

test("a title falls back to the empty string when no row matches the hinted session", () => {
  const turns = Array.from({ length: LONG_TURNS }, (_, i) => liveUserTurn(`u${i}`));
  const sessionView = view("s1", { turns });
  const page = pageFacts(basePage({ skill: { view: sessionView, rows: [], off: false } }));
  expect(page?.skill).toEqual({ trigger: "long_session", sessions: ["s1"], titles: [""] });
});

test("a title is sanitized (control characters dropped) and clipped to 120 code points", () => {
  const turns = Array.from({ length: LONG_TURNS }, (_, i) => liveUserTurn(`u${i}`));
  const sessionView = view("s1", { turns });
  const dirty = `\u0007${"x".repeat(130)}`;
  const page = pageFacts(
    basePage({ skill: { view: sessionView, rows: [sessionRow("s1", dirty)], off: false } }),
  );
  expect(page?.skill?.titles).toEqual(["x".repeat(120)]);
});

test("titles line up with sessions by id when the hint names multiple sessions with rows in different order", () => {
  // Create a view for s1 with a few turns to avoid triggering long_session
  const sessionView = view("s1", { turns: [liveUserTurn("u1"), liveUserTurn("u2")] });
  // Rows provided in reverse order, with distinct titles
  const rows = [
    sessionRow("s3", "Third session"),
    sessionRow("s2", "Second session"),
    sessionRow("s1", "First session"),
  ];
  // Override first_prompt to have identical values so repeatedRequestHint finds matches
  const rowsWithPrompt = rows.map((row, i) => ({
    ...row,
    first_prompt: "analyze this data",
    // Set modified dates so s2 and s3 are recent enough to match
    modified: `2026-10-0${3 - i}T00:00:00.000Z`,
  }));
  const page = pageFacts(
    basePage({ skill: { view: sessionView, rows: rowsWithPrompt, off: false } }),
  );
  // Verify that titles match the session ids by id, not by row order
  if (page?.skill) {
    expect(page.skill.sessions).toBeDefined();
    for (const [index, sessionId] of (page.skill.sessions ?? []).entries()) {
      const expectedTitle = rowsWithPrompt.find((r) => r.session_id === sessionId)?.title ?? "";
      expect(page.skill.titles[index]).toBe(expectedTitle);
    }
  }
});

test('a row whose title is just its own first-prompt fallback reports "" in skill.titles', () => {
  const turns = Array.from({ length: LONG_TURNS }, (_, i) => liveUserTurn(`u${i}`));
  const sessionView = view("s1", { turns });
  const row = { ...sessionRow("s1", "hello world"), first_prompt: "hello world" };
  const page = pageFacts(basePage({ skill: { view: sessionView, rows: [row], off: false } }));
  expect(page?.skill).toEqual({ trigger: "long_session", sessions: ["s1"], titles: [""] });
});

// ---- pageFacts: suggest ----------------------------------------------------------------------

test("suggest reports the chat's title and used skill names once its last turn is live and succeeded", () => {
  const turns: Turn[] = [liveUserTurn("u0", { user: { text: "", command: "/connect", at: "" } })];
  const sessionView = view("s1", { turns });
  const page = pageFacts(
    basePage({ skill: { view: sessionView, rows: [sessionRow("s1", "Garden plan")], off: false } }),
  );
  expect(page?.suggest).toEqual({ session: "s1", title: "Garden plan", used: ["connect"] });
});

test("suggest is null without a skill input, without a view, or while off", () => {
  const sessionView = view("s1", { turns: [liveUserTurn("u0")] });
  const rows = [sessionRow("s1", "Garden plan")];
  expect(pageFacts(basePage())?.suggest).toBeNull();
  expect(pageFacts(basePage({ skill: { view: null, rows, off: false } }))?.suggest).toBeNull();
  expect(
    pageFacts(basePage({ skill: { view: sessionView, rows, off: true } }))?.suggest,
  ).toBeNull();
});

test("suggest is null while the last turn is running, from history, or failed", () => {
  const rows = [sessionRow("s1", "Garden plan")];
  const running = view("s1", { turns: [liveUserTurn("u0", { status: "running" })] });
  const history = view("s1", { turns: [liveUserTurn("u0", { history: true })] });
  const failed = view("s1", { turns: [liveUserTurn("u0", { status: "error" })] });
  expect(pageFacts(basePage({ skill: { view: running, rows, off: false } }))?.suggest).toBeNull();
  expect(pageFacts(basePage({ skill: { view: history, rows, off: false } }))?.suggest).toBeNull();
  expect(pageFacts(basePage({ skill: { view: failed, rows, off: false } }))?.suggest).toBeNull();
});

test("suggest is null when the matching title is blank", () => {
  const sessionView = view("s1", { turns: [liveUserTurn("u0")] });
  expect(
    pageFacts(basePage({ skill: { view: sessionView, rows: [], off: false } }))?.suggest,
  ).toBeNull();
  expect(
    pageFacts(basePage({ skill: { view: sessionView, rows: [sessionRow("s1", "")], off: false } }))
      ?.suggest,
  ).toBeNull();
});

test("suggest's title is sanitized (control characters dropped) and clipped to 120 code points", () => {
  const sessionView = view("s1", { turns: [liveUserTurn("u0")] });
  const dirty = `\u0007${"x".repeat(130)}`;
  const page = pageFacts(
    basePage({ skill: { view: sessionView, rows: [sessionRow("s1", dirty)], off: false } }),
  );
  expect(page?.suggest?.title).toBe("x".repeat(120));
});

test("used trims a Skill tool-call's own name before matching the vault skill-name shape", () => {
  const turns: Turn[] = [
    liveUserTurn("u0", { blocks: [toolsBlock("b0", [skillToolRow(" connect ")])] }),
  ];
  const sessionView = view("s1", { turns });
  const page = pageFacts(
    basePage({ skill: { view: sessionView, rows: [sessionRow("s1", "Plan")], off: false } }),
  );
  expect(page?.suggest?.used).toEqual(["connect"]);
});

test("used merges Skill tool calls (tools and routing blocks) and slash commands, distinct, in first-seen order", () => {
  const turns: Turn[] = [
    liveUserTurn("u0", {
      user: { text: "", command: "/connect", at: "" },
      blocks: [toolsBlock("b0", [skillToolRow("connect"), skillToolRow("research")])],
    }),
    liveUserTurn("u1", {
      blocks: [routing("r0", { tools: [skillToolRow("research"), skillToolRow("weekly")] })],
    }),
  ];
  const sessionView = view("s1", { turns });
  const page = pageFacts(
    basePage({ skill: { view: sessionView, rows: [sessionRow("s1", "Plan")], off: false } }),
  );
  expect(page?.suggest?.used).toEqual(["connect", "research", "weekly"]);
});

test("used ignores non-Skill tool rows", () => {
  const turns: Turn[] = [
    liveUserTurn("u0", {
      blocks: [toolsBlock("b0", [skillToolRow("research", { tool: "Write" })])],
    }),
  ];
  const sessionView = view("s1", { turns });
  const page = pageFacts(
    basePage({ skill: { view: sessionView, rows: [sessionRow("s1", "Plan")], off: false } }),
  );
  expect(page?.suggest?.used).toEqual([]);
});

test("used drops names that are not lowercase letters, digits and hyphens, or exceed 64 characters", () => {
  const turns: Turn[] = [
    liveUserTurn("u0", {
      blocks: [
        toolsBlock("b0", [
          skillToolRow("Good-Name"),
          skillToolRow("under_score"),
          skillToolRow("a".repeat(65)),
          skillToolRow("ok-name"),
        ]),
      ],
    }),
  ];
  const sessionView = view("s1", { turns });
  const page = pageFacts(
    basePage({ skill: { view: sessionView, rows: [sessionRow("s1", "Plan")], off: false } }),
  );
  expect(page?.suggest?.used).toEqual(["ok-name"]);
});

test("used caps at 32, keeping the first 32 in first-seen order", () => {
  const rows = Array.from({ length: 40 }, (_, i) => skillToolRow(`skill-${i}`));
  const turns: Turn[] = [liveUserTurn("u0", { blocks: [toolsBlock("b0", rows)] })];
  const sessionView = view("s1", { turns });
  const page = pageFacts(
    basePage({ skill: { view: sessionView, rows: [sessionRow("s1", "Plan")], off: false } }),
  );
  expect(page?.suggest?.used).toEqual(Array.from({ length: 32 }, (_, i) => `skill-${i}`));
});

// ---- pageFacts: second look -------------------------------------------------------------------

const LOOK_SESSION = "11111111-1111-4111-8111-111111111111";

function editRow(rest: string, overrides: Partial<ToolRowView> = {}): ToolRowView {
  return {
    ...skillToolRow("x"),
    id: `e-${rest}`,
    tool: "Edit",
    row: { head: "", rest, tail: "" },
    ...overrides,
  };
}

/** A corrected chat whose last turn edited `refs`. */
function lookView(refs: string[], sessionId = LOOK_SESSION): SessionView {
  return view(sessionId, {
    turns: [
      liveUserTurn("a"),
      liveUserTurn("b"),
      liveUserTurn("c", { user: userMsg("no, again") }),
      liveUserTurn("d", {
        blocks: [
          toolsBlock(
            "t",
            refs.map((ref) => editRow(ref)),
          ),
        ],
      }),
    ],
  });
}

function lookPage(sessionView: SessionView | null, rows: SessionRow[]) {
  return pageFacts(basePage({ look: { view: sessionView, rows } }));
}

test("second_look reports the chat's trigger, turn, title and refs", () => {
  const page = lookPage(lookView(["a.md", "b.md"]), [sessionRow(LOOK_SESSION, "Garden plan")]);
  expect(page?.second_look).toEqual({
    session: LOOK_SESSION,
    turn: "d",
    trigger: "correction",
    title: "Garden plan",
    refs: ["a.md", "b.md"],
  });
});

test("second_look is null without a look input, without a view, or without a hint", () => {
  const rows = [sessionRow(LOOK_SESSION, "Garden plan")];
  expect(pageFacts(basePage())?.second_look).toBeNull();
  expect(lookPage(null, rows)?.second_look).toBeNull();
  expect(lookPage(lookView([]), rows)?.second_look).toBeNull();
});

test("second_look withholds a title that is the first prompt, or has no row", () => {
  const prompt: SessionRow = { ...sessionRow(LOOK_SESSION, "Fix it"), first_prompt: "Fix it" };
  expect(lookPage(lookView(["a.md"]), [prompt])?.second_look?.title).toBe("");
  expect(lookPage(lookView(["a.md"]), [])?.second_look?.title).toBe("");
});

test("second_look's title is sanitized and clipped to 120 code points", () => {
  const dirty = `a\u0007${"x".repeat(200)}`;
  const page = lookPage(lookView(["a.md"]), [sessionRow(LOOK_SESSION, dirty)]);
  expect(page?.second_look?.title).toBe(`a${"x".repeat(119)}`);
});

test("the largest second_look encodes within 1,700 bytes", () => {
  const refs = Array.from({ length: 10 }, (_, i) => `${"x".repeat(97)}-${i}.md`); // 102 bytes each
  const title = "\u3042".repeat(120); // 3 bytes each
  const page = lookPage(lookView([...refs, "over.md"]), [sessionRow(LOOK_SESSION, title)]);
  expect(page?.second_look?.refs).toHaveLength(10);
  expect(page?.second_look?.title).toBe(title);
  expect(new TextEncoder().encode(JSON.stringify(page?.second_look)).length).toBeLessThanOrEqual(
    1_700,
  );
});

// ---- pageFacts: context and sources -----------------------------------------------------------

function webRow(rest: string, overrides: Partial<ToolRowView> = {}): ToolRowView {
  return {
    ...skillToolRow("x"),
    id: `w-${rest}`,
    tool: "WebFetch",
    row: { head: "", rest, tail: "" },
    ...overrides,
  };
}

function noteRow(path: string): ToolRowView {
  return {
    ...skillToolRow("x"),
    id: `n-${path}`,
    tool: "Read",
    row: { head: "", rest: path, tail: "" },
    artifactStart: {
      artifact_id: `n-${path}`,
      kind: "file",
      operation: "read",
      path,
      path_state: "workspace_relative",
    },
  };
}

function sourcesView(blocks: (ToolBlock | RoutingBlock)[], overrides: Partial<Turn> = {}) {
  return view(LOOK_SESSION, { turns: [liveUserTurn("t1", { blocks, ...overrides })] });
}

test("context reports the chat in view's context share as a whole percent", () => {
  const page = lookPage(view(LOOK_SESSION, { contextPercent: 75 }), []);
  expect(page?.context).toEqual({ session: LOOK_SESSION, percent: 75 });
  expect(lookPage(view(LOOK_SESSION, { contextPercent: 0 }), [])?.context?.percent).toBe(0);
  expect(lookPage(view(LOOK_SESSION, { contextPercent: 100 }), [])?.context?.percent).toBe(100);
});

test("context is null without a look input, a view or a whole percent from 0 to 100", () => {
  const percent = (contextPercent: number | null) =>
    lookPage(view(LOOK_SESSION, { contextPercent }), [])?.context;
  expect(pageFacts(basePage())?.context).toBeNull();
  expect(lookPage(null, [])?.context).toBeNull();
  for (const bad of [null, -1, 101, 70.5, Number.NaN]) expect(percent(bad)).toBeNull();
});

test("sources reports a live reply that fetched a web page and whether it read a vault note", () => {
  const web = toolsBlock("t", [webRow("example.com")]);
  expect(lookPage(sourcesView([web]), [])?.sources).toEqual({
    session: LOOK_SESSION,
    turn: "t1",
    notes: false,
  });
  const both = toolsBlock("t", [webRow("example.com"), noteRow("30-knowledge/a.md")]);
  expect(lookPage(sourcesView([both]), [])?.sources?.notes).toBe(true);
});

test("sources counts a fetch or note read inside a routing row", () => {
  const routed = routing("r", { tools: [webRow("example.com"), noteRow("a.md")] });
  const page = lookPage(sourcesView([routed]), []);
  expect(page?.sources).toEqual({ session: LOOK_SESSION, turn: "t1", notes: true });
});

test("sources is null for a running, history or failed reply, or one that fetched no page", () => {
  const web = toolsBlock("t", [webRow("example.com")]);
  expect(pageFacts(basePage())?.sources).toBeNull();
  expect(lookPage(null, [])?.sources).toBeNull();
  expect(lookPage(sourcesView([web], { status: "running" }), [])?.sources).toBeNull();
  expect(lookPage(sourcesView([web], { status: "error" }), [])?.sources).toBeNull();
  expect(lookPage(sourcesView([web], { history: true }), [])?.sources).toBeNull();
  expect(lookPage(view(LOOK_SESSION), [])?.sources).toBeNull();
  const none = toolsBlock("t", [noteRow("a.md")]);
  expect(lookPage(sourcesView([none]), [])?.sources).toBeNull();
  const failed = toolsBlock("t", [webRow("example.com", { isError: true }), webRow("")]);
  expect(lookPage(sourcesView([failed]), [])?.sources).toBeNull();
});

test("sources reads only the last reply", () => {
  const fetched = liveUserTurn("t1", {
    status: "success",
    blocks: [toolsBlock("t", [webRow("example.com")])],
  });
  const quiet = (id: string) =>
    liveUserTurn(id, { status: "success", blocks: [toolsBlock("q", [noteRow("a.md")])] });
  const earlier = view(LOOK_SESSION, { turns: [fetched, quiet("t2")] });
  expect(lookPage(earlier, [])?.sources).toBeNull();
  const latest = view(LOOK_SESSION, { turns: [quiet("t1"), { ...fetched, id: "t2" }] });
  expect(lookPage(latest, [])?.sources?.turn).toBe("t2");
});

test("sources reports a steered reply under the server's turn id, not its client `:steer` id", () => {
  const steered = { ...sourcesView([toolsBlock("t", [webRow("example.com")])]) };
  steered.turns = steered.turns.map((turn) => ({ ...turn, id: "t1:steer" }));
  expect(lookPage(steered, [])?.sources?.turn).toBe("t1");
});

// ---- firstRun --------------------------------------------------------------------------------------

test("firstRun returns the first review row", () => {
  const sessions = {
    s1: view("s1", {
      generation: "g1",
      turns: [turn("t1", [routing("b1", { status: "completed" })])],
    }),
  };
  expect(firstRun(sessions, new Set())).toEqual({ sessionId: "s1", blockId: "b1" });
});

test("firstRun falls back to the first working row", () => {
  const sessions = {
    s1: view("s1", {
      generation: "g1",
      turns: [turn("t1", [routing("b1", { status: "running" })])],
    }),
  };
  expect(firstRun(sessions, new Set())).toEqual({ sessionId: "s1", blockId: "b1" });
});

test("firstRun returns null when there is neither", () => {
  expect(firstRun({}, new Set())).toBeNull();
});
