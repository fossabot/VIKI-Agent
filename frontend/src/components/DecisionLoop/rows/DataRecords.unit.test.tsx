import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { createFakeDecisionLoopApi } from "../../../clients/decisionLoop/fake";
import type {
  CallDetailView,
  DataKind,
  DataQuery,
  DataRecordsView,
  DataRecordView,
} from "../../../clients/decisionLoop/types";
import { t } from "../../../i18n/t";
import { localClock } from "../wording";
import { localDate } from "./DataParts";
import { DataRecords } from "./DataRecords";

const AS_OF = "2026-10-22T00:00:00.000Z";
afterEach(() => {
  cleanup();
});

function setup(kind: DataKind = "decisions", writable = true) {
  const fake = createFakeDecisionLoopApi();
  const onKind = vi.fn();
  const onTopic = vi.fn();
  const onDeleted = vi.fn();
  const onOpenContext = vi.fn();
  const view = render(
    <DataRecords
      api={fake}
      asOf={AS_OF}
      kind={kind}
      onKind={onKind}
      topic={null}
      onTopic={onTopic}
      writable={writable}
      onDeleted={onDeleted}
      onOpenContext={onOpenContext}
      nowMs={Date.parse("2026-10-21T15:00:00.000Z")}
    />,
  );
  return { fake, onKind, onTopic, onDeleted, onOpenContext, ...view };
}

function lastQuery(fake: ReturnType<typeof createFakeDecisionLoopApi>): DataQuery {
  const request = fake.requests.filter((item) => item.method === "dataRecords").at(-1);
  return request?.args[0] as DataQuery;
}

test("decision filters and local-midnight period use exact query keys", async () => {
  const { fake, onTopic } = setup();
  await screen.findByText(/decisions match/);
  expect(lastQuery(fake)).toEqual({
    kind: "decisions",
    ids: null,
    topic: null,
    shown: null,
    answer: null,
    state: null,
    outcome: null,
    since: null,
    until: AS_OF,
    before: null,
  });
  fireEvent.change(screen.getByRole("combobox", { name: "Shown as" }), {
    target: { value: "held" },
  });
  await waitFor(() => expect(lastQuery(fake).shown).toBe("held"));
  fireEvent.change(screen.getByRole("combobox", { name: "Your answer" }), {
    target: { value: "none" },
  });
  await waitFor(() => expect(lastQuery(fake).answer).toBe("none"));
  fireEvent.change(screen.getByRole("combobox", { name: "When" }), { target: { value: "7" } });
  const start = new Date("2026-10-21T15:00:00.000Z");
  start.setHours(0, 0, 0, 0);
  start.setDate(start.getDate() - 6);
  await waitFor(() => expect(lastQuery(fake).since).toBe(start.toISOString()));
  fireEvent.change(screen.getByRole("combobox", { name: "Topic" }), {
    target: { value: "tasks_due" },
  });
  expect(onTopic).toHaveBeenCalledWith("tasks_due");
  expect(
    within(screen.getByRole("combobox", { name: "Shown as" }))
      .getAllByRole("option")
      .map((option) => option.textContent),
  ).toEqual(["Any", "Held back", "Listed", "Got your attention", "Report"]);
});

test("suggestion and call filters, call context, and details route", async () => {
  const { fake, onOpenContext } = setup("calls");
  await screen.findByText(/model calls match/);
  expect(lastQuery(fake)).toMatchObject({ kind: "calls", until: null, outcome: null });
  fireEvent.change(screen.getByRole("combobox", { name: "Outcome" }), {
    target: { value: "failed" },
  });
  await waitFor(() => expect(lastQuery(fake).outcome).toBe("failed"));
  expect(screen.getAllByRole("button", { name: "Open in Context" })).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "Open in Context" }));
  expect(onOpenContext).toHaveBeenCalledOnce();
  fireEvent.click(screen.getAllByRole("button", { name: "Details" })[0] as HTMLElement);
  await waitFor(() =>
    expect(fake.requests.some((request) => request.method === "call")).toBe(true),
  );
});

