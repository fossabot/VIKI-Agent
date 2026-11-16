import { expect, test } from "vitest";
import type { View } from "../commands";
import {
  commitBack,
  NAVIGATION_HISTORY_MAX,
  type NavigationContext,
  type NavigationHistory,
  peekBack,
  recordTransition,
} from "./navigation";

function context(view: View): NavigationContext {
  return {
    navigation: { view, page: "general", panel: "locations", onboardingMode: "choose" },
    routineId: null,
    note: null,
    opener: null,
    scroll: { top: 0, left: 0 },
  };
}

test("rejected transitions and repeated destinations preserve the exact stack", () => {
  const history = [context("code")];
  const previous = context("settings");
  expect(recordTransition(history, previous, context("data"), false)).toBe(history);
  const repeated = {
    ...previous,
    navigation: {
      ...previous.navigation,
      panel: "export" as const,
      onboardingMode: "open" as const,
    },
    routineId: "inactive routine",
    note: { workspace_id: "inactive workspace", ref: "notes/inactive.md" },
    opener: document.createElement("button"),
    scroll: { top: 100, left: 20 },
  };
  expect(recordTransition(history, previous, repeated, true)).toBe(history);
  expect(recordTransition(history, context("chat"), repeated, false)).toBe(history);
});

test.each(["settings", "data", "onboarding", "routines", "vault"] as const)(
  "accepted visible selection changes in %s record the prior context",
  (view) => {
    const previous = context(view);
    const next: NavigationContext = {
      ...previous,
      navigation: {
        ...previous.navigation,
        ...(view === "settings" ? { page: "privacy" as const } : {}),
        ...(view === "data" ? { panel: "export" as const } : {}),
        ...(view === "onboarding" ? { onboardingMode: "open" as const } : {}),
      },
      routineId: view === "routines" ? "routine:%2f" : null,
      note: view === "vault" ? { workspace_id: "workspace:A", ref: "notes/same-name%2F.md" } : null,
    };
    expect(recordTransition([], previous, next, true)).toEqual([previous]);
  },
);

test("qualified note identity distinguishes workspaces and an identical ref is a noop", () => {
  const previous = { ...context("vault"), note: { workspace_id: "A", ref: "notes/same-name.md" } };
  const history = [context("chat")];
  const same = { ...previous, note: { ...previous.note } };
  expect(recordTransition(history, previous, same, true)).toBe(history);
  const other = { ...same, note: { ...same.note, workspace_id: "B" } };
  expect(recordTransition(history, previous, other, true)).toEqual([...history, previous]);
});

test("Bots retain selected identity in same-view history without changing older callers", () => {
  const previous = { ...context("bots"), botId: "bot-a" };
  const next = { ...previous, botId: "bot-b" };
  const history = recordTransition([], previous, next, true);
  expect(history).toEqual([previous]);
  expect(recordTransition(history, next, { ...next }, true)).toBe(history);
  expect(recordTransition(history, next, { ...next, botId: "bot-c" }, false)).toBe(history);
  expect(recordTransition([], context("bots"), { ...context("bots"), botId: null }, true)).toEqual(
    [],
  );
  expect(peekBack(history, "bots").context?.botId).toBe("bot-a");
});

test("history retains the newest forty accepted prior contexts in order", () => {
  let history: NavigationHistory = [];
  for (let id = 0; id < 42; id++) {
    const previous = { ...context("routines"), routineId: `routine:${id}` };
    const next = { ...previous, routineId: `routine:${id + 1}` };
    history = recordTransition(history, previous, next, true);
  }
  expect(NAVIGATION_HISTORY_MAX).toBe(40);
  expect(history.map((entry) => entry.routineId)).toEqual(
    Array.from({ length: 40 }, (_, id) => `routine:${id + 2}`),
  );
});

test("accepted snapshots copy literal context values while retaining the opener reference", () => {
  const navigation = { ...context("vault").navigation };
  const note = { workspace_id: "workspace:%2F", ref: "notes/same-name%2f.md" };
  const scroll = { top: 12.5, left: 7 };
  const opener = document.createElement("button");
  const previous = { navigation, note, scroll, opener, routineId: "routine:%2f" };
  const history = recordTransition([], previous, context("settings"), true);
  navigation.page = "privacy";
  note.workspace_id = "different workspace";
  note.ref = "other.md";
  scroll.top = 100;
  scroll.left = 20;
  expect(history[0]).toEqual({
    navigation: context("vault").navigation,
    note: { workspace_id: "workspace:%2F", ref: "notes/same-name%2f.md" },
    scroll: { top: 12.5, left: 7 },
    opener,
    routineId: "routine:%2f",
  });
  expect(history[0]?.opener).toBe(opener);
});

test("Back peeks without popping and only accepted current-top admission removes an entry", () => {
  const previous = context("code");
  const current = context("settings");
  const history = recordTransition([], previous, current, true);
  const target = peekBack(history, "settings");
  expect(target.reason).toBeNull();
  expect(target.context).toEqual(previous);
  expect(history).toHaveLength(1);
  const expected = target.context as NavigationContext;
  expect(commitBack(history, "settings", expected, false)).toBe(history);
  expect(commitBack(history, "settings", { ...expected }, true)).toBe(history);
  const newer = recordTransition(history, current, context("data"), true);
  expect(commitBack(newer, "data", expected, true)).toBe(newer);
  expect(commitBack(history, "settings", expected, true)).toEqual([]);
});

test("Recovery is an explicit constrained root and accepted entering or leaving clears history", () => {
  const history = [context("code")];
  const ordinary = context("settings");
  const recovery = context("recovery");
  expect(peekBack(history, "recovery")).toEqual({ context: null, reason: "recovery_root" });
  expect(commitBack(history, "recovery", history[0] as NavigationContext, true)).toBe(history);
  expect(recordTransition(history, ordinary, recovery, false)).toBe(history);
  expect(recordTransition(history, ordinary, recovery, true)).toEqual([]);
  expect(recordTransition(history, recovery, ordinary, true)).toEqual([]);
});

test("an empty stack gives an honest unavailable reason without fabricating a return destination", () => {
  const history: NavigationHistory = [];
  expect(peekBack(history, "settings")).toEqual({ context: null, reason: "empty_history" });
  expect(peekBack(history, "recovery")).toEqual({ context: null, reason: "recovery_root" });
  expect(commitBack(history, "settings", context("chat"), true)).toBe(history);
});
