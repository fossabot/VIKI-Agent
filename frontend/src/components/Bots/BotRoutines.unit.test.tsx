import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import type {
  RoutineDetailData,
  RoutineRun,
  RoutinesApi,
} from "../../clients/automation/routinesApi";
import type { BotRow } from "../../clients/messaging/botsApi";
import type { RoutinesSnapshot } from "../../state/routines";
import { BotRoutines, type BotRoutinesProps } from "./BotRoutines";

afterEach(cleanup);
const bot: BotRow = {
  bot_id: "bot1",
  profile_id: "p1",
  workspace_id: "w1",
  chat_session_id: "s-bot",
  state_revision: 1,
  created_at: "2026-10-14T00:00:00.000Z",
  updated_at: "2026-10-14T00:00:00.000Z",
  profile: {
    revision: 1,
    archived: false,
    identity: {
      name: "Planner",
      purpose: "Plan tasks",
      avatar_asset_id: null,
      accent: "blue",
      shape: "circle",
    },
  },
  workspace: { name: "Main vault", policy: "ordinary", active: true },
  chat: { profile_revision: null, started: false, archived: false },
  routine_ids: ["r1"],
  delivery: { enabled: false, available: false, reason: "not_connected" },
  latest_delivery: null,
};
function detail(over: Partial<RoutineDetailData> = {}): RoutineDetailData {
  return {
    routine_id: "r1",
    name: "Morning",
    state: "draft",
    current_revision: 1,
    state_revision: 1,
    schedule: { kind: "daily", timezone: "UTC" },
    next_run: null,
    last_run: null,
    last_attempt: null,
    active_run: null,
    document: {
      name: "Morning",
      purpose: "",
      prompt: "Review tasks",
      profile_id: "p1",
      workspace_id: "w1",
      deadline_minutes: 30,
      schedule: {
        kind: "daily",
        times: ["09:00"],
        days: [],
        timezone: "UTC",
        start_date: null,
        end_date: null,
        catch_up: "skip",
      },
    },
    next_five: [],
    readiness: { blockers: [], run_blockers: [], warnings: [] },
    resolved: {
      profile: {
        profile_id: "p1",
        name: "Planner",
        revision: 1,
        provider: "claude",
        model: null,
        permission_mode: "default",
        mode_source: "profile",
        connection_pinned: false,
      },
      workspace: { workspace_id: "w1", name: "Main vault", policy: "ordinary", active: true },
      first_turn: "Review tasks",
      deadline_minutes: 30,
    },
    tested_revision: null,
    activates_at: "2026-10-14T00:00:00.000Z",
    ...over,
  };
}
function snapshot(
  items = [detail(), detail({ routine_id: "r-other", name: "Other Bot routine" })],
): RoutinesSnapshot {
  return {
    status: "ready",
    items,
    scheduler: { state: "running", attended: true, paused: false },
    error: null,
  };
}
function setup(props: Partial<BotRoutinesProps> = {}) {
  const api = {
    get: vi.fn().mockResolvedValue(detail()),
    runs: vi.fn().mockResolvedValue([]),
    preview: vi.fn().mockResolvedValue(detail()),
    create: vi
      .fn()
      .mockResolvedValue({ routine_id: "r-new", revision: 1, state: "draft", state_revision: 1 }),
    revise: vi
      .fn()
      .mockResolvedValue({ routine_id: "r1", revision: 2, state: "draft", state_revision: 2 }),
    run: vi.fn().mockResolvedValue({ occurrence_id: "o1", attempt_id: "a1", session_id: "s1" }),
    setState: vi
      .fn()
      .mockResolvedValue({ routine_id: "r1", revision: 1, state: "enabled", state_revision: 2 }),
    list: vi.fn(),
  } satisfies RoutinesApi;
  const all: BotRoutinesProps = {
    bot,
    api,
    snapshot: snapshot(),
    sessions: {},
    nowMs: Date.parse("2026-10-14T00:00:00.000Z"),
    workspaces: [
      { workspace_id: "w1", name: "Main vault", kind: "vault", persistent: true },
    ] as WorkspaceJson[],
    controls: {
      paused: false,
      notificationsEnabled: false,
      quietStart: null,
      quietEnd: null,
      permission: "granted",
    },
    onSettings: vi.fn().mockResolvedValue(undefined),
    onRequestPermission: vi.fn().mockResolvedValue(undefined),
    onRefresh: vi.fn(),
    onOpenSession: vi.fn(),
    onStop: vi.fn(),
    confirm: vi.fn().mockReturnValue(true),
    timers: { set: vi.fn().mockReturnValue(1), clear: vi.fn() },
    ...props,
  };
  const view = render(<BotRoutines {...all} />);
  return { api, all, view };
}
async function click(name: string) {
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name }));
  });
}

