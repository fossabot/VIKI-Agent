import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { createFakeDecisionLoopApi } from "../../../clients/decisionLoop/fake";
import type {
  DataSummaryView,
  DecisionLoopApi,
  PageView,
} from "../../../clients/decisionLoop/types";
import { t, tf } from "../../../i18n/t";
import { localDate, shiftDay, topicLabel } from "./DataParts";
import { type DataFocus, DataRow } from "./DataRows";

afterEach(() => {
  cleanup();
});

function props(overrides: Partial<React.ComponentProps<typeof DataRow>> = {}) {
  const api = createFakeDecisionLoopApi();
  const onRefresh = vi.fn();
  const onJump = vi.fn();
  const onOpenContext = vi.fn();
  const page = { data: { episodes: 23 } } as PageView;
  return {
    api,
    onRefresh,
    onJump,
    onOpenContext,
    ...render(
      <DataRow
        page={page}
        values={{}}
        writable
        patch={vi.fn()}
        api={api as DecisionLoopApi}
        onRefresh={onRefresh}
        onJump={onJump}
        onOpenContext={onOpenContext}
        {...overrides}
      />,
    ),
  };
}

function summary(api: ReturnType<typeof createFakeDecisionLoopApi>): DataSummaryView {
  return api.data.data_summary;
}

function decisionCount(api: ReturnType<typeof createFakeDecisionLoopApi>): number {
  return summary(api).decisions.decisions + summary(api).decisions.reports;
}

test("Data row chip and summary load only on its first open", async () => {
  const { api } = props();
  expect(screen.getByRole("status").textContent).toBe(tf("decision_page.data.chip", { n: 23 }));
  expect(api.requests.filter((request) => request.method === "dataSummary")).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "Data" }));
  await screen.findByRole("article", { name: t("decision_page.data.kind.decisions.name") });
  expect(api.requests.filter((request) => request.method === "dataSummary")).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "Data" }));
  fireEvent.click(screen.getByRole("button", { name: "Data" }));
  await waitFor(() =>
    expect(api.requests.filter((request) => request.method === "dataSummary")).toHaveLength(1),
  );
});

