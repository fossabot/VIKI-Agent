import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import {
  createFakeDecisionLoopApi,
  decisionLoopExamples,
  type FakeDecisionLoopApi,
} from "../../../clients/decisionLoop/fake";
import {
  type DecisionCandidate,
  MODEL_STATES,
  type PageView,
} from "../../../clients/decisionLoop/types";
import { RULES_ONLY_MODEL_STATES } from "../wording";
import { ReachRow, WatchRow } from "./ReachRows";

afterEach(cleanup);

const click = async (element: HTMLElement) => act(async () => void fireEvent.click(element));
const change = async (element: HTMLElement, value: string) =>
  act(async () => void fireEvent.change(element, { target: { value } }));

function basePage(overrides: Partial<PageView> = {}): PageView {
  return { ...decisionLoopExamples().page, ...overrides };
}

interface Harness {
  page: PageView;
  values: Record<string, unknown>;
  writable: boolean;
  patch: (changes: Record<string, unknown>) => Promise<unknown>;
  api: FakeDecisionLoopApi;
  onRefresh: () => void;
}

function harness(overrides: Partial<Harness> = {}): Harness {
  return {
    page: basePage(),
    values: {},
    writable: true,
    patch: vi.fn(async () => undefined),
    api: createFakeDecisionLoopApi(),
    onRefresh: vi.fn(),
    ...overrides,
  };
}

/** Every control lives in the row's collapsible body; open it the way a user would. */
function openRow(name: string): void {
  fireEvent.click(screen.getByRole("button", { name }));
}

// --- WatchRow ---

