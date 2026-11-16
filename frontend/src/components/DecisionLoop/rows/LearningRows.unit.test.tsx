import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import {
  createFakeDecisionLoopApi,
  decisionLoopExamples,
  type FakeDecisionLoopApi,
} from "../../../clients/decisionLoop/fake";
import type { PageView } from "../../../clients/decisionLoop/types";
import { LearningRow } from "./LearningRows";

afterEach(cleanup);

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
 * `getByText` correctly treat as absent from the page); open it, as a user click would, so a
 * row's own tests can reach its body (matches `DoingRows.unit.test.tsx`'s own `open`). */
function open(name: string): void {
  fireEvent.click(screen.getByRole("button", { name }));
}

function mountLearningRow(overrides: RowOverrides & { nowMs?: number } = {}) {
  const api = overrides.api ?? createFakeDecisionLoopApi();
  const patch = vi.fn(async (_changes: Record<string, unknown>) => ({}));
  const onRefresh = vi.fn();
  render(
    <LearningRow
      page={overrides.page ?? basePage()}
      values={overrides.values ?? {}}
      writable={overrides.writable ?? true}
      patch={patch}
      api={api}
      onRefresh={onRefresh}
      nowMs={overrides.nowMs ?? Date.now()}
    />,
  );
  open("Learning");
  for (const summary of document.querySelectorAll<HTMLElement>(
    "#learning-body details > summary",
  )) {
    fireEvent.click(summary);
  }
  return { api, patch, onRefresh };
}

test("LearningRow starts closed, kept mounted, and opens when its own disclosure is clicked", () => {
  render(
    <LearningRow
      page={basePage()}
      values={{}}
      writable={true}
      patch={vi.fn()}
      api={createFakeDecisionLoopApi()}
      onRefresh={vi.fn()}
    />,
  );
  expect(document.getElementById("learning-body")?.hidden).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Learning" }));
  expect(document.getElementById("learning-body")?.hidden).toBe(false);
  expect(document.querySelector<HTMLDetailsElement>("#learning-body details")?.open).toBe(false);
  expect(
    [...document.querySelectorAll<HTMLDetailsElement>("#learning-body details")].every(
      (details) => !details.open,
    ),
  ).toBe(true);
});

// --- status ------------------------------------------------------------------------------------

test("status reads Off while learning is disabled", () => {
  const page = basePage();
  page.learning = { ...page.learning, enabled: false };
  mountLearningRow({ page });
  expect(screen.getByRole("status").textContent).toBe("Off");
});

test("status counts toward the label floor while under it", () => {
  const page = basePage();
  page.learning = { ...page.learning, enabled: true, labels: 5, min_labels: 20 };
  mountLearningRow({ page });
  expect(screen.getByRole("status").textContent).toBe("Learning (5 of 20 feedback results)");
});

test("status counts feedback above the floor and keeps the learning requirements visible", () => {
  const page = basePage();
  page.learning = { ...page.learning, enabled: true, labels: 64, min_labels: 20 };
  mountLearningRow({ page });
  expect(screen.getByRole("status").textContent).toBe("Learning from 64 feedback results");
  expect(
    screen.getByText(
      /requires feedback on at least 20 Decision model decisions, including at least 3 positive and 3 negative results/,
    ),
  ).toBeTruthy();
});

// T (seen-in-time) and C (attention cost) adapt from the first decisions, with their
// own shrinkage; only the topic and question weights wait for the label floor. A fixture with
// Learning on, under the floor and one reach level already moved pins both halves: the status
// does not claim nothing has started, and the body explains what still waits.
test("under the floor: status is honest that reach already moved, and names what still waits", () => {
  const page = basePage();
  const learning = page.learning;
  page.learning = {
    ...learning,
    enabled: true,
    labels: 5,
    min_labels: 20,
    bias: { start: learning.bias.start, now: learning.bias.start },
    weights: learning.weights.map((weight) => ({ ...weight, now: weight.start })),
    topics: learning.topics.map((topic) => ({ ...topic, now: topic.start })),
    reach: learning.reach.map((reach) =>
      reach.level === "nudge"
        ? { ...reach, start: 0.7, now: 0.77 }
        : { ...reach, now: reach.start },
    ),
    costs: learning.costs.map((cost) => ({ ...cost, now: cost.start })),
  };
  mountLearningRow({ page });
  expect(screen.getByRole("status").textContent).toBe("Learning (5 of 20 feedback results)");
  expect(
    screen.getByText(
      "Learning which questions and topics matter to you requires feedback on at least 20 Decision model decisions, including at least 3 positive and 3 negative results. Until then, VIKI adjusts only timing and how prominently suggestions appear.",
    ),
  ).toBeTruthy();
  expect(screen.getByText("Seen in time at Nudge — higher (0.70 → 0.77)")).toBeTruthy();
});

