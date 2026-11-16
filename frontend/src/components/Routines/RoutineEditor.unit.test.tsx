import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import {
  type RoutineDetailData,
  type RoutineDocument,
  type RoutinePreviewData,
  type RoutinesApi,
  RoutinesApiError,
} from "../../clients/automation/routinesApi";
import type { ProfileApi } from "../../clients/profiles/profileApi";
import { t } from "../../i18n/t";
import { hasUnsavedWork } from "../../state/unsavedWork";
import { RoutineEditor, type RoutineEditorProps } from "./RoutineEditor";

afterEach(cleanup);

const ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const CHANGE = { routine_id: "r-new", revision: 1, state: "draft", state_revision: 1 } as const;

const WORKSPACES = [
  { workspace_id: "w1", kind: "vault", persistent: true, name: "Main vault" },
  { workspace_id: "w2", kind: "vault", persistent: true, name: "Second vault" },
  { workspace_id: "w3", kind: "vault", persistent: false, name: "Temporary vault" },
  { workspace_id: "w4", kind: "scratch", persistent: true, name: "Scratch" },
] as unknown as WorkspaceJson[];

function previewData(profileName = "Prof A"): RoutinePreviewData {
  return {
    next_five: [],
    readiness: { blockers: [], run_blockers: [], warnings: [] },
    resolved: {
      profile: {
        profile_id: "p1",
        name: profileName,
        revision: 1,
        provider: "claude",
        model: null,
        permission_mode: null,
        mode_source: null,
        connection_pinned: false,
      },
      workspace: { workspace_id: "w1", name: "Main vault", policy: "ordinary", active: true },
      first_turn: "hello",
      deadline_minutes: 30,
    },
  };
}

function savedDocument(schedule: Partial<RoutineDocument["schedule"]> = {}): RoutineDocument {
  return {
    name: "Morning",
    purpose: "",
    prompt: "Summarize",
    profile_id: "p1",
    workspace_id: "w1",
    deadline_minutes: 30,
    schedule: {
      kind: "daily",
      times: ["09:00"],
      days: [],
      timezone: "Asia/Tokyo",
      start_date: null,
      end_date: null,
      catch_up: "skip",
      ...schedule,
    },
  };
}

function detail(over: Partial<RoutineDetailData> = {}): RoutineDetailData {
  return {
    routine_id: "r1",
    name: "Morning",
    state: "draft",
    current_revision: 7,
    state_revision: 2,
    schedule: { kind: "daily", timezone: "Asia/Tokyo" },
    next_run: null,
    last_run: null,
    last_attempt: null,
    active_run: null,
    ...previewData("Shown"),
    document: savedDocument(),
    tested_revision: null,
    activates_at: "2026-10-03T00:00:00.000Z",
    ...over,
  };
}

function profilesApi(rows: { id: string; name: string; archived?: boolean }[]): ProfileApi {
  const list = vi.fn().mockResolvedValue(
    rows.map((row) => ({
      profile_id: row.id,
      archived: row.archived ?? false,
      identity: { name: row.name },
    })),
  );
  return { list } as unknown as ProfileApi;
}

function manualTimers() {
  let next = 1;
  const queue = new Map<number, { callback: () => void; ms: number }>();
  return {
    set(callback: () => void, ms: number): number {
      const handle = next++;
      queue.set(handle, { callback, ms });
      return handle;
    },
    clear(handle: number): void {
      queue.delete(handle);
    },
    delays: (): number[] => [...queue.values()].map((entry) => entry.ms),
    async flush(): Promise<void> {
      const due = [...queue.values()];
      queue.clear();
      await act(async () => {
        for (const entry of due) entry.callback();
      });
    },
  };
}

type Api = {
  get: ReturnType<typeof vi.fn>;
  create: ReturnType<typeof vi.fn>;
  revise: ReturnType<typeof vi.fn>;
  preview: ReturnType<typeof vi.fn>;
};

function fakeApi(over: Partial<Api> = {}): RoutinesApi & Api {
  const unused = () => {
    throw new Error("unused");
  };
  return {
    list: unused,
    get: vi.fn().mockResolvedValue(detail()),
    runs: unused,
    setState: unused,
    run: unused,
    create: vi.fn().mockResolvedValue(CHANGE),
    revise: vi.fn().mockResolvedValue({ ...CHANGE, routine_id: "r1", revision: 8 }),
    preview: vi.fn().mockResolvedValue(previewData()),
    ...over,
  } as unknown as RoutinesApi & Api;
}

async function setup(props: Partial<RoutineEditorProps> = {}) {
  const timers = manualTimers();
  const api = (props.api as ReturnType<typeof fakeApi> | undefined) ?? fakeApi();
  const onSaved = vi.fn();
  const onCancel = vi.fn();
  const view = render(
    <RoutineEditor
      api={api}
      initial={null}
      profiles={profilesApi([
        { id: "p1", name: "Prof A" },
        { id: "p2", name: "Prof B" },
      ])}
      workspaces={WORKSPACES}
      activeWorkspaceId="w2"
      timers={timers}
      onSaved={onSaved}
      onCancel={onCancel}
      {...props}
    />,
  );
  await act(async () => {});
  return { timers, api, onSaved, onCancel, view };
}

