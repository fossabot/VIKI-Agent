import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import type {
  RoutineDetailData,
  RoutineItem,
  RoutinesApi,
} from "../../clients/automation/routinesApi";
import type { ProfileApi } from "../../clients/profiles/profileApi";
import { t } from "../../i18n/t";
import type { RoutinesSnapshot } from "../../state/routines";
import { RoutinesView, type RoutinesViewProps } from "./RoutinesView";

afterEach(cleanup);

const NOW = new Date(2026, 9, 3, 7, 0).getTime();

function item(over: Partial<RoutineItem> = {}): RoutineItem {
  return {
    routine_id: "r1",
    name: "Morning",
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

function detailOf(from: RoutineItem): RoutineDetailData {
  return {
    ...from,
    next_five: [],
    readiness: { blockers: [], run_blockers: [], warnings: [] },
    resolved: { profile: null, workspace: null, first_turn: null, deadline_minutes: 30 },
    document: {
      name: from.name,
      purpose: "",
      prompt: "p",
      profile_id: "p1",
      workspace_id: "w1",
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
  };
}

const WORKSPACES = [
  { workspace_id: "w1", kind: "vault", persistent: true, name: "Main vault" },
] as unknown as WorkspaceJson[];

const profiles = {
  list: vi
    .fn()
    .mockResolvedValue([{ profile_id: "p1", archived: false, identity: { name: "Prof A" } }]),
} as unknown as ProfileApi;

function snap(items: RoutineItem[], over: Partial<RoutinesSnapshot> = {}): RoutinesSnapshot {
  return {
    status: "ready",
    items,
    scheduler: { state: "running", attended: true, paused: false },
    error: null,
    ...over,
  };
}

function setup(
  snapshot: RoutinesSnapshot,
  props: Partial<RoutinesViewProps> = {},
  items: RoutineItem[] = snapshot.items,
) {
  const api = {
    get: vi.fn(async (id: string) => detailOf(items.find((i) => i.routine_id === id) ?? item())),
    runs: vi.fn().mockResolvedValue([]),
    preview: vi.fn().mockResolvedValue(detailOf(item())),
    create: vi.fn().mockResolvedValue({
      routine_id: "r-new",
      revision: 1,
      state: "draft",
      state_revision: 1,
    }),
    setState: vi.fn(),
    run: vi.fn(),
  } as unknown as RoutinesApi & Record<"get" | "create", ReturnType<typeof vi.fn>>;
  const handlers = {
    onSelect: vi.fn(),
    onRefresh: vi.fn(),
    onBack: vi.fn(),
    onSettings: vi.fn().mockResolvedValue(undefined),
    onRequestPermission: vi.fn().mockResolvedValue(undefined),
    onOpenSession: vi.fn(),
    onStop: vi.fn(),
  };
  const all: RoutinesViewProps = {
    api,
    snapshot,
    sessions: {},
    nowMs: NOW,
    selectedId: null,
    profiles,
    workspaces: WORKSPACES,
    activeWorkspaceId: "w1",
    controls: {
      paused: false,
      notificationsEnabled: false,
      quietStart: null,
      quietEnd: null,
      permission: "granted",
    },
    ...handlers,
    ...props,
  };
  const view = render(<RoutinesView {...all} />);
  return { api, ...handlers, view, all };
}

const click = async (element: HTMLElement) => act(async () => void fireEvent.click(element));
const flush = async () => act(async () => {});
const names = () =>
  Array.from(document.querySelectorAll(".routines-row-name")).map((el) => el.textContent);
const WITH_CARD = { s1: { cards: [{}] } } as unknown as RoutinesViewProps["sessions"];

/** Opens the create editor and makes it dirty. */
async function dirtyEditor() {
  await click(screen.getByRole("button", { name: t("routines.new") }));
  await flush();
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Draft" } });
}

describe("RoutinesView", () => {
  test("the head is a page-head holding the focused page-title, with no intro", () => {
    setup(snap([]));
    const head = document.querySelector("header.page-head") as HTMLElement;
    const title = head.querySelector("h1.page-title") as HTMLElement;
    expect(title.textContent).toBe(t("routines.title"));
    expect(title.tabIndex).toBe(-1);
    expect(document.activeElement).toBe(title);
    expect(head.querySelector(".page-intro")).toBeNull();
    expect(screen.getByRole("button", { name: t("routines.new") }).className).toBe("btn go");
  });

  test("leading occurrence blocks distinguish local times, manual and unscheduled rows", () => {
    setup(
      snap([
        item({
          routine_id: "next",
          name: "Scheduled",
          next_run: new Date(2026, 9, 3, 9, 0).toISOString(),
          schedule: { kind: "daily", timezone: "America/New_York" },
        }),
        item({
          routine_id: "manual",
          name: "Manual routine",
          schedule: { kind: "manual", timezone: null },
        }),
        item({ routine_id: "paused", name: "Paused routine", state: "paused" }),
      ]),
    );
    const row = screen.getByRole("button", { name: "Scheduled" }).closest("li") as HTMLElement;
    expect(row.firstElementChild?.classList.contains("routines-row-next")).toBe(true);
    expect(within(row).getByText("09:00")).toBeTruthy();
    expect(within(row).getByText("Next · local time")).toBeTruthy();
    expect(within(row).queryByText("America/New_York")).toBeNull();
    const manual = screen
      .getByRole("button", { name: "Manual routine" })
      .closest("li") as HTMLElement;
    expect(
      within(manual).getByText("Manual", { selector: ".routines-row-time-label" }),
    ).toBeTruthy();
    expect(within(manual).getByText(t("routines.next_none"))).toBeTruthy();
    const paused = screen
      .getByRole("button", { name: "Paused routine" })
      .closest("li") as HTMLElement;
    expect(within(paused).getByText("No next run")).toBeTruthy();
    expect(within(paused).queryByText("Next · local time")).toBeNull();
  });

  test("missing occurrences use a decorative dash with status text and preserve tomorrow labels", () => {
    const { api } = setup(
      snap([
        item({
          routine_id: "tomorrow",
          name: "Tomorrow routine",
          next_run: new Date(2026, 9, 4, 9, 0).toISOString(),
        }),
        item({
          routine_id: "manual",
          name: "Manual routine",
          schedule: { kind: "manual", timezone: null },
        }),
        item({ routine_id: "paused", name: "Paused routine", state: "paused" }),
      ]),
    );
    const tomorrow = screen
      .getByRole("button", { name: "Tomorrow routine" })
      .closest("li") as HTMLElement;
    const clock = within(tomorrow).getByText("Tomorrow 09:00", {
      selector: ".routines-row-clock",
    });
    expect(clock.hasAttribute("aria-hidden")).toBe(false);
    expect(within(tomorrow).getByText("Next · local time")).toBeTruthy();
    for (const [name, label] of [
      ["Manual routine", "Manual"],
      ["Paused routine", "No next run"],
    ] as const) {
      const row = screen.getByRole("button", { name }).closest("li") as HTMLElement;
      const dash = within(row).getByText("—", { selector: ".routines-row-clock" });
      expect(dash.getAttribute("aria-hidden")).toBe("true");
      const status = within(row).getByText(t("routines.next_none"));
      expect(status.classList.contains("routines-row-time-label")).toBe(true);
      expect(status.closest('[aria-hidden="true"]')).toBeNull();
      expect(within(row).getByText(label)).toBeTruthy();
    }
    expect(api.get).not.toHaveBeenCalled();
  });

  test("compact scheduling retains the exact policy and keeps settings outside its disclosure", async () => {
    setup(snap([]));
    expect(screen.getByText("Scheduler running")).toBeTruthy();
    expect(
      screen.getByText("Routines run only while VIKI Agent is open.", { exact: true }),
    ).toBeTruthy();
    const policy = screen.getByText(t("routines.app_open"));
    const disclosure = policy.closest("details") as HTMLDetailsElement;
    expect(disclosure).not.toBeNull();
    expect(disclosure.open).toBe(false);
    expect(
      screen.getByRole("button", { name: t("routines.controls.pause_all") }).closest("details"),
    ).toBeNull();
    expect(screen.getByLabelText(t("routines.controls.notify")).closest("details")).toBeNull();
    await click(within(disclosure).getByText("Scheduling details", { selector: "summary" }));
    expect(disclosure.open).toBe(true);
    expect(policy.textContent).toBe(t("routines.app_open"));
  });

  test("focuses the heading and always shows the app-open notice", () => {
    setup(snap([]));
    expect(document.activeElement).toBe(screen.getByRole("heading", { level: 1 }));
    expect(screen.getByText(t("routines.app_open"))).toBeTruthy();
    expect(screen.queryByText(t("routines.scheduler_off"))).toBeNull();
    expect(screen.queryByText(t("routines.paused_notice"))).toBeNull();
  });

  test("shows the scheduler-off and paused notices from the scheduler status", () => {
    setup(snap([], { scheduler: { state: "stopped", attended: false, paused: true } }));
    expect(screen.getByText(t("routines.scheduler_off"))).toBeTruthy();
    expect(screen.getByText(t("routines.paused_notice"))).toBeTruthy();
  });

  test("shows no scheduler notice while the scheduler is unknown", () => {
    setup(snap([], { status: "loading", scheduler: null }));
    expect(screen.queryByText(t("routines.scheduler_off"))).toBeNull();
  });

  test.each(["running", "stopped", null] as const)(
    "qualifies a %s scheduler status retained after a failed refresh",
    (state) => {
      setup(
        snap([], {
          status: "error",
          scheduler: state === null ? null : { state, attended: false, paused: false },
        }),
      );
      expect(
        screen.getByText(
          state === null
            ? "Scheduler status unavailable"
            : state === "running"
              ? "Last known: scheduler running"
              : "Last known: scheduler not running",
        ),
      ).toBeTruthy();
      expect(screen.queryByText("Scheduler running", { exact: true })).toBeNull();
      expect(screen.queryByText("Scheduler not running", { exact: true })).toBeNull();
      expect(screen.getByRole("alert").textContent).toBe(t("routines.load_failed"));
      expect(screen.getByRole("button", { name: t("routines.controls.pause_all") })).toBeTruthy();
      expect(screen.getByLabelText(t("routines.controls.notify"))).toBeTruthy();
      expect(Boolean(screen.queryByText(t("routines.scheduler_off")))).toBe(state === "stopped");
    },
  );

  test("lists live routines first and archived ones dimmed last, with chips", () => {
    const items = [
      item({ routine_id: "a", name: "Old", state: "archived" }),
      item({
        routine_id: "b",
        name: "<i>Bold</i>",
        next_run: new Date(2026, 9, 3, 9, 0).toISOString(),
        last_run: {
          occurrence_id: "o",
          kind: "scheduled",
          scheduled_for: null,
          trigger_id: null,
          revision: 1,
          state: "done",
          reason: null,
          recorded_at: "",
          attempt: {
            attempt_id: "t",
            session_id: "s",
            state: "done",
            outcome: "blocked",
            reason: "billing",
            profile_id: null,
            profile_revision: null,
            native_version: null,
            created_at: "",
            dispatched_at: null,
            finished_at: null,
          },
        },
      }),
      item({ routine_id: "c", name: "Plain", state: "draft" }),
    ];
    setup(snap(items));
    expect(names()).toEqual(["<i>Bold</i>", "Plain", "Old"]);
    const rows = document.querySelectorAll(".routines-row");
    expect(rows[2]?.classList.contains("routines-row--archived")).toBe(true);
    expect(within(rows[0] as HTMLElement).getByText("09:00")).toBeTruthy();
    expect(within(rows[0] as HTMLElement).getByText(t("routines.attention.chip"))).toBeTruthy();
    expect(within(rows[0] as HTMLElement).getByText(t("routines.state.enabled"))).toBeTruthy();
    expect(within(rows[1] as HTMLElement).getByText(t("routines.next_none"))).toBeTruthy();
    expect(within(rows[1] as HTMLElement).queryByText(t("routines.attention.chip"))).toBeNull();
    expect(document.querySelector("i")).toBeNull();
  });

  test("groups attention and supplied occurrences, preserving order and keeping archived last", () => {
    const active_run = {
      attempt_id: "a1",
      session_id: "s1",
      state: "running",
      stop_by: null,
    };
    const next_run = new Date(2026, 9, 3, 9, 0).toISOString();
    const items = [
      item({ routine_id: "old", name: "Old", state: "archived" }),
      item({ routine_id: "s1", name: "First scheduled", next_run }),
      item({ routine_id: "paused", name: "Paused", state: "paused" }),
      item({ routine_id: "waiting", name: "Waiting", active_run, next_run }),
      item({
        routine_id: "manual",
        name: "Manual",
        schedule: { kind: "manual", timezone: null },
      }),
      item({ routine_id: "s2", name: "Second scheduled", next_run }),
      item({ routine_id: "draft", name: "Draft", state: "draft" }),
      item({ routine_id: "old-waiting", name: "Old waiting", state: "archived", active_run }),
    ];
    const { view, all, api } = setup(snap(items), { sessions: WITH_CARD });
    const list = document.querySelector(".routines-list ul") as HTMLElement;
    expect(
      Array.from(list.children).map(
        (el) => el.querySelector(".routines-row-name, h2")?.textContent,
      ),
    ).toEqual([
      "Needs you",
      "Waiting",
      "Scheduled",
      "First scheduled",
      "Second scheduled",
      "Not scheduled",
      "Paused",
      "Manual",
      "Draft",
      "Archived",
      "Old",
      "Old waiting",
    ]);
    const waiting = screen.getByRole("button", { name: "Waiting" }).closest("li");
    expect(waiting?.classList.contains("routines-row--attention")).toBe(true);
    expect(within(waiting as HTMLElement).getByText(t("routines.attention.chip"))).toBeTruthy();
    expect(within(waiting as HTMLElement).getByText("09:00")).toBeTruthy();
    expect(api.get).not.toHaveBeenCalled();
    view.rerender(<RoutinesView {...all} snapshot={snap([items[6] as RoutineItem])} />);
    expect(screen.getAllByRole("heading", { level: 2 }).map((el) => el.textContent)).toEqual([
      "Not scheduled",
    ]);
    view.rerender(<RoutinesView {...all} snapshot={snap([])} />);
    expect(screen.queryByRole("heading", { level: 2 })).toBeNull();
  });

  test("a live attention regroup keeps the selected row and its keyboard focus", async () => {
    const first = item({
      next_run: new Date(2026, 9, 3, 9, 0).toISOString(),
      active_run: { attempt_id: "a1", session_id: "s1", state: "running", stop_by: null },
    });
    const other = item({ routine_id: "r2", name: "Evening", next_run: first.next_run });
    const { view, all, api, onSelect } = setup(snap([other, first]), { selectedId: "r1" });
    await flush();
    const button = screen.getByRole("button", { name: "Morning" });
    const row = button.closest("li");
    button.focus();
    view.rerender(<RoutinesView {...all} sessions={WITH_CARD} />);
    expect(screen.getByRole("heading", { name: "Needs you", level: 2 })).toBeTruthy();
    expect(names()).toEqual(["Morning", "Evening"]);
    expect(screen.getByRole("button", { name: "Morning" })).toBe(button);
    expect(button.closest("li")).toBe(row);
    expect(document.activeElement).toBe(button);
    expect(button.getAttribute("aria-current")).toBe("true");
    expect(row?.classList.contains("routines-row--selected")).toBe(true);
    expect(row?.classList.contains("routines-row--attention")).toBe(true);
    expect(api.get).toHaveBeenCalledTimes(1);
    expect(onSelect).not.toHaveBeenCalled();
  });

  test("regrouping retains a focused dirty editor and its declined selection guard", async () => {
    const first = item({
      next_run: new Date(2026, 9, 3, 9, 0).toISOString(),
      active_run: { attempt_id: "a1", session_id: "s1", state: "running", stop_by: null },
    });
    const confirm = vi.fn().mockReturnValue(false);
    const { view, all, onSelect } = setup(snap([first]), { confirm });
    await dirtyEditor();
    const name = screen.getByLabelText<HTMLInputElement>("Name");
    name.focus();
    view.rerender(<RoutinesView {...all} sessions={WITH_CARD} />);
    expect(screen.getByRole("heading", { name: "Needs you", level: 2 })).toBeTruthy();
    expect(screen.getByLabelText("Name")).toBe(name);
    expect(name.value).toBe("Draft");
    expect(document.activeElement).toBe(name);
    expect(confirm).not.toHaveBeenCalled();
    await click(screen.getByRole("button", { name: "Morning" }));
    expect(confirm).toHaveBeenCalledWith(t("routines.editor.discard"));
    expect(onSelect).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Name")).toBe(name);
  });

  test("loading and empty states need no items; failures remain visible with retained items", () => {
    setup(snap([], { status: "loading" }));
    expect(screen.getByRole("status").textContent).toBe(t("routines.loading"));
    cleanup();
    setup(snap([], { status: "error", error: "unavailable" }));
    expect(screen.getByRole("alert").textContent).toBe(t("routines.load_failed"));
    cleanup();
    setup(snap([item()], { status: "error", error: "unavailable" }));
    expect(screen.getByRole("alert").textContent).toBe(t("routines.load_failed"));
    cleanup();
    setup(snap([]));
    expect(screen.getByText(t("routines.empty"))).toBeTruthy();
  });

  test("a first list failure offers read Retry without claiming an empty list", async () => {
    const { onRefresh, onSettings } = setup(snap([], { status: "error", error: "unavailable" }));
    expect(screen.queryByText(t("routines.empty"))).toBeNull();
    await click(screen.getByRole("button", { name: "Retry" }));
    expect(onRefresh).toHaveBeenCalledTimes(1);
    expect(onSettings).not.toHaveBeenCalled();
  });

  test("retained routines show failed refresh and read Retry preserves selection and order", async () => {
    const items = [item({ routine_id: "old", name: "Old", state: "archived" }), item()];
    const { view, all, api, onRefresh, onSelect } = setup(snap(items), { selectedId: "r1" });
    await flush();
    view.rerender(<RoutinesView {...all} snapshot={snap(items, { status: "error" })} />);
    expect(screen.getByRole("alert").textContent).toBe(t("routines.load_failed"));
    expect(screen.getByText("Showing the last loaded routine list.")).toBeTruthy();
    expect(names()).toEqual(["Morning", "Old"]);
    expect(screen.getByRole("button", { name: "Morning" }).getAttribute("aria-current")).toBe(
      "true",
    );
    expect(screen.queryByText(t("routines.empty"))).toBeNull();
    await click(screen.getByRole("button", { name: "Retry" }));
    expect(onRefresh).toHaveBeenCalledTimes(1);
    expect(onSelect).not.toHaveBeenCalled();
    expect(api.get).toHaveBeenCalledTimes(1);
    view.rerender(<RoutinesView {...all} snapshot={snap(items)} />);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(names()).toEqual(["Morning", "Old"]);
  });

  test("selecting a routine calls onSelect and the detail loads for the selected id", async () => {
    const items = [item({ routine_id: "r1" }), item({ routine_id: "r2", name: "Evening" })];
    const { onSelect, api } = setup(snap(items), { selectedId: "r2" });
    await flush();
    expect(api.get.mock.calls[0]?.[0]).toBe("r2");
    await click(screen.getByRole("button", { name: "Morning" }));
    expect(onSelect).toHaveBeenCalledWith("r1");
  });

  test("the detail reloads when the selected item's active run or revision changes", async () => {
    const first = item({ routine_id: "r1" });
    const { api, view, all } = setup(snap([first]), { selectedId: "r1" });
    await flush();
    expect(api.get).toHaveBeenCalledTimes(1);
    const rerender = async (next: RoutineItem) => {
      view.rerender(<RoutinesView {...all} snapshot={snap([next])} />);
      await flush();
    };
    const running = item({
      routine_id: "r1",
      active_run: { attempt_id: "a1", session_id: "s1", state: "running", stop_by: null },
    });
    await rerender(running);
    expect(api.get).toHaveBeenCalledTimes(2);
    await rerender({ ...running, state_revision: 2 });
    expect(api.get).toHaveBeenCalledTimes(3);
  });

  test("Back and the controls reach their handlers", async () => {
    const { onBack, onSettings, onRefresh } = setup(snap([]));
    await click(screen.getByRole("button", { name: t("routines.action.back") }));
    expect(onBack).toHaveBeenCalledTimes(1);
    await click(screen.getByRole("button", { name: t("routines.controls.pause_all") }));
    expect(onSettings).toHaveBeenCalledWith({ "routines.paused": true });
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  test("New opens the create editor; Cancel closes it without asking when clean", async () => {
    const confirm = vi.fn();
    setup(snap([]), { confirm });
    await click(screen.getByRole("button", { name: t("routines.new") }));
    await flush();
    expect(screen.getByRole("heading", { name: t("routines.editor.create") })).toBeTruthy();
    await click(screen.getByRole("button", { name: t("routines.editor.cancel") }));
    expect(screen.queryByRole("heading", { name: t("routines.editor.create") })).toBeNull();
    expect(confirm).not.toHaveBeenCalled();
  });

  test("saving a new routine selects it and refreshes", async () => {
    const { api, onSelect, onRefresh } = setup(snap([]));
    await click(screen.getByRole("button", { name: t("routines.new") }));
    await flush();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Brief" } });
    fireEvent.change(screen.getByLabelText("Prompt"), { target: { value: "Do it" } });
    await click(screen.getByRole("button", { name: t("routines.editor.save") }));
    await flush();
    expect(api.create).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith("r-new");
    expect(onRefresh).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("heading", { name: t("routines.editor.create") })).toBeNull();
  });

  test("Edit on the detail opens the editor for that routine", async () => {
    setup(snap([item({ routine_id: "r1", state: "draft" })]), { selectedId: "r1" });
    await flush();
    await click(await screen.findByRole("button", { name: t("routines.action.edit") }));
    await flush();
    expect(screen.getByRole("heading", { name: t("routines.editor.edit") })).toBeTruthy();
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("Morning");
  });

  test("a dirty editor asks before Cancel; declined keeps it, accepted closes it", async () => {
    const confirm = vi.fn().mockReturnValueOnce(false).mockReturnValueOnce(true);
    setup(snap([]), { confirm });
    await dirtyEditor();
    await click(screen.getByRole("button", { name: t("routines.editor.cancel") }));
    expect(confirm).toHaveBeenCalledWith(t("routines.editor.discard"));
    expect(screen.getByRole("heading", { name: t("routines.editor.create") })).toBeTruthy();
    await click(screen.getByRole("button", { name: t("routines.editor.cancel") }));
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("heading", { name: t("routines.editor.create") })).toBeNull();
  });

  test("a dirty editor asks before selecting another routine, New and Back", async () => {
    const confirm = vi.fn().mockReturnValue(false);
    const { onSelect, onBack } = setup(snap([item()]), { confirm });
    await dirtyEditor();
    await click(screen.getByRole("button", { name: "Morning" }));
    await click(screen.getByRole("button", { name: t("routines.action.back") }));
    await click(screen.getByRole("button", { name: t("routines.new") }));
    expect(confirm).toHaveBeenCalledTimes(3);
    expect(onSelect).not.toHaveBeenCalled();
    expect(onBack).not.toHaveBeenCalled();
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("Draft");
    confirm.mockReturnValue(true);
    await click(screen.getByRole("button", { name: "Morning" }));
    expect(onSelect).toHaveBeenCalledWith("r1");
    expect(screen.queryByRole("heading", { name: t("routines.editor.create") })).toBeNull();
  });
});

test("a supplied Routine header Back replaces the default and calls the clean parent once", async () => {
  const confirm = vi.fn();
  const { onBack } = setup(snap([]), {
    confirm,
    renderBack: (back) => (
      <button type="button" onClick={back}>
        Back
      </button>
    ),
  });
  expect(document.activeElement).toBe(screen.getByRole("heading", { name: t("routines.title") }));
  expect(screen.queryByRole("button", { name: t("routines.action.back") })).toBeNull();
  expect(screen.getAllByRole("button", { name: "Back" })).toHaveLength(1);
  await click(screen.getByRole("button", { name: "Back" }));
  expect(onBack).toHaveBeenCalledTimes(1);
  expect(confirm).not.toHaveBeenCalled();
});

test("the supplied Routine Back retains a declined draft and admits the next accepted click", async () => {
  const confirm = vi.fn().mockReturnValueOnce(false).mockReturnValueOnce(true);
  const { api, onBack, onSelect, onRefresh } = setup(snap([]), {
    confirm,
    renderBack: (back) => (
      <button type="button" onClick={back}>
        Back
      </button>
    ),
  });
  await dirtyEditor();
  const name = screen.getByLabelText("Name") as HTMLInputElement;
  await click(screen.getByRole("button", { name: "Back" }));
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(confirm).toHaveBeenCalledWith(t("routines.editor.discard"));
  expect(onBack).not.toHaveBeenCalled();
  expect(screen.getByLabelText("Name")).toBe(name);
  expect(name.value).toBe("Draft");
  expect(onSelect).not.toHaveBeenCalled();
  expect(onRefresh).not.toHaveBeenCalled();
  expect(api.create).not.toHaveBeenCalled();
  await click(screen.getByRole("button", { name: "Back" }));
  expect(confirm).toHaveBeenCalledTimes(2);
  expect(onBack).toHaveBeenCalledTimes(1);
});

test.each([false, true])(
  "%s custom header preserves a declined draft and its admitted callback contract",
  async (supplied) => {
    const confirm = vi.fn().mockReturnValueOnce(false).mockReturnValueOnce(true);
    const { onBack } = setup(snap([item()]), {
      selectedId: "r1",
      confirm,
      ...(supplied
        ? {
            renderBack: (back: () => void) => (
              <button type="button" onClick={back}>
                Back
              </button>
            ),
          }
        : {}),
    });
    await dirtyEditor();
    const name = screen.getByLabelText("Name") as HTMLInputElement;
    const control = screen.getByRole("button", {
      name: supplied ? "Back" : t("routines.action.back"),
    });
    await click(control);
    expect(screen.getByLabelText("Name")).toBe(name);
    expect(name.value).toBe("Draft");
    expect(onBack).not.toHaveBeenCalled();
    await click(control);
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(onBack).toHaveBeenCalledTimes(1);
    if (supplied) expect(screen.queryByLabelText("Name")).toBeNull();
    else expect(screen.getByLabelText("Name")).toBe(name);
  },
);

test.each([false, true])(
  "%s custom header resets an accepted ID while the omitted-renderer pane stays original",
  async (supplied) => {
    const items = [item(), item({ routine_id: "r2", name: "Evening" })];
    const confirm = vi.fn();
    const { view, all, onSelect, onBack } = setup(snap(items), {
      selectedId: "r1",
      confirm,
      ...(supplied
        ? {
            renderBack: (back: () => void) => (
              <button type="button" onClick={back}>
                Back
              </button>
            ),
          }
        : {}),
    });
    await dirtyEditor();
    const name = screen.getByLabelText("Name");
    view.rerender(<RoutinesView {...all} selectedId="r2" />);
    await flush();
    if (supplied) expect(screen.getByRole("heading", { name: "Evening" })).toBeTruthy();
    else expect(screen.getByLabelText("Name")).toBe(name);
    expect(confirm).not.toHaveBeenCalled();
    expect(onSelect).not.toHaveBeenCalled();
    expect(onBack).not.toHaveBeenCalled();
  },
);

test("changing only the supplied header callback identity retains the admitted Routine draft", async () => {
  const confirm = vi.fn();
  const { view, all, onSelect, onBack } = setup(snap([item()]), {
    selectedId: "r1",
    confirm,
    renderBack: (back: () => void) => (
      <button type="button" onClick={back}>
        Back
      </button>
    ),
  });
  await dirtyEditor();
  const name = screen.getByLabelText<HTMLInputElement>("Name");
  name.focus();
  view.rerender(
    <RoutinesView
      {...all}
      renderBack={(back) => (
        <button type="button" onClick={back}>
          Back
        </button>
      )}
    />,
  );
  await flush();
  expect(screen.getByLabelText("Name")).toBe(name);
  expect(name.value).toBe("Draft");
  expect(document.activeElement).toBe(name);
  expect(confirm).not.toHaveBeenCalled();
  expect(onSelect).not.toHaveBeenCalled();
  expect(onBack).not.toHaveBeenCalled();
});