test("learning is Off while its requirements remain available as help", () => {
  const page = basePage();
  page.learning = { ...page.learning, enabled: false, labels: 5, min_labels: 20 };
  mountLearningRow({ page });
  expect(screen.getByRole("status").textContent).toBe("Off");
  expect(
    screen.getByText(/requires feedback on at least 20 Decision model decisions/),
  ).toBeTruthy();
});

// --- body copy and the since date ---------------------------------------------------------------

test("explains what it tunes and what stays a user limit, and names the start date", () => {
  mountLearningRow();
  expect(screen.getByText(/VIKI learns from Useful, Not useful, Stop these/)).toBeTruthy();
  expect(
    screen.getByText(
      /Your quiet hours, nudges per hour, alerts, muted topics and choices about what VIKI may do/,
    ),
  ).toBeTruthy();
  expect(screen.getByText("Learning since Sep 1, 2026")).toBeTruthy();
});

test("omits the since line once learning has never started", () => {
  const page = basePage();
  page.learning = { ...page.learning, since: null };
  mountLearningRow({ page });
  expect(screen.queryByText(/^Learning since/)).toBeNull();
});

// --- the switch ----------------------------------------------------------------------------------

test("the switch defaults on and saves decision.learning off", async () => {
  const { patch } = mountLearningRow();
  const toggle = screen.getByRole("switch", { name: "Learning" });
  expect(toggle.getAttribute("aria-checked")).toBe("true");
  fireEvent.click(toggle);
  await waitFor(() => expect(patch).toHaveBeenCalledWith({ "decision.learning": false }));
});

test("the switch reflects a saved decision.learning value of false", () => {
  mountLearningRow({ values: { "decision.learning": false } });
  expect(screen.getByRole("switch", { name: "Learning" }).getAttribute("aria-checked")).toBe(
    "false",
  );
});

test("a failed save shows Couldn't save that change.", async () => {
  const patch = vi.fn().mockRejectedValue(new Error("nope"));
  render(
    <LearningRow
      page={basePage()}
      values={{}}
      writable={true}
      patch={patch}
      api={createFakeDecisionLoopApi()}
      onRefresh={vi.fn()}
    />,
  );
  open("Learning");
  fireEvent.click(screen.getByRole("switch", { name: "Learning" }));
  await waitFor(() =>
    expect(screen.getByRole("alert").textContent).toBe("Couldn't save that change."),
  );
});

// --- What changed, the numbers table, calibration and questions -------------------------------

test("renders What changed from the learning snapshot", () => {
  mountLearningRow();
  expect(screen.getByText("About Tasks due — higher (0.00 → 0.46)")).toBeTruthy();
});

test("Show all numbers reveals the table and Hide all numbers removes it, following aria-expanded", () => {
  mountLearningRow();
  const toggle = () => screen.getByRole("button", { name: /all numbers/ });
  expect(toggle().getAttribute("aria-expanded")).toBe("false");
  expect(toggle().getAttribute("aria-controls")).toBe("learning-numbers");
  expect(screen.queryByRole("table")).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Show all numbers" }));
  expect(toggle().getAttribute("aria-expanded")).toBe("true");
  expect(within(screen.getByRole("table")).getByText("Baseline")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "Hide all numbers" }));
  expect(toggle().getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByRole("table")).toBeNull();
});