test("kept rows lack checkbox; selected delete confirms effects and sends frozen ids", async () => {
  const { fake, onDeleted } = setup();
  await screen.findByText(/decisions match/);
  const selects = screen.getAllByRole("checkbox");
  expect(selects).toHaveLength(1);
  fireEvent.click(selects[0] as HTMLElement);
  fireEvent.click(screen.getByRole("button", { name: "Delete selected (1)" }));
  const dialog = screen.getByRole("alertdialog");
  expect(dialog.textContent).toContain("This can't be undone.");
  expect(within(dialog).getAllByRole("list")).toHaveLength(2);
  expect(within(dialog).getAllByRole("listitem")).toHaveLength(11);
  fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
  await waitFor(() =>
    expect(fake.requests.some((request) => request.method === "deleteData")).toBe(true),
  );
  const query = fake.requests.find((request) => request.method === "deleteData")
    ?.args[0] as DataQuery;
  expect(query).toMatchObject({
    kind: "decisions",
    ids: [fake.data.data_decisions.next?.id],
    until: null,
    before: null,
  });
  await waitFor(() => expect(onDeleted).toHaveBeenCalledOnce());
});

test("matching delete requires filter and read-only disables deletes", async () => {
  const { fake, rerender } = setup("suggestions");
  await screen.findByText(/suggestions match/);
  expect(screen.getByText(t("decision_page.data.delete.need_filter"))).toBeTruthy();
  fireEvent.change(screen.getByRole("combobox", { name: "State" }), { target: { value: "done" } });
  await waitFor(() => expect(lastQuery(fake).state).toBe("done"));
  fireEvent.click(screen.getByRole("button", { name: /Delete all .* matching/ }));
  expect(within(screen.getByRole("alertdialog")).getAllByRole("listitem")).toHaveLength(7);
  fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Cancel" }));
  rerender(
    <DataRecords
      api={fake}
      asOf={AS_OF}
      kind="suggestions"
      onKind={vi.fn()}
      topic={null}
      onTopic={vi.fn()}
      writable={false}
      onDeleted={vi.fn()}
      onOpenContext={vi.fn()}
    />,
  );
  expect(
    (screen.getByRole("button", { name: /Delete all .* matching/ }) as HTMLButtonElement).disabled,
  ).toBe(true);
});

test("suggestion and call confirmations show exactly their stated effects and stays", async () => {
  const effects = {
    suggestions: ["comes_back", "titles", "item_pauses", "today"],
    calls: ["see_why", "last_call"],
  } as const;
  const stays = { suggestions: ["decisions", "chats", "settings"], calls: ["today"] } as const;
  for (const kind of ["suggestions", "calls"] as const) {
    const view = setup(kind);
    await screen.findByText(kind === "calls" ? /model calls match/ : /suggestions match/);
    fireEvent.click(screen.getAllByRole("checkbox")[0] as HTMLElement);
    fireEvent.click(screen.getByRole("button", { name: "Delete selected (1)" }));
    const dialog = screen.getByRole("alertdialog");
    expect(dialog.textContent).toContain("This can't be undone.");
    const lists = within(dialog).getAllByRole("list");
    expect(
      lists.map((list) =>
        within(list)
          .getAllByRole("listitem")
          .map((item) => item.textContent),
      ),
    ).toEqual([
      effects[kind].map((key) => t(`decision_page.data.effect.${key}`)),
      stays[kind].map((key) => t(`decision_page.data.stay.${key}`)),
    ]);
    view.unmount();
  }
});

