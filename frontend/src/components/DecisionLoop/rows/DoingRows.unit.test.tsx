import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import {
  createFakeDecisionLoopApi,
  decisionLoopExamples,
  type FakeDecisionLoopApi,
} from "../../../clients/decisionLoop/fake";
import type { MissionView, PageView } from "../../../clients/decisionLoop/types";
import { DoRow, HowRow } from "./DoingRows";

afterEach(cleanup);

const DEFAULT_VALUES: Readonly<Record<string, unknown>> = {
  "decision.grant.prepare": "allowed",
  "decision.grant.run": "ask",
  "decision.limit.drafts": 5,
  "decision.limit.chats": 3,
  "decision.limit.changes": 3,
  "decision.plan_brake": "80",
};

const SECOND_MISSION: MissionView = {
  mission_id: "second-mission",
  session_id: "second-session",
  work: "run",
  state: "running",
  title: "File the inbox notes",
  started_at: "2026-10-21T13:00:00.000Z",
  finished_at: null,
  reason: "",
};

function basePage(): PageView {
  return decisionLoopExamples().page;
}

interface RowOverrides {
  page?: PageView;
  values?: Readonly<Record<string, unknown>>;
  writable?: boolean;
  api?: FakeDecisionLoopApi;
}

/** Every row starts closed (`DecisionRow`'s body is mounted but `hidden`, which `getByRole` and
 * `getByLabelText` correctly treat as absent from the page); open it, as a user click would,
 * so a row's own tests can reach its body through those queries. */
function open(name: string): void {
  fireEvent.click(screen.getByRole("button", { name }));
}

function mountDoRow(overrides: RowOverrides & { nowMs?: number } = {}) {
  const api = overrides.api ?? createFakeDecisionLoopApi();
  const patch = vi.fn(async (_changes: Record<string, unknown>) => ({}));
  const onRefresh = vi.fn();
  const onOpenChat = vi.fn();
  render(
    <DoRow
      page={overrides.page ?? basePage()}
      values={overrides.values ?? DEFAULT_VALUES}
      writable={overrides.writable ?? true}
      patch={patch}
      api={api}
      onRefresh={onRefresh}
      onOpenChat={onOpenChat}
      nowMs={overrides.nowMs ?? Date.now()}
    />,
  );
  open("What VIKI may do");
  for (const summary of Array.from(document.querySelectorAll("#do-body details > summary"))) {
    fireEvent.click(summary);
  }
  return { api, patch, onRefresh, onOpenChat };
}

function mountHowRow(overrides: RowOverrides = {}) {
  const api = overrides.api ?? createFakeDecisionLoopApi();
  const patch = vi.fn(async (_changes: Record<string, unknown>) => ({}));
  const onRefresh = vi.fn();
  const onOpenData = vi.fn();
  render(
    <HowRow
      page={overrides.page ?? basePage()}
      values={overrides.values ?? DEFAULT_VALUES}
      writable={overrides.writable ?? true}
      patch={patch}
      api={api}
      onRefresh={onRefresh}
      onOpenData={onOpenData}
    />,
  );
  open("How VIKI is doing");
  return { api, onRefresh, onOpenData };
}

// --- DoRow -----------------------------------------------------------------------------------

test("DoRow starts closed, kept mounted, and opens when its own disclosure is clicked", () => {
  render(
    <DoRow
      page={basePage()}
      values={DEFAULT_VALUES}
      writable={true}
      patch={vi.fn()}
      api={createFakeDecisionLoopApi()}
      onRefresh={vi.fn()}
      onOpenChat={vi.fn()}
    />,
  );
  expect(document.getElementById("do-body")?.hidden).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "What VIKI may do" }));
  expect(document.getElementById("do-body")?.hidden).toBe(false);
});

test("DoRow shows both grants, the fixed line, and today's use against each daily limit", () => {
  mountDoRow();
  const section = within(screen.getByRole("region", { name: "What VIKI may do" }));
  const prepareGroup = within(section.getByRole("group", { name: "Prepare drafts" }));
  expect((prepareGroup.getByRole("radio", { name: "Allowed" }) as HTMLInputElement).checked).toBe(
    true,
  );
  const runGroup = within(section.getByRole("group", { name: "Start chats to do work" }));
  expect((runGroup.getByRole("radio", { name: "Ask first" }) as HTMLInputElement).checked).toBe(
    true,
  );
  expect(
    section.getByText(
      "VIKI starts a Bot only through its own tested routine, under Start Bots' work. Apart from the notice of a Bot whose delivery you turned on, it does not message other chats or people or send to your phone.",
    ),
  ).toBeTruthy();
  expect(section.getByText("Today: 2 of 5 drafts")).toBeTruthy();
  expect(section.getByText("Today: 1 of 3 chats")).toBeTruthy();
  expect(section.getByText("Today: 1 of 3 vault changes")).toBeTruthy();
});

test("DoRow's status chip reads Off only once all six grants are off and nothing is running", () => {
  const page = basePage();
  page.missions.running = [];
  page.missions.team.running = 0;
  const off = { "decision.grant.prepare": "off", "decision.grant.run": "off" };
  const allOff = {
    ...off,
    "decision.grant.team": "off",
    "decision.grant.look": "off",
    "decision.grant.second_look": "off",
    "decision.grant.project_plan": "off",
    "decision.grant.check": "off",
    "decision.grant.inbox_triage": "off",
  };
  mountDoRow({ page, values: allOff });
  expect(screen.getByRole("status").textContent).toBe("Off");
  cleanup();
  mountDoRow({ page, values: { ...allOff, "decision.grant.check": "ask" } });
  expect(screen.getByRole("status").textContent).toBe("On");
  cleanup();
  mountDoRow({ page, values: { ...allOff, "decision.grant.look": "ask" } });
  expect(screen.getByRole("status").textContent).toBe("On");
  cleanup();
  mountDoRow({ page, values: { ...allOff, "decision.grant.second_look": "ask" } });
  expect(screen.getByRole("status").textContent).toBe("On");
  cleanup();
  mountDoRow({ page, values: { ...allOff, "decision.grant.project_plan": "ask" } });
  expect(screen.getByRole("status").textContent).toBe("On");
  cleanup();
  cleanup();
  mountDoRow({ page, values: { ...allOff, "decision.grant.inbox_triage": "ask" } });
  expect(screen.getByRole("status").textContent).toBe("On");
  cleanup();
  mountDoRow({ page, values: off });
  expect(screen.getByRole("status").textContent).toBe("On");
});

test("the status chip reads Off off Windows with the check grant left at Ask first", () => {
  const page = basePage();
  page.missions.running = [];
  page.missions.team.running = 0;
  page.missions.checks = null;
  mountDoRow({
    page,
    values: {
      "decision.grant.prepare": "off",
      "decision.grant.run": "off",
      "decision.grant.team": "off",
      "decision.grant.look": "off",
      "decision.grant.second_look": "off",
      "decision.grant.project_plan": "off",
      "decision.grant.check": "ask",
      "decision.grant.inbox_triage": "off",
    },
  });
  expect(screen.getByRole("status").textContent).toBe("Off");
});

test("DoRow's status chip counts running missions and Bot runs ahead of the grants", () => {
  mountDoRow();
  expect(screen.getByRole("status").textContent).toBe("2 running");
});