test("closing while summary is pending keeps its one request and renders the result on reopen", async () => {
  const { api } = props();
  let finish: (value: DataSummaryView) => void = () => {};
  api.dataSummary = vi.fn(
    () =>
      new Promise<DataSummaryView>((resolve) => {
        finish = resolve;
      }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Data" }));
  fireEvent.click(screen.getByRole("button", { name: "Data" }));
  fireEvent.click(screen.getByRole("button", { name: "Data" }));
  await act(async () => finish(summary(api)));
  await screen.findByRole("article", { name: t("decision_page.data.kind.decisions.name") });
  expect(api.dataSummary).toHaveBeenCalledOnce();
});

test("late summary from replaced API cannot replace current API result", async () => {
  const { api: apiA, rerender } = props();
  apiA.data.data_summary.decisions.decisions = 1111;
  let finishA: (value: DataSummaryView) => void = () => {};
  apiA.dataSummary = vi.fn(
    () =>
      new Promise<DataSummaryView>((resolve) => {
        finishA = resolve;
      }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Data" }));

  const apiB = createFakeDecisionLoopApi();
  apiB.data.data_summary.decisions.decisions = 7777;
  let finishB: (value: DataSummaryView) => void = () => {};
  apiB.dataSummary = vi.fn(
    () =>
      new Promise<DataSummaryView>((resolve) => {
        finishB = resolve;
      }),
  );
  rerender(
    <DataRow
      page={{ data: { episodes: 23 } } as PageView}
      values={{}}
      writable
      patch={vi.fn()}
      api={apiB}
      onRefresh={vi.fn()}
      onJump={vi.fn()}
      onOpenContext={vi.fn()}
      open
    />,
  );
  await act(async () => finishB(summary(apiB)));
  const meterName = t("decision_page.data.kind.decisions.name");
  const expected = String(7777 + summary(apiB).decisions.reports);
  await waitFor(() =>
    expect(screen.getByRole("meter", { name: meterName }).getAttribute("aria-valuenow")).toBe(
      expected,
    ),
  );
  await act(async () => finishA(summary(apiA)));
  expect(screen.getByRole("meter", { name: meterName }).getAttribute("aria-valuenow")).toBe(
    expected,
  );
  expect(apiA.dataSummary).toHaveBeenCalledOnce();
  expect(apiB.dataSummary).toHaveBeenCalledOnce();
});

test("loaded summary and delete confirmation reset when API changes", async () => {
  const { api: apiA, rerender } = props({ open: true });
  await screen.findByRole("article", { name: t("decision_page.data.kind.decisions.name") });
  const asOfA = localDate(summary(apiA).as_of);
  fireEvent.click(screen.getByRole("button", { name: t("decision_page.data.all.button") }));
  expect(screen.getByRole("alertdialog")).toBeTruthy();

  const apiB = createFakeDecisionLoopApi();
  apiB.data.data_summary.decisions.decisions = 7777;
  apiB.data.data_summary.as_of = "2026-11-09T00:00:00.000Z";
  let finishB: (value: DataSummaryView) => void = () => {};
  apiB.dataSummary = vi.fn(
    () =>
      new Promise<DataSummaryView>((resolve) => {
        finishB = resolve;
      }),
  );
  rerender(
    <DataRow
      page={{ data: { episodes: 23 } } as PageView}
      values={{}}
      writable
      patch={vi.fn()}
      api={apiB}
      onRefresh={vi.fn()}
      onJump={vi.fn()}
      onOpenContext={vi.fn()}
      open
    />,
  );
  expect(screen.queryByRole("alertdialog")).toBeNull();
  expect(
    screen.queryByRole("article", { name: t("decision_page.data.kind.decisions.name") }),
  ).toBeNull();
  expect(screen.queryByText(tf("decision_page.data.as_of", { time: asOfA }))).toBeNull();
  expect(apiB.dataSummary).toHaveBeenCalledOnce();

  await act(async () => finishB(summary(apiB)));
  const meter = await screen.findByRole("meter", {
    name: t("decision_page.data.kind.decisions.name"),
  });
  expect(meter.getAttribute("aria-valuenow")).toBe(String(7777 + summary(apiB).decisions.reports));
  expect(screen.getByText(tf("decision_page.data.as_of", { time: "2026-11-09" }))).toBeTruthy();
  expect(screen.queryByRole("alertdialog")).toBeNull();
});

test.each(["busy", "unavailable"] as const)("%s summary failure offers Retry", async (code) => {
  const { api } = props();
  api.fail("dataSummary", code);
  fireEvent.click(screen.getByRole("button", { name: "Data" }));
  await screen.findByText(t(`decision_page.data.${code}`));
  fireEvent.click(screen.getByRole("button", { name: t("common.button.retry") }));
  await screen.findByRole("article", { name: t("decision_page.data.kind.decisions.name") });
});

test("all seven inventory cards show summary values, date lines, limits, notes and meters", async () => {
  const { api } = props();
  const data = summary(api);
  fireEvent.click(screen.getByRole("button", { name: "Data" }));
  await screen.findByRole("article", { name: t("decision_page.data.kind.decisions.name") });
  const cards = screen.getAllByRole("article");
  expect(cards).toHaveLength(7);
  const [decisions, suggestions, calls, chats, proposals, preferences, key] = cards;
  expect(decisions?.textContent).toContain(
    tf("decision_page.data.kind.decisions.count", data.decisions),
  );
  expect(decisions?.querySelectorAll(".dloop-card-detail")[0]?.textContent).toBe(
    tf("decision_page.data.dates", {
      oldest: localDate(data.decisions.oldest ?? ""),
      newest: localDate(data.decisions.newest ?? ""),
    }),
  );
  expect(decisions?.textContent).toContain(
    tf("decision_page.data.kind.decisions.limit", {
      max: data.decisions.max,
      days: data.decisions.retention_days,
    }),
  );
  expect(
    within(decisions as HTMLElement)
      .getByRole("meter")
      .getAttribute("aria-valuenow"),
  ).toBe(String(data.decisions.decisions + data.decisions.reports));
  expect(suggestions?.textContent).toContain(
    tf("decision_page.data.kind.suggestions.count", data.suggestions),
  );
  expect(suggestions?.querySelectorAll(".dloop-card-detail")[0]?.textContent).toBe(
    tf("decision_page.data.dates", {
      oldest: localDate(data.suggestions.oldest ?? ""),
      newest: localDate(data.suggestions.newest ?? ""),
    }),
  );
  expect(suggestions?.textContent).toContain(
    tf("decision_page.data.kind.suggestions.limit", {
      max: data.suggestions.max_open,
      days: data.suggestions.closed_days,
    }),
  );
  expect(
    within(suggestions as HTMLElement)
      .getByRole("meter")
      .getAttribute("aria-valuenow"),
  ).toBe(String(data.suggestions.open));
  expect(calls?.textContent).toContain(
    tf("decision_page.data.kind.calls.count", { n: data.calls.count }),
  );
  expect(calls?.querySelectorAll(".dloop-card-detail")[0]?.textContent).toBe(
    tf("decision_page.data.dates", {
      oldest: localDate(data.calls.oldest ?? ""),
      newest: localDate(data.calls.newest ?? ""),
    }),
  );
  expect(calls?.textContent).toContain(
    tf("decision_page.data.kind.calls.limit", { max: data.calls.max }),
  );
  expect(
    within(calls as HTMLElement)
      .getByRole("meter")
      .getAttribute("aria-valuemax"),
  ).toBe(String(data.calls.max));
  expect(chats?.textContent).toContain(
    tf("decision_page.data.kind.chats.count", { n: data.chats.count, running: data.chats.running }),
  );
  expect(chats?.querySelectorAll(".dloop-card-detail")[0]?.textContent).toBe(
    tf("decision_page.data.dates", {
      oldest: localDate(data.chats.oldest ?? ""),
      newest: localDate(data.chats.newest ?? ""),
    }),
  );
  expect(chats?.textContent).toContain(t("decision_page.data.kind.chats.kept"));
  expect(chats?.textContent).toContain(t("decision_page.data.no_limit"));
  expect(proposals?.textContent).toContain(
    tf("decision_page.data.kind.proposals.count", data.proposals),
  );
  expect(proposals?.querySelectorAll(".dloop-card-detail")[0]?.textContent).toBe(
    tf("decision_page.data.dates", {
      oldest: localDate(data.proposals.oldest ?? ""),
      newest: localDate(data.proposals.newest ?? ""),
    }),
  );
  expect(proposals?.textContent).toContain(
    tf("decision_page.data.kind.proposals.limit", {
      wordings: data.proposals.max_wordings,
      changes: data.proposals.max_changes,
    }),
  );
  expect(
    within(proposals as HTMLElement)
      .getAllByRole("meter")
      .map((meter) => meter.getAttribute("aria-valuenow")),
  ).toEqual([String(data.proposals.wordings), String(data.proposals.changes)]);
  expect(proposals?.textContent).toContain(t("decision_page.data.kind.proposals.kept"));
  expect(preferences?.textContent).toContain(
    tf("decision_page.data.kind.preferences.count", data.preferences),
  );
  expect(preferences?.querySelectorAll(".dloop-card-detail")[0]?.textContent).toBe(
    tf("decision_page.data.changed", { date: localDate(data.preferences.updated ?? "") }),
  );
  expect(preferences?.textContent).toContain(t("decision_page.data.kind.preferences.limit"));
  expect(preferences?.textContent).toContain(t("decision_page.data.kind.preferences.kept"));
  expect(key?.textContent).toContain(
    t(data.key.saved ? "decision_page.data.kind.key.saved" : "decision_page.data.kind.key.missing"),
  );
  expect(key?.textContent).toContain(t("decision_page.data.kind.key.kept"));
  expect(key?.querySelectorAll(".dloop-card-detail")[0]?.textContent).toBe(
    tf("decision_page.data.changed", { date: localDate(data.key.updated ?? "") }),
  );
  expect(key?.textContent).toContain(t("decision_page.data.kind.key.limit"));
  for (const card of cards)
    expect(card.querySelector(".dloop-card-detail")?.textContent).toBeTruthy();
});

test("null card dates use dates.none and preference/key timestamps use changed", async () => {
  const { api } = props();
  const data = summary(api);
  data.decisions.oldest = null;
  data.decisions.newest = null;
  data.suggestions.oldest = null;
  data.suggestions.newest = null;
  data.calls.oldest = null;
  data.calls.newest = null;
  data.chats.oldest = null;
  data.chats.newest = null;
  data.proposals.oldest = null;
  data.proposals.newest = null;
  data.preferences.updated = "2026-11-01T12:00:00.000Z";
  data.key.updated = null;
  fireEvent.click(screen.getByRole("button", { name: "Data" }));
  await screen.findByRole("article", { name: t("decision_page.data.kind.decisions.name") });
  expect(screen.getAllByText(t("decision_page.data.dates.none"))).toHaveLength(6);
  expect(screen.getByText(tf("decision_page.data.changed", { date: "2026-11-01" }))).toBeTruthy();
});

test("Explore selects the matching browser kind; preference and key actions jump to their rows", async () => {
  const { onJump } = props();
  fireEvent.click(screen.getByRole("button", { name: "Data" }));
  await screen.findByRole("article", { name: t("decision_page.data.kind.decisions.name") });
  for (const name of ["decisions", "suggestions", "calls"] as const) {
    const card = screen.getByRole("article", {
      name: t(`decision_page.data.kind.${name}.name`),
    });
    fireEvent.click(within(card).getByRole("button", { name: t("decision_page.data.explore") }));
    expect(document.activeElement?.id).toBe("data-browse");
    expect(
      (
        screen.getByRole("radio", {
          name:
            name === "decisions"
              ? t("decision_page.data.browse.decisions")
              : t(`decision_page.data.kind.${name}.name`),
        }) as HTMLInputElement
      ).checked,
    ).toBe(true);
  }
  fireEvent.click(screen.getByRole("button", { name: t("decision_page.data.open_learning") }));
  fireEvent.click(screen.getByRole("button", { name: t("decision_page.data.open_reach") }));
  fireEvent.click(screen.getByRole("button", { name: t("decision_page.data.open_model") }));
  expect(onJump.mock.calls).toEqual([["learning"], ["reach"], ["model"]]);
});

test("range controls both charts and topic buttons filter the browser", async () => {
  const { api } = props();
  api.data.data_summary.days = [
    {
      day: shiftDay(api.data.data_summary.today, -60),
      held: 0,
      listed: 3,
      attention: 0,
      reports: 0,
      useful: 2,
      wrong: 0,
      later: 0,
      mute: 0,
      right_to_hold: 0,
      missed: 0,
    },
    {
      day: api.data.data_summary.today,
      held: 1,
      listed: 0,
      attention: 0,
      reports: 0,
      useful: 0,
      wrong: 0,
      later: 0,
      mute: 0,
      right_to_hold: 0,
      missed: 0,
    },
  ];
  fireEvent.click(screen.getByRole("button", { name: "Data" }));
  await screen.findByRole("article", { name: t("decision_page.data.kind.decisions.name") });
  expect(screen.getByText("Shown to you: 0")).toBeTruthy();
  fireEvent.click(screen.getByRole("radio", { name: t("decision_page.data.range.90") }));
  expect(screen.getByRole("img", { name: /Last 90 days/ })).toBeTruthy();
  expect(screen.getByText("Shown to you: 3")).toBeTruthy();
  expect(screen.getByText(t("decision_page.data.answers.hint"))).toBeTruthy();
  fireEvent.click(
    screen.getByRole("button", { name: topicLabel(summary(api).topics[0]?.topic ?? "approvals") }),
  );
  await waitFor(() => {
    const requests = api.requests.filter((request) => request.method === "dataRecords");
    expect(requests.at(-1)?.args[0]).toMatchObject({
      kind: "decisions",
      topic: summary(api).topics[0]?.topic,
    });
  });
  expect(
    screen.getByRole("radio", { name: t("decision_page.data.browse.decisions") }),
  ).toBeTruthy();
  expect(api.requests.some((request) => request.method === "dataRecords")).toBe(true);
});

test("delete all confirms exact count and sections, then deletes, reloads and refreshes", async () => {
  const { api, onRefresh } = props();
  api.deleteEpisodes = vi.fn(async () => ({ deleted: 23 }));
  fireEvent.click(screen.getByRole("button", { name: "Data" }));
  await screen.findByRole("article", { name: t("decision_page.data.kind.decisions.name") });
  fireEvent.click(screen.getByRole("button", { name: t("decision_page.data.all.button") }));
  const dialog = screen.getByRole("alertdialog", { name: t("decision_page.data.all.confirm") });
  expect(dialog.textContent).toContain(
    tf("decision_page.data.all.remove.decisions", { n: decisionCount(api) }),
  );
  expect(within(dialog).getAllByRole("listitem")).toHaveLength(15);
  expect(
    within(dialog)
      .getAllByRole("listitem")
      .map((item) => item.textContent),
  ).toEqual([
    tf("decision_page.data.all.remove.decisions", { n: decisionCount(api) }),
    t("decision_page.data.all.remove.suggestions"),
    t("decision_page.data.all.remove.calls"),
    t("decision_page.data.all.remove.learned"),
    ...[
      "comes_back",
      "item_pauses",
      "today",
      "looks",
      "comparisons",
      "vault_change",
      "improve",
    ].map((key) => t(`decision_page.data.effect.${key}` as Parameters<typeof t>[0])),
    ...["settings", "chats", "proposals", "today"].map((key) =>
      t(`decision_page.data.stay.${key}` as Parameters<typeof t>[0]),
    ),
  ]);
  fireEvent.click(
    within(dialog).getByRole("button", { name: t("decision_page.data.confirm.delete") }),
  );
  await screen.findByText(tf("decision_page.data.all.done", { n: 23 }));
  expect(api.requests.filter((request) => request.method === "dataSummary")).toHaveLength(2);
  expect(onRefresh).toHaveBeenCalledOnce();
});

test.each([
  { pageCount: 0, summaryCount: 7, disabled: false },
  { pageCount: 9, summaryCount: 0, disabled: true },
])(
  "delete all availability and count follow the loaded summary when page count differs",
  async ({ pageCount, summaryCount, disabled }) => {
    const { api, rerender } = props({ page: { data: { episodes: pageCount } } as PageView });
    api.data.data_summary.decisions.decisions = summaryCount;
    api.data.data_summary.decisions.reports = 0;
    rerender(
      <DataRow
        page={{ data: { episodes: pageCount } } as PageView}
        values={{}}
        writable
        patch={vi.fn()}
        api={api}
        onRefresh={vi.fn()}
        onJump={vi.fn()}
        onOpenContext={vi.fn()}
        open
      />,
    );
    await screen.findByRole("article", { name: t("decision_page.data.kind.decisions.name") });
    const deleteButton = screen.getByRole("button", {
      name: t("decision_page.data.all.button"),
    }) as HTMLButtonElement;
    expect(deleteButton.disabled).toBe(disabled);
    if (!disabled) {
      fireEvent.click(deleteButton);
      expect(screen.getByRole("alertdialog").textContent).toContain(
        tf("decision_page.data.all.remove.decisions", { n: summaryCount }),
      );
    }
  },
);

test("delete all stays open on failure and is disabled when read-only or empty", async () => {
  const { api, rerender } = props();
  fireEvent.click(screen.getByRole("button", { name: "Data" }));
  await screen.findByRole("article", { name: t("decision_page.data.kind.decisions.name") });
  api.fail("deleteEpisodes", "unavailable");
  fireEvent.click(screen.getByRole("button", { name: t("decision_page.data.all.button") }));
  fireEvent.click(
    within(screen.getByRole("alertdialog")).getByRole("button", {
      name: t("decision_page.data.confirm.delete"),
    }),
  );
  await screen.findByText(t("decision_page.data.all.failed"));
  expect(screen.getByRole("alertdialog")).toBeTruthy();
  const emptyApi = createFakeDecisionLoopApi();
  emptyApi.data.data_summary.decisions.decisions = 0;
  emptyApi.data.data_summary.decisions.reports = 0;
  rerender(
    <DataRow
      page={{ data: { episodes: 23 } } as PageView}
      values={{}}
      writable={false}
      patch={vi.fn()}
      api={api as DecisionLoopApi}
      onRefresh={vi.fn()}
      onJump={vi.fn()}
      onOpenContext={vi.fn()}
    />,
  );
  expect(
    (screen.getByRole("button", { name: t("decision_page.data.all.button") }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  rerender(
    <DataRow
      page={{ data: { episodes: 0 } } as PageView}
      values={{}}
      writable
      patch={vi.fn()}
      api={emptyApi}
      onRefresh={vi.fn()}
      onJump={vi.fn()}
      onOpenContext={vi.fn()}
      open
    />,
  );
  await screen.findByRole("article", { name: t("decision_page.data.kind.decisions.name") });
  expect(
    (screen.getByRole("button", { name: t("decision_page.data.all.button") }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
});

test.each([
  ["decisions", "data-browse"],
  ["answers", "data-answers"],
] as const)("focus target %s is reached after summary load", async (target, id) => {
  const focus: DataFocus = { target, seq: 1 };
  const { rerender } = props({ focus });
  fireEvent.click(screen.getByRole("button", { name: "Data" }));
  await screen.findByRole("article", { name: t("decision_page.data.kind.decisions.name") });
  await waitFor(() => expect(document.activeElement?.id).toBe(id));
  rerender(
    <DataRow
      page={{ data: { episodes: 23 } } as PageView}
      values={{}}
      writable
      patch={vi.fn()}
      api={createFakeDecisionLoopApi()}
      onRefresh={vi.fn()}
      onJump={vi.fn()}
      onOpenContext={vi.fn()}
      focus={{ target, seq: 2 }}
      open
    />,
  );
  await waitFor(() => expect(document.activeElement?.id).toBe(id));
});

const DELETED_LINE = [
  tf("decision_page.data.deleted", { n: 1 }),
  tf("decision_page.data.deleted.kept", { n: 1 }),
  tf("decision_page.data.deleted.gone", { n: 1 }),
].join(" ");

function summaryRequests(api: ReturnType<typeof createFakeDecisionLoopApi>): number {
  return api.requests.filter((request) => request.method === "dataSummary").length;
}

/** Opens Data, filters the browser, selects three records and confirms their delete. */
async function deleteThreeRecords(api: ReturnType<typeof createFakeDecisionLoopApi>) {
  const row = api.data.data_decisions.rows[1];
  if (!row) throw new Error("fixture row missing");
  api.data.data_decisions.rows = [
    row,
    { ...row, id: "11111111-1111-4111-8111-111111111111" },
    { ...row, id: "22222222-2222-4222-8222-222222222222" },
  ];
  fireEvent.click(screen.getByRole("button", { name: "Data" }));
  await screen.findByRole("article", { name: t("decision_page.data.kind.decisions.name") });
  fireEvent.change(screen.getByRole("combobox", { name: t("decision_page.data.filter.shown") }), {
    target: { value: "held" },
  });
  fireEvent.change(screen.getByRole("combobox", { name: t("decision_page.data.filter.answer") }), {
    target: { value: "none" },
  });
  await waitFor(() => expect(screen.getAllByRole("checkbox")).toHaveLength(3));
  for (const checkbox of screen.getAllByRole("checkbox")) fireEvent.click(checkbox);
  fireEvent.click(
    screen.getByRole("button", { name: tf("decision_page.data.delete.selected", { n: 3 }) }),
  );
  fireEvent.click(
    within(screen.getByRole("alertdialog")).getByRole("button", {
      name: t("decision_page.data.confirm.delete"),
    }),
  );
}

function filterValues(): string[] {
  return [t("decision_page.data.filter.shown"), t("decision_page.data.filter.answer")].map(
    (name) => (screen.getByRole("combobox", { name }) as HTMLSelectElement).value,
  );
}

test("a record delete keeps its result line, filters and focus through the summary re-read", async () => {
  const { api, onRefresh, container } = props();
  // The server stamps each summary with the current time, so the re-read carries a new cutoff.
  api.deleteData = vi.fn(async () => {
    api.data.data_summary.as_of = "2026-10-22T09:30:00.000Z";
    api.data.data_summary.decisions.decisions = 4321;
    return { deleted: 1, kept: 1 };
  });
  await deleteThreeRecords(api);
  const browser = container.querySelector(".dloop-data-records");

  await screen.findByText(DELETED_LINE);
  await waitFor(() =>
    expect(
      screen
        .getByRole("meter", { name: t("decision_page.data.kind.decisions.name") })
        .getAttribute("aria-valuenow"),
    ).toBe(String(4321 + summary(api).decisions.reports)),
  );
  expect(summaryRequests(api)).toBe(2);
  await waitFor(() => expect(screen.getAllByRole("checkbox")).toHaveLength(3));
  const lastRecords = api.requests.filter((request) => request.method === "dataRecords").at(-1);
  expect(lastRecords?.args[0]).toMatchObject({
    shown: "held",
    answer: "none",
    until: "2026-10-22T09:30:00.000Z",
  });
  expect(container.querySelector(".dloop-data-records")).toBe(browser);
  expect(screen.getByText(DELETED_LINE).getAttribute("role")).toBe("status");
  expect(filterValues()).toEqual(["held", "none"]);
  expect(document.activeElement?.id).toBe("data-browse");
  expect(onRefresh).toHaveBeenCalledOnce();
});

test("a summary re-read that fails after a record delete keeps the result and offers Retry", async () => {
  const { api } = props();
  api.deleteData = vi.fn(async () => {
    api.fail("dataSummary", "busy");
    return { deleted: 1, kept: 1 };
  });
  await deleteThreeRecords(api);

  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toContain(t("decision_page.data.busy"));
  expect(screen.getByText(DELETED_LINE)).toBeTruthy();
  expect(filterValues()).toEqual(["held", "none"]);
  expect(
    screen.getByRole("article", { name: t("decision_page.data.kind.decisions.name") }),
  ).toBeTruthy();

  fireEvent.click(within(alert).getByRole("button", { name: t("common.button.retry") }));
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  expect(summaryRequests(api)).toBe(3);
  expect(screen.getByText(DELETED_LINE)).toBeTruthy();
});

test("delete all shows its result when the summary read after it fails, and reloads the browser", async () => {
  const { api } = props();
  api.deleteEpisodes = vi.fn(async () => {
    api.fail("dataSummary", "unavailable");
    return { deleted: 23 };
  });
  fireEvent.click(screen.getByRole("button", { name: "Data" }));
  await screen.findByText(/decisions match/);
  const recordReads = () =>
    api.requests.filter((request) => request.method === "dataRecords").length;
  const before = recordReads();
  fireEvent.click(screen.getByRole("button", { name: t("decision_page.data.all.button") }));
  fireEvent.click(
    within(screen.getByRole("alertdialog")).getByRole("button", {
      name: t("decision_page.data.confirm.delete"),
    }),
  );

  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toContain(t("decision_page.data.unavailable"));
  expect(screen.getByText(tf("decision_page.data.all.done", { n: 23 }))).toBeTruthy();
  // The counts and records it emptied are not left on screen as if they were current.
  expect(
    screen.queryByRole("article", { name: t("decision_page.data.kind.decisions.name") }),
  ).toBeNull();
  expect(screen.queryByText(/decisions match/)).toBeNull();

  fireEvent.click(within(alert).getByRole("button", { name: t("common.button.retry") }));
  await screen.findByText(/decisions match/);
  expect(recordReads()).toBeGreaterThan(before);
  expect(screen.getByText(tf("decision_page.data.all.done", { n: 23 }))).toBeTruthy();
});