const documentAt = (mock: ReturnType<typeof vi.fn>, call: number, arg: number): RoutineDocument =>
  (mock.mock.calls[call] as unknown[])[arg] as RoutineDocument;
const type = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
const save = async () =>
  act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
  });

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

describe("RoutineEditor", () => {
  test("keeps a blocked draft editable while refusing both Save and form submission", async () => {
    const { api } = await setup({ initial: detail(), saveBlocked: "Ownership changed." });
    type("Name", "My draft");
    expect(screen.getByText("Ownership changed.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Save" })).toHaveProperty("disabled", true);
    await act(async () => {
      fireEvent.submit(screen.getByLabelText("Name").closest("form") as HTMLFormElement);
    });
    expect(api.revise).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Name")).toHaveProperty("value", "My draft");
    expect(hasUnsavedWork()).toBe(true);
  });
  test("locks a new Bot routine's visible owner and preview/save payload", async () => {
    const { api, timers } = await setup({
      profiles: profilesApi([]),
      lockedOwner: {
        botName: "Planner Bot",
        profileId: "p-bot",
        profileName: "Planner",
        workspaceId: "w1",
        workspaceName: "Main vault",
      },
    });
    expect(screen.getByText("Bot owner: Planner Bot")).toBeTruthy();
    expect((screen.getByLabelText("Profile") as HTMLSelectElement).disabled).toBe(true);
    expect((screen.getByLabelText("Workspace") as HTMLSelectElement).disabled).toBe(true);
    expect(screen.queryByRole("alert")).toBeNull();
    type("Profile", "p2");
    type("Workspace", "w2");
    type("Name", "Daily brief");
    type("Prompt", "Review tasks");
    await timers.flush();
    expect(documentAt(api.preview, 0, 0)).toMatchObject({
      profile_id: "p-bot",
      workspace_id: "w1",
    });
    await save();
    expect(documentAt(api.create, 0, 1)).toMatchObject({ profile_id: "p-bot", workspace_id: "w1" });
  });

  test("a create-only locked context leaves existing routine ownership editable", async () => {
    const { api } = await setup({
      initial: detail(),
      lockedOwner: {
        botName: "Other Bot",
        profileId: "p-other",
        profileName: "Other",
        workspaceId: "w2",
        workspaceName: "Second vault",
      },
    });
    expect(screen.queryByText("Bot owner: Other Bot")).toBeNull();
    expect((screen.getByLabelText("Profile") as HTMLSelectElement).disabled).toBe(false);
    type("Profile", "p2");
    await save();
    expect(documentAt(api.revise, 0, 3).profile_id).toBe("p2");
  });

  test("creates with the defaults, the first profile and the active workspace", async () => {
    const { api, onSaved } = await setup();
    const profiles = within(screen.getByLabelText(t("routines.editor.profile")));
    const workspaces = within(screen.getByLabelText(t("routines.editor.workspace")));
    expect(profiles.getAllByRole("option").map((option) => option.textContent)).toContain("Prof A");
    expect(workspaces.queryByRole("option", { name: "Temporary vault" })).toBeNull();
    expect(workspaces.queryByRole("option", { name: "Scratch" })).toBeNull();
    type("Name", "Daily brief");
    type("Prompt", "Do it");
    await save();
    expect(api.create).toHaveBeenCalledTimes(1);
    const [commandId, document] = api.create.mock.calls[0] as [string, RoutineDocument];
    expect(commandId).toMatch(/^[0-9a-f-]{36}$/);
    expect(document).toEqual({
      name: "Daily brief",
      purpose: "",
      prompt: "Do it",
      profile_id: "p1",
      workspace_id: "w2",
      deadline_minutes: 30,
      schedule: {
        kind: "daily",
        times: ["09:00"],
        days: [],
        timezone: ZONE,
        start_date: null,
        end_date: null,
        catch_up: "skip",
      },
    });
    expect(api.revise).not.toHaveBeenCalled();
    expect(onSaved).toHaveBeenCalledWith("r-new");
  });

  test("card + seat + stop sends both keys; the preview request carries them", async () => {
    const { api, timers } = await setup();
    type("Name", "Daily brief");
    type("Prompt", "Do it");
    fireEvent.click(screen.getByLabelText(t("routines.editor.run_if_card")));
    type("Seat", "research");
    fireEvent.click(screen.getByLabelText(t("routines.editor.on_confirmation_stop")));
    await timers.flush();
    expect(documentAt(api.preview, 0, 0)).toMatchObject({
      run_only_if: { kind: "card", seat: "research" },
      on_confirmation: "stop",
    });
    await save();
    expect(documentAt(api.create, 0, 1)).toMatchObject({
      run_only_if: { kind: "card", seat: "research" },
      on_confirmation: "stop",
    });
  });

  test("Manual drops run_only_if and keeps on_confirmation", async () => {
    const { api } = await setup();
    type("Name", "Daily brief");
    type("Prompt", "Do it");
    fireEvent.click(screen.getByLabelText(t("routines.editor.run_if_card")));
    type("Seat", "research");
    fireEvent.click(screen.getByLabelText(t("routines.editor.on_confirmation_stop")));
    fireEvent.click(screen.getByLabelText(t("routines.editor.kind_manual")));
    expect(screen.queryByLabelText("Seat")).toBeNull();
    await save();
    const document = documentAt(api.create, 0, 1);
    expect(document).not.toHaveProperty("run_only_if");
    expect(document.on_confirmation).toBe("stop");
  });

  test("a seat with a capital is refused and nothing is sent", async () => {
    const { api } = await setup();
    type("Name", "Daily brief");
    type("Prompt", "Do it");
    fireEvent.click(screen.getByLabelText(t("routines.editor.run_if_card")));
    type("Seat", "Research");
    await save();
    expect(screen.getByRole("alert").textContent).toBe("Check your input: Seat");
    expect(api.create).not.toHaveBeenCalled();
    type("Seat", "a".repeat(33));
    await save();
    expect(screen.getByRole("alert").textContent).toBe("Check your input: Seat");
    expect(api.create).not.toHaveBeenCalled();
  });

  test("changed + folder sends the folder condition without outer spaces or slashes", async () => {
    const { api, timers } = await setup();
    type("Name", "Inbox triage");
    type("Prompt", "Triage the inbox");
    fireEvent.click(screen.getByLabelText(t("routines.editor.run_if_changed")));
    expect(screen.queryByLabelText("Seat")).toBeNull();
    expect(screen.getByText(t("routines.editor.folder_note"))).toBeTruthy();
    expect(screen.queryByText(t("routines.editor.seat_note"))).toBeNull();
    type("Folder", " /00-inbox/ ");
    await timers.flush();
    expect(documentAt(api.preview, 0, 0)).toMatchObject({
      run_only_if: { kind: "changed", folder: "00-inbox" },
    });
    await save();
    expect(documentAt(api.create, 0, 1).run_only_if).toEqual({
      kind: "changed",
      folder: "00-inbox",
    });
  });

  test.each([
    "",
    ".claude",
    "notes/.hidden",
    "a//b",
    "c:notes",
    "a\\b",
    "a\u0001b",
    "a".repeat(256),
    "\u{1F600}".repeat(256),
  ])("the folder %j is refused and nothing is sent", async (folder) => {
    const { api } = await setup();
    type("Name", "Inbox triage");
    type("Prompt", "Triage the inbox");
    fireEvent.click(screen.getByLabelText(t("routines.editor.run_if_changed")));
    type("Folder", folder);
    await save();
    expect(screen.getByRole("alert").textContent).toBe("Check your input: Folder");
    expect(api.create).not.toHaveBeenCalled();
  });

  test("a folder of 255 characters outside the BMP is sent: the limit counts code points", async () => {
    const { api } = await setup();
    type("Name", "Inbox triage");
    type("Prompt", "Triage the inbox");
    fireEvent.click(screen.getByLabelText(t("routines.editor.run_if_changed")));
    type("Folder", "\u{1F600}".repeat(255));
    await save();
    expect(documentAt(api.create, 0, 1).run_only_if).toEqual({
      kind: "changed",
      folder: "\u{1F600}".repeat(255),
    });
  });

  test("a saved folder routine reopens with its folder", async () => {
    const { api } = await setup({
      initial: detail({
        document: { ...savedDocument(), run_only_if: { kind: "changed", folder: "40-sources" } },
      }),
    });
    expect(screen.getByLabelText(t("routines.editor.run_if_changed"))).toHaveProperty(
      "checked",
      true,
    );
    expect(screen.getByLabelText("Folder")).toHaveProperty("value", "40-sources");
    await save();
    expect(documentAt(api.revise, 0, 3).run_only_if).toEqual({
      kind: "changed",
      folder: "40-sources",
    });
  });

  test("a saved routine reopens with its settings", async () => {
    const { api } = await setup({
      initial: detail({
        document: {
          ...savedDocument(),
          run_only_if: { kind: "card", seat: "research" },
          on_confirmation: "stop",
        },
      }),
    });
    expect(screen.getByLabelText(t("routines.editor.run_if_card"))).toHaveProperty("checked", true);
    expect(screen.getByLabelText("Seat")).toHaveProperty("value", "research");
    expect(screen.getByLabelText(t("routines.editor.on_confirmation_stop"))).toHaveProperty(
      "checked",
      true,
    );
    await save();
    expect(documentAt(api.revise, 0, 3)).toMatchObject({
      run_only_if: { kind: "card", seat: "research" },
      on_confirmation: "stop",
    });
  });

  test("revises with the routine's current revision", async () => {
    const { api, onSaved } = await setup({ initial: detail() });
    type("Name", "Evening");
    await save();
    expect(api.create).not.toHaveBeenCalled();
    expect(api.revise).toHaveBeenCalledTimes(1);
    const [id, , revision, document] = api.revise.mock.calls[0] as [
      string,
      string,
      number,
      RoutineDocument,
    ];
    expect([id, revision, document.name]).toEqual(["r1", 7, "Evening"]);
    expect(onSaved).toHaveBeenCalledWith("r1");
  });

  test("reuses the command id after a failure, and mints a new one after an edit or a success", async () => {
    const create = vi
      .fn()
      .mockRejectedValueOnce(new RoutinesApiError(0, "unavailable"))
      .mockRejectedValueOnce(new RoutinesApiError(0, "unavailable"))
      .mockResolvedValue(CHANGE);
    const { api, onSaved } = await setup({ api: fakeApi({ create }) });
    type("Name", "Daily brief");
    type("Prompt", "Do it");
    await save();
    expect(screen.getByRole("alert").textContent).toBe(t("routines.error.unavailable"));
    await save();
    type("Prompt", "Do it differently");
    await save();
    const ids = api.create.mock.calls.map((call) => call[0] as string);
    expect(ids).toHaveLength(3);
    expect(ids[1]).toBe(ids[0]);
    expect(ids[2]).not.toBe(ids[0]);
    expect(onSaved).toHaveBeenCalledTimes(1);
  });

  test("drops the command id after a success", async () => {
    const { api } = await setup();
    type("Name", "Daily brief");
    type("Prompt", "Do it");
    await save();
    await save();
    const ids = api.create.mock.calls.map((call) => call[0] as string);
    expect(ids).toHaveLength(2);
    expect(ids[1]).not.toBe(ids[0]);
  });

  test("sorts and dedupes times and days before sending", async () => {
    const { api } = await setup({
      initial: detail({
        document: savedDocument({
          kind: "weekly",
          times: ["18:00", "09:00", "18:00"],
          days: [5, 1, 3],
        }),
      }),
    });
    await save();
    const document = documentAt(api.revise, 0, 3);
    expect(document.schedule.times).toEqual(["09:00", "18:00"]);
    expect(document.schedule.days).toEqual([1, 3, 5]);
  });

  test("daily clears the days; manual clears times, days, zone and dates", async () => {
    const { api } = await setup({
      initial: detail({
        document: savedDocument({ kind: "weekly", days: [1, 2], start_date: "2026-10-01" }),
      }),
    });
    fireEvent.click(screen.getByLabelText("Daily"));
    await save();
    expect(documentAt(api.revise, 0, 3).schedule).toMatchObject({
      kind: "daily",
      days: [],
      times: ["09:00"],
      start_date: "2026-10-01",
    });
    fireEvent.click(screen.getByLabelText("Manual only"));
    expect(screen.queryByLabelText("Time zone")).toBeNull();
    await save();
    expect(documentAt(api.revise, 1, 3).schedule).toEqual({
      kind: "manual",
      times: [],
      days: [],
      timezone: null,
      start_date: null,
      end_date: null,
      catch_up: "skip",
    });
  });

  test("weekly sends the ticked days; the zone select keeps a zone the browser does not list", async () => {
    const { api } = await setup({
      initial: detail({
        document: savedDocument({ kind: "weekly", days: [], timezone: "Mars/Olympus" }),
      }),
    });
    expect((screen.getByLabelText("Time zone") as HTMLSelectElement).value).toBe("Mars/Olympus");
    fireEvent.click(screen.getByLabelText("Sun"));
    fireEvent.click(screen.getByLabelText("Mon"));
    await save();
    expect(documentAt(api.revise, 0, 3).schedule.days).toEqual([1, 7]);
  });

  test("adds and removes times, at most four, at least one", async () => {
    await setup();
    const add = screen.getByRole("button", { name: "Add a time" }) as HTMLButtonElement;
    const remove = () => screen.getAllByRole("button", { name: "Remove" }) as HTMLButtonElement[];
    expect(remove()[0]?.disabled).toBe(true);
    for (let count = 0; count < 3; count++) fireEvent.click(add);
    expect(screen.getAllByLabelText(/^Times \d$/)).toHaveLength(4);
    expect(add.disabled).toBe(true);
    fireEvent.click(remove()[3] as HTMLButtonElement);
    expect(screen.getAllByLabelText(/^Times \d$/)).toHaveLength(3);
    expect(add.disabled).toBe(false);
  });

  test.each([
    ["name", () => type("Name", ""), "Name"],
    ["name over 80 characters", () => type("Name", "a".repeat(81)), "Name"],
    [
      "purpose over 1,000 characters",
      () => type("Purpose (optional)", "a".repeat(1001)),
      "Purpose (optional)",
    ],
    ["a name with a control character", () => type("Name", "Daily\u0007brief"), "Name"],
    ["empty prompt", () => type("Prompt", ""), "Prompt"],
    ["prompt over 32,768 bytes", () => type("Prompt", "あ".repeat(10_923)), "Prompt"],
    ["deadline under 5", () => type("Time limit (minutes)", "4"), "Time limit (minutes)"],
    ["deadline over 240", () => type("Time limit (minutes)", "241"), "Time limit (minutes)"],
    ["an empty time", () => type("Times 1", ""), "Times"],
    [
      "an end date before the start date",
      () => {
        type("Start date (optional)", "2026-10-10");
        type("End date (optional)", "2026-10-09");
      },
      "End date (optional)",
    ],
  ])("refuses %s and sends nothing", async (_name, edit, field) => {
    const { api, onSaved } = await setup();
    type("Name", "Daily brief");
    type("Prompt", "Do it");
    edit();
    await save();
    expect(screen.getByRole("alert").textContent).toBe(`Check your input: ${field}`);
    expect(api.create).not.toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();
  });

  test("accepts the bounds: 80 characters, 32,766 bytes, 5 and 240 minutes", async () => {
    const { api } = await setup();
    type("Name", "a".repeat(80));
    type("Prompt", "あ".repeat(10_922));
    type("Time limit (minutes)", "5");
    await save();
    type("Time limit (minutes)", "240");
    await save();
    expect(api.create).toHaveBeenCalledTimes(2);
  });

  test("a weekly routine with no day is refused", async () => {
    const { api } = await setup();
    type("Name", "Daily brief");
    type("Prompt", "Do it");
    fireEvent.click(screen.getByLabelText("Weekly"));
    await save();
    expect(screen.getByRole("alert").textContent).toBe("Check your input: Days");
    expect(api.create).not.toHaveBeenCalled();
  });

  test("previews 300 ms after the last change and aborts the previous request", async () => {
    const signals: AbortSignal[] = [];
    let settleFirst: (data: RoutinePreviewData) => void = () => {};
    const preview = vi
      .fn()
      .mockImplementationOnce((_document: RoutineDocument, signal: AbortSignal) => {
        signals.push(signal);
        return new Promise<RoutinePreviewData>((resolve) => {
          settleFirst = resolve;
        });
      })
      .mockImplementation((_document: RoutineDocument, signal: AbortSignal) => {
        signals.push(signal);
        return Promise.resolve(previewData("Fresh"));
      });
    const timers = manualTimers();
    const api = fakeApi({ preview });
    await setup({ api, timers, initial: null });
    type("Name", "Daily brief");
    type("Prompt", "Do it");
    type("Prompt", "Do it now");
    expect(timers.delays()).toEqual([300]);
    expect(preview).not.toHaveBeenCalled();
    await timers.flush();
    expect(preview).toHaveBeenCalledTimes(1);
    expect(documentAt(preview, 0, 0).prompt).toBe("Do it now");
    expect(signals[0]?.aborted).toBe(false);

    type("Prompt", "Do it later");
    expect(signals[0]?.aborted).toBe(true);
    await act(async () => settleFirst(previewData("Stale")));
    expect(screen.queryByText("Stale")).toBeNull();
    await timers.flush();
    expect(preview).toHaveBeenCalledTimes(2);
    expect(signals[1]?.aborted).toBe(false);
    expect(screen.getByText("Fresh")).toBeTruthy();
  });

  test("an invalid document is not previewed and drops the shown rehearsal", async () => {
    const { api, timers } = await setup({ initial: detail() });
    expect(screen.getByText("Shown")).toBeTruthy();
    type("Name", "");
    expect(timers.delays()).toEqual([]);
    expect(screen.queryByText("Shown")).toBeNull();
    await timers.flush();
    expect(api.preview).not.toHaveBeenCalled();
  });

  test("old rehearsal is marked stale until latest success", async () => {
    const read = deferred<RoutinePreviewData>();
    const preview = vi.fn().mockReturnValue(read.promise);
    const timers = manualTimers();
    await setup({ api: fakeApi({ preview }), timers, initial: detail() });
    expect(screen.queryByRole("status")).toBeNull();
    type("Prompt", "Changed");
    expect(screen.getByRole("status").textContent).toContain("Rehearsal is stale");
    type("Prompt", "Summarize");
    expect(screen.getByRole("status").textContent).toContain("Rehearsal is stale");
    expect(screen.getByText("Shown")).toBeTruthy();
    expect(preview).not.toHaveBeenCalled();
    await timers.flush();
    expect(screen.getByRole("status").textContent).toContain("Rehearsal is stale");
    expect(documentAt(preview, 0, 0).prompt).toBe("Summarize");
    await act(async () => read.resolve(previewData("Latest")));
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.queryByText("Shown")).toBeNull();
    expect(screen.getByText("Latest")).toBeTruthy();
  });

  test.each(["daily", "manual"] as const)(
    "retained %s rehearsal keeps its original schedule semantics",
    async (kind) => {
      const initial = detail({ document: savedDocument({ kind }) });
      initial.next_five = [
        { at: null, local: "Old slot", zone: "UTC", offset_minutes: 0, note: null },
      ];
      const timers = manualTimers();
      const { api } = await setup({ initial, timers });
      const oldMeaning = kind === "manual" ? t("routines.preview.manual") : "Old slot UTC";
      expect(screen.getByText(oldMeaning)).toBeTruthy();
      fireEvent.click(
        screen.getByLabelText(t(`routines.editor.kind_${kind === "manual" ? "daily" : "manual"}`)),
      );
      expect(screen.getByText(oldMeaning)).toBeTruthy();
      expect(screen.getByRole("status").textContent).toContain("Rehearsal is stale");
      expect(api.preview).not.toHaveBeenCalled();
      await timers.flush();
      expect(screen.queryByText(oldMeaning)).toBeNull();
      expect(screen.queryByRole("status")).toBeNull();
      expect(
        screen.getByText(
          t(kind === "manual" ? "routines.preview.unattended" : "routines.preview.manual"),
        ),
      ).toBeTruthy();
    },
  );

  test("failed rehearsal stays stale and explicit Retry reads the current draft", async () => {
    const retry = deferred<RoutinePreviewData>();
    const preview = vi
      .fn()
      .mockRejectedValueOnce(new RoutinesApiError(0, "unavailable"))
      .mockReturnValueOnce(retry.promise);
    const timers = manualTimers();
    const { api } = await setup({ api: fakeApi({ preview }), timers, initial: detail() });
    type("Prompt", "Retry this draft");
    await timers.flush();
    expect(screen.getByRole("alert").textContent).toBe(t("routines.error.unavailable"));
    expect(screen.getByRole("status").textContent).toContain("Rehearsal is stale");
    expect(screen.getByText("Shown")).toBeTruthy();
    expect(preview).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Retry rehearsal" }));
    await timers.flush();
    expect(preview).toHaveBeenCalledTimes(2);
    expect(documentAt(preview, 1, 0).prompt).toBe("Retry this draft");
    expect(screen.getByRole("status").textContent).toContain("Rehearsal is stale");
    expect(api.create).not.toHaveBeenCalled();
    expect(api.revise).not.toHaveBeenCalled();
    await act(async () => retry.resolve(previewData("Retried")));
    expect(screen.getByText("Retried")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
  });

  test("late reads cannot replace a fresh rehearsal or survive unmount", async () => {
    const old = deferred<RoutinePreviewData>();
    const unmounted = deferred<RoutinePreviewData>();
    const preview = vi
      .fn()
      .mockReturnValueOnce(old.promise)
      .mockResolvedValueOnce(previewData("Current"))
      .mockReturnValueOnce(unmounted.promise);
    const timers = manualTimers();
    const { view } = await setup({ api: fakeApi({ preview }), timers, initial: detail() });
    await timers.flush();
    type("Prompt", "Current draft");
    await timers.flush();
    await act(async () => old.reject(new RoutinesApiError(0, "unavailable")));
    expect(screen.getByText("Current")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
    type("Prompt", "Unmounted draft");
    await timers.flush();
    const signal = preview.mock.calls[2]?.[1] as AbortSignal;
    view.unmount();
    expect(signal.aborted).toBe(true);
    expect(timers.delays()).toEqual([]);
    await act(async () => unmounted.resolve(previewData("Unmounted")));
    expect(screen.queryByText("Unmounted")).toBeNull();
    expect(preview).toHaveBeenCalledTimes(3);
  });

  test("a preview refusal is shown and Save is still offered", async () => {
    const preview = vi.fn().mockRejectedValue(new RoutinesApiError(400, "invalid_schedule"));
    const { timers } = await setup({ api: fakeApi({ preview }), initial: detail() });
    await timers.flush();
    expect(screen.getByRole("alert").textContent).toBe(t("routines.error.invalid_schedule"));
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(
      false,
    );
  });

  test("a stale revision shows the conflict text and changes nothing", async () => {
    const revise = vi.fn().mockRejectedValue(new RoutinesApiError(409, "conflict"));
    const { onSaved } = await setup({ api: fakeApi({ revise }), initial: detail() });
    type("Name", "Evening");
    await save();
    expect(screen.getByRole("alert").textContent).toBe(t("routines.error.conflict"));
    expect(onSaved).not.toHaveBeenCalled();
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("Evening");
    expect(hasUnsavedWork()).toBe(true);
  });

  test("reviewed conflict can save against the fresh revision", async () => {
    const revise = vi
      .fn()
      .mockRejectedValueOnce(new RoutinesApiError(409, "conflict"))
      .mockResolvedValue({ ...CHANGE, routine_id: "r1", revision: 9 });
    const get = vi.fn().mockResolvedValue(
      detail({
        current_revision: 8,
        document: { ...savedDocument(), name: "Saved elsewhere", prompt: "Saved prompt" },
      }),
    );
    const { onSaved } = await setup({ api: fakeApi({ revise, get }), initial: detail() });
    type("Name", "Evening draft");
    await save();
    expect(get).not.toHaveBeenCalled();
    await save();
    expect(revise).toHaveBeenCalledTimes(1);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reload current revision" }));
    });
    expect(get).toHaveBeenCalledWith("r1", expect.any(AbortSignal));
    expect(screen.getByRole("heading", { name: "Current saved revision 8" })).toBeTruthy();
    expect(screen.getByText("Saved elsewhere")).toBeTruthy();
    expect(screen.getByText("Saved prompt")).toBeTruthy();
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("Evening draft");
    expect((screen.getByLabelText("Prompt") as HTMLTextAreaElement).value).toBe("Summarize");
    expect(hasUnsavedWork()).toBe(true);
    expect(revise).toHaveBeenCalledTimes(1);
    expect(onSaved).not.toHaveBeenCalled();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save draft against revision 8" }));
    });
    expect(revise).toHaveBeenCalledTimes(2);
    expect(revise.mock.calls[1]?.[2]).toBe(8);
    expect(revise.mock.calls[1]?.[1]).not.toBe(revise.mock.calls[0]?.[1]);
    expect(documentAt(revise, 1, 3)).toMatchObject({ name: "Evening draft", prompt: "Summarize" });
    expect(onSaved).toHaveBeenCalledWith("r1");
    expect(hasUnsavedWork()).toBe(false);
  });

  test("failed and pending conflict reloads keep the draft and block saves", async () => {
    let settle: (data: RoutineDetailData) => void = () => {};
    const get = vi
      .fn()
      .mockRejectedValueOnce(new RoutinesApiError(0, "unavailable"))
      .mockImplementationOnce(
        () =>
          new Promise<RoutineDetailData>((resolve) => {
            settle = resolve;
          }),
      );
    const revise = vi.fn().mockRejectedValue(new RoutinesApiError(409, "conflict"));
    const { view, onSaved } = await setup({ api: fakeApi({ revise, get }), initial: detail() });
    type("Name", "Evening draft");
    await save();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reload current revision" }));
    });
    expect(screen.getByRole("alert").textContent).toBe(t("routines.error.unavailable"));
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => {
      const reload = screen.getByRole("button", { name: "Reload current revision" });
      fireEvent.click(reload);
      fireEvent.click(reload);
      fireEvent.submit(screen.getByLabelText("Name").closest("form") as HTMLFormElement);
    });
    expect(get).toHaveBeenCalledTimes(2);
    expect(revise).toHaveBeenCalledTimes(1);
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
    type("Name", "Typing during reload");
    expect(hasUnsavedWork()).toBe(true);
    const signal = get.mock.calls[1]?.[1] as AbortSignal;
    view.unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => settle(detail({ current_revision: 8 })));
    expect(onSaved).not.toHaveBeenCalled();
    expect(revise).toHaveBeenCalledTimes(1);
  });

  test("reports unsaved work as owner routine, and not for the starting profile default", async () => {
    const { view } = await setup();
    expect(hasUnsavedWork()).toBe(false);
    type("Name", "Daily brief");
    expect(hasUnsavedWork()).toBe(true);
    type("Name", "");
    expect(hasUnsavedWork()).toBe(false);
    type("Name", "x");
    view.unmount();
    expect(hasUnsavedWork()).toBe(false);
  });

  test("no profiles: says so and disables Save", async () => {
    const { api } = await setup({ profiles: profilesApi([]) });
    expect(screen.getByText(t("routines.editor.no_profiles"))).toBeTruthy();
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
    expect(api.create).not.toHaveBeenCalled();
  });

  test("archived profiles are not offered, and no profile client means none", async () => {
    await setup({
      profiles: profilesApi([
        { id: "p9", name: "Old", archived: true },
        { id: "p1", name: "Prof A" },
      ]),
    });
    expect(screen.queryByRole("option", { name: "Old" })).toBeNull();
    cleanup();
    render(
      <RoutineEditor
        api={fakeApi()}
        initial={null}
        workspaces={WORKSPACES}
        activeWorkspaceId="w1"
        timers={manualTimers()}
        onSaved={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.getByText(t("routines.editor.no_profiles"))).toBeTruthy();
  });

  test("notes: tested says retest, enabled says the next run, a draft says neither", async () => {
    await setup({ initial: detail({ state: "tested" }) });
    expect(screen.getByText(t("routines.editor.retest"))).toBeTruthy();
    expect(screen.queryByText(t("routines.editor.enabled_note"))).toBeNull();
    cleanup();
    await setup({ initial: detail({ state: "enabled" }) });
    expect(screen.getByText(t("routines.editor.enabled_note"))).toBeTruthy();
    expect(screen.queryByText(t("routines.editor.retest"))).toBeNull();
    cleanup();
    await setup({ initial: detail({ state: "draft" }) });
    expect(screen.queryByText(t("routines.editor.retest"))).toBeNull();
    expect(screen.queryByText(t("routines.editor.enabled_note"))).toBeNull();
  });

  test("Cancel calls onCancel", async () => {
    const { onCancel } = await setup();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});

describe("delayed workspace delivery causality proof", () => {
  test("a new empty unowned form adopts a late workspace and can preview", async () => {
    const profiles = profilesApi([{ id: "p1", name: "Prof A" }]);
    const { api, timers, view, onSaved, onCancel } = await setup({
      profiles,
      workspaces: [],
      activeWorkspaceId: "",
    });
    type("Name", "Daily brief");
    type("Prompt", "Do it");
    fireEvent.click(screen.getByLabelText("Manual only"));
    await timers.flush();
    expect(api.preview).not.toHaveBeenCalled();
    view.rerender(
      <RoutineEditor
        api={api}
        initial={null}
        profiles={profiles}
        workspaces={WORKSPACES}
        activeWorkspaceId="w2"
        timers={timers}
        onSaved={onSaved}
        onCancel={onCancel}
      />,
    );
    await timers.flush();
    expect(api.preview).toHaveBeenCalledTimes(1);
    expect(documentAt(api.preview, 0, 0)).toMatchObject({
      name: "Daily brief",
      prompt: "Do it",
      profile_id: "p1",
      workspace_id: "w2",
    });
    expect(screen.getByLabelText("Name")).toHaveProperty("value", "Daily brief");
    expect(screen.getByLabelText("Prompt")).toHaveProperty("value", "Do it");
    expect(hasUnsavedWork()).toBe(true);
  });

  test("late workspace data does not retarget an existing routine", async () => {
    const profiles = profilesApi([{ id: "p1", name: "Prof A" }]);
    const initial = detail();
    const { api, timers, view, onSaved, onCancel } = await setup({
      profiles,
      initial,
      workspaces: [],
      activeWorkspaceId: "",
    });
    view.rerender(
      <RoutineEditor
        api={api}
        initial={initial}
        profiles={profiles}
        workspaces={WORKSPACES}
        activeWorkspaceId="w2"
        timers={timers}
        onSaved={onSaved}
        onCancel={onCancel}
      />,
    );
    await timers.flush();
    expect(documentAt(api.preview, api.preview.mock.calls.length - 1, 0).workspace_id).toBe("w1");
  });

  test("late active workspace does not retarget the user's explicit choice", async () => {
    const profiles = profilesApi([{ id: "p1", name: "Prof A" }]);
    const { api, timers, view, onSaved, onCancel } = await setup({
      profiles,
      activeWorkspaceId: "w1",
    });
    type("Name", "Daily brief");
    type("Prompt", "Do it");
    type("Workspace", "w1");
    view.rerender(
      <RoutineEditor
        api={api}
        initial={null}
        profiles={profiles}
        workspaces={WORKSPACES}
        activeWorkspaceId="w2"
        timers={timers}
        onSaved={onSaved}
        onCancel={onCancel}
      />,
    );
    await timers.flush();
    expect(documentAt(api.preview, api.preview.mock.calls.length - 1, 0).workspace_id).toBe("w1");
  });

  test("late discovery does not retarget a locked Bot owner", async () => {
    const profiles = profilesApi([{ id: "p1", name: "Prof A" }]);
    const lockedOwner = {
      botName: "Planner",
      profileId: "p1",
      profileName: "Prof A",
      workspaceId: "w1",
      workspaceName: "Main vault",
    };
    const { api, timers, view, onSaved, onCancel } = await setup({
      profiles,
      lockedOwner,
      workspaces: [],
      activeWorkspaceId: "",
    });
    type("Name", "Daily brief");
    type("Prompt", "Do it");
    view.rerender(
      <RoutineEditor
        api={api}
        initial={null}
        profiles={profiles}
        lockedOwner={lockedOwner}
        workspaces={WORKSPACES}
        activeWorkspaceId="w2"
        timers={timers}
        onSaved={onSaved}
        onCancel={onCancel}
      />,
    );
    await timers.flush();
    expect(documentAt(api.preview, api.preview.mock.calls.length - 1, 0).workspace_id).toBe("w1");
    expect(screen.getByLabelText("Workspace")).toHaveProperty("disabled", true);
  });

  test("late discovery initializes a pristine form without marking it unsaved", async () => {
    const profiles = profilesApi([{ id: "p1", name: "Prof A" }]);
    const { api, timers, view, onSaved, onCancel } = await setup({
      profiles,
      workspaces: [],
      activeWorkspaceId: "",
    });
    expect(hasUnsavedWork()).toBe(false);
    view.rerender(
      <RoutineEditor
        api={api}
        initial={null}
        profiles={profiles}
        workspaces={WORKSPACES}
        activeWorkspaceId="w2"
        timers={timers}
        onSaved={onSaved}
        onCancel={onCancel}
      />,
    );
    expect(screen.getByLabelText("Workspace")).toHaveProperty("value", "w2");
    expect(hasUnsavedWork()).toBe(false);
  });
});