test("Start Bots' work shows ask by default, patches decision.grant.team, and hints", () => {
  const { patch } = mountDoRow();
  const group = within(screen.getByRole("group", { name: "Start Bots' work" }));
  expect((group.getByRole("radio", { name: "Ask first" }) as HTMLInputElement).checked).toBe(true);
  fireEvent.click(group.getByRole("radio", { name: "Allowed" }));
  expect(patch).toHaveBeenCalledWith({ "decision.grant.team": "allowed" });
  expect(
    screen.getByText(
      "When one of your todo cards waits for a seat, VIKI starts that seat's one enabled, tested routine. Ask first offers Start in VIKI's launcher instead.",
    ),
  ).toBeTruthy();
});

test("the Bot-run limit patches decision.limit.team with a number and shows today's use", () => {
  const { patch } = mountDoRow();
  expect(screen.getByText("Today: 1 of 4 Bot runs")).toBeTruthy();
  fireEvent.change(screen.getByLabelText("Daily limit: Bot runs VIKI starts"), {
    target: { value: "6" },
  });
  expect(patch).toHaveBeenCalledWith({ "decision.limit.team": 6 });
});

test("Look on my behalf shows allowed by default, patches decision.grant.look, and hints", () => {
  const { patch } = mountDoRow();
  const group = within(screen.getByRole("group", { name: "Look on my behalf" }));
  expect((group.getByRole("radio", { name: "Allowed" }) as HTMLInputElement).checked).toBe(true);
  fireEvent.click(group.getByRole("radio", { name: "Ask first" }));
  expect(patch).toHaveBeenCalledWith({ "decision.grant.look": "ask" });
  expect(
    screen.getByText(
      "When a trusted code folder's uncommitted changes have not changed for a day or more, VIKI starts a chat there in plan mode to look them over; it waits for your approval before it changes anything. Ask first offers Take a look in VIKI's launcher instead.",
    ),
  ).toBeTruthy();
});

test("choosing Off for Look on my behalf patches decision.grant.look", () => {
  const { patch } = mountDoRow();
  const group = within(screen.getByRole("group", { name: "Look on my behalf" }));
  fireEvent.click(group.getByRole("radio", { name: "Off" }));
  expect(patch).toHaveBeenCalledWith({ "decision.grant.look": "off" });
});

test("the look limit patches decision.limit.look with a number and shows today's use", () => {
  const page = basePage();
  page.missions.look = { today: 2, limit: 3 };
  const { patch } = mountDoRow({ page });
  expect(screen.getByText("Today: 2 of 3 looks")).toBeTruthy();
  const select = screen.getByLabelText("Daily limit: looks VIKI starts") as HTMLSelectElement;
  expect(select.value).toBe("3");
  fireEvent.change(select, { target: { value: "7" } });
  expect(patch).toHaveBeenCalledWith({ "decision.limit.look": 7 });
});

test("an allowed look grant alone makes the brake hold show Holding", () => {
  mountDoRow({
    page: heldPage(null),
    values: {
      ...DEFAULT_VALUES,
      "decision.grant.prepare": "ask",
      "decision.grant.team": "ask",
      "decision.grant.look": "allowed",
    },
    nowMs: NOON,
  });
  expect(screen.getByRole("status").textContent).toBe("Holding");
});

test("Take a second look at my work shows allowed by default, patches its grant, and hints", () => {
  const { patch } = mountDoRow();
  const group = within(screen.getByRole("group", { name: "Take a second look at my work" }));
  expect((group.getByRole("radio", { name: "Allowed" }) as HTMLInputElement).checked).toBe(true);
  fireEvent.click(group.getByRole("radio", { name: "Ask first" }));
  expect(patch).toHaveBeenCalledWith({ "decision.grant.second_look": "ask" });
  expect(
    screen.getByText(
      "When a long task or work you corrected finishes in the chat you have open and edited notes, VIKI starts a chat in plan mode to look the work over and present one flaw, better approach or missing piece as a plan; it waits for your approval before it changes anything. Ask first offers Take a second look in VIKI's launcher instead.",
    ),
  ).toBeTruthy();
});

test("choosing Off for Take a second look patches decision.grant.second_look", () => {
  const { patch } = mountDoRow();
  const group = within(screen.getByRole("group", { name: "Take a second look at my work" }));
  fireEvent.click(group.getByRole("radio", { name: "Off" }));
  expect(patch).toHaveBeenCalledWith({ "decision.grant.second_look": "off" });
});

test("the second-look limit patches decision.limit.second_look and shows today's use", () => {
  const page = basePage();
  page.missions.second_look = { today: 1, limit: 3 };
  const { patch } = mountDoRow({ page });
  expect(screen.getByText("Today: 1 of 3 second looks")).toBeTruthy();
  const select = screen.getByLabelText(
    "Daily limit: second looks VIKI starts",
  ) as HTMLSelectElement;
  expect(select.value).toBe("3");
  fireEvent.change(select, { target: { value: "5" } });
  expect(patch).toHaveBeenCalledWith({ "decision.limit.second_look": 5 });
});

test("an allowed second-look grant alone makes the brake hold show Holding", () => {
  mountDoRow({
    page: heldPage(null),
    values: {
      ...DEFAULT_VALUES,
      "decision.grant.prepare": "ask",
      "decision.grant.team": "ask",
      "decision.grant.look": "ask",
      "decision.grant.second_look": "allowed",
    },
    nowMs: NOON,
  });
  expect(screen.getByRole("status").textContent).toBe("Holding");
});

test("Plan next steps for my projects shows allowed by default, patches its grant, and hints", () => {
  const { patch } = mountDoRow();
  const group = within(screen.getByRole("group", { name: "Plan next steps for my projects" }));
  expect((group.getByRole("radio", { name: "Allowed" }) as HTMLInputElement).checked).toBe(true);
  fireEvent.click(group.getByRole("radio", { name: "Ask first" }));
  expect(patch).toHaveBeenCalledWith({ "decision.grant.project_plan": "ask" });
  expect(
    screen.getByText(
      "When nothing in an active project has changed for two weeks, or a project's tasks are three days or more overdue, VIKI starts a chat in plan mode: for the project it proposes three next actions, and for the tasks it proposes ticking the done ones and new dates for the rest; it waits for your approval before it changes anything. Ask first offers Plan next steps or Check these tasks in VIKI's launcher instead.",
    ),
  ).toBeTruthy();
});

test("choosing Off for Plan next steps patches decision.grant.project_plan", () => {
  const { patch } = mountDoRow();
  const group = within(screen.getByRole("group", { name: "Plan next steps for my projects" }));
  fireEvent.click(group.getByRole("radio", { name: "Off" }));
  expect(patch).toHaveBeenCalledWith({ "decision.grant.project_plan": "off" });
});

test("the project-plan limit patches decision.limit.project_plan and shows today's use", () => {
  const page = basePage();
  page.missions.project_plan = { today: 2, limit: 3 };
  const { patch } = mountDoRow({ page });
  expect(screen.getByText("Today: 2 of 3 project plans")).toBeTruthy();
  const select = screen.getByLabelText(
    "Daily limit: project plans VIKI starts",
  ) as HTMLSelectElement;
  expect(select.value).toBe("3");
  fireEvent.change(select, { target: { value: "5" } });
  expect(patch).toHaveBeenCalledWith({ "decision.limit.project_plan": 5 });
});

