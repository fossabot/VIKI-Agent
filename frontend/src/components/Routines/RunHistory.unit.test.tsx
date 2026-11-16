import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import type { RoutineRun, RoutinesApi } from "../../clients/automation/routinesApi";
import { t } from "../../i18n/t";
import { RunHistory } from "./RunHistory";

afterEach(cleanup);

function run(id: string, over: Partial<RoutineRun> = {}): RoutineRun {
  return {
    occurrence_id: id,
    kind: "schedule",
    scheduled_for: null,
    trigger_id: null,
    revision: 3,
    state: "claimed",
    reason: null,
    recorded_at: new Date(2026, 9, 3, 7, 5).toISOString(),
    attempt: {
      attempt_id: `a-${id}`,
      session_id: `s-${id}`,
      state: "done",
      outcome: "succeeded",
      reason: null,
      profile_id: null,
      profile_revision: null,
      native_version: null,
      created_at: "",
      dispatched_at: null,
      finished_at: null,
    },
    ...over,
  };
}

function fakeApi(runs: RoutinesApi["runs"]): RoutinesApi {
  return { runs } as unknown as RoutinesApi;
}

describe("RunHistory", () => {
  test("empty", async () => {
    const api = fakeApi(vi.fn().mockResolvedValue([]));
    render(<RunHistory api={api} routineId="r" refreshKey={0} onOpenSession={() => {}} />);
    expect(screen.getByRole("status")).toBeTruthy();
    expect(await screen.findByText(t("routines.history.empty"))).toBeTruthy();
  });

  test("rows, open session, skipped row without a button, and an error", async () => {
    const skipped = run("o2", { attempt: null, state: "skipped", reason: "missed" });
    const runs = vi.fn().mockResolvedValue([run("o1"), skipped]);
    const open = vi.fn();
    render(<RunHistory api={fakeApi(runs)} routineId="r" refreshKey={0} onOpenSession={open} />);
    expect(
      await screen.findByText(`${t("routines.outcome.skipped")} (${t("routines.reason.missed")})`),
    ).toBeTruthy();
    expect(screen.getAllByText(t("routines.kind.schedule"))).toHaveLength(2);
    expect(screen.getAllByText("Revision 3")).toHaveLength(2);
    expect(runs).toHaveBeenCalledWith("r", { limit: 20 }, expect.any(AbortSignal));
    const buttons = screen.getAllByRole("button", { name: t("routines.action.open_session") });
    expect(buttons).toHaveLength(1);
    fireEvent.click(buttons[0] as HTMLElement);
    expect(open).toHaveBeenCalledWith("s-o1");
  });

  test("a run VIKI started reads Started by VIKI", async () => {
    const runs = vi.fn().mockResolvedValue([run("d1", { kind: "decision", revision: null })]);
    render(
      <RunHistory api={fakeApi(runs)} routineId="r" refreshKey={0} onOpenSession={() => {}} />,
    );
    expect(await screen.findByText("Started by VIKI")).toBeTruthy();
  });

  test("gate results read in plain English", async () => {
    const noCard = run("o3", { attempt: null, state: "skipped", reason: "no_card" });
    const base = run("o4");
    const stopped = run("o4", {
      attempt: base.attempt && { ...base.attempt, outcome: "blocked", reason: "needs_decision" },
    });
    const runs = vi.fn().mockResolvedValue([noCard, stopped]);
    render(
      <RunHistory api={fakeApi(runs)} routineId="r" refreshKey={0} onOpenSession={() => {}} />,
    );
    expect(await screen.findByText("Skipped (No card was waiting on Kanban)")).toBeTruthy();
    expect(
      screen.getByText("Stopped for your decision (A step needed your confirmation)"),
    ).toBeTruthy();
    expect(screen.getAllByRole("button", { name: t("routines.action.open_session") })).toHaveLength(
      1,
    );
  });

  test("a failed load shows an alert", async () => {
    const api = fakeApi(vi.fn().mockRejectedValue(new Error("x")));
    render(<RunHistory api={api} routineId="r" refreshKey={0} onOpenSession={() => {}} />);
    expect(await screen.findByRole("alert")).toBeTruthy();
  });

  test("a full page offers more, which passes the last occurrence as before", async () => {
    const first = Array.from({ length: 20 }, (_, i) => run(`o${i}`));
    const runs = vi
      .fn()
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce([run("tail")]);
    render(
      <RunHistory api={fakeApi(runs)} routineId="r" refreshKey={0} onOpenSession={() => {}} />,
    );
    fireEvent.click(await screen.findByRole("button", { name: t("routines.history.more") }));
    await waitFor(() => expect(screen.getAllByText("Revision 3")).toHaveLength(21));
    expect(runs).toHaveBeenLastCalledWith(
      "r",
      { limit: 20, before: "o19" },
      expect.any(AbortSignal),
    );
    expect(screen.queryByRole("button", { name: t("routines.history.more") })).toBeNull();
  });

  test("aborts on unmount and on id change; reloads on refreshKey", async () => {
    const signals: AbortSignal[] = [];
    const runs = vi.fn((_id: string, _page: unknown, signal?: AbortSignal) => {
      if (signal) signals.push(signal);
      return new Promise<RoutineRun[]>(() => {});
    });
    const api = fakeApi(runs as unknown as RoutinesApi["runs"]);
    const view = render(
      <RunHistory api={api} routineId="a" refreshKey={0} onOpenSession={() => {}} />,
    );
    view.rerender(<RunHistory api={api} routineId="b" refreshKey={0} onOpenSession={() => {}} />);
    expect(signals[0]?.aborted).toBe(true);
    view.rerender(<RunHistory api={api} routineId="b" refreshKey={1} onOpenSession={() => {}} />);
    expect(runs).toHaveBeenCalledTimes(3);
    expect(signals[1]?.aborted).toBe(true);
    view.unmount();
    expect(signals[2]?.aborted).toBe(true);
  });
});