describe("WatchRow", () => {
  test("the status chip counts how many of the ten are on, visible before opening", () => {
    const h = harness({ values: { "decision.watch.inbox": false } });
    render(<WatchRow {...h} />);
    expect(screen.getByText("9 of 10 on")).toBeTruthy();
  });

  test("renders all ten watches, on by default when a key is missing from values", () => {
    const h = harness();
    render(<WatchRow {...h} />);
    openRow("What VIKI watches");
    for (const label of [
      "Tasks due",
      "Inbox",
      "Vault changes",
      "Approvals",
      "Chats and runs",
      "Routines",
      "Self-improvement",
      "Kanban",
      "Vault health",
      "Messaging",
    ]) {
      expect(screen.getByRole("switch", { name: label }).getAttribute("aria-checked")).toBe("true");
    }
  });

  test("the Self-improvement switch is on by default and toggling it patches decision.watch.improve", async () => {
    const h = harness();
    render(<WatchRow {...h} />);
    openRow("What VIKI watches");
    expect(
      screen.getByRole("switch", { name: "Self-improvement" }).getAttribute("aria-checked"),
    ).toBe("true");
    await click(screen.getByRole("switch", { name: "Self-improvement" }));
    expect(h.patch).toHaveBeenCalledWith({ "decision.watch.improve": false });
  });

  test("the Kanban switch is on by default and toggling it patches decision.watch.kanban", async () => {
    const h = harness();
    render(<WatchRow {...h} />);
    openRow("What VIKI watches");
    expect(screen.getByRole("switch", { name: "Kanban" }).getAttribute("aria-checked")).toBe(
      "true",
    );
    await click(screen.getByRole("switch", { name: "Kanban" }));
    expect(h.patch).toHaveBeenCalledWith({ "decision.watch.kanban": false });
  });

  test.each([
    ["Vault health", "decision.watch.vault_health"],
    ["Messaging", "decision.watch.messaging"],
  ])("the %s switch is on by default and toggling it patches %s", async (label, key) => {
    const h = harness();
    render(<WatchRow {...h} />);
    openRow("What VIKI watches");
    expect(screen.getByRole("switch", { name: label }).getAttribute("aria-checked")).toBe("true");
    await click(screen.getByRole("switch", { name: label }));
    expect(h.patch).toHaveBeenCalledWith({ [key]: false });
  });

  test("an explicit false reads as off", () => {
    const h = harness({ values: { "decision.watch.inbox": false } });
    render(<WatchRow {...h} />);
    openRow("What VIKI watches");
    expect(screen.getByRole("switch", { name: "Inbox" }).getAttribute("aria-checked")).toBe(
      "false",
    );
  });

  test("toggling a switch patches its own key and does not refresh (not mirrored in page)", async () => {
    const h = harness();
    render(<WatchRow {...h} />);
    openRow("What VIKI watches");
    await click(screen.getByRole("switch", { name: "Approvals" }));
    expect(h.patch).toHaveBeenCalledWith({ "decision.watch.approvals": false });
    expect(h.onRefresh).not.toHaveBeenCalled();
  });

  test("not writable disables the cadence choice and every switch, with no hint text of its own", () => {
    const h = harness({ writable: false });
    render(<WatchRow {...h} />);
    openRow("What VIKI watches");
    expect(screen.getByRole("switch", { name: "Tasks due" }).getAttribute("aria-disabled")).toBe(
      "true",
    );
    const group = within(screen.getByRole("group", { name: "How often VIKI checks" }));
    fireEvent.click(group.getByRole("radio", { name: "Every 15 minutes" }));
    expect(h.patch).not.toHaveBeenCalled();
    expect(screen.queryByText("Settings aren't ready to change right now.")).toBeNull();
  });

  test("the cadence choice defaults to About every minute and its hint describes the group", () => {
    const h = harness();
    render(<WatchRow {...h} />);
    openRow("What VIKI watches");
    const group = screen.getByRole("group", { name: "How often VIKI checks" });
    expect(
      (within(group).getByRole("radio", { name: "About every minute" }) as HTMLInputElement)
        .checked,
    ).toBe(true);
    expect(within(group).getByRole("radio", { name: "Every 5 minutes" })).toBeTruthy();
    expect(group.getAttribute("aria-describedby")).toBe("watch-cadence-hint");
    expect(document.getElementById("watch-cadence-hint")?.textContent).toBe(
      "VIKI also checks when you switch chats or notes, open the launcher or come back to the window.",
    );
  });

  test("the cadence choice reflects values and patches decision.cadence without a refresh", async () => {
    const h = harness({ values: { "decision.cadence": "5m" } });
    render(<WatchRow {...h} />);
    openRow("What VIKI watches");
    const group = within(screen.getByRole("group", { name: "How often VIKI checks" }));
    expect(
      (group.getByRole("radio", { name: "Every 5 minutes" }) as HTMLInputElement).checked,
    ).toBe(true);
    await click(group.getByRole("radio", { name: "Every 15 minutes" }));
    expect(h.patch).toHaveBeenCalledWith({ "decision.cadence": "15m" });
    await click(group.getByRole("radio", { name: "About every minute" }));
    expect(h.patch).toHaveBeenCalledWith({ "decision.cadence": "change" });
    expect(h.onRefresh).not.toHaveBeenCalled();
  });

  test("the cadence choice shows Every 15 minutes when values holds 15m", () => {
    const h = harness({ values: { "decision.cadence": "15m" } });
    render(<WatchRow {...h} />);
    openRow("What VIKI watches");
    const group = within(screen.getByRole("group", { name: "How often VIKI checks" }));
    expect(
      (group.getByRole("radio", { name: "Every 15 minutes" }) as HTMLInputElement).checked,
    ).toBe(true);
  });

  test("the rules note follows the cadence hint and comes before the first subhead", () => {
    const h = harness();
    render(<WatchRow {...h} />);
    openRow("What VIKI watches");
    const hint = document.getElementById("watch-cadence-hint");
    const note = screen.getByText(/^These switches also work with simple rules\./);
    const firstSubhead = screen.getAllByRole("heading", { level: 4 })[0];
    expect(note.className).toBe("dloop-hint");
    expect(hint?.compareDocumentPosition(note)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(note.compareDocumentPosition(firstSubhead as HTMLElement)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });

  test("three subheads hold their switches in order", () => {
    const h = harness();
    render(<WatchRow {...h} />);
    openRow("What VIKI watches");
    const groups = Array.from(document.querySelectorAll("#watch-body .dloop-subgroup")).map(
      (group) => ({
        title: group.querySelector("h4.dloop-subhead")?.textContent,
        switches: within(group as HTMLElement)
          .getAllByRole("switch")
          .map((control) => control.getAttribute("aria-labelledby")),
      }),
    );
    expect(groups).toEqual([
      {
        title: "In your vault",
        switches: [
          "watch-tasks",
          "watch-inbox",
          "watch-vault-changes",
          "watch-kanban",
          "watch-vault-health",
        ],
      },
      {
        title: "In the app",
        switches: ["watch-approvals", "watch-chats", "watch-routines", "watch-messaging"],
      },
      { title: "Improvements", switches: ["watch-improve"] },
    ]);
  });

  test("Tasks due, Inbox and Vault changes carry a note after their hint, in describedBy", () => {
    const h = harness();
    render(<WatchRow {...h} />);
    openRow("What VIKI watches");
    for (const [id, text] of [
      ["watch-tasks", "Plans and work chats for due tasks need the Decision model."],
      ["watch-inbox", "Off also stops inbox suggestions."],
      ["watch-vault-changes", "Note links and Superseded notes proposals"],
    ] as const) {
      const control = document.querySelector(`[aria-labelledby="${id}"]`);
      expect(control?.getAttribute("aria-describedby")).toBe(`${id}-hint ${id}-dep`);
      expect(document.getElementById(`${id}-dep`)?.textContent).toContain(text);
      expect(document.getElementById(`${id}-dep`)?.className).toBe("dloop-hint");
    }
    const others = document.querySelector('[aria-labelledby="watch-approvals"]');
    expect(others?.getAttribute("aria-describedby")).toBe("watch-approvals-hint");
    expect(document.getElementById("watch-approvals-dep")).toBeNull();
  });

  test("Self-improvement's description names what needs the Decision model; Kanban has no note", () => {
    const h = harness();
    render(<WatchRow {...h} />);
    openRow("What VIKI watches");
    expect(document.getElementById("watch-improve-hint")?.textContent).toBe(
      "Skill suggestions, Sources to learn, feedback reviews and vault upkeep proposals. Off stops them all. Sources to learn, Note links and Superseded notes need the Decision model.",
    );
    expect(document.getElementById("watch-improve-hint")?.textContent).toContain(
      "need the Decision model",
    );
    expect(document.getElementById("watch-tasks-dep")?.textContent).toContain(
      "need the Decision model",
    );
    expect(document.getElementById("watch-kanban-dep")).toBeNull();
    expect(document.getElementById("watch-kanban-hint")?.textContent).toBe(
      "Cards that need attention or could move to a Bot.",
    );
  });

  test("a rejected patch shows the failure line and does not refresh", async () => {
    const h = harness({ patch: vi.fn().mockRejectedValue(new Error("nope")) });
    render(<WatchRow {...h} />);
    openRow("What VIKI watches");
    await click(screen.getByRole("switch", { name: "Routines" }));
    expect(screen.getByRole("alert").textContent).toBe("Could not save that change. Try again.");
    expect(h.onRefresh).not.toHaveBeenCalled();
  });
});

// --- ReachRow ---

describe("ReachRow", () => {
  test.each([
    [0, "No nudges · Alerts off"],
    [1, "1 nudge an hour · Alerts off"],
    [2, "2 nudges an hour · Alerts off"],
  ])("the status chip for %i nudges reads %s, visible before opening", (count, text) => {
    const h = harness({ values: { "decision.nudges_per_hour": count } });
    render(<ReachRow {...h} />);
    expect(screen.getByRole("status").textContent).toBe(text);
  });

  test("the status chip says Alerts on when alerts are on", () => {
    const h = harness({ values: { "decision.alerts": true } });
    render(<ReachRow {...h} />);
    expect(screen.getByRole("status").textContent).toBe("2 nudges an hour · Alerts on");
  });

  test.each(MODEL_STATES.filter((state) => RULES_ONLY_MODEL_STATES.has(state)))(
    "the rules-only note is the body's first child while the model is %s",
    (state) => {
      const h = harness({ page: basePage({ model: { ...basePage().model, state } }) });
      render(<ReachRow {...h} />);
      openRow("How VIKI may reach you");
      const first = document.querySelector("#reach-body")?.firstElementChild;
      expect(first?.tagName).toBe("P");
      expect(first?.className).toBe("dloop-hint");
      expect(first?.textContent).toBe(
        "VIKI is using simple rules, so it only lists items. Nudges and alerts need the Decision model.",
      );
    },
  );

  test("no rules-only note while the model is ready", () => {
    const h = harness({ page: basePage({ model: { ...basePage().model, state: "ready" } }) });
    render(<ReachRow {...h} />);
    openRow("How VIKI may reach you");
    expect(screen.queryByText(/VIKI is using simple rules/)).toBeNull();
  });

  test("the legend lists Listed, Badge, Nudge and Alert with their meanings", () => {
    const h = harness();
    render(<ReachRow {...h} />);
    openRow("How VIKI may reach you");
    const legend = document.querySelector("dl.dloop-legend");
    expect(Array.from(legend?.querySelectorAll("dt") ?? []).map((n) => n.textContent)).toEqual([
      "Listed",
      "Badge",
      "Nudge",
      "Alert",
    ]);
    expect(Array.from(legend?.querySelectorAll("dd") ?? []).map((n) => n.textContent)).toEqual([
      "Added to the launcher's list; the launcher brightens briefly.",
      "A number on the launcher.",
      "The launcher's ring pulses once.",
      "The ring stays lit until you open the panel.",
    ]);
  });

  test("each control's hint text is linked by aria-describedby", () => {
    const h = harness();
    render(<ReachRow {...h} />);
    openRow("How VIKI may reach you");
    const described = (control: Element | null) =>
      (control?.getAttribute("aria-describedby") ?? "")
        .split(" ")
        .map((id) => document.getElementById(id)?.textContent);
    expect(described(screen.getByLabelText("Nudges per hour"))).toEqual([
      "The most nudges and alerts in any hour. 0 means none.",
    ]);
    expect(described(screen.getByRole("switch", { name: "Alerts" }))).toEqual([
      "For a task linked to an active project that is due within 2 hours or overdue.",
    ]);
    expect(
      described(
        screen.getByRole("switch", { name: "Tell me when I open the related chat or note" }),
      ),
    ).toEqual(["VIKI may hold an item until you open the chat or note it is about."]);
    expect(described(screen.getByRole("group", { name: "Quiet hours" }))).toEqual([
      "No nudges or alerts. VIKI holds its own looks, second looks, project plans, saved checks and inbox runs, and Bots start only routines that stop at a draft.",
    ]);
  });

  test("defaults: 2 nudges, alerts off, in-context on", () => {
    const h = harness();
    render(<ReachRow {...h} />);
    openRow("How VIKI may reach you");
    expect((screen.getByLabelText("Nudges per hour") as HTMLSelectElement).value).toBe("2");
    expect(screen.getByRole("switch", { name: "Alerts" }).getAttribute("aria-checked")).toBe(
      "false",
    );
    expect(
      screen
        .getByRole("switch", { name: "Tell me when I open the related chat or note" })
        .getAttribute("aria-checked"),
    ).toBe("true");
  });

  test("changing nudges, alerts and in-context each patch their own key and never refresh", async () => {
    const h = harness();
    render(<ReachRow {...h} />);
    openRow("How VIKI may reach you");
    await change(screen.getByLabelText("Nudges per hour"), "0");
    expect(h.patch).toHaveBeenCalledWith({ "decision.nudges_per_hour": 0 });
    await click(screen.getByRole("switch", { name: "Alerts" }));
    expect(h.patch).toHaveBeenCalledWith({ "decision.alerts": true });
    await click(
      screen.getByRole("switch", { name: "Tell me when I open the related chat or note" }),
    );
    expect(h.patch).toHaveBeenCalledWith({ "decision.in_context": false });
    expect(h.onRefresh).not.toHaveBeenCalled();
  });

  test("quiet-hour selects offer None plus 48 half hours, patch both keys, null when cleared, no refresh", async () => {
    const h = harness({ values: { "notifications.quiet_start": "22:00" } });
    render(<ReachRow {...h} />);
    openRow("How VIKI may reach you");
    const from = screen.getByLabelText("From") as HTMLSelectElement;
    const options = Array.from(from.options).map((option) => option.value);
    expect(options).toHaveLength(49);
    expect(options[0]).toBe("");
    expect(options[1]).toBe("00:00");
    expect(options[48]).toBe("23:30");
    expect(from.value).toBe("22:00");
    await change(from, "23:30");
    expect(h.patch).toHaveBeenCalledWith({ "notifications.quiet_start": "23:30" });
    const to = screen.getByLabelText("To");
    await change(to, "06:00");
    expect(h.patch).toHaveBeenCalledWith({ "notifications.quiet_end": "06:00" });
    await change(from, "");
    expect(h.patch).toHaveBeenCalledWith({ "notifications.quiet_start": null });
    expect(h.onRefresh).not.toHaveBeenCalled();
  });

  test("stopped topics list the muted candidates with their plain labels; Unmute calls preference then refreshes", async () => {
    const h = harness({ page: basePage({ muted: ["approvals", "uncommitted"] }) });
    render(<ReachRow {...h} />);
    openRow("How VIKI may reach you");
    expect(screen.getByText("Topics you stopped")).toBeTruthy();
    expect(screen.getByText("Approvals")).toBeTruthy();
    expect(screen.getByText("Uncommitted changes")).toBeTruthy();
    await click(screen.getByRole("button", { name: "Unmute Approvals" }));
    expect(h.api.requests.at(-1)).toEqual({
      method: "preference",
      args: [{ name: "unmute", candidate: "approvals" }],
    });
    expect(h.onRefresh).toHaveBeenCalledTimes(1);
  });

  test("no stopped topics shows the empty text", () => {
    const h = harness({ page: basePage({ muted: [] }) });
    render(<ReachRow {...h} />);
    openRow("How VIKI may reach you");
    expect(
      screen.getByText("None. Choose Stop these on an item in the launcher to stop a topic."),
    ).toBeTruthy();
    expect(screen.queryByText("Nothing stopped.")).toBeNull();
  });

  test("an unknown muted candidate reads as Other", () => {
    const h = harness({ page: basePage({ muted: ["mystery" as DecisionCandidate] }) });
    render(<ReachRow {...h} />);
    openRow("How VIKI may reach you");
    expect(screen.getByText("Other")).toBeTruthy();
  });

  test("not writable disables every control, with no hint text of its own", () => {
    const h = harness({ writable: false, page: basePage({ muted: ["routine"] }) });
    render(<ReachRow {...h} />);
    openRow("How VIKI may reach you");
    expect((screen.getByLabelText("Nudges per hour") as HTMLSelectElement).disabled).toBe(true);
    expect((screen.getByLabelText("From") as HTMLSelectElement).disabled).toBe(true);
    expect(screen.getByRole("switch", { name: "Alerts" }).getAttribute("aria-disabled")).toBe(
      "true",
    );
    expect(
      (screen.getByRole("button", { name: "Unmute Routines" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(screen.queryByText("Settings aren't ready to change right now.")).toBeNull();
  });

  test("a rejected preference call shows its own failure line and does not refresh", async () => {
    const h = harness({ page: basePage({ muted: ["inbox"] }) });
    h.api.fail("preference", "invalid_request");
    render(<ReachRow {...h} />);
    openRow("How VIKI may reach you");
    await click(screen.getByRole("button", { name: "Unmute Inbox" }));
    expect(screen.getByRole("alert").textContent).toBe("Could not unmute that. Try again.");
    expect(h.onRefresh).not.toHaveBeenCalled();
  });
});