test("the message-draft limit defaults to 3, patches decision.limit.message_draft and shows today's use", () => {
  const page = basePage();
  page.message_drafts = { today: 1, limit: 3 };
  const { patch } = mountDoRow({ page });
  expect(screen.getByText("Today: 1 of 3 message drafts")).toBeTruthy();
  expect(
    screen.getByText(/Under Prepare drafts, VIKI also offers a message for the chat you have open/),
  ).toBeTruthy();
  const select = screen.getByLabelText("Daily limit: message drafts") as HTMLSelectElement;
  expect(select.value).toBe("3");
  fireEvent.change(select, { target: { value: "5" } });
  expect(patch).toHaveBeenCalledWith({ "decision.limit.message_draft": 5 });
});

test("the message-draft limit reads its own stored key", () => {
  mountDoRow({ values: { ...DEFAULT_VALUES, "decision.limit.message_draft": 7 } });
  expect((screen.getByLabelText("Daily limit: message drafts") as HTMLSelectElement).value).toBe(
    "7",
  );
});

test("the project-plan and second-look controls read their own stored keys", () => {
  mountDoRow({
    values: {
      ...DEFAULT_VALUES,
      "decision.grant.project_plan": "ask",
      "decision.limit.project_plan": 7,
      "decision.grant.second_look": "off",
      "decision.limit.second_look": 5,
    },
  });
  const plan = within(screen.getByRole("group", { name: "Plan next steps for my projects" }));
  expect((plan.getByRole("radio", { name: "Ask first" }) as HTMLInputElement).checked).toBe(true);
  expect(
    (screen.getByLabelText("Daily limit: project plans VIKI starts") as HTMLSelectElement).value,
  ).toBe("7");
  const look = within(screen.getByRole("group", { name: "Take a second look at my work" }));
  expect((look.getByRole("radio", { name: "Off" }) as HTMLInputElement).checked).toBe(true);
  expect(
    (screen.getByLabelText("Daily limit: second looks VIKI starts") as HTMLSelectElement).value,
  ).toBe("5");
});

test("an allowed project-plan grant alone makes the brake hold show Holding", () => {
  mountDoRow({
    page: heldPage(null),
    values: {
      ...DEFAULT_VALUES,
      "decision.grant.prepare": "ask",
      "decision.grant.team": "ask",
      "decision.grant.look": "ask",
      "decision.grant.second_look": "ask",
      "decision.grant.project_plan": "allowed",
    },
    nowMs: NOON,
  });
  expect(screen.getByRole("status").textContent).toBe("Holding");
});

test("the hand-off switch is off by default and patches decision.team.handoffs", () => {
  const { patch } = mountDoRow();
  const toggle = screen.getByRole("switch", { name: "Let seats hand work to each other" });
  expect(toggle.getAttribute("aria-checked")).toBe("false");
  fireEvent.click(toggle);
  expect(patch).toHaveBeenCalledWith({ "decision.team.handoffs": true });
  expect(
    screen.getByText(
      "On: a card one Bot writes for another counts like your own card. Off: that card gets Start instead.",
    ),
  ).toBeTruthy();
});

test("the Bot controls write nothing when not writable", () => {
  const { patch } = mountDoRow({ writable: false });
  fireEvent.click(
    within(screen.getByRole("group", { name: "Start Bots' work" })).getByRole("radio", {
      name: "Allowed",
    }),
  );
  fireEvent.click(
    within(screen.getByRole("group", { name: "Look on my behalf" })).getByRole("radio", {
      name: "Off",
    }),
  );
  fireEvent.change(screen.getByLabelText("Daily limit: looks VIKI starts"), {
    target: { value: "7" },
  });
  fireEvent.click(
    within(screen.getByRole("group", { name: "Take a second look at my work" })).getByRole(
      "radio",
      { name: "Off" },
    ),
  );
  fireEvent.change(screen.getByLabelText("Daily limit: second looks VIKI starts"), {
    target: { value: "5" },
  });
  fireEvent.click(
    within(screen.getByRole("group", { name: "Plan next steps for my projects" })).getByRole(
      "radio",
      { name: "Off" },
    ),
  );
  fireEvent.change(screen.getByLabelText("Daily limit: project plans VIKI starts"), {
    target: { value: "5" },
  });
  fireEvent.click(screen.getByRole("switch", { name: "Let seats hand work to each other" }));
  expect(patch).not.toHaveBeenCalled();
});

test("running Bot runs show under Running chats and count toward Stop all", () => {
  const page = basePage();
  page.missions.running = [];
  page.missions.team.running = 2;
  mountDoRow({ page });
  expect(screen.getByText("Bot runs VIKI started, still running: 2")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Stop all" })).toBeTruthy();
});

test("an allowed Bot grant alone makes the brake hold show Holding", () => {
  mountDoRow({
    page: heldPage(null),
    values: {
      ...DEFAULT_VALUES,
      "decision.grant.prepare": "ask",
      "decision.grant.team": "allowed",
    },
    nowMs: NOON,
  });
  expect(screen.getByRole("status").textContent).toBe("Holding");
});

test("choosing Ask first for Prepare drafts patches decision.grant.prepare", () => {
  const { patch } = mountDoRow();
  const group = within(screen.getByRole("group", { name: "Prepare drafts" }));
  fireEvent.click(group.getByRole("radio", { name: "Ask first" }));
  expect(patch).toHaveBeenCalledWith({ "decision.grant.prepare": "ask" });
});

test("changing the drafts limit patches decision.limit.drafts with a number, not a string", () => {
  const { patch } = mountDoRow();
  fireEvent.change(screen.getByLabelText("Daily limit: drafts"), { target: { value: "7" } });
  expect(patch).toHaveBeenCalledWith({ "decision.limit.drafts": 7 });
});

test("changing the vault changes limit patches decision.limit.changes with a number, not a string", () => {
  const { patch } = mountDoRow();
  fireEvent.change(screen.getByLabelText("Daily limit: vault changes"), {
    target: { value: "7" },
  });
  expect(patch).toHaveBeenCalledWith({ "decision.limit.changes": 7 });
});

test("with no missions running, DoRow shows no Running chats group and no Stop all", () => {
  const page = basePage();
  page.missions.running = [];
  page.missions.team.running = 0;
  mountDoRow({ page });
  expect(screen.queryByText("No chats running right now.")).toBeNull();
  expect(screen.queryByText("Running chats")).toBeNull();
  expect(screen.queryByRole("button", { name: "Stop all" })).toBeNull();
});

test("a running mission shows its title and how long it has run, with Open and Stop but no Stop all", () => {
  const page = basePage();
  page.missions.team.running = 0;
  mountDoRow({ page, nowMs: Date.parse("2026-10-21T13:15:30.000Z") });
  expect(screen.getByText("Draft a plan for the trip budget — running 5m 0s")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Open" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Stop all" })).toBeNull();
});

test("Open calls onOpenChat with the running mission's own session id", () => {
  const { onOpenChat } = mountDoRow();
  fireEvent.click(screen.getByRole("button", { name: "Open" }));
  expect(onOpenChat).toHaveBeenCalledWith("fda2250d-d245-468a-9d7c-5fa673b14d3a");
});

