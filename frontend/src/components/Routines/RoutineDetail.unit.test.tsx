import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import {
  type RoutineDetailData,
  type RoutinesApi,
  RoutinesApiError,
} from "../../clients/automation/routinesApi";
import { t, tf } from "../../i18n/t";
import { whenLabel } from "../../state/routines";
import { RoutineDetail, type RoutineDetailProps } from "./RoutineDetail";

afterEach(cleanup);

const NOW = new Date(2026, 9, 3, 7, 0).getTime();
const STOP_BY = new Date(2026, 9, 3, 7, 30).toISOString();

function detail(over: Partial<RoutineDetailData> = {}): RoutineDetailData {
  return {
    routine_id: "r1",
    name: "<b>Morning</b>",
    state: "draft",
    current_revision: 3,
    state_revision: 5,
    schedule: { kind: "manual", timezone: null },
    next_run: null,
    last_run: null,
    last_attempt: null,
    active_run: null,
    next_five: [],
    readiness: { blockers: [], run_blockers: [], warnings: [] },
    resolved: { profile: null, workspace: null, first_turn: null, deadline_minutes: 30 },
    document: {
      name: "Morning",
      purpose: "",
      prompt: "p",
      profile_id: "p",
      workspace_id: "w",
      deadline_minutes: 30,
      schedule: {
        kind: "manual",
        times: [],
        days: [],
        timezone: null,
        start_date: null,
        end_date: null,
        catch_up: "skip",
      },
    },
    tested_revision: null,
    activates_at: "",
    ...over,
  };
}

function fakeApi(over: Partial<RoutinesApi> & { detail?: RoutineDetailData } = {}) {
  const { detail: data = detail(), ...rest } = over;
  return {
    get: vi.fn().mockResolvedValue(data),
    runs: vi.fn().mockResolvedValue([]),
    setState: vi.fn().mockResolvedValue({}),
    run: vi.fn().mockResolvedValue({}),
    ...rest,
  } as unknown as RoutinesApi & {
    get: ReturnType<typeof vi.fn>;
    setState: ReturnType<typeof vi.fn>;
    run: ReturnType<typeof vi.fn>;
  };
}

function setup(api: RoutinesApi, props: Partial<RoutineDetailProps> = {}) {
  const handlers = {
    onEdit: vi.fn(),
    onOpenSession: vi.fn(),
    onStop: vi.fn(),
    onChanged: vi.fn(),
  };
  const all: RoutineDetailProps = {
    api,
    routineId: "r1",
    nowMs: NOW,
    attention: null,
    confirm: () => true,
    ...handlers,
    ...props,
  };
  const view = render(<RoutineDetail {...all} />);
  return { ...handlers, view, all };
}

const label = (action: string) => t(`routines.action.${action}` as never);

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

const TABLE: Record<RoutineDetailData["state"], string[]> = {
  draft: ["edit", "test", "archive"],
  tested: ["edit", "enable", "test", "archive"],
  enabled: ["edit", "pause", "run_now", "archive"],
  paused: ["edit", "enable", "run_now", "archive"],
  archived: [],
};

