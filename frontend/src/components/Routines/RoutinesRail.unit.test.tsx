import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import type { RoutineItem, RunAttempt } from "../../clients/automation/routinesApi";
import { t } from "../../i18n/t";
import type { RoutinesSnapshot } from "../../state/routines";
import { RoutinesRail, type RoutinesRailProps } from "./RoutinesRail";

afterEach(cleanup);

const NOW = new Date(2026, 9, 3, 8, 0).getTime();
const at = (h: number, m = 0): string => new Date(2026, 9, 3, h, m).toISOString();

function item(over: Partial<RoutineItem>): RoutineItem {
  return {
    routine_id: over.name ?? "r",
    name: "R",
    state: "enabled",
    current_revision: 1,
    state_revision: 1,
    schedule: { kind: "daily", timezone: null },
    next_run: null,
    last_run: null,
    last_attempt: over.last_run?.attempt ? over.last_run : null,
    active_run: null,
    ...over,
  };
}

function lastRun(over: Partial<RunAttempt>): RoutineItem["last_run"] {
  return {
    occurrence_id: "o",
    kind: "schedule",
    scheduled_for: null,
    trigger_id: null,
    revision: 1,
    state: "claimed",
    reason: null,
    recorded_at: at(7),
    attempt: {
      attempt_id: "a",
      session_id: "s1",
      state: "done",
      outcome: "succeeded",
      reason: null,
      profile_id: null,
      profile_revision: null,
      native_version: null,
      created_at: at(7),
      dispatched_at: null,
      finished_at: null,
      ...over,
    },
  };
}

function snap(items: RoutineItem[], over: Partial<RoutinesSnapshot> = {}): RoutinesSnapshot {
  return {
    status: "ready",
    items,
    scheduler: { state: "running", attended: true, paused: false },
    error: null,
    ...over,
  };
}

function show(
  snapshot: RoutinesSnapshot,
  sessions: RoutinesRailProps["sessions"] = {},
  onOpen = vi.fn(),
  onRunNow?: RoutinesRailProps["onRunNow"],
) {
  const onShowAll = vi.fn();
  render(
    <RoutinesRail
      snapshot={snapshot}
      sessions={sessions}
      nowMs={NOW}
      onOpen={onOpen}
      onShowAll={onShowAll}
      {...(onRunNow !== undefined ? { onRunNow } : {})}
    />,
  );
  return { onOpen, onShowAll };
}

const names = (): string[] =>
  screen.queryAllByRole("button").flatMap((b) => {
    const n = b.querySelector(".routines-rail-name");
    return n ? [n.textContent ?? ""] : [];
  });

describe("order", () => {
  test("attention, running, enabled by next run, then the rest by name; archived left out", () => {
    const active = { attempt_id: "a", session_id: "s1", state: "running", stop_by: null };
    show(
      snap([
        item({ name: "zdraft", state: "draft" }),
        item({ name: "late", next_run: at(18) }),
        item({ name: "adraft", state: "draft" }),
        item({ name: "gone", state: "archived" }),
        item({ name: "soon", next_run: at(9) }),
        item({ name: "running", active_run: active }),
        item({ name: "blocked", last_run: lastRun({ outcome: "blocked", reason: "x" }) }),
      ]),
    );
    expect(names()).toEqual(["blocked", "running", "soon", "late", "adraft"]);
  });

  test("a waiting card ranks above a blocked routine", () => {
    const active = { attempt_id: "a", session_id: "s1", state: "running", stop_by: null };
    show(
      snap([
        item({ name: "a-blocked", last_run: lastRun({ outcome: "blocked", reason: "x" }) }),
        item({ name: "z-waiting", active_run: active }),
      ]),
      { s1: { cards: [{}] } } as never,
    );
    expect(names()).toEqual(["a-blocked", "z-waiting"]);
  });

  test("only five rows show", () => {
    show(snap(["a", "b", "c", "d", "e", "f", "g"].map((name) => item({ name }))));
    expect(names()).toEqual(["a", "b", "c", "d", "e"]);
  });
});