test("Stop calls api.stopMission with the running mission's id, then onRefresh", async () => {
  const { api, onRefresh } = mountDoRow();
  fireEvent.click(screen.getByRole("button", { name: "Stop" }));
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
  expect(api.requests.at(-1)).toEqual({
    method: "stopMission",
    args: ["9273d0ed-e3e0-4ac6-8319-80e6c5e10247"],
  });
});

test("Stop all shows once two or more missions run, and stops every one of them via onRefresh", async () => {
  const page = basePage();
  page.missions.running = [...page.missions.running, SECOND_MISSION];
  const { api, onRefresh } = mountDoRow({ page });
  expect(screen.getAllByRole("button", { name: "Stop" })).toHaveLength(2);
  fireEvent.click(screen.getByRole("button", { name: "Stop all" }));
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
  expect(api.requests.at(-1)).toEqual({ method: "stopAllMissions", args: [] });
});

test("a failed Stop shows one plain error line", async () => {
  const api = createFakeDecisionLoopApi();
  api.fail("stopMission", "unknown_item");
  mountDoRow({ api });
  fireEvent.click(screen.getByRole("button", { name: "Stop" }));
  await waitFor(() =>
    expect(screen.getByRole("alert").textContent).toBe("Could not stop that chat. Try again."),
  );
});

test("DoRow disables its own controls while a stop request is in flight, then recovers", async () => {
  const api = createFakeDecisionLoopApi();
  let settle: (mission: MissionView) => void = () => undefined;
  api.stopMission = vi.fn(
    () =>
      new Promise<MissionView>((resolve) => {
        settle = resolve;
      }),
  );
  const { onRefresh } = mountDoRow({ api });
  const stopButton = screen.getByRole("button", { name: "Stop" }) as HTMLButtonElement;
  const openButton = screen.getByRole("button", { name: "Open" }) as HTMLButtonElement;
  fireEvent.click(stopButton);
  expect(stopButton.disabled).toBe(true);
  expect(openButton.disabled).toBe(true);
  await act(async () => {
    settle({
      mission_id: "9273d0ed-e3e0-4ac6-8319-80e6c5e10247",
      session_id: "fda2250d-d245-468a-9d7c-5fa673b14d3a",
      work: "prepare",
      state: "stopped",
      title: "Draft a plan for the trip budget",
      started_at: "2026-10-21T13:10:30.000Z",
      finished_at: "2026-10-21T13:16:00.000Z",
      reason: "owner",
    });
  });
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
  expect(stopButton.disabled).toBe(false);
});

test("DoRow blocks the grant write when not writable, even though jsdom still fires a disabled radio's onChange", () => {
  const { patch } = mountDoRow({ writable: false });
  const group = within(screen.getByRole("group", { name: "Prepare drafts" }));
  fireEvent.click(group.getByRole("radio", { name: "Ask first" }));
  expect(patch).not.toHaveBeenCalled();
});

test("DoRow disables its selects and mission buttons when not writable", () => {
  const { patch } = mountDoRow({ writable: false });
  for (const name of [
    "Daily limit: second looks VIKI starts",
    "Daily limit: project plans VIKI starts",
    "Daily limit: message drafts",
  ]) {
    const select = screen.getByLabelText(name) as HTMLSelectElement;
    expect(select.disabled).toBe(true);
    const other = Array.from(select.options).find((o) => o.value !== select.value);
    fireEvent.change(select, { target: { value: other?.value } });
  }
  expect(patch).not.toHaveBeenCalled();
  expect((screen.getByLabelText("Daily limit: drafts") as HTMLSelectElement).disabled).toBe(true);
  expect((screen.getByLabelText("Daily limit: chats") as HTMLSelectElement).disabled).toBe(true);
  expect((screen.getByLabelText("Daily limit: vault changes") as HTMLSelectElement).disabled).toBe(
    true,
  );
  expect((screen.getByRole("button", { name: "Open" }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: "Stop" }) as HTMLButtonElement).disabled).toBe(true);
});

const NOON = new Date(2026, 9, 21, 9, 0).getTime();

function heldPage(until: string | null): PageView {
  const page = basePage();
  page.missions.running = [];
  page.missions.team.running = 0;
  page.missions.hold = { until };
  return page;
}

function brakeRadio(label: string): HTMLInputElement {
  const group = within(
    screen.getByRole("group", { name: "Pause automatic starts at Claude plan usage" }),
  );
  return group.getByRole("radio", { name: label }) as HTMLInputElement;
}

test("the plan brake shows 80% by default and when the key is missing", () => {
  mountDoRow();
  expect(brakeRadio("80%").checked).toBe(true);
  cleanup();
  mountDoRow({ values: { ...DEFAULT_VALUES, "decision.plan_brake": undefined } });
  expect(brakeRadio("80%").checked).toBe(true);
});

test("choosing 95% patches decision.plan_brake", () => {
  const { patch } = mountDoRow();
  fireEvent.click(brakeRadio("95%"));
  expect(patch).toHaveBeenCalledWith({ "decision.plan_brake": "95" });
});

test("the plan brake writes nothing when not writable", () => {
  const { patch } = mountDoRow({ writable: false });
  fireEvent.click(brakeRadio("95%"));
  expect(patch).not.toHaveBeenCalled();
});

test("a held brake with an end says when, and the chip reads Holding", () => {
  mountDoRow({
    page: heldPage(new Date(2026, 9, 21, 14, 0).toISOString()),
    nowMs: NOON,
  });
  expect(screen.getByText("Holding VIKI's starts until 14:00.")).toBeTruthy();
  expect(screen.getByRole("status").textContent).toBe("Holding");
});

test("a held brake with no end says Claude Code must report again", () => {
  mountDoRow({ page: heldPage(null), nowMs: NOON });
  expect(
    screen.getByText("Holding VIKI's starts until Claude Code reports your plan use again."),
  ).toBeTruthy();
  expect(screen.getByRole("status").textContent).toBe("Holding");
});