test("paging passes cursor and focuses first new row", async () => {
  const fake = createFakeDecisionLoopApi();
  const original = fake.dataRecords;
  fake.dataRecords = vi.fn(async (query, signal) => {
    const page = await original(query, signal);
    if (page.kind !== "decisions") return page;
    return query.before === null
      ? { ...page, rows: page.rows.slice(0, 1) }
      : { ...page, next: null, rows: page.rows.slice(1) };
  });
  render(
    <DataRecords
      api={fake}
      asOf={AS_OF}
      kind="decisions"
      onKind={vi.fn()}
      topic={null}
      onTopic={vi.fn()}
      writable
      onDeleted={vi.fn()}
      onOpenContext={vi.fn()}
    />,
  );
  await screen.findByText(/decisions match/);
  fireEvent.click(screen.getByRole("button", { name: "Show 50 more" }));
  await waitFor(() => expect(lastQuery(fake).before).toEqual(fake.data.data_decisions.next));
  await waitFor(() =>
    expect(document.activeElement?.getAttribute("data-data-row")).toBe(
      fake.data.data_decisions.rows[1]?.id,
    ),
  );
});

test("matching delete retains confirmed filter and cutoff after props change", async () => {
  const { fake, rerender } = setup("suggestions");
  await screen.findByText(/suggestions match/);
  fireEvent.change(screen.getByRole("combobox", { name: "State" }), { target: { value: "done" } });
  await waitFor(() => expect(lastQuery(fake).state).toBe("done"));
  fireEvent.click(screen.getByRole("button", { name: /Delete all .* matching/ }));
  rerender(
    <DataRecords
      api={fake}
      asOf="2026-12-01T00:00:00.000Z"
      kind="calls"
      onKind={vi.fn()}
      topic={null}
      onTopic={vi.fn()}
      writable
      onDeleted={vi.fn()}
      onOpenContext={vi.fn()}
    />,
  );
  fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Delete" }));
  await waitFor(() =>
    expect(fake.requests.some((request) => request.method === "deleteData")).toBe(true),
  );
  expect(fake.requests.find((request) => request.method === "deleteData")?.args[0]).toEqual({
    kind: "suggestions",
    ids: null,
    topic: null,
    shown: null,
    answer: null,
    state: "done",
    outcome: null,
    since: null,
    until: AS_OF,
    before: null,
  });
});

test("a period at the cutoff skips request", async () => {
  const { fake, rerender } = setup();
  await screen.findByText(/decisions match/);
  const count = fake.requests.filter((request) => request.method === "dataRecords").length;
  rerender(
    <DataRecords
      api={fake}
      asOf="2026-10-20T00:00:00.000Z"
      kind="decisions"
      onKind={vi.fn()}
      topic={null}
      onTopic={vi.fn()}
      writable
      onDeleted={vi.fn()}
      onOpenContext={vi.fn()}
      nowMs={Date.parse("2026-10-21T15:00:00.000Z")}
    />,
  );
  fireEvent.change(screen.getByRole("combobox", { name: "When" }), { target: { value: "today" } });
  await screen.findByText(t("decision_page.data.results.none"));
  expect(fake.requests.filter((request) => request.method === "dataRecords")).toHaveLength(
    count + 1,
  );
});

test("decision delete shows Removing, then deleted, kept, and gone parts", async () => {
  const fake = createFakeDecisionLoopApi();
  const row = fake.data.data_decisions.rows[1];
  if (!row) throw new Error("fixture row missing");
  fake.data.data_decisions.rows = [
    row,
    { ...row, id: "11111111-1111-4111-8111-111111111111" },
    { ...row, id: "22222222-2222-4222-8222-222222222222" },
  ];
  let finish: (value: { deleted: number; kept: number }) => void = () => {};
  fake.deleteData = vi.fn(
    () =>
      new Promise<{ deleted: number; kept: number }>((resolve) => {
        finish = resolve;
      }),
  );
  render(
    <DataRecords
      api={fake}
      asOf={AS_OF}
      kind="decisions"
      onKind={vi.fn()}
      topic={null}
      onTopic={vi.fn()}
      writable
      onDeleted={vi.fn()}
      onOpenContext={vi.fn()}
    />,
  );
  await screen.findByText(/decisions match/);
  for (const checkbox of screen.getAllByRole("checkbox")) fireEvent.click(checkbox);
  fireEvent.click(screen.getByRole("button", { name: "Delete selected (3)" }));
  fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Delete" }));
  expect(screen.getByText(t("decision_page.data.removing"))).toBeTruthy();
  await act(async () => finish({ deleted: 1, kept: 1 }));
  expect(screen.getByText("Deleted 1. Kept 1 still in use. 1 were already gone.")).toBeTruthy();
});