test("shows how well it matches you and each question's own rates", () => {
  mountLearningRow();
  expect(screen.getByRole("heading", { name: "How well it matches you" })).toBeTruthy();
  expect(screen.getByText(/Prediction error 0.18 across 64 decisions with feedback/)).toBeTruthy();
  expect(screen.getByRole("heading", { name: "Each question" })).toBeTruthy();
  expect(
    screen.getByText(
      "Linked to a goal? When the model answered Yes: 68% counted useful (41 decisions with feedback) · When the model answered No: 21% counted useful (19 decisions with feedback)",
    ),
  ).toBeTruthy();
});

// --- Second looks --------------------------------------------------------------------------------

function pausedLooks(page: PageView): void {
  const looks = page.learning.second_looks;
  page.learning = {
    ...page.learning,
    second_looks: {
      triggers: looks.triggers.map((trigger, i) =>
        i === 0
          ? {
              ...trigger,
              paused: true,
              reasons: { out_of_date: 0, already_knew: 2, wrong_topic: 0, bad_timing: 1 },
            }
          : trigger,
      ),
      kinds: looks.kinds.map((entry) =>
        entry.kind === "approach" ? { ...entry, rejected_in_a_row: 5, paused: true } : entry,
      ),
    },
  };
}

test("Second looks shows each trigger's counts, its Not useful reasons and a kind's streak", () => {
  mountLearningRow();
  expect(screen.getByRole("heading", { name: "Second looks" })).toBeTruthy();
  expect(
    screen.getByText(/^VIKI continues each kind of second look while you approve at least 1 in 4/),
  ).toBeTruthy();
  expect(
    screen.getByText(
      /A look that finds nothing does not count\. Keep planning counts as turned down only if that chat then ends without an approval\./,
    ),
  ).toBeTruthy();
  expect(
    screen.getByText("After a long task: 4 started, 1 approved, 2 turned down, 1 found nothing"),
  ).toBeTruthy();
  expect(
    screen.getByText("After a correction: 0 started, 0 approved, 0 turned down, 0 found nothing"),
  ).toBeTruthy();
  expect(
    within(
      screen.getByText("After a long task: 4 started, 1 approved, 2 turned down, 1 found nothing")
        .parentElement as HTMLElement,
    ).getByText("Not useful: Already knew 1"),
  ).toBeTruthy();
  expect(screen.getByText("Better approaches: turned down 2 in a row")).toBeTruthy();
  expect(screen.queryByText(/^Flaws:/)).toBeNull();
  expect(screen.queryByText(/Paused:/)).toBeNull();
  expect(screen.queryByRole("button", { name: /^Try again/ })).toBeNull();
  expect(screen.queryByRole("button", { name: /^Show these again/ })).toBeNull();
});

test("a paused trigger and kind say so and offer Try again and Show these again", () => {
  const page = basePage();
  pausedLooks(page);
  mountLearningRow({ page });
  expect(screen.getByText("Not useful: Already knew 2, Bad timing 1")).toBeTruthy();
  expect(screen.getByText("Paused: fewer than 1 in 4 approved.")).toBeTruthy();
  expect(
    screen.getByText("Better approaches: turned down 5 in a row - suggestions paused"),
  ).toBeTruthy();
});

test("Try again posts try_again for its trigger, then refreshes", async () => {
  const page = basePage();
  pausedLooks(page);
  const { api, onRefresh } = mountLearningRow({ page });
  fireEvent.click(screen.getByRole("button", { name: "Try again: After a long task" }));
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
  expect(api.requests).toEqual([
    { method: "preference", args: [{ name: "try_again", candidate: "long_task_look" }] },
  ]);
});

test("Show these again posts raise_again for its kind, then refreshes", async () => {
  const page = basePage();
  pausedLooks(page);
  const { api, onRefresh } = mountLearningRow({ page });
  fireEvent.click(screen.getByRole("button", { name: "Show these again: Better approaches" }));
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
  expect(api.requests).toEqual([
    { method: "preference", args: [{ name: "raise_again", kind: "approach" }] },
  ]);
});