test("a held brake shows no line and no Holding chip while both grants only ask", () => {
  mountDoRow({
    page: heldPage(new Date(2026, 9, 21, 14, 0).toISOString()),
    values: {
      ...DEFAULT_VALUES,
      "decision.grant.prepare": "ask",
      "decision.grant.run": "ask",
      "decision.grant.look": "ask",
      "decision.grant.second_look": "ask",
      "decision.grant.project_plan": "ask",
      "decision.grant.inbox_triage": "ask",
    },
    nowMs: NOON,
  });
  expect(screen.queryByText(/^Holding VIKI's starts/)).toBeNull();
  expect(screen.getByRole("status").textContent).toBe("On");
});

// --- DoRow groups, legend and notes ------------------------------------------------------------

function groupTitles(): string[] {
  return Array.from(document.querySelectorAll("#do-body h4")).map((h4) => h4.textContent ?? "");
}

/** The grant line a legend (or, for a line without a grant, its plain label) names. */
function lineOf(label: string): HTMLElement {
  const names = Array.from(
    document.querySelectorAll("#do-body legend, #do-body .dloop-grant-label"),
  );
  const name = names.find((el) => el.textContent === label);
  if (!name) throw new Error(`no grant line named ${label}`);
  return name.closest(".dloop-grant") as HTMLElement;
}

function notesOn(label: string): string[] {
  return Array.from(lineOf(label).querySelectorAll(".dloop-grant-note")).map(
    (note) => note.textContent ?? "",
  );
}

function linesUnder(title: string): string[] {
  const group = Array.from(document.querySelectorAll("#do-body .dloop-subgroup")).find(
    (el) => el.querySelector("h4")?.textContent === title,
  );
  if (!group) throw new Error(`no group titled ${title}`);
  return Array.from(group.querySelectorAll("legend, .dloop-grant-label")).map(
    (el) => el.textContent ?? "",
  );
}

const TASKS_NEED_MODEL = "Plans and work chats for due tasks need the Decision model.";
const LINKS_NEED_MODEL = "Note links and Superseded notes proposals need the Decision model.";
const TASKS_OFF =
  "Tasks due is off in What VIKI watches, so there are no plans or work chats for due tasks.";
const OVERDUE_OFF =
  "Tasks due is off in What VIKI watches, so VIKI plans nothing for overdue tasks.";
const INBOX_OFF = "Inbox is off in What VIKI watches, so VIKI won't suggest destinations.";
const IMPROVE_OFF = "Self-improvement is off in What VIKI watches, so VIKI proposes no upkeep.";
const UPKEEP_BOTH_OFF =
  "Vault changes and Tasks due are off in What VIKI watches, so VIKI proposes no upkeep.";
const UPKEEP_LINKS_OFF =
  "Vault changes is off in What VIKI watches, so VIKI proposes no Note links or Superseded notes.";
const UPKEEP_TASKS_OFF =
  "Tasks due is off in What VIKI watches, so VIKI proposes no archiving of ticked tasks.";

test("DoRow's description says to choose per kind of work, and its legend names the three choices", () => {
  mountDoRow();
  expect(document.getElementById("do-description")?.textContent).toBe(
    "For each kind of work, choose whether VIKI may start it.",
  );
  const legend = document.querySelector("#do-body dl.dloop-legend") as HTMLElement;
  expect(Array.from(legend.querySelectorAll("dt")).map((dt) => dt.textContent)).toEqual([
    "Off",
    "Ask first",
    "Allowed",
  ]);
  expect(Array.from(legend.querySelectorAll("dd")).map((dd) => dd.textContent)).toEqual([
    "VIKI doesn't start it.",
    "VIKI offers it as an action on its item in the launcher.",
    "VIKI starts it at its next check, within the daily limit.",
  ]);
  expect(
    screen.getByText(
      "Message drafts wait for you to select them, under Ask first and Allowed alike.",
    ),
  ).toBeTruthy();
});

test("the body groups its lines under four headings, in order", () => {
  mountDoRow();
  expect(groupTitles()).toEqual([
    "Running chats",
    "Prepared for your review",
    "Work that can make changes",
    "Safety",
  ]);
  expect(linesUnder("Prepared for your review")).toEqual([
    "Prepare drafts",
    "Look on my behalf",
    "Take a second look at my work",
    "Plan next steps for my projects",
    "Suggest destinations for my inbox",
    "Vault upkeep proposals",
  ]);
  expect(linesUnder("Work that can make changes")).toEqual([
    "Start chats to do work",
    "Start Bots' work",
    "Run my saved checks",
  ]);
  expect(linesUnder("Safety")).toEqual(["Pause automatic starts at Claude plan usage"]);
  expect(screen.getByText("Nothing changes until you approve.")).toBeTruthy();
  expect(
    screen.getByText(
      "Chats use your usual Claude Code permissions, Bot runs use their routine's settings, and saved checks are your own commands.",
    ),
  ).toBeTruthy();
});

test("no Daily limits disclosure remains", () => {
  mountDoRow();
  expect(screen.queryByText("Daily limits", { selector: "summary" })).toBeNull();
  expect(document.querySelector(".dloop-limits")).toBeNull();
});

test("Running chats leads the body while a mission runs, with its list and Stop all", () => {
  const page = basePage();
  page.missions.team.running = 0;
  page.missions.running = [...page.missions.running, SECOND_MISSION];
  mountDoRow({ page });
  expect(groupTitles()[0]).toBe("Running chats");
  const group = within(document.querySelector("#do-body .dloop-subgroup") as HTMLElement);
  expect(group.getAllByRole("button", { name: "Open" })).toHaveLength(2);
  expect(group.getByRole("button", { name: "Stop all" })).toBeTruthy();
});

test("Running chats shows for a Bot run alone, without an empty list", () => {
  const page = basePage();
  page.missions.running = [];
  page.missions.team.running = 1;
  mountDoRow({ page });
  expect(groupTitles()[0]).toBe("Running chats");
  expect(document.querySelector("#do-body ul")).toBeNull();
  expect(screen.getByText("Bot runs VIKI started, still running: 1")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Stop all" })).toBeNull();
});

test("Running chats shows for a saved-check run alone", () => {
  const page = basePage();
  page.missions.running = [];
  page.missions.team.running = 0;
  page.missions.checks = { today: 1, limit: 3, running: 1 };
  mountDoRow({ page });
  expect(groupTitles()[0]).toBe("Running chats");
  expect(screen.getByText("Saved-check runs VIKI started, still running: 1")).toBeTruthy();
});

test("Running chats is the only group missing while nothing runs", () => {
  const page = basePage();
  page.missions.running = [];
  page.missions.team.running = 0;
  mountDoRow({ page });
  expect(groupTitles()).toEqual([
    "Prepared for your review",
    "Work that can make changes",
    "Safety",
  ]);
});

test("each line carries its own hint, and More about this only where a paragraph follows", () => {
  mountDoRow();
  const hints: Record<string, string> = {
    "Prepare drafts":
      "Plans for due tasks that wait for your approval, and message drafts for the chat you have open.",
    "Look on my behalf": "Reviews code changes left uncommitted for a day, in a plan-mode chat.",
    "Take a second look at my work":
      "After a long or corrected reply that edited notes, looks for one flaw, better approach or missing piece.",
    "Plan next steps for my projects":
      "For a project idle two weeks or tasks three days overdue, proposes next steps.",
    "Suggest destinations for my inbox":
      "A read-only run suggests where each capture could go. It sends your captures' opening text to Anthropic.",
    "Vault upkeep proposals":
      "Link notes, mark replaced notes and archive ticked tasks, in an ordinary vault. Each waits in Approvals. 0 turns them off.",
    "Start chats to do work": "A chat that works on a due task.",
    "Start Bots' work":
      "Starts a Bot's tested routine when one of your Kanban cards waits for that Bot.",
  };
  for (const [label, hint] of Object.entries(hints)) {
    expect(within(lineOf(label)).getByText(hint)).toBeTruthy();
  }
  const withMore = [
    "Prepare drafts",
    "Look on my behalf",
    "Take a second look at my work",
    "Plan next steps for my projects",
    "Suggest destinations for my inbox",
    "Start Bots' work",
    "Run my saved checks",
  ];
  for (const label of [...Object.keys(hints), "Run my saved checks"]) {
    const summaries = within(lineOf(label)).queryAllByText("More about this");
    expect(summaries).toHaveLength(withMore.includes(label) ? 1 : 0);
  }
});

test("each line carries exactly its own daily limits, in order", () => {
  mountDoRow();
  const limits: Record<string, string[]> = {
    "Prepare drafts": ["Daily limit: drafts", "Daily limit: message drafts"],
    "Look on my behalf": ["Daily limit: looks VIKI starts"],
    "Take a second look at my work": ["Daily limit: second looks VIKI starts"],
    "Plan next steps for my projects": ["Daily limit: project plans VIKI starts"],
    "Suggest destinations for my inbox": ["Daily limit: inbox suggestion items"],
    "Vault upkeep proposals": ["Daily limit: vault changes"],
    "Start chats to do work": ["Daily limit: chats"],
    "Start Bots' work": ["Daily limit: Bot runs VIKI starts"],
    "Run my saved checks": ["Daily limit: new commits VIKI checks"],
  };
  for (const [label, expected] of Object.entries(limits)) {
    const names = Array.from(lineOf(label).querySelectorAll("select")).map(
      (select) => select.labels?.[0]?.textContent ?? "",
    );
    expect(names).toEqual(expected);
  }
});

test("changing the chats limit patches decision.limit.chats with a number", () => {
  const { patch } = mountDoRow();
  fireEvent.change(screen.getByLabelText("Daily limit: chats"), { target: { value: "6" } });
  expect(patch).toHaveBeenCalledWith({ "decision.limit.chats": 6 });
});

test("the hand-off switch sits under Start Bots' work, and the brake hint closes the Safety group", () => {
  mountDoRow();
  const bots = within(lineOf("Start Bots' work"));
  expect(bots.getByRole("switch", { name: "Let seats hand work to each other" })).toBeTruthy();
  expect(
    screen.getByText(
      "When your Claude plan usage reaches this share, or your plan limit is reached, VIKI holds the Claude Code work it would start by itself. You can still start it yourself.",
    ),
  ).toBeTruthy();
});

test("a rules-only model state adds the Decision-model notes, and ready adds none", () => {
  for (const state of ["rules", "no_key", "key_rejected", "capped", "unreachable"] as const) {
    const page = basePage();
    page.model.state = state;
    mountDoRow({ page });
    expect(notesOn("Prepare drafts")).toEqual([TASKS_NEED_MODEL]);
    expect(notesOn("Start chats to do work")).toEqual([TASKS_NEED_MODEL]);
    expect(notesOn("Vault upkeep proposals")).toEqual([LINKS_NEED_MODEL]);
    expect(notesOn("Look on my behalf")).toEqual([]);
    expect(notesOn("Plan next steps for my projects")).toEqual([]);
    cleanup();
  }
  const page = basePage();
  page.model.state = "ready";
  mountDoRow({ page });
  for (const label of linesUnder("Prepared for your review")) expect(notesOn(label)).toEqual([]);
  for (const label of linesUnder("Work that can make changes")) expect(notesOn(label)).toEqual([]);
});

test("Tasks due off adds its notes to drafts, chats and project plans, after a model note", () => {
  const page = basePage();
  page.model.state = "ready";
  mountDoRow({ page, values: { ...DEFAULT_VALUES, "decision.watch.tasks": false } });
  expect(notesOn("Prepare drafts")).toEqual([TASKS_OFF]);
  expect(notesOn("Start chats to do work")).toEqual([TASKS_OFF]);
  expect(notesOn("Plan next steps for my projects")).toEqual([OVERDUE_OFF]);
  expect(notesOn("Suggest destinations for my inbox")).toEqual([]);
  cleanup();
  page.model.state = "rules";
  mountDoRow({ page, values: { ...DEFAULT_VALUES, "decision.watch.tasks": false } });
  expect(notesOn("Prepare drafts")).toEqual([TASKS_NEED_MODEL, TASKS_OFF]);
});

test("Inbox off adds its note to the inbox line alone", () => {
  const page = basePage();
  page.model.state = "ready";
  mountDoRow({ page, values: { ...DEFAULT_VALUES, "decision.watch.inbox": false } });
  expect(notesOn("Suggest destinations for my inbox")).toEqual([INBOX_OFF]);
  expect(notesOn("Prepare drafts")).toEqual([]);
  expect(notesOn("Vault upkeep proposals")).toEqual([]);
});

test("upkeep shows exactly the watch note that matches the switches that are off", () => {
  const cases: [Record<string, boolean>, string[]][] = [
    [{ improve: false }, [IMPROVE_OFF]],
    [{ improve: false, vault_changes: false, tasks: false }, [IMPROVE_OFF]],
    [{ vault_changes: false, tasks: false }, [UPKEEP_BOTH_OFF]],
    [{ vault_changes: false }, [UPKEEP_LINKS_OFF]],
    [{ tasks: false }, [UPKEEP_TASKS_OFF]],
    [{}, []],
  ];
  for (const [off, expected] of cases) {
    const page = basePage();
    page.model.state = "ready";
    const watch = Object.fromEntries(
      Object.entries(off).map(([name, on]) => [`decision.watch.${name}`, on]),
    );
    mountDoRow({ page, values: { ...DEFAULT_VALUES, ...watch } });
    expect(notesOn("Vault upkeep proposals")).toEqual(expected);
    cleanup();
  }
});

test("upkeep has no radio, only its vault-changes limit", () => {
  const { patch } = mountDoRow();
  const upkeep = within(lineOf("Vault upkeep proposals"));
  expect(upkeep.queryAllByRole("radio")).toHaveLength(0);
  fireEvent.change(upkeep.getByLabelText("Daily limit: vault changes"), { target: { value: "2" } });
  expect(patch).toHaveBeenCalledWith({ "decision.limit.changes": 2 });
  expect(upkeep.getByText("Today: 1 of 3 vault changes")).toBeTruthy();
});

// --- HowRow ----------------------------------------------------------------------------------

test("HowRow starts closed, kept mounted, and opens when its own disclosure is clicked", () => {
  render(
    <HowRow
      page={basePage()}
      values={DEFAULT_VALUES}
      writable={true}
      patch={vi.fn()}
      api={createFakeDecisionLoopApi()}
      onRefresh={vi.fn()}
      onOpenData={vi.fn()}
    />,
  );
  expect(document.getElementById("how-body")?.hidden).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "How VIKI is doing" }));
  expect(document.getElementById("how-body")?.hidden).toBe(false);
});

