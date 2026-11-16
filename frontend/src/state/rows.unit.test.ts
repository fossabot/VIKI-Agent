/**
 * Unit tests pinning state/rows.ts behaviour that rows.accept.test.ts leaves implicit: a
 * month/year boundary in groupSessions and timeLabel, an unparseable date, a running agent
 * deduplicated across two sessions rather than just within one, and the TERMINAL_ONLY list
 * staying in step with driver.py's.
 */
// @ts-expect-error no @types/node in this browser-focused tsconfig; Node itself is present
// under Vitest, which runs tests through Node, not a bundled browser.
import { readFileSync } from "node:fs";
// @ts-expect-error see above
import { fileURLToPath } from "node:url";
import { expect, test } from "vitest";
import { agent, localIso, sessionRow } from "../accept/m3b/fakes";
import type { Command } from "../protocol";
import {
  groupSessions,
  hereNowLabel,
  menuCommands,
  rosterRunning,
  runningAgents,
  timeLabel,
  titleIsPrompt,
} from "./rows";
import type { AppSnapshot } from "./store";
import { emptyView, type RoutingBlock } from "./view";

// "Today" is the first of a new month/year: yesterday is the last day of the previous one.
const NOW = new Date(2027, 0, 1, 9, 0).getTime(); // 2027-01-01 09:00 local

test("groupSessions labels yesterday correctly across a year boundary", () => {
  const today = sessionRow({ session_id: "today", modified: localIso(2027, 1, 1, 8, 0) });
  const yesterday = sessionRow({
    session_id: "yesterday",
    modified: localIso(2026, 12, 31, 23, 0),
  });
  const earlier = sessionRow({ session_id: "earlier", modified: localIso(2026, 12, 30, 12, 0) });
  const { groups } = groupSessions([earlier, yesterday, today], NOW);
  expect(groups.map((g) => g.label)).toEqual(["Today", "Yesterday · 12-31", "Earlier"]);
  expect(groups.map((g) => g.rows.map((r) => r.session_id))).toEqual([
    ["today"],
    ["yesterday"],
    ["earlier"],
  ]);
});

test("timeLabel crosses a month boundary the same way it crosses a day", () => {
  // "Today" per NOW is 2027-01-01: a row from 2026-12-31 is not today, however close in time.
  expect(timeLabel(localIso(2026, 12, 31, 23, 59), NOW)).toBe("12-31");
  expect(timeLabel(localIso(2027, 1, 1, 0, 1), NOW)).toBe("00:01");
});

test("a row with an unparseable modified date lands in Earlier, not Today or Yesterday", () => {
  const bad = sessionRow({ session_id: "bad", modified: "not-a-date" });
  const { groups } = groupSessions([bad], NOW);
  expect(groups).toEqual([{ label: "Earlier", rows: [bad] }]);
});

function routing(id: string, name: string): RoutingBlock {
  return {
    kind: "routing",
    id,
    agent: name,
    model: "",
    description: "",
    reason: "",
    status: "running",
    tools: [],
  };
}

test("runningAgents deduplicates the same agent running across two different sessions", () => {
  const snapshot: AppSnapshot = {
    sessions: {
      s1: {
        ...emptyView("s1"),
        turns: [
          {
            id: "t0",
            user: null,
            blocks: [routing("r1", "researcher")],
            status: "running",
            usage: null,
            durationMs: 0,
            history: false,
          },
        ],
      },
      s2: {
        ...emptyView("s2"),
        turns: [
          {
            id: "t0",
            user: null,
            blocks: [routing("r2", "researcher")],
            status: "running",
            usage: null,
            durationMs: 0,
            history: false,
          },
        ],
      },
    },
    others: [],
    status: "open",
  };
  expect(runningAgents(snapshot)).toEqual(["researcher"]);
});

test("rosterRunning drops a running name rails.py never put in the roster", () => {
  const roster = [agent({ name: "connector" }), agent({ name: "researcher" })];
  // "workflow", a built-in subagent_type, and a ~/.claude user-scope agent all have no row.
  for (const phantom of ["workflow", "general-purpose", "user-scope-agent"]) {
    expect(rosterRunning(roster, [phantom])).toEqual([]);
    expect(hereNowLabel(true, rosterRunning(roster, [phantom]))).toBe("1 here now"); // VIKI only
  }
  // A roster agent that is running is kept, alongside a phantom name in the same list.
  expect(rosterRunning(roster, ["workflow", "researcher"])).toEqual(["researcher"]);
});

function command(overrides: Partial<Command> & { name: string }): Command {
  return { description: "", argument_hint: "", skill: false, ...overrides };
}

test("menuCommands drops the terminal-only commands and /clear", () => {
  const commands = [
    command({ name: "clear" }),
    command({ name: "doctor" }),
    command({ name: "color" }),
    command({ name: "review" }),
  ];
  expect(menuCommands(commands).map((c) => c.name)).toEqual(["review"]);
});