describe("RoutineDetail", () => {
  test("saved purpose is literal and only the resolved prompt is initially disclosed", async () => {
    const value = detail();
    value.document.purpose = "Review <b>saved</b> notes.";
    value.document.schedule.timezone = "Asia/Tokyo";
    value.resolved.first_turn = "Read <i>synthetic</i> notes.";
    value.readiness.blockers = ["profile_missing"];
    const api = fakeApi({ detail: value });
    setup(api);
    const purpose = await screen.findByText(value.document.purpose);
    expect(purpose.classList.contains("routine-detail-purpose")).toBe(true);
    expect(purpose.querySelector("b")).toBeNull();
    expect(screen.getByText("Asia/Tokyo")).toBeTruthy();
    const prompt = screen.getByText(value.resolved.first_turn);
    const disclosure = prompt.closest("details") as HTMLDetailsElement;
    expect(disclosure).not.toBeNull();
    expect(disclosure.open).toBe(false);
    expect(screen.getByText(t("routines.reason.profile_missing")).closest("details")).toBeNull();
    expect(screen.getByRole("button", { name: label("test") }).closest("details")).toBeNull();
    await act(
      async () =>
        void fireEvent.click(
          within(disclosure).getByText("First message sent", { selector: "summary" }),
        ),
    );
    expect(disclosure.open).toBe(true);
    expect(prompt.querySelector("i")).toBeNull();
    expect(api.get).toHaveBeenCalledTimes(1);
    expect(api.run).not.toHaveBeenCalled();
  });

  for (const [state, actions] of Object.entries(TABLE)) {
    test(`actions for ${state}`, async () => {
      const api = fakeApi({ detail: detail({ state: state as RoutineDetailData["state"] }) });
      setup(api);
      await screen.findByText("<b>Morning</b>");
      const names = Array.from(
        document.querySelectorAll(".routine-detail-actions button"),
        (button) => button.textContent,
      );
      expect(names).toEqual(actions.map(label));
    });
  }

  test("a card and stop routine shows the card and stop lines, not the unattended line", async () => {
    const base = detail();
    const api = fakeApi({
      detail: detail({
        document: {
          ...base.document,
          run_only_if: { kind: "card", seat: "research" },
          on_confirmation: "stop",
        },
      }),
    });
    setup(api);
    await screen.findByText("<b>Morning</b>");
    expect(screen.getByText(t("routines.preview.card").replace("{seat}", "research"))).toBeTruthy();
    expect(screen.getByText(t("routines.preview.stop"))).toBeTruthy();
    expect(screen.queryByText(t("routines.preview.unattended"))).toBeNull();
  });

  test("a folder routine shows the folder line and no card line", async () => {
    const base = detail();
    const api = fakeApi({
      detail: detail({
        document: { ...base.document, run_only_if: { kind: "changed", folder: "00-inbox" } },
      }),
    });
    setup(api);
    await screen.findByText("<b>Morning</b>");
    const line = t("routines.preview.changed").replace("{folder}", "00-inbox");
    expect(screen.getByText(line)).toBeTruthy();
    expect(screen.queryByText(/owner\//)).toBeNull();
  });

  test("loading, then a load error", async () => {
    const api = fakeApi({ get: vi.fn().mockRejectedValue(new RoutinesApiError(0, "unavailable")) });
    setup(api);
    expect(screen.getByRole("status").textContent).toBe(t("routines.loading"));
    expect((await screen.findByRole("alert")).textContent).toBe(t("routines.error.generic"));
  });

  test("first detail failure offers read Retry and recovers without an action", async () => {
    const first = deferred<RoutineDetailData>();
    const api = fakeApi({
      get: vi.fn().mockReturnValueOnce(first.promise).mockResolvedValue(detail()),
    });
    setup(api);
    await act(async () => first.reject(new RoutinesApiError(503, "unavailable")));
    expect(screen.getByRole("alert").textContent).toBe(t("routines.error.generic"));
    expect(screen.queryByText(t("routines.empty"))).toBeNull();
    await act(async () => void fireEvent.click(screen.getByRole("button", { name: "Retry" })));
    expect(api.get).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("heading", { name: "<b>Morning</b>" })).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(api.run).not.toHaveBeenCalled();
    expect(api.setState).not.toHaveBeenCalled();
  });

  test("retained detail shows failed refresh and Retry does not replay the accepted action", async () => {
    const refresh = deferred<RoutineDetailData>();
    const api = fakeApi({ detail: detail({ state: "enabled" }) });
    api.get.mockReturnValueOnce(Promise.resolve(detail({ state: "enabled" })));
    api.get.mockReturnValueOnce(refresh.promise).mockResolvedValue(detail({ name: "Current" }));
    const handlers = setup(api);
    await act(async () => {});
    await act(
      async () => void fireEvent.click(screen.getByRole("button", { name: label("run_now") })),
    );
    await act(async () => refresh.reject(new RoutinesApiError(503, "unavailable")));
    expect(screen.getByRole("heading", { name: "<b>Morning</b>" })).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toBe(
      "Refresh failed. Showing the last loaded routine details.",
    );
    expect(screen.getByText(t("routines.action.accepted"))).toBeTruthy();
    await act(async () => void fireEvent.click(screen.getByRole("button", { name: "Retry" })));
    expect(api.get).toHaveBeenCalledTimes(3);
    expect(api.get.mock.calls.map((call) => call[0])).toEqual(["r1", "r1", "r1"]);
    expect(screen.getByRole("heading", { name: "Current" })).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(api.run).toHaveBeenCalledTimes(1);
    expect(api.run).toHaveBeenCalledWith("r1", expect.any(String), 3);
    expect(api.setState).not.toHaveBeenCalled();
    expect(handlers.onChanged).toHaveBeenCalledTimes(1);
  });

  for (const completion of ["resolve", "reject"] as const) {
    test(`an obsolete read Retry cannot ${completion} over the newly selected routine`, async () => {
      const first = deferred<RoutineDetailData>();
      const obsolete = deferred<RoutineDetailData>();
      const api = fakeApi({
        get: vi
          .fn()
          .mockReturnValueOnce(first.promise)
          .mockReturnValueOnce(obsolete.promise)
          .mockResolvedValue(detail({ routine_id: "r2", name: "Beta" })),
      });
      const { view, all } = setup(api);
      await act(async () => first.reject(new RoutinesApiError(503, "unavailable")));
      await act(async () => void fireEvent.click(screen.getByRole("button", { name: "Retry" })));
      const signal = api.get.mock.calls[1]?.[1] as AbortSignal;
      await act(async () => view.rerender(<RoutineDetail {...all} routineId="r2" />));
      expect(signal.aborted).toBe(true);
      await act(async () => {
        if (completion === "resolve") obsolete.resolve(detail({ name: "Obsolete" }));
        else obsolete.reject(new RoutinesApiError(503, "unavailable"));
      });
      expect(screen.getByRole("heading", { name: "Beta" })).toBeTruthy();
      expect(screen.queryByText("Obsolete")).toBeNull();
      expect(screen.queryByRole("alert")).toBeNull();
      expect(api.get.mock.calls.map((call) => call[0])).toEqual(["r1", "r1", "r2"]);
      expect(api.run).not.toHaveBeenCalled();
      expect(api.setState).not.toHaveBeenCalled();
      const unmounted = deferred<RoutineDetailData>();
      api.get.mockReturnValueOnce(unmounted.promise);
      await act(async () =>
        view.rerender(<RoutineDetail {...all} routineId="r2" refreshKey={1} />),
      );
      const unmountedSignal = api.get.mock.calls[3]?.[1] as AbortSignal;
      view.unmount();
      expect(unmountedSignal.aborted).toBe(true);
      await act(async () => {
        if (completion === "resolve") unmounted.resolve(detail({ routine_id: "r2" }));
        else unmounted.reject(new RoutinesApiError(503, "unavailable"));
      });
      expect(document.querySelector(".routine-detail")).toBeNull();
      expect(api.run).not.toHaveBeenCalled();
      expect(api.setState).not.toHaveBeenCalled();
    });
  }

  test("a changed routineId whose load fails shows the alert and cannot act on the old routine", async () => {
    let resolveR1: (data: RoutineDetailData) => void = () => {};
    let rejectR2: (error: Error) => void = () => {};
    const get = vi
      .fn()
      .mockReturnValueOnce(new Promise<RoutineDetailData>((resolve) => (resolveR1 = resolve)))
      .mockReturnValueOnce(new Promise<RoutineDetailData>((_, reject) => (rejectR2 = reject)));
    const api = fakeApi({ get });
    const props = {
      api,
      nowMs: NOW,
      attention: null,
      confirm: () => true,
      onEdit: vi.fn(),
      onOpenSession: vi.fn(),
      onStop: vi.fn(),
      onChanged: vi.fn(),
    };
    const view = render(<RoutineDetail {...props} routineId="r1" />);
    await act(async () => resolveR1(detail({ name: "Alpha", state: "paused" })));
    expect(screen.getByText("Alpha")).toBeTruthy();
    view.rerender(<RoutineDetail {...props} routineId="r2" />);
    await act(async () => rejectR2(new RoutinesApiError(503, "overloaded")));
    expect(screen.getByRole("alert").textContent).toBe(t("routines.error.generic"));
    expect(screen.queryByText("Alpha")).toBeNull();
    expect(screen.queryByText(label("archive"))).toBeNull();
    expect(api.setState).not.toHaveBeenCalled();
  });

  test("edit hands over the detail; the name is a text node", async () => {
    const api = fakeApi();
    const handlers = setup(api);
    await screen.findByText("<b>Morning</b>");
    expect(document.querySelector("b")).toBeNull();
    fireEvent.click(screen.getByText(label("edit")));
    expect(handlers.onEdit).toHaveBeenCalledWith(expect.objectContaining({ routine_id: "r1" }));
  });

  test("test and run now call run with current_revision; state actions use state_revision", async () => {
    const api = fakeApi({ detail: detail({ state: "tested" }) });
    const handlers = setup(api);
    await screen.findByText(label("enable"));
    fireEvent.click(screen.getByText(label("test")));
    await waitFor(() => expect(api.run).toHaveBeenCalledWith("r1", expect.any(String), 3));
    fireEvent.click(await screen.findByText(label("enable")));
    await waitFor(() =>
      expect(api.setState).toHaveBeenCalledWith("r1", "enable", expect.any(String), 5),
    );
    expect(handlers.onChanged).toHaveBeenCalledTimes(2);
    expect(await screen.findByText(t("routines.action.accepted"))).toBeTruthy();
  });

  test("202 reloads the detail", async () => {
    const api = fakeApi({ detail: detail({ state: "enabled" }) });
    setup(api);
    fireEvent.click(await screen.findByText(label("run_now")));
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
  });

  test("a 422 lists the blockers under not_ready", async () => {
    const api = fakeApi({
      detail: detail({ state: "tested" }),
      setState: vi
        .fn()
        .mockRejectedValue(new RoutinesApiError(422, "not_ready", ["missing_profile", "zzz"])),
    });
    const handlers = setup(api);
    fireEvent.click(await screen.findByText(label("enable")));
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(t("routines.not_ready"))).toBeTruthy();
    expect(within(alert).getAllByRole("listitem")).toHaveLength(2);
    expect(within(alert).getByText(tf("routines.reason.other", { code: "zzz" }))).toBeTruthy();
    expect(handlers.onChanged).not.toHaveBeenCalled();
  });

  test("a known code and an unknown code", async () => {
    const setState = vi
      .fn()
      .mockRejectedValueOnce(new RoutinesApiError(409, "conflict"))
      .mockRejectedValueOnce(new RoutinesApiError(500, "weird"));
    const api = fakeApi({ detail: detail({ state: "tested" }), setState });
    setup(api);
    fireEvent.click(await screen.findByText(label("enable")));
    expect((await screen.findByRole("alert")).textContent).toBe(t("routines.error.conflict"));
    fireEvent.click(screen.getByText(label("enable")));
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toBe(t("routines.error.generic")),
    );
  });

  test("a keyed id is reused after a failure and new after success", async () => {
    const setState = vi
      .fn()
      .mockRejectedValueOnce(new RoutinesApiError(0, "unavailable"))
      .mockResolvedValue({});
    const api = fakeApi({ detail: detail({ state: "tested" }), setState });
    setup(api);
    const click = async () => {
      fireEvent.click(await screen.findByText(label("enable")));
      await waitFor(() => expect(setState).toHaveBeenCalledTimes(click.calls + 1));
      click.calls += 1;
    };
    click.calls = 0;
    await click();
    await screen.findByRole("alert");
    await click();
    await screen.findByText(t("routines.action.accepted"));
    await click();
    const ids = setState.mock.calls.map((call) => call[2]);
    expect(ids[1]).toBe(ids[0]);
    expect(ids[2]).not.toBe(ids[0]);
  });

  test("archive asks first and does nothing when declined", async () => {
    const api = fakeApi({ detail: detail({ state: "paused" }) });
    const confirm = vi.fn().mockReturnValueOnce(false).mockReturnValue(true);
    setup(api, { confirm });
    fireEvent.click(await screen.findByText(label("archive")));
    expect(confirm).toHaveBeenCalledWith(t("routines.action.archive_confirm"));
    expect(api.setState).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText(label("archive")));
    await waitFor(() =>
      expect(api.setState).toHaveBeenCalledWith("r1", "archive", expect.any(String), 5),
    );
  });

  test("active run: stop_by, waiting sentence, open session and stop", async () => {
    const api = fakeApi({
      detail: detail({
        state: "enabled",
        active_run: { attempt_id: "a1", session_id: "s1", state: "running", stop_by: STOP_BY },
      }),
    });
    const handlers = setup(api, { attention: { kind: "waiting", stopBy: STOP_BY } });
    const panel = (
      await screen.findByText(tf("routines.run.stop_by", { time: whenLabel(STOP_BY, NOW) }))
    ).parentElement as HTMLElement;
    expect(within(panel).getByText(t("routines.outcome.running"))).toBeTruthy();
    expect(
      within(panel).getByText(tf("routines.attention.waiting", { time: whenLabel(STOP_BY, NOW) })),
    ).toBeTruthy();
    fireEvent.click(within(panel).getByText(label("open_session")));
    expect(handlers.onOpenSession).toHaveBeenCalledWith("s1");
    fireEvent.click(within(panel).getByText(label("stop")));
    expect(handlers.onStop).toHaveBeenCalledWith("s1");
  });

  test("an untimed waiting sentence", async () => {
    const api = fakeApi({
      detail: detail({
        state: "enabled",
        active_run: { attempt_id: "a1", session_id: "s1", state: "running", stop_by: null },
      }),
    });
    setup(api, { attention: { kind: "waiting", stopBy: null } });
    expect(await screen.findByText(t("routines.attention.waiting_untimed"))).toBeTruthy();
  });

  test("a needs_decision block has its own sentence; billing still could not start", async () => {
    const api = fakeApi({ detail: detail({ state: "enabled" }) });
    setup(api, { attention: { kind: "blocked", reason: "needs_decision" } });
    expect(await screen.findByText(t("routines.attention.needs_decision"))).toBeTruthy();
    cleanup();
    setup(api, { attention: { kind: "blocked", reason: "billing" } });
    expect(await screen.findByText(/could not start/)).toBeTruthy();
  });

  test("blocked and expired attention", async () => {
    const api = fakeApi({ detail: detail({ state: "enabled" }) });
    setup(api, { attention: { kind: "blocked", reason: "overlap" } });
    expect(
      await screen.findByText(
        tf("routines.attention.blocked", { reason: t("routines.reason.overlap") }),
      ),
    ).toBeTruthy();
    cleanup();
    setup(api, { attention: { kind: "expired" } });
    expect(await screen.findByText(t("routines.attention.expired"))).toBeTruthy();
  });
});