test("HowRow's 30-day counts use the same words as the feedback buttons", () => {
  mountHowRow();
  const section = within(screen.getByRole("region", { name: "How VIKI is doing" }));
  expect(section.getByText("Last 30 days")).toBeTruthy();
  expect(
    section.getByText(
      "128 shown · 41 useful · 9 not useful · 17 not now · 3 stopped · 2 should have told me · 11 right to hold",
    ),
  ).toBeTruthy();
});

test("HowRow's collapsed row says what it is, what it asks and what the answers change", () => {
  mountHowRow();
  const section = within(screen.getByRole("region", { name: "How VIKI is doing" }));
  // The example's weekly check has one unanswered row and one already labelled.
  expect(section.getByRole("status").textContent).toBe("1 to check");
  expect(
    screen.getByText(
      "A record of what VIKI showed you and what it held back. Once a week, tell VIKI whether holding things back was right; your answers help tune when it speaks up.",
    ),
  ).toBeTruthy();
});

test("HowRow's status says nothing is needed once the weekly check has no unanswered row", () => {
  const page = basePage();
  page.held_back = page.held_back.filter((row) => row.feedback !== null);
  mountHowRow({ page });
  const section = within(screen.getByRole("region", { name: "How VIKI is doing" }));
  expect(section.getByRole("status").textContent).toBe("Nothing to do now");
  expect(section.getByText("All answered. No action needed until next week.")).toBeTruthy();
});