test("busy list offers Retry", async () => {
  const fake = createFakeDecisionLoopApi();
  fake.fail("dataRecords", "busy");
  render(
    <DataRecords
      api={fake}
      asOf={AS_OF}
      kind="decisions"
      onKind={vi.fn()}
      topic={null}
      onTopic={vi.fn()}
      writable
      onDeleted={vi.fn()}
      onOpenContext={vi.fn()}
    />,
  );
  await screen.findByText(t("decision_page.data.busy"));
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await screen.findByText(/decisions match/);
});

test("details show decision evidence ref, suggestion what, and call answers", async () => {
  for (const kind of ["decisions", "suggestions", "calls"] as const) {
    const view = setup(kind);
    await screen.findByText(
      kind === "decisions"
        ? /decisions match/
        : kind === "suggestions"
          ? /suggestions match/
          : /model calls match/,
    );
    fireEvent.click(screen.getAllByRole("button", { name: "Details" })[0] as HTMLElement);
    await waitFor(() => expect(document.querySelector(".dloop-data-detail")).not.toBeNull());
    const dl = document.querySelector(".dloop-data-detail") as HTMLElement;
    if (kind === "decisions") {
      expect(dl.textContent).toContain("30-knowledge/seedlings.md");
      expect(dl.textContent).toContain("Due today; linked to your goal");
      expect(dl.textContent).toContain("Model request:");
    } else if (kind === "suggestions") {
      expect(dl.textContent).toContain("Two garden planner tasks are due today");
      expect(dl.textContent).toContain("Last changed");
    } else {
      expect(dl.textContent).toContain(view.fake.data.call_detail.version);
      for (const question of view.fake.data.call_detail.questions) {
        expect(dl.textContent).toContain(question.label);
        expect(dl.textContent).toContain(question.answer);
      }
    }
    view.unmount();
  }
});

test("late response from an older kind cannot replace current rows", async () => {
  const fake = createFakeDecisionLoopApi();
  const pending: { kind: DataKind; finish: (view: DataRecordsView) => void }[] = [];
  fake.dataRecords = vi.fn(
    (query) =>
      new Promise<DataRecordsView>((resolve) => {
        pending.push({ kind: query.kind, finish: resolve });
      }),
  );
  const view = render(
    <DataRecords
      api={fake}
      asOf={AS_OF}
      kind="decisions"
      onKind={vi.fn()}
      topic={null}
      onTopic={vi.fn()}
      writable
      onDeleted={vi.fn()}
      onOpenContext={vi.fn()}
    />,
  );
  expect(pending[0]?.kind).toBe("decisions");
  view.rerender(
    <DataRecords
      api={fake}
      asOf={AS_OF}
      kind="calls"
      onKind={vi.fn()}
      topic={null}
      onTopic={vi.fn()}
      writable
      onDeleted={vi.fn()}
      onOpenContext={vi.fn()}
    />,
  );
  expect(pending[1]?.kind).toBe("calls");
  await act(async () => pending[1]?.finish(fake.data.data_calls));
  await screen.findByText(/model calls match/);
  await act(async () => pending[0]?.finish(fake.data.data_decisions));
  expect(screen.getByText(/model calls match/)).toBeTruthy();
  expect(screen.queryByText(/decisions match/)).toBeNull();
});

