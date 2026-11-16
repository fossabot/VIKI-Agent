import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { createFakeDecisionLoopApi, decisionLoopExamples } from "../../clients/decisionLoop/fake";
import type { CallDetailView, CallsView } from "../../clients/decisionLoop/types";
import { DecisionTab, type DecisionTabApi } from "./DecisionTab";

afterEach(cleanup);

// The default fixture's three calls (examples.json, newest first): "Water the seedlings" (open
// for OPEN_CHAT), "Trip budget" and "Inbox", neither of the last two tied to any chat.
const CALL_1_ID = "d2df3dad-403c-4e31-a5f0-1f07efb73934";
const CALL_1_AT = "2026-10-21T14:02:00.000Z";
const CALL_2_ID = "e5d94faa-166d-47dd-be3c-08390a4d43d8";
const CALL_2_AT = "2026-10-21T13:10:00.000Z";
const CALL_3_AT = "2026-10-21T12:30:00.000Z";
const OPEN_CHAT = "fe07f4bf-5447-4a8d-9d58-b68385409b99";

// Computed the same way the component reads a call's time (local HH:MM, DecisionLoop/wording.ts's
// `localClock`), so no assertion here depends on the test runner's own timezone.
function clock(iso: string): string {
  const at = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(at.getHours())}:${pad(at.getMinutes())}`;
}

function neverCallsApi(): DecisionTabApi {
  return { calls: vi.fn(() => new Promise<CallsView>(() => {})), call: vi.fn() };
}

it("shows Decision is off, and calls no route, while disabled", () => {
  const api = createFakeDecisionLoopApi();
  render(<DecisionTab api={api} openChatId={null} enabled={false} />);
  expect(screen.getByText("Decision is off")).toBeTruthy();
  expect(
    screen.getByRole("heading", { name: "Decision model context window", level: 2 }),
  ).toBeTruthy();
  expect(api.requests).toEqual([]);
});

it("shows the loading kit while the calls list is in flight", () => {
  render(<DecisionTab api={neverCallsApi()} openChatId={null} enabled={true} />);
  expect(screen.getByText("Loading your decisions…")).toBeTruthy();
});

it("shows No decisions yet when nothing has ever been decided", async () => {
  const api = createFakeDecisionLoopApi({ calls: { enabled: true, latest: null, calls: [] } });
  render(<DecisionTab api={api} openChatId={null} enabled={true} />);
  expect(await screen.findByText("No decisions yet")).toBeTruthy();
});

it("shows Rules only: no model call when rules decided last and no call exists", async () => {
  const api = createFakeDecisionLoopApi({ calls: { enabled: true, latest: "rules", calls: [] } });
  render(<DecisionTab api={api} openChatId={null} enabled={true} />);
  expect(await screen.findByText("Rules only: no model call")).toBeTruthy();
});

it("never marks a call this chat when no chat is open, even if the call has no session either", async () => {
  const api = createFakeDecisionLoopApi();
  render(<DecisionTab api={api} openChatId={null} enabled={true} />);
  const picker = (await screen.findByRole("combobox", { name: "Call" })) as HTMLSelectElement;
  expect(Array.from(picker.options).map((option) => option.textContent)).toEqual([
    `${clock(CALL_1_AT)} · Water the seedlings · 3,912 tokens`,
    `${clock(CALL_2_AT)} · Trip budget · 3,488 tokens`,
    `${clock(CALL_3_AT)} · Inbox · 2,204 tokens`,
  ]);
});

it("shows the newest call's bars, facts, split, questions and what was sent, matching the plan's example", async () => {
  const api = createFakeDecisionLoopApi();
  const { container } = render(<DecisionTab api={api} openChatId={OPEN_CHAT} enabled={true} />);

  expect(
    screen.getByRole("heading", { name: "Decision model context window", level: 2 }),
  ).toBeTruthy();

  const picker = (await screen.findByRole("combobox", { name: "Call" })) as HTMLSelectElement;
  expect(picker.value).toBe(CALL_1_ID);
  expect(Array.from(picker.options).map((option) => option.textContent)).toEqual([
    `${clock(CALL_1_AT)} · Water the seedlings · 3,912 tokens · this chat`,
    `${clock(CALL_2_AT)} · Trip budget · 3,488 tokens`,
    `${clock(CALL_3_AT)} · Inbox · 2,204 tokens`,
  ]);

  expect(await screen.findByText("Request: 3,912 of 64,000 tokens (6%) · measured")).toBeTruthy();
  expect(screen.getByText("Largest question: 2,870 of 32,000 tokens (9%)")).toBeTruthy();
  expect(
    screen.getByText("Version 1.14.0, pinned · answered in 112 ms · $0.00016 · Long context: off"),
  ).toBeTruthy();
  expect(screen.getByText("split estimated")).toBeTruthy();
  expect(screen.getByText("candidate · 1,104 tokens")).toBeTruthy();
  expect(screen.getByText("questions · 1,460 tokens")).toBeTruthy();
  expect(screen.getByText("Linked to a goal?")).toBeTruthy();
  expect(screen.getByText("Yes, 0.91")).toBeTruthy();
  expect(screen.getByText("Question playbook 1")).toBeTruthy();
  expect(screen.getByRole("heading", { name: "What was sent", level: 3 })).toBeTruthy();
  expect(screen.getByText("4 items held back by your never-send list")).toBeTruthy();

  const pre = container.querySelector(".context-decision-pre");
  expect(pre?.textContent).toContain("Water the seedlings before noon");

  const bars = container.querySelectorAll(".context-bar");
  expect(bars).toHaveLength(2);
  expect(bars[0]?.getAttribute("aria-label")).toBe(
    "Request: 3,912 of 64,000 tokens (6%) · measured",
  );
  expect(bars[1]?.getAttribute("aria-label")).toBe("Largest question: 2,870 of 32,000 tokens (9%)");
});

it("selecting a different call in the picker requests its own detail", async () => {
  const api = createFakeDecisionLoopApi();
  render(<DecisionTab api={api} openChatId={null} enabled={true} />);
  await screen.findByText("Question playbook 1");

  fireEvent.change(screen.getByRole("combobox", { name: "Call" }), {
    target: { value: CALL_2_ID },
  });

  expect(api.requests[api.requests.length - 1]).toEqual({ method: "call", args: [CALL_2_ID] });
});

it("shows the plain failure in words instead of answers, for a failed call", async () => {
  const api = createFakeDecisionLoopApi({
    call_detail: {
      ...decisionLoopExamples().call_detail,
      outcome: "failed",
      failure: "overloaded",
    },
  });
  render(<DecisionTab api={api} openChatId={null} enabled={true} />);

  expect(await screen.findByText("The Decision model is busy. Try again later.")).toBeTruthy();
  expect(screen.queryByText("Linked to a goal?")).toBeNull();
  // The rest of the call still shows: only the answers are replaced.
  expect(
    screen.getByText("Version 1.14.0, pinned · answered in 112 ms · $0.00016 · Long context: off"),
  ).toBeTruthy();
  expect(screen.getByRole("heading", { name: "What was sent", level: 3 })).toBeTruthy();
});

it("omits the held-back note when nothing was held back", async () => {
  const api = createFakeDecisionLoopApi({
    call_detail: { ...decisionLoopExamples().call_detail, held_back: 0 },
  });
  render(<DecisionTab api={api} openChatId={null} enabled={true} />);
  await screen.findByText("Question playbook 1");
  expect(screen.queryByText(/items held back/)).toBeNull();
});

it.each([
  [0.000003, "< $0.00001"],
  [0.00001, "$0.00001"],
  [0.1, "$0.1"],
  [2, "$2"],
  [0, "$0"],
] as const)("formats a cost of %s as %s in the facts line", async (cost, costText) => {
  const api = createFakeDecisionLoopApi({
    call_detail: { ...decisionLoopExamples().call_detail, cost_usd: cost },
  });
  render(<DecisionTab api={api} openChatId={null} enabled={true} />);
  expect(
    await screen.findByText(
      `Version 1.14.0, pinned · answered in 112 ms · ${costText} · Long context: off`,
    ),
  ).toBeTruthy();
});

it.each([
  ["pinned", "pinned"],
  ["latest", "latest stable"],
  ["preview", "preview"],
] as const)("shows a version_setting of %s as %s", async (setting, word) => {
  const api = createFakeDecisionLoopApi({
    call_detail: { ...decisionLoopExamples().call_detail, version_setting: setting },
  });
  render(<DecisionTab api={api} openChatId={null} enabled={true} />);
  expect(
    await screen.findByText(
      `Version 1.14.0, ${word} · answered in 112 ms · $0.00016 · Long context: off`,
    ),
  ).toBeTruthy();
});

it("Copy puts the exact state sent on the clipboard and shows Copied", async () => {
  const written: string[] = [];
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: {
      writeText: (text: string) => {
        written.push(text);
        return Promise.resolve();
      },
    },
  });
  const api = createFakeDecisionLoopApi();
  render(<DecisionTab api={api} openChatId={null} enabled={true} />);
  await screen.findByText("Question playbook 1");

  fireEvent.click(screen.getByRole("button", { name: "Copy what was sent" }));
  expect(await screen.findByText("Copied")).toBeTruthy();
  expect(written).toEqual([decisionLoopExamples().call_detail.state_text]);
});

it("drops a stale detail answer for a call no longer selected", async () => {
  let resolveFirst!: (value: CallDetailView) => void;
  const detailWith = (playbookVersion: string): CallDetailView => ({
    ...decisionLoopExamples().call_detail,
    playbook_version: playbookVersion,
  });
  const api: DecisionTabApi = {
    calls: vi.fn().mockResolvedValue(decisionLoopExamples().calls),
    call: vi.fn((callId: string) =>
      callId === CALL_1_ID
        ? new Promise<CallDetailView>((resolve) => {
            resolveFirst = resolve;
          })
        : Promise.resolve(detailWith("second")),
    ),
  };
  render(<DecisionTab api={api} openChatId={null} enabled={true} />);
  await screen.findByText("Loading this call…");

  fireEvent.change(screen.getByRole("combobox", { name: "Call" }), {
    target: { value: CALL_2_ID },
  });
  await screen.findByText("Question playbook second");

  await act(async () => resolveFirst(detailWith("first")));
  expect(screen.queryByText("Question playbook first")).toBeNull();
  expect(screen.getByText("Question playbook second")).toBeTruthy();
});

it("aborts the in-flight detail fetch on unmount without a React warning", async () => {
  let resolveDetail!: (value: CallDetailView) => void;
  const api: DecisionTabApi = {
    calls: vi.fn().mockResolvedValue(decisionLoopExamples().calls),
    call: vi.fn(
      () =>
        new Promise<CallDetailView>((resolve) => {
          resolveDetail = resolve;
        }),
    ),
  };
  const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
  const { unmount } = render(<DecisionTab api={api} openChatId={null} enabled={true} />);
  await screen.findByText("Loading this call…");

  unmount();
  await act(async () => resolveDetail(decisionLoopExamples().call_detail));

  expect(errorSpy).not.toHaveBeenCalled();
  errorSpy.mockRestore();
});

it("shows a generic failure for the calls list, and Try again recovers", async () => {
  const api = createFakeDecisionLoopApi();
  api.fail("calls", "unavailable");
  render(<DecisionTab api={api} openChatId={null} enabled={true} />);
  expect(await screen.findByText("Could not load your decisions.")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(await screen.findByText("Question playbook 1")).toBeTruthy();
});

it("shows a generic failure for a call's detail, and Try again recovers", async () => {
  const api = createFakeDecisionLoopApi();
  api.fail("call", "unavailable");
  render(<DecisionTab api={api} openChatId={null} enabled={true} />);
  expect(await screen.findByText("Could not load this call.")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(await screen.findByText("Question playbook 1")).toBeTruthy();
});

// "See why" names the call its answers came from, via `requestedCall`.
it("selects the requested call once the calls list loads", async () => {
  const api = createFakeDecisionLoopApi();
  render(
    <DecisionTab
      api={api}
      openChatId={null}
      enabled={true}
      requestedCall={{ callId: CALL_2_ID, request: 1 }}
    />,
  );
  const picker = (await screen.findByRole("combobox", { name: "Call" })) as HTMLSelectElement;
  expect(picker.value).toBe(CALL_2_ID);
  await act(async () => {});
  expect(api.requests).toContainEqual({ method: "call", args: [CALL_2_ID] });
});

it("falls back to the newest call when the requested id is not listed", async () => {
  const api = createFakeDecisionLoopApi();
  render(
    <DecisionTab
      api={api}
      openChatId={null}
      enabled={true}
      requestedCall={{ callId: "00000000-0000-4000-8000-000000000000", request: 1 }}
    />,
  );
  const picker = (await screen.findByRole("combobox", { name: "Call" })) as HTMLSelectElement;
  expect(picker.value).toBe(CALL_1_ID);
});

it("a fresh request for the same call re-selects it, even after the user picked another", async () => {
  const api = createFakeDecisionLoopApi();
  const { rerender } = render(
    <DecisionTab
      api={api}
      openChatId={null}
      enabled={true}
      requestedCall={{ callId: CALL_2_ID, request: 1 }}
    />,
  );
  const picker = (await screen.findByRole("combobox", { name: "Call" })) as HTMLSelectElement;
  expect(picker.value).toBe(CALL_2_ID);

  fireEvent.change(picker, { target: { value: CALL_1_ID } });
  expect(picker.value).toBe(CALL_1_ID);

  rerender(
    <DecisionTab
      api={api}
      openChatId={null}
      enabled={true}
      requestedCall={{ callId: CALL_2_ID, request: 2 }}
    />,
  );
  expect(picker.value).toBe(CALL_2_ID);
});