test("each paused trigger and kind has its own Try again and Show these again", async () => {
  const page = basePage();
  pausedLooks(page);
  const looks = page.learning.second_looks;
  page.learning = {
    ...page.learning,
    second_looks: {
      triggers: looks.triggers.map((trigger) => ({ ...trigger, paused: true })),
      kinds: looks.kinds.map((entry) =>
        entry.kind === "missing" ? { ...entry, rejected_in_a_row: 4, paused: true } : entry,
      ),
    },
  };
  const { api, onRefresh } = mountLearningRow({ page });
  const names = screen
    .getAllByRole("button", { name: /^(Try again|Show these again)/ })
    .map((button) => button.getAttribute("aria-label"));
  expect(names).toEqual([
    "Try again: After a long task",
    "Try again: After a correction",
    "Show these again: Better approaches",
    "Show these again: Missing pieces",
  ]);
  fireEvent.click(screen.getByRole("button", { name: "Try again: After a correction" }));
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
  fireEvent.click(screen.getByRole("button", { name: "Show these again: Missing pieces" }));
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(2));
  expect(api.requests).toEqual([
    { method: "preference", args: [{ name: "try_again", candidate: "correction_look" }] },
    { method: "preference", args: [{ name: "raise_again", kind: "missing" }] },
  ]);
});

test("a failed Try again shows Couldn't save that change. and does not refresh", async () => {
  const page = basePage();
  pausedLooks(page);
  const api = createFakeDecisionLoopApi();
  api.fail("preference", "unavailable");
  const { onRefresh } = mountLearningRow({ page, api });
  fireEvent.click(screen.getByRole("button", { name: "Try again: After a long task" }));
  await waitFor(() =>
    expect(screen.getByRole("alert").textContent).toBe("Couldn't save that change."),
  );
  expect(onRefresh).not.toHaveBeenCalled();
});

test("Try again and Show these again are disabled when not writable", () => {
  const page = basePage();
  pausedLooks(page);
  mountLearningRow({ page, writable: false });
  for (const name of ["Try again: After a long task", "Show these again: Better approaches"]) {
    expect((screen.getByRole("button", { name }) as HTMLButtonElement).disabled).toBe(true);
  }
});

// --- Project plans -------------------------------------------------------------------------------

function pausedPlans(page: PageView, both = false): void {
  page.learning = {
    ...page.learning,
    project_plans: page.learning.project_plans.map((plan, i) =>
      i === 0 || both ? { ...plan, paused: true } : plan,
    ),
  };
}

test("Project plans shows each type's counts and Not useful reasons, no Paused line", () => {
  mountLearningRow();
  expect(screen.getByRole("heading", { name: "Project plans" })).toBeTruthy();
  expect(
    screen.getByText(
      "VIKI keeps making a kind of project plan only while you approve at least 1 in 4, counted once 10 are answered, and a plan that finds nothing to propose counts as not approved. Keep planning counts as turned down only if that chat then ends without an approval. Your permission setting, daily limit and quiet hours stay as you set them.",
    ),
  ).toBeTruthy();
  expect(
    screen.getByText(
      "For a stalled project: 3 started, 1 approved, 1 turned down, 1 found nothing",
    ),
  ).toBeTruthy();
  expect(
    screen.getByText("For overdue tasks: 0 started, 0 approved, 0 turned down, 0 found nothing"),
  ).toBeTruthy();
  expect(screen.getByText("Not useful: Wrong topic 1")).toBeTruthy();
  expect(screen.queryByText(/Paused:/)).toBeNull();
  expect(screen.queryByRole("button", { name: /^Try again/ })).toBeNull();
});