test("selection stops at 50 and announces the limit", async () => {
  const fake = createFakeDecisionLoopApi();
  const sample = fake.data.data_decisions.rows[1];
  if (!sample) throw new Error("fixture row missing");
  const rows = Array.from({ length: 51 }, (_, index) => ({
    ...sample,
    id: `11111111-1111-4111-8111-${String(index).padStart(12, "0")}`,
  }));
  fake.dataRecords = vi.fn(async (query) => ({
    kind: "decisions" as const,
    total: 51,
    deletable: 51,
    next: query.before ? null : { at: rows[49]?.at ?? sample.at, id: rows[49]?.id ?? sample.id },
    rows: query.before ? rows.slice(50) : rows.slice(0, 50),
  }));
  render(
    <DataRecords
      api={fake}
      asOf={AS_OF}
      kind="decisions"
      onKind={vi.fn()}
      topic={null}
      onTopic={vi.fn()}
      writable
      onDeleted={vi.fn()}
      onOpenContext={vi.fn()}
    />,
  );
  await screen.findByText(/decisions match/);
  for (const checkbox of screen.getAllByRole("checkbox")) fireEvent.click(checkbox);
  fireEvent.click(screen.getByRole("button", { name: "Show 50 more" }));
  await waitFor(() => expect(screen.getAllByRole("checkbox")).toHaveLength(51));
  fireEvent.click(screen.getAllByRole("checkbox")[50] as HTMLElement);
  expect(screen.getByText(t("decision_page.data.select.limit"))).toBeTruthy();
  expect(screen.getByRole("button", { name: "Delete selected (50)" })).toBeTruthy();
});

test("decision confirmation lists every change and stay in order", async () => {
  setup();
  await screen.findByText(/decisions match/);
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "Delete selected (1)" }));
  const lists = within(screen.getByRole("alertdialog")).getAllByRole("list");
  const words = lists.map((list) =>
    within(list)
      .getAllByRole("listitem")
      .map((item) => item.textContent),
  );
  expect(words).toEqual([
    [
      "learned",
      "answers",
      "showings",
      "comparisons",
      "vault_change",
      "pauses",
      "improve",
      "history",
    ].map((key) => t(`decision_page.data.effect.${key}` as Parameters<typeof t>[0])),
    ["chats", "settings", "today"].map((key) =>
      t(`decision_page.data.stay.${key}` as Parameters<typeof t>[0]),
    ),
  ]);
});

test("delete refusal keeps confirmation open with busy error", async () => {
  const { fake } = setup("calls");
  await screen.findByText(/model calls match/);
  fake.fail("deleteData", "busy");
  fireEvent.click(screen.getAllByRole("checkbox")[0] as HTMLElement);
  fireEvent.click(screen.getByRole("button", { name: "Delete selected (1)" }));
  fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Delete" }));
  expect((await within(screen.getByRole("alertdialog")).findByRole("alert")).textContent).toBe(
    t("decision_page.data.busy"),
  );
  expect(screen.getByRole("alertdialog")).toBeTruthy();
});

test("losing write access while confirming prevents the delete", async () => {
  const { fake, rerender } = setup("calls");
  await screen.findByText(/model calls match/);
  fireEvent.click(screen.getAllByRole("checkbox")[0] as HTMLElement);
  fireEvent.click(screen.getByRole("button", { name: "Delete selected (1)" }));
  rerender(
    <DataRecords
      api={fake}
      asOf={AS_OF}
      kind="calls"
      onKind={vi.fn()}
      topic={null}
      onTopic={vi.fn()}
      writable={false}
      onDeleted={vi.fn()}
      onOpenContext={vi.fn()}
    />,
  );
  const confirm = within(screen.getByRole("alertdialog")).getByRole("button", { name: "Delete" });
  expect(confirm.getAttribute("aria-disabled")).toBe("true");
  fireEvent.click(confirm);
  expect(fake.requests.some((request) => request.method === "deleteData")).toBe(false);
});