describe("BotRoutines", () => {
  it("keeps a blocked draft editable and refuses direct Save or routine mutations", async () => {
    const { api, all, view } = setup();
    await click("New routine");
    const name = screen.getByLabelText("Name");
    fireEvent.change(name, { target: { value: "My draft" } });
    fireEvent.change(screen.getByLabelText("Prompt"), { target: { value: "Keep this draft" } });
    view.rerender(
      <BotRoutines
        {...all}
        blocked="This conversation is no longer canonical. Your draft is still open."
      />,
    );
    expect(name).toHaveProperty("value", "My draft");
    expect(name.matches(":disabled")).toBe(false);
    expect(screen.getByRole("button", { name: "Save" })).toHaveProperty("disabled", true);
    await act(async () => fireEvent.submit(name.closest("form") as HTMLFormElement));
    await click("Pause all");
    expect(api.create).not.toHaveBeenCalled();
    expect(all.onSettings).not.toHaveBeenCalled();
  });

  it("refuses a direct Test run after the current canonical context is blocked", async () => {
    const { api, all, view } = setup();
    await click("Morning");
    view.rerender(<BotRoutines {...all} blocked="This conversation is no longer canonical." />);
    expect(screen.getByRole("button", { name: "Test run" }).matches(":disabled")).toBe(true);
    await click("Test run");
    expect(api.run).not.toHaveBeenCalled();
  });
  it("shows only exact owned IDs and explains attended scheduling", () => {
    setup();
    expect(screen.getByRole("button", { name: "Morning" })).toBeTruthy();
    expect(screen.queryByText("Other Bot routine")).toBeNull();
    expect(
      screen.getByText("Scheduled routines need an awake host and an open VIKI Agent window."),
    ).toBeTruthy();
  });

  it("uses existing Test-before-Enable, Enable/Pause and Run now controls", async () => {
    const { api, all, view } = setup();
    await click("Morning");
    expect(screen.queryByRole("button", { name: "Enable" })).toBeNull();
    await click("Test run");
    expect(api.run).toHaveBeenCalledWith("r1", expect.any(String), 1);
    expect(all.onRefresh).toHaveBeenCalled();
    api.get.mockResolvedValue(detail({ state: "tested", tested_revision: 1, state_revision: 2 }));
    view.rerender(
      <BotRoutines
        {...all}
        snapshot={snapshot([detail({ state: "tested", state_revision: 2 })])}
      />,
    );
    await act(async () => {});
    await click("Enable");
    expect(api.setState).toHaveBeenCalledWith("r1", "enable", expect.any(String), 2);
    api.get.mockResolvedValue(detail({ state: "enabled", tested_revision: 1, state_revision: 3 }));
    view.rerender(
      <BotRoutines
        {...all}
        snapshot={snapshot([detail({ state: "enabled", state_revision: 3 })])}
      />,
    );
    await act(async () => {});
    await click("Run now");
    await click("Pause");
    expect(api.setState).toHaveBeenCalledWith("r1", "pause", expect.any(String), 3);
  });

  it("opens isolated current/history run chats and stops a live run", async () => {
    const current = detail({
      state: "enabled",
      active_run: { attempt_id: "a1", session_id: "s-live", state: "running", stop_by: null },
    });
    const { api, all } = setup({ snapshot: snapshot([current]) });
    api.get.mockResolvedValue(current);
    api.runs.mockResolvedValue([
      {
        occurrence_id: "o1",
        kind: "scheduled",
        scheduled_for: null,
        trigger_id: null,
        revision: 1,
        state: "finished",
        reason: null,
        recorded_at: "2026-10-14T00:00:00.000Z",
        attempt: {
          attempt_id: "a-old",
          session_id: "s-old",
          state: "finished",
          outcome: "succeeded",
          reason: null,
          profile_id: "p1",
          profile_revision: 1,
          native_version: null,
          created_at: "2026-10-14T00:00:00.000Z",
          dispatched_at: null,
          finished_at: "2026-10-14T00:00:00.000Z",
        },
      } satisfies RoutineRun,
    ]);
    await click("Morning");
    const live = screen.getByRole("region", { name: "Running" });
    fireEvent.click(within(live).getByRole("button", { name: "Open session" }));
    fireEvent.click(within(live).getByRole("button", { name: "Stop" }));
    expect(all.onOpenSession).toHaveBeenCalledWith("s-live");
    expect(all.onStop).toHaveBeenCalledWith("s-live");
    fireEvent.click(
      screen.getAllByRole("button", { name: "Open session" })[1] as HTMLButtonElement,
    );
    expect(all.onOpenSession).toHaveBeenCalledWith("s-old");
  });

  it("creates with locked Bot ownership and refreshes both ownership and routine state", async () => {
    const { api, all } = setup();
    await click("New routine");
    expect(screen.getByText("Bot owner: Planner")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Weekly review" } });
    fireEvent.change(screen.getByLabelText("Prompt"), { target: { value: "Review tasks" } });
    await click("Save");
    expect(api.create).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({ profile_id: "p1", workspace_id: "w1" }),
    );
    expect(all.onRefresh).toHaveBeenCalled();
  });

  it("uses shared global routine settings and labels their scope", async () => {
    const { all } = setup();
    expect(
      screen.getByText("These scheduling and notification settings apply to all routines."),
    ).toBeTruthy();
    await click("Pause all");
    expect(all.onSettings).toHaveBeenCalledWith({ "routines.paused": true });
  });

  it("removes moved ownership and reloads a revised selected routine", async () => {
    const { api, all, view } = setup();
    await click("Morning");
    api.get.mockResolvedValue(
      detail({
        current_revision: 2,
        name: "Revised",
        document: { ...detail().document, name: "Revised" },
      }),
    );
    view.rerender(
      <BotRoutines
        {...all}
        snapshot={snapshot([detail({ current_revision: 2, name: "Revised" })])}
      />,
    );
    await act(async () => {});
    expect(screen.getByRole("heading", { name: "Revised" })).toBeTruthy();
    view.rerender(<BotRoutines {...all} bot={{ ...bot, routine_ids: [] }} />);
    expect(screen.queryByRole("button", { name: "Morning" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Test run" })).toBeNull();
  });

  it("refuses a detail whose current document moved to a different owner", async () => {
    const { api } = setup();
    api.get.mockResolvedValue(
      detail({ document: { ...detail().document, profile_id: "p-other" } }),
    );
    await click("Morning");
    expect(screen.queryByRole("button", { name: "Test run" })).toBeNull();
    expect(screen.getByRole("alert")).toBeTruthy();
  });

  it("drops old Bot create/edit state and ignores late detail responses", async () => {
    let resolve!: (value: RoutineDetailData) => void;
    const { api, all, view } = setup();
    api.get.mockImplementationOnce(
      () =>
        new Promise<RoutineDetailData>((ready) => {
          resolve = ready;
        }),
    );
    await click("Morning");
    const signal = api.get.mock.calls[0]?.[1] as AbortSignal;
    view.rerender(
      <BotRoutines {...all} bot={{ ...bot, bot_id: "bot2", profile_id: "p2", routine_ids: [] }} />,
    );
    expect(signal.aborted).toBe(true);
    await act(async () => resolve(detail()));
    expect(screen.queryByRole("button", { name: "Test run" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Morning" })).toBeNull();
  });

  it("protects a dirty editor when selecting another routine", async () => {
    const confirm = vi.fn().mockReturnValue(false);
    const { api } = setup({ confirm });
    await click("New routine");
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Draft" } });
    await click("Morning");
    expect(confirm).toHaveBeenCalled();
    expect(screen.getByLabelText("Name")).toHaveProperty("value", "Draft");
    expect(api.get).not.toHaveBeenCalled();
  });

  it("preserves an edited draft after ownership moves and blocks saving it", async () => {
    const confirm = vi.fn().mockReturnValue(false);
    const { api, all, view } = setup({ confirm });
    await click("Morning");
    await click("Edit");
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "My draft" } });
    view.rerender(<BotRoutines {...all} bot={{ ...bot, routine_ids: [] }} />);
    const name = screen.getByLabelText("Name");
    expect(name).toHaveProperty("value", "My draft");
    expect(
      screen.getByText("This routine is no longer owned by this Bot. Your draft is still open."),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "Save" })).toHaveProperty("disabled", true);
    await act(async () => {
      fireEvent.submit(name.closest("form") as HTMLFormElement);
    });
    expect(api.revise).not.toHaveBeenCalled();
    await click("Cancel");
    expect(confirm).toHaveBeenCalled();
    expect(screen.getByLabelText("Name")).toHaveProperty("value", "My draft");
  });
});