describe("row", () => {
  test("shows the time when enabled with a next run, else the state", () => {
    show(snap([item({ name: "a", next_run: at(9, 30) }), item({ name: "b", state: "paused" })]));
    const [a, b] = screen.getAllByRole("button");
    expect(a?.querySelector(".h")?.textContent).toBe("09:30");
    expect(b?.querySelector(".h")?.textContent).toBe(t("routines.state.paused"));
  });

  test("uses the supplied title, defaulting to Routines", () => {
    const { rerender } = render(
      <RoutinesRail
        title="This Bot's routines"
        snapshot={snap([])}
        sessions={{}}
        nowMs={NOW}
        onOpen={vi.fn()}
        onShowAll={vi.fn()}
      />,
    );
    expect(document.querySelector(".rail-section-head .lbl")?.textContent).toBe(
      "This Bot's routines",
    );
    rerender(
      <RoutinesRail
        snapshot={snap([])}
        sessions={{}}
        nowMs={NOW}
        onOpen={vi.fn()}
        onShowAll={vi.fn()}
      />,
    );
    expect(document.querySelector(".rail-section-head .lbl")?.textContent).toBe("Routines");
  });

  test("chips: attention, running, any last-run outcome including success", () => {
    const active = { attempt_id: "a", session_id: "s1", state: "running", stop_by: null };
    show(
      snap([
        item({ name: "att", last_run: lastRun({ outcome: "blocked", reason: "x" }) }),
        item({ name: "run", active_run: active }),
        item({ name: "bad", last_run: lastRun({ outcome: "failed" }) }),
        item({ name: "ok", last_run: lastRun({ outcome: "succeeded" }) }),
      ]),
    );
    const chips = Object.fromEntries(
      screen
        .getAllByRole("button")
        .filter((b) => b.querySelector(".routines-rail-name"))
        .map((b) => [
          b.querySelector(".routines-rail-name")?.textContent,
          b.querySelector(".chip"),
        ]),
    );
    expect(chips.att?.textContent).toBe(t("routines.attention.chip"));
    expect(chips.att?.classList.contains("dim")).toBe(false);
    expect(chips.run?.textContent).toBe(t("routines.outcome.running"));
    expect(chips.run?.classList.contains("dim")).toBe(true);
    expect(chips.bad?.textContent).toBe(t("routines.outcome.failed"));
    expect(chips.bad?.classList.contains("dim")).toBe(true);
    expect(chips.ok?.textContent).toBe(t("routines.outcome.succeeded"));
    expect(chips.ok?.classList.contains("dim")).toBe(true);
  });

  test("click opens the routine; show all and the footnote", () => {
    const { onOpen, onShowAll } = show(snap([item({ name: "a", routine_id: "id-a" })]));
    fireEvent.click(screen.getByText("a"));
    expect(onOpen).toHaveBeenCalledWith("id-a");
    fireEvent.click(screen.getByText(t("routines.rail.all")));
    expect(onShowAll).toHaveBeenCalledTimes(1);
    expect(screen.getByText(t("routines.rail.app_open"))).toBeTruthy();
  });

  test("a markup-shaped name is text", () => {
    const { container } = render(
      <RoutinesRail
        snapshot={snap([item({ name: "<img src=x onerror=alert(1)>" })])}
        sessions={{}}
        nowMs={NOW}
        onOpen={vi.fn()}
        onShowAll={vi.fn()}
      />,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeTruthy();
  });
});

describe("count", () => {
  const blocked = item({ name: "b", last_run: lastRun({ outcome: "blocked", reason: "x" }) });

  test("attention count is orange", () => {
    show(snap([blocked]));
    const count = document.querySelector(".rail-section-count");
    expect(count?.textContent).toBe("1");
    expect(count?.classList.contains("rail-section-count--dim")).toBe(false);
  });

  test("paused scheduler shows Paused, dim", () => {
    show(snap([blocked], { scheduler: { state: "running", attended: true, paused: true } }));
    const count = document.querySelector(".rail-section-count");
    expect(count?.textContent).toBe(t("routines.rail.paused"));
    expect(count?.classList.contains("rail-section-count--dim")).toBe(true);
  });

  test("no count when nothing needs attention", () => {
    show(snap([item({ name: "a" })]));
    expect(document.querySelector(".rail-section-count")).toBeNull();
  });
});

describe("empty and error", () => {
  test("ready and empty", () => {
    show(snap([item({ name: "old", state: "archived" })]));
    expect(screen.getByText(t("routines.rail.empty"))).toBeTruthy();
  });

  test("error with no items", () => {
    show(snap([], { status: "error", error: "unavailable" }));
    expect(screen.getByRole("alert").textContent).toBe(t("routines.load_failed"));
  });

  test("error keeps the last items and shows no alert", () => {
    show(snap([item({ name: "a" })], { status: "error", error: "unavailable" }));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(names()).toEqual(["a"]);
  });
});

describe("run now", () => {
  const active = { attempt_id: "a", session_id: "s1", state: "running", stop_by: null };

  test("offered for enabled and paused with no active run; not for a draft or an active run", () => {
    show(
      snap([
        item({ name: "en", state: "enabled" }),
        item({ name: "pa", state: "paused" }),
        item({ name: "dr", state: "draft" }),
        item({ name: "ac", state: "enabled", active_run: active }),
      ]),
      {},
      vi.fn(),
      vi.fn(),
    );
    expect(screen.getByRole("button", { name: "Run en now" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Run pa now" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Run dr now" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Run ac now" })).toBeNull();
  });

  test("not offered with no onRunNow", () => {
    show(snap([item({ name: "no-handler", state: "enabled" })]));
    expect(screen.queryByRole("button", { name: "Run no-handler now" })).toBeNull();
  });

  test("click calls onRunNow with the item; the button disables and reads Starting… until it settles", async () => {
    let resolveRun: () => void = () => {};
    const onRunNow = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveRun = resolve;
        }),
    );
    const theItem = item({ name: "a", routine_id: "id-a", state: "enabled" });
    show(snap([theItem]), {}, vi.fn(), onRunNow);
    const button = screen.getByRole("button", { name: "Run a now" });

    fireEvent.click(button);
    expect(onRunNow).toHaveBeenCalledWith(theItem);
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(button.textContent).toBe("Starting…");

    await act(async () => {
      resolveRun();
      await Promise.resolve();
    });
    expect((button as HTMLButtonElement).disabled).toBe(false);
    expect(button.textContent).toBe(t("routines.action.run_now"));
  });

  test("a failure shows the alert until the row's next click, then clears", async () => {
    const onRunNow = vi.fn(() => Promise.reject(new Error("nope")));
    const theItem = item({ name: "a", routine_id: "id-a", state: "enabled" });
    const { onOpen } = show(snap([theItem]), {}, vi.fn(), onRunNow);
    const button = screen.getByRole("button", { name: "Run a now" });

    await act(async () => {
      fireEvent.click(button);
    });
    expect(screen.getByRole("alert").textContent).toBe(
      "Could not start this routine. Open it for details.",
    );
    expect((button as HTMLButtonElement).disabled).toBe(false);

    fireEvent.click(screen.getByText("a"));
    expect(onOpen).toHaveBeenCalledWith("id-a");
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