test("a paused project plan type says so and offers Try again, which posts then refreshes", async () => {
  const page = basePage();
  pausedPlans(page);
  const { api, onRefresh } = mountLearningRow({ page });
  expect(screen.getByText("Paused: fewer than 1 in 4 approved.")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Try again: For a stalled project" }));
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
  expect(api.requests).toEqual([
    { method: "preference", args: [{ name: "try_again", candidate: "stalled_project" }] },
  ]);
});

test("two paused project plan types have distinct Try again names, each posting its own", async () => {
  const page = basePage();
  pausedPlans(page, true);
  const { api, onRefresh } = mountLearningRow({ page });
  const names = screen
    .getAllByRole("button", { name: /^Try again: For / })
    .map((button) => button.getAttribute("aria-label"));
  expect(names).toEqual(["Try again: For a stalled project", "Try again: For overdue tasks"]);
  fireEvent.click(screen.getByRole("button", { name: "Try again: For overdue tasks" }));
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
  expect(api.requests).toEqual([
    { method: "preference", args: [{ name: "try_again", candidate: "overdue_tasks" }] },
  ]);
});

test("a failed project plan Try again shows Couldn't save that change. and does not refresh", async () => {
  const page = basePage();
  pausedPlans(page);
  const api = createFakeDecisionLoopApi();
  api.fail("preference", "unavailable");
  const { onRefresh } = mountLearningRow({ page, api });
  fireEvent.click(screen.getByRole("button", { name: "Try again: For a stalled project" }));
  await waitFor(() =>
    expect(screen.getByRole("alert").textContent).toBe("Couldn't save that change."),
  );
  expect(onRefresh).not.toHaveBeenCalled();
});

test("project plan Try again is disabled when not writable", () => {
  const page = basePage();
  pausedPlans(page, true);
  mountLearningRow({ page, writable: false });
  for (const name of ["Try again: For a stalled project", "Try again: For overdue tasks"]) {
    expect((screen.getByRole("button", { name }) as HTMLButtonElement).disabled).toBe(true);
  }
});

// --- Message drafts ------------------------------------------------------------------------------

function pausedDrafts(page: PageView, both = false): void {
  page.learning = {
    ...page.learning,
    message_drafts: page.learning.message_drafts.map((draft, i) =>
      i === 0 || both ? { ...draft, paused: true } : draft,
    ),
  };
}

test("Message drafts shows each type's counts and Not useful reasons, no Paused line", () => {
  mountLearningRow();
  expect(screen.getByRole("heading", { name: "Message drafts" })).toBeTruthy();
  expect(
    screen.getByText(
      "VIKI keeps offering a kind of message draft only while you use at least 1 in 4, counted once 10 are used, turned down or closed unused; selecting a draft's button counts as using it. Your permission setting, daily limit and quiet hours stay as you set them.",
    ),
  ).toBeTruthy();
  expect(
    screen.getByText("For a nearly full chat: 4 offered, 1 used, 1 turned down, 2 closed unused"),
  ).toBeTruthy();
  expect(
    screen.getByText("For fetched pages: 0 offered, 0 used, 0 turned down, 0 closed unused"),
  ).toBeTruthy();
  expect(
    within(
      screen.getByText("For a nearly full chat: 4 offered, 1 used, 1 turned down, 2 closed unused")
        .parentElement as HTMLElement,
    ).getByText("Not useful: Already knew 1"),
  ).toBeTruthy();
  expect(screen.queryByText(/Paused:/)).toBeNull();
  expect(screen.queryByRole("button", { name: /^Try again/ })).toBeNull();
});

test("a paused message draft type says so and offers Try again, which posts then refreshes", async () => {
  const page = basePage();
  pausedDrafts(page);
  const { api, onRefresh } = mountLearningRow({ page });
  expect(screen.getByText("Paused: fewer than 1 in 4 used.")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Try again: For a nearly full chat" }));
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
  expect(api.requests).toEqual([
    { method: "preference", args: [{ name: "try_again", candidate: "context_handoff" }] },
  ]);
});

test("two paused message draft types have distinct Try again names, each posting its own", async () => {
  const page = basePage();
  pausedDrafts(page, true);
  const { api, onRefresh } = mountLearningRow({ page });
  const names = screen
    .getAllByRole("button", { name: /^Try again: For (a nearly|fetched)/ })
    .map((button) => button.getAttribute("aria-label"));
  expect(names).toEqual(["Try again: For a nearly full chat", "Try again: For fetched pages"]);
  fireEvent.click(screen.getByRole("button", { name: "Try again: For fetched pages" }));
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
  expect(api.requests).toEqual([
    { method: "preference", args: [{ name: "try_again", candidate: "source_note" }] },
  ]);
});

test("message draft Try again is disabled when not writable", () => {
  const page = basePage();
  pausedDrafts(page, true);
  mountLearningRow({ page, writable: false });
  for (const name of ["Try again: For a nearly full chat", "Try again: For fetched pages"]) {
    expect((screen.getByRole("button", { name }) as HTMLButtonElement).disabled).toBe(true);
  }
});

// --- Saved checks --------------------------------------------------------------------------------

const COMMIT_LINE = "On a new commit: 3 offered, 1 used, 1 turned down";

function pausedChecks(page: PageView): void {
  page.learning = {
    ...page.learning,
    saved_checks: page.learning.saved_checks.map((check) => ({ ...check, paused: true })),
  };
}

test("Saved checks is hidden while checks is null", () => {
  const page = basePage();
  page.missions.checks = null;
  mountLearningRow({ page });
  expect(screen.queryByRole("heading", { name: "Saved checks" })).toBeNull();
  expect(screen.queryByText(COMMIT_LINE)).toBeNull();
});

test("Saved checks shows its copy, the counts without a fourth, and the Not useful reasons", () => {
  mountLearningRow();
  const headings = screen.getAllByRole("heading").map((heading) => heading.textContent);
  expect(headings.indexOf("Saved checks")).toBe(headings.indexOf("Inbox suggestions") + 1);
  expect(
    screen.getByText(
      "VIKI keeps offering or running your saved checks on new commits only while you use at least 1 in 4 of them, counted once 10 are used or turned down; selecting Run checks or Useful counts as using one, and an item you leave unanswered counts toward neither. Your permission setting, daily limit and quiet hours stay as you set them.",
    ),
  ).toBeTruthy();
  const line = screen.getByText(COMMIT_LINE);
  expect(
    within(line.parentElement as HTMLElement).getByText("Not useful: Already knew 1"),
  ).toBeTruthy();
  expect(screen.queryByText(/Paused:/)).toBeNull();
});

test("a paused saved-check type says so and Try again posts commit_checks then refreshes", async () => {
  const page = basePage();
  pausedChecks(page);
  const { api, onRefresh } = mountLearningRow({ page });
  expect(screen.getByText("Paused: fewer than 1 in 4 used.")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Try again: On a new commit" }));
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
  expect(api.requests).toEqual([
    { method: "preference", args: [{ name: "try_again", candidate: "commit_checks" }] },
  ]);
});

test("saved-check Try again is disabled when not writable", () => {
  const page = basePage();
  pausedChecks(page);
  mountLearningRow({ page, writable: false });
  expect(
    (screen.getByRole("button", { name: "Try again: On a new commit" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
});

// --- Inbox suggestions ---------------------------------------------------------------------------

const TRIAGE_LINE = "Before you triage: 4 offered, 1 used, 1 turned down, 1 found nothing";

test("Inbox suggestions follows Message drafts with its copy, counts, reasons and no pause", () => {
  mountLearningRow();
  const headings = screen.getAllByRole("heading").map((heading) => heading.textContent);
  expect(headings.indexOf("Inbox suggestions")).toBe(headings.indexOf("Message drafts") + 1);
  expect(
    screen.getByText(
      "VIKI keeps running inbox suggestions only while you use at least 1 in 4, counted once 10 are used, turned down or found nothing; selecting Triage inbox or Suggest destinations on the item, or Useful, counts as using one, and an item you leave unanswered counts toward neither. Your permission setting, daily limit and quiet hours stay as you set them.",
    ),
  ).toBeTruthy();
  const line = screen.getByText(TRIAGE_LINE);
  expect(
    within(line.parentElement as HTMLElement).getByText("Not useful: Already knew 1"),
  ).toBeTruthy();
  expect(screen.queryByText(/Paused:/)).toBeNull();
});

test("a paused inbox-suggestion type says so and Try again posts inbox_triage then refreshes", async () => {
  const page = basePage();
  page.learning = {
    ...page.learning,
    inbox_triage: page.learning.inbox_triage.map((entry) => ({ ...entry, paused: true })),
  };
  const { api, onRefresh } = mountLearningRow({ page });
  expect(screen.getByText("Paused: fewer than 1 in 4 used.")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Try again: Before you triage" }));
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
  expect(api.requests).toEqual([
    { method: "preference", args: [{ name: "try_again", candidate: "inbox_triage" }] },
  ]);
});

// --- no model version comparison in this row ----------------------------------------------------

test("has no Compare with another model version group", () => {
  mountLearningRow();
  expect(screen.queryByRole("group", { name: "Compare with another model version" })).toBeNull();
  expect(screen.queryByText("Compare model versions (advanced)")).toBeNull();
});

// --- Reset what VIKI learned -------------------------------------------------------------------

test("Reset asks to confirm, then forgets what VIKI learned and refreshes", async () => {
  const { api, onRefresh } = mountLearningRow();
  fireEvent.click(screen.getByRole("button", { name: "Reset what VIKI learned" }));
  expect(
    screen.getByText(
      "Reset VIKI's learned values to their starting values? Your recorded decisions and feedback stay.",
    ),
  ).toBeTruthy();
  expect(api.requests).toEqual([]);
  fireEvent.click(screen.getByRole("button", { name: "Reset" }));
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
  expect(api.requests).toEqual([{ method: "preference", args: [{ name: "reset_learning" }] }]);
});

test("Reset's confirm Cancel resets nothing", () => {
  const { api } = mountLearningRow();
  fireEvent.click(screen.getByRole("button", { name: "Reset what VIKI learned" }));
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.getByRole("button", { name: "Reset what VIKI learned" })).toBeTruthy();
  expect(api.requests).toEqual([]);
});

test("a failed Reset shows Couldn't save that change.", async () => {
  const api = createFakeDecisionLoopApi();
  api.fail("preference", "unavailable");
  mountLearningRow({ api });
  fireEvent.click(screen.getByRole("button", { name: "Reset what VIKI learned" }));
  fireEvent.click(screen.getByRole("button", { name: "Reset" }));
  await waitFor(() =>
    expect(screen.getByRole("alert").textContent).toBe("Couldn't save that change."),
  );
});

// --- not writable ----------------------------------------------------------------------------

test("disables the switch and Reset trigger when not writable", () => {
  mountLearningRow({ writable: false });
  expect(screen.getByRole("switch", { name: "Learning" }).getAttribute("aria-disabled")).toBe(
    "true",
  );
  expect(
    (screen.getByRole("button", { name: "Reset what VIKI learned" }) as HTMLButtonElement).disabled,
  ).toBe(true);
});

// --- a fresh, unfitted snapshot ----------------------------------------------------------------

test("renders cleanly with a fresh, unfitted snapshot", () => {
  const page = basePage();
  const learning = page.learning;
  page.learning = {
    ...learning,
    enabled: true,
    labels: 0,
    fitted_at: null,
    since: null,
    bias: { start: learning.bias.start, now: learning.bias.start },
    weights: learning.weights.map((weight) => ({ ...weight, now: weight.start })),
    topics: learning.topics.map((topic) => ({ ...topic, now: topic.start, support: 0 })),
    reach: learning.reach.map((reach) => ({ ...reach, now: reach.start, support: 0 })),
    costs: learning.costs.map((cost) => ({ ...cost, now: cost.start, support: 0 })),
    calibration: {
      n: 0,
      brier: null,
      bins: learning.calibration.bins.map((bin) => ({
        ...bin,
        n: 0,
        mean_p: null,
        rate: null,
      })),
    },
    questions: learning.questions.map((question) => ({
      ...question,
      yes_n: 0,
      yes_rate: null,
      no_n: 0,
      no_rate: null,
    })),
    shadow: {
      setting: "off",
      decisions: 0,
      agree: 0,
      kind_agree: 0,
      labels: 0,
      brier: null,
      live_brier: null,
    },
  };
  mountLearningRow({ page });
  expect(screen.getByRole("status").textContent).toBe("Learning (0 of 20 feedback results)");
  expect(screen.getByText("No large changes to VIKI's learned values yet.")).toBeTruthy();
  expect(screen.getByText("No feedback results yet.")).toBeTruthy();
});