test("menuCommands puts vault skills first, each group alphabetical", () => {
  const commands = [
    command({ name: "review" }),
    command({ name: "capture", skill: true }),
    command({ name: "deep-research", skill: true }),
    command({ name: "agents" }),
  ];
  expect(menuCommands(commands).map((c) => c.name)).toEqual([
    "capture",
    "deep-research",
    "agents",
    "review",
  ]);
});

test("menuCommands ignores a leading slash on either side of the terminal-only check and the sort", () => {
  const commands = [command({ name: "/clear" }), command({ name: "/review" })];
  expect(menuCommands(commands).map((c) => c.name)).toEqual(["/review"]);
});

/** Relative to THIS file (frontend/src/state) — the two sources the check below compares. */
const DRIVER_PY = "../../../src/viki_agent/drivers/claude/driver.py";
const ROWS_TS = "./rows.ts";

/** Both lists scraped rather than imported, the way tests/test_contract.py scrapes unload.ts for
 * WRITE_TOOLS: Python and TS builds share no module, and exporting the set from rows.ts purely
 * so a test could read it would widen the module's surface for nobody else's benefit. */
function quotedNames(source: string, pattern: RegExp, where: string): Set<string> {
  const block = pattern.exec(source);
  expect(block, `TERMINAL_ONLY not found in ${where}`).toBeTruthy();
  return new Set([...(block?.[1] ?? "").matchAll(/"(\w+)"/g)].map((m) => m[1] as string));
}

// titleIsPrompt mirrors catalog.py's _row_from_state/_clip (TITLE_LEN 80, PROMPT_LEN 200)
// closely enough to tell a real custom/AI title apart from the bare first-prompt fallback using
// only the two fields a SessionRow already carries.
test("titleIsPrompt keeps a custom or AI title that is unrelated to the first prompt", () => {
  const row = sessionRow({
    session_id: "s1",
    title: "Garden plan",
    first_prompt: "what should I grow this spring in a shaded backyard bed",
  });
  expect(titleIsPrompt(row)).toBe(false);
});

test("titleIsPrompt is true for a short first prompt, uncut, where title equals it", () => {
  const row = sessionRow({
    session_id: "s2",
    title: "fix the parser bug",
    first_prompt: "fix the parser bug",
  });
  expect(titleIsPrompt(row)).toBe(true);
});

test("titleIsPrompt is true for a long first prompt, title cut to 80 chars with a trailing …", () => {
  // Mirrors catalog.py's _clip: the same raw text, collapsed, cut to TITLE_LEN (80) for `title`
  // and to the longer PROMPT_LEN (200) for `first_prompt`, so the 79-character prefix plus "…"
  // is exactly what a real untitled chat's row would carry.
  const raw =
    "please help me plan a trip to Portugal for two weeks in June with my family of " +
    "four, mostly coastal towns and old city centers, budget around three thousand dollars";
  const row = sessionRow({
    session_id: "s3",
    title: `${raw.slice(0, 79)}…`,
    first_prompt: raw.slice(0, 200),
  });
  expect(titleIsPrompt(row)).toBe(true);
});

test("titleIsPrompt holds across the whitespace collapsing catalog.py's _clip already did", () => {
  // Both fields reach the frontend already collapsed to single spaces by the same _clip call on
  // the same raw text; a title cut from a multi-word, multi-space prompt must still match.
  const raw = "remember   to\tcall  the  bookshop about the order before it sells out on Friday";
  const row = sessionRow({ session_id: "s4", title: `${raw.slice(0, 20)}…`, first_prompt: raw });
  expect(titleIsPrompt(row)).toBe(true);
});

test("titleIsPrompt keeps a title that ends with … but is not the first prompt's own prefix", () => {
  const row = sessionRow({
    session_id: "s5",
    title: "Something else entirely…",
    first_prompt: "a totally unrelated first message that does not match the title at all",
  });
  expect(titleIsPrompt(row)).toBe(false);
});

test("TERMINAL_ONLY still names exactly what driver.py's own frozenset names", () => {
  // rows.ts mirrors driver.py deliberately (see its comment), and a mirror with no check drifts
  // in the direction that costs: drop "color" from driver.py to start offering it and the server
  // dutifully sends it, this stale copy filters it back out, every Python test stays green, and
  // the command simply never appears — with the debugger pointed at a server behaving correctly.
  const here = import.meta.url;
  const python: string = readFileSync(fileURLToPath(new URL(DRIVER_PY, here)), "utf-8");
  const ts: string = readFileSync(fileURLToPath(new URL(ROWS_TS, here)), "utf-8");
  expect(quotedNames(ts, /const TERMINAL_ONLY = new Set\(\[([^\]]*)\]\)/, "rows.ts")).toEqual(
    quotedNames(python, /TERMINAL_ONLY = frozenset\(\{([^}]*)\}\)/, "driver.py"),
  );
});