test("decision detail pairs distinct decision and answer times with their labels", async () => {
  const fake = createFakeDecisionLoopApi();
  fake.data.data_decision.at = "2026-10-20T10:00:00.000Z";
  fake.data.data_decision.answered_at = "2026-10-21T14:00:00.000Z";
  fake.data.data_decision.answer_reason = "out_of_date";
  const { at, answered_at } = fake.data.data_decision;
  render(
    <DataRecords
      api={fake}
      asOf={AS_OF}
      kind="decisions"
      onKind={vi.fn()}
      topic={null}
      onTopic={vi.fn()}
      writable
      onDeleted={vi.fn()}
      onOpenContext={vi.fn()}
    />,
  );
  await screen.findByText(/decisions match/);
  fireEvent.click(screen.getAllByRole("button", { name: "Details" })[0] as HTMLElement);
  await waitFor(() => expect(document.querySelector(".dloop-data-detail")).not.toBeNull());
  const pairs = [...document.querySelectorAll(".dloop-data-detail > div")].map((row) => ({
    label: row.querySelector("dt")?.textContent,
    value: row.querySelector("dd")?.textContent,
  }));
  expect(pairs.filter((row) => row.label === "Decided")).toEqual([
    { label: "Decided", value: `${localDate(at)} ${localClock(at)}` },
  ]);
  const answers = pairs.filter((row) => row.label === "Your answer");
  expect(answers).toHaveLength(1);
  expect(answers[0]?.value).toContain("Useful");
  expect(answers[0]?.value).toContain("Out of date");
  expect(answers[0]?.value).toContain(
    `${localDate(answered_at ?? "")} ${localClock(answered_at ?? "")}`,
  );
});

test("unmount aborts a pending detail read", async () => {
  for (const kind of ["decisions", "calls"] as const) {
    const fake = createFakeDecisionLoopApi();
    let signal: AbortSignal | undefined;
    if (kind === "decisions")
      fake.dataRecord = vi.fn((_kind, _id, pending) => {
        signal = pending;
        return new Promise<DataRecordView>(() => {});
      });
    else
      fake.call = vi.fn((_id, pending) => {
        signal = pending;
        return new Promise<CallDetailView>(() => {});
      });
    const view = render(
      <DataRecords
        api={fake}
        asOf={AS_OF}
        kind={kind}
        onKind={vi.fn()}
        topic={null}
        onTopic={vi.fn()}
        writable
        onDeleted={vi.fn()}
        onOpenContext={vi.fn()}
      />,
    );
    await screen.findByText(kind === "calls" ? /model calls match/ : /decisions match/);
    fireEvent.click(screen.getAllByRole("button", { name: "Details" })[0] as HTMLElement);
    expect(signal?.aborted).toBe(false);
    view.unmount();
    expect(signal?.aborted).toBe(true);
  }
});

test("unmount aborts a pending next page", async () => {
  const fake = createFakeDecisionLoopApi();
  const firstPage = fake.dataRecords;
  let signal: AbortSignal | undefined;
  fake.dataRecords = vi.fn((query, pending) =>
    query.before === null
      ? firstPage(query, pending)
      : new Promise<DataRecordsView>(() => {
          signal = pending;
        }),
  );
  const view = render(
    <DataRecords
      api={fake}
      asOf={AS_OF}
      kind="decisions"
      onKind={vi.fn()}
      topic={null}
      onTopic={vi.fn()}
      writable
      onDeleted={vi.fn()}
      onOpenContext={vi.fn()}
    />,
  );
  await screen.findByText(/decisions match/);
  fireEvent.click(screen.getByRole("button", { name: "Show 50 more" }));
  expect(signal?.aborted).toBe(false);
  view.unmount();
  expect(signal?.aborted).toBe(true);
});