test("each part of the opened row says whether it needs the user", () => {
  mountHowRow();
  const section = within(screen.getByRole("region", { name: "How VIKI is doing" }));
  expect(
    section.getByText(
      /^Only two parts here need you: the Weekly check, and Report a missing suggestion/,
    ),
  ).toBeTruthy();
  expect(section.getByText(/^Read only\. How many suggestions VIKI showed you/)).toBeTruthy();
  expect(section.getByText(/^Needs you, once a week\./)).toBeTruthy();
  expect(section.getByText(/While Learning is on, it uses your answers/)).toBeTruthy();
  expect(section.getByText("Something VIKI missed")).toBeTruthy();
  expect(section.getByText(/^Optional, any time\./)).toBeTruthy();
});

test("HowRow opens the Data answers and decisions headings", () => {
  const { onOpenData } = mountHowRow();
  fireEvent.click(screen.getByRole("button", { name: "See how you answered, day by day" }));
  fireEvent.click(screen.getByRole("button", { name: "See every decision in Data" }));
  expect(onOpenData.mock.calls).toEqual([["answers"], ["decisions"]]);
});

test("the weekly check carries its heading and says what each answer means", () => {
  mountHowRow();
  const section = within(screen.getByRole("region", { name: "How VIKI is doing" }));
  expect(section.getByText("Weekly check")).toBeTruthy();
  expect(
    section.getByText(
      /Up to 10 things VIKI kept quiet about last week, picked at random\. Choose Right to hold if staying quiet was right, or Should have told me if you wanted to see it\./,
    ),
  ).toBeTruthy();
});

test("a held-back row names the candidate and why, with no dash when why is empty", () => {
  const page = basePage();
  page.held_back = [
    {
      episode_id: "empty-why",
      created_at: "2026-10-20T00:00:00.000Z",
      candidate: "approvals",
      level: "silent",
      reason: "approvals_quiet",
      why: "",
      feedback: null,
    },
  ];
  mountHowRow({ page });
  expect(screen.getByText("Approvals")).toBeTruthy();
});

test("a held-back row that already carries feedback shows its label instead of buttons", () => {
  mountHowRow();
  const row = screen.getByText("Inbox — a few notes in the inbox").closest("li") as HTMLElement;
  expect(within(row).getByText("Your answer: Right to hold")).toBeTruthy();
  expect(within(row).queryByRole("button")).toBeNull();
});

test("an unlabelled held-back row offers Right to hold, which feeds back and refreshes", async () => {
  const { api, onRefresh } = mountHowRow();
  const row = screen.getByText("Routines — a routine runs soon").closest("li") as HTMLElement;
  fireEvent.click(within(row).getByRole("button", { name: "Right to hold" }));
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
  expect(api.requests.at(-1)).toEqual({
    method: "feedback",
    args: [
      {
        item_key: null,
        episode_id: "3c6418ec-4ce4-42a8-b6f3-a74dde91a050",
        value: "right_to_hold",
        reason: null,
        source: "page",
      },
    ],
  });
});

test("Should have told me sends the missed feedback value for that same episode", async () => {
  const { api, onRefresh } = mountHowRow();
  const row = screen.getByText("Routines — a routine runs soon").closest("li") as HTMLElement;
  fireEvent.click(within(row).getByRole("button", { name: "Should have told me" }));
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
  const call = api.requests.at(-1) as { method: string; args: unknown[] };
  expect(call.method).toBe("feedback");
  expect((call.args[0] as { value: string; episode_id: string }).value).toBe("missed");
  expect((call.args[0] as { value: string; episode_id: string }).episode_id).toBe(
    "3c6418ec-4ce4-42a8-b6f3-a74dde91a050",
  );
});

test("HowRow says so when there is nothing to check from last week", () => {
  const page = basePage();
  page.held_back = [];
  mountHowRow({ page });
  expect(screen.getByText("Nothing to check from last week. No action needed.")).toBeTruthy();
  expect(screen.getByRole("status").textContent).toBe("Nothing to do now");
});

test("Report a missing suggestion reports unwatched from the page, thanks the user, and does not refresh the page", async () => {
  const { api, onRefresh } = mountHowRow();
  fireEvent.click(screen.getByRole("button", { name: "Report a missing suggestion" }));
  await waitFor(() => expect(screen.getByText("Recorded.")).toBeTruthy());
  expect(api.requests.at(-1)).toEqual({ method: "report", args: ["unwatched", "page"] });
  expect(onRefresh).not.toHaveBeenCalled();
});

test("a failed feedback call shows one plain error line", async () => {
  const api = createFakeDecisionLoopApi();
  api.fail("feedback", "unknown_item");
  mountHowRow({ api });
  const row = screen.getByText("Routines — a routine runs soon").closest("li") as HTMLElement;
  fireEvent.click(within(row).getByRole("button", { name: "Right to hold" }));
  await waitFor(() =>
    expect(screen.getByRole("alert").textContent).toBe("Could not save your feedback. Try again."),
  );
});

test("HowRow disables its feedback and report controls when not writable", () => {
  mountHowRow({ writable: false });
  const row = screen.getByText("Routines — a routine runs soon").closest("li") as HTMLElement;
  expect(
    (within(row).getByRole("button", { name: "Right to hold" }) as HTMLButtonElement).disabled,
  ).toBe(true);
  expect(
    (screen.getByRole("button", { name: "Report a missing suggestion" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
});

// --- Run my saved checks -----------------------------------------------------------------------

const CHECKS_HINT =
  "When a trusted code folder stays for ten minutes at a new commit that its saved checks have not run on, a pulled commit included, VIKI runs the saved checks you have run in that folder before, as Run in its Checks tab does, and reports whether they passed. Ask first offers Run checks in VIKI's launcher instead. Each check is your own command: it runs with your account's rights and can change files and use the network.";

function pageWithChecks(checks: PageView["missions"]["checks"]): PageView {
  const page = basePage();
  page.missions.checks = checks;
  return page;
}

test("with checks null (off Windows) no saved-check row, limit or running line shows", () => {
  mountDoRow({ page: pageWithChecks(null) });
  expect(screen.queryByRole("group", { name: "Run my saved checks" })).toBeNull();
  expect(screen.queryByText(CHECKS_HINT)).toBeNull();
  expect(screen.queryByLabelText("Daily limit: new commits VIKI checks")).toBeNull();
  expect(screen.queryByText(/Saved-check runs VIKI started/)).toBeNull();
});

test("Run my saved checks defaults to Ask first, patches decision.grant.check and hints", () => {
  const { patch } = mountDoRow({ page: pageWithChecks({ today: 1, limit: 3, running: 0 }) });
  const group = within(screen.getByRole("group", { name: "Run my saved checks" }));
  expect((group.getByRole("radio", { name: "Ask first" }) as HTMLInputElement).checked).toBe(true);
  fireEvent.click(group.getByRole("radio", { name: "Allowed" }));
  expect(patch).toHaveBeenCalledWith({ "decision.grant.check": "allowed" });
  expect(screen.getByText(CHECKS_HINT)).toBeTruthy();
});

test("choosing Off for Run my saved checks patches decision.grant.check", () => {
  const { patch } = mountDoRow();
  fireEvent.click(
    within(screen.getByRole("group", { name: "Run my saved checks" })).getByRole("radio", {
      name: "Off",
    }),
  );
  expect(patch).toHaveBeenCalledWith({ "decision.grant.check": "off" });
});

test("the saved-check limit defaults to 3, patches decision.limit.check and shows today's use", () => {
  const { patch } = mountDoRow({ page: pageWithChecks({ today: 2, limit: 3, running: 0 }) });
  expect(screen.getByText("Today: 2 of 3 new commits")).toBeTruthy();
  const select = screen.getByLabelText("Daily limit: new commits VIKI checks") as HTMLSelectElement;
  expect(select.value).toBe("3");
  fireEvent.change(select, { target: { value: "5" } });
  expect(patch).toHaveBeenCalledWith({ "decision.limit.check": 5 });
});

test("the saved-check controls read their own stored keys", () => {
  mountDoRow({
    values: {
      ...DEFAULT_VALUES,
      "decision.grant.check": "allowed",
      "decision.limit.check": 7,
      "decision.limit.message_draft": 2,
    },
  });
  const group = within(screen.getByRole("group", { name: "Run my saved checks" }));
  expect((group.getByRole("radio", { name: "Allowed" }) as HTMLInputElement).checked).toBe(true);
  expect(
    (screen.getByLabelText("Daily limit: new commits VIKI checks") as HTMLSelectElement).value,
  ).toBe("7");
});

test("the saved-check controls write nothing when not writable", () => {
  const { patch } = mountDoRow({ writable: false });
  const radio = within(screen.getByRole("group", { name: "Run my saved checks" })).getByRole(
    "radio",
    { name: "Allowed" },
  ) as HTMLInputElement;
  const select = screen.getByLabelText("Daily limit: new commits VIKI checks") as HTMLSelectElement;
  expect(radio.disabled).toBe(true);
  expect(select.disabled).toBe(true);
  fireEvent.click(radio);
  fireEvent.change(select, { target: { value: "7" } });
  expect(patch).not.toHaveBeenCalled();
});

test("running saved-check runs show under Running chats and count toward Stop all", () => {
  const page = pageWithChecks({ today: 1, limit: 3, running: 2 });
  page.missions.running = [];
  page.missions.team.running = 0;
  mountDoRow({ page });
  expect(screen.getByText("Saved-check runs VIKI started, still running: 2")).toBeTruthy();
  expect(screen.getByRole("status").textContent).toBe("2 running");
  expect(screen.getByRole("button", { name: "Stop all" })).toBeTruthy();
});

test("no saved-check running line while none run", () => {
  mountDoRow({ page: pageWithChecks({ today: 1, limit: 3, running: 0 }) });
  expect(screen.queryByText(/Saved-check runs VIKI started/)).toBeNull();
});

test("an allowed check grant alone does not make the brake hold show Holding", () => {
  const page = heldPage(null);
  page.missions.checks = { today: 0, limit: 3, running: 0 };
  mountDoRow({
    page,
    values: {
      ...DEFAULT_VALUES,
      "decision.grant.prepare": "ask",
      "decision.grant.team": "ask",
      "decision.grant.look": "ask",
      "decision.grant.second_look": "ask",
      "decision.grant.project_plan": "ask",
      "decision.grant.check": "allowed",
      "decision.grant.inbox_triage": "ask",
    },
    nowMs: NOON,
  });
  expect(screen.getByRole("status").textContent).toBe("On");
});

// --- Suggest destinations for my inbox -----------------------------------------------------------

const INBOX_HINT =
  "When your inbox has more than three captures, or one older than three days, and some have no suggested destination, VIKI asks Claude Code once, with read-only tools, where each could go, ten minutes after the newest one changed, so suggestions wait in Triage inbox. It sends your captures' opening text to Anthropic, as Ask VIKI to suggest does, and denies any permission that run asks for. Ask first offers Suggest destinations in VIKI's launcher instead.";

const INBOX_LIMIT_LABEL = "Daily limit: inbox suggestion items";

test("Suggest destinations for my inbox defaults to Allowed, patches its key and hints", () => {
  const { patch } = mountDoRow();
  const group = within(screen.getByRole("group", { name: "Suggest destinations for my inbox" }));
  expect((group.getByRole("radio", { name: "Allowed" }) as HTMLInputElement).checked).toBe(true);
  expect(screen.getByText(INBOX_HINT)).toBeTruthy();
  fireEvent.click(group.getByRole("radio", { name: "Ask first" }));
  expect(patch).toHaveBeenCalledWith({ "decision.grant.inbox_triage": "ask" });
});

test("the inbox limit defaults to 3, patches its key and shows today's use", () => {
  const page = basePage();
  page.missions.inbox = { today: 2, limit: 3 };
  const { patch } = mountDoRow({ page });
  const select = screen.getByLabelText(INBOX_LIMIT_LABEL) as HTMLSelectElement;
  expect(select.value).toBe("3");
  expect(screen.getByText("Today: 2 of 3 inbox suggestion items")).toBeTruthy();
  fireEvent.change(select, { target: { value: "5" } });
  expect(patch).toHaveBeenCalledWith({ "decision.limit.inbox_triage": 5 });
});

test("the inbox controls read their own stored keys", () => {
  mountDoRow({
    values: {
      ...DEFAULT_VALUES,
      "decision.grant.inbox_triage": "off",
      "decision.limit.inbox_triage": 7,
    },
  });
  const group = within(screen.getByRole("group", { name: "Suggest destinations for my inbox" }));
  expect((group.getByRole("radio", { name: "Off" }) as HTMLInputElement).checked).toBe(true);
  expect((screen.getByLabelText(INBOX_LIMIT_LABEL) as HTMLSelectElement).value).toBe("7");
});

test("the inbox controls write nothing when not writable", () => {
  const { patch } = mountDoRow({ writable: false });
  const radio = within(
    screen.getByRole("group", { name: "Suggest destinations for my inbox" }),
  ).getByRole("radio", { name: "Ask first" }) as HTMLInputElement;
  const select = screen.getByLabelText(INBOX_LIMIT_LABEL) as HTMLSelectElement;
  expect(radio.disabled).toBe(true);
  expect(select.disabled).toBe(true);
  fireEvent.click(radio);
  fireEvent.change(select, { target: { value: "5" } });
  expect(patch).not.toHaveBeenCalled();
});

test("an allowed inbox grant alone makes the brake hold show Holding", () => {
  mountDoRow({
    page: { ...heldPage(null), missions: { ...heldPage(null).missions, running: [] } },
    values: {
      ...DEFAULT_VALUES,
      "decision.grant.prepare": "ask",
      "decision.grant.team": "ask",
      "decision.grant.look": "ask",
      "decision.grant.second_look": "ask",
      "decision.grant.project_plan": "ask",
      "decision.grant.check": "ask",
      "decision.grant.inbox_triage": "allowed",
    },
    nowMs: NOON,
  });
  expect(screen.getByRole("status").textContent).toBe("Holding");
});
