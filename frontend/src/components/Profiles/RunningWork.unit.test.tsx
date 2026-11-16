import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { ModelConnectionsContext } from "../../clients/app/modelConnectionsApi";
import {
  type PreparedProfilePreview,
  ProfileLaunchApiError,
} from "../../clients/profiles/profileLaunchApi";
import type { OtherSession } from "../../protocol";
import { type RunningRow, RunningWork } from "./RunningWork";

afterEach(cleanup);

function localIso(year: number, month1: number, day: number, hour = 0, minute = 0): string {
  return new Date(year, month1 - 1, day, hour, minute).toISOString();
}

const NOW = new Date(2024, 0, 1, 12, 0).getTime();

const row = (overrides: Partial<RunningRow> = {}): RunningRow => ({
  sessionId: "s1",
  title: "Alpha",
  busy: true,
  since: localIso(2024, 1, 1, 11, 55),
  ...overrides,
});

const preview = (
  sessionId: string,
  overrides: Partial<PreparedProfilePreview> = {},
): PreparedProfilePreview => ({
  session_id: sessionId,
  profile_id: "profile",
  profile_revision: 2,
  generation: "generation",
  fields: [],
  launchable: true,
  blockers: [],
  warnings: [],
  prefs: { mode: "plan", effort: "high", model: null },
  workspace_id: "workspace-1",
  workspace_revision: 1,
  policy: "ordinary",
  policy_revision: 1,
  settings_revision: 0,
  runtime: "claude",
  capability_key: "a".repeat(64),
  draft: { key: sessionId, revision: 1, text: "", saved_at: null, attachments: [] },
  identity: {
    name: "Helper",
    avatar_asset_id: null,
    accent: "blue",
    shape: "circle",
    profile_revision: 2,
  },
  ...overrides,
});

const notFound = () => Promise.reject(new ProfileLaunchApiError(404, "not_found"));

function noop() {
  return undefined;
}

function gatedInspections() {
  const pending: { id: string; resolve: (value: PreparedProfilePreview) => void }[] = [];
  let inFlight = 0;
  let peak = 0;
  const inspect = vi.fn((id: string, _signal: AbortSignal) => {
    inFlight += 1;
    peak = Math.max(peak, inFlight);
    return new Promise<PreparedProfilePreview>((resolve) => pending.push({ id, resolve })).finally(
      () => {
        inFlight -= 1;
      },
    );
  });
  return {
    inspect,
    get peak() {
      return peak;
    },
    async completeNext(name = "Helper") {
      const request = pending.shift();
      if (!request) throw new Error("No inspection is pending");
      const result = preview(request.id);
      if (result.identity) result.identity.name = name;
      await act(async () => request.resolve(result));
    },
  };
}

it("renders exactly the given rows with title, busy/idle and since, and nothing else", async () => {
  render(
    <RunningWork
      rows={[
        row({ sessionId: "s1", title: "Alpha", busy: true, since: localIso(2024, 1, 1, 11, 55) }),
        row({ sessionId: "s2", title: "Beta", busy: false, since: localIso(2024, 1, 1, 11, 30) }),
      ]}
      others={[]}
      inspect={notFound}
      nowMs={NOW}
      onOpen={noop}
      onStop={noop}
      onClose={noop}
    />,
  );
  // Settles the not-found inspection before asserting, as ProfileSessionReview's 404 test does,
  // so a row that should stay ordinary really has: no failure status either.
  await act(async () => {});
  expect(screen.getAllByRole("listitem")).toHaveLength(2);
  const alpha = screen.getByText("Alpha").closest("li");
  const beta = screen.getByText("Beta").closest("li");
  expect(alpha?.textContent).toContain("busy");
  expect(alpha?.textContent).toContain("11:55");
  expect(beta?.textContent).toContain("idle");
  expect(beta?.textContent).toContain("11:30");
  expect(screen.queryByRole("status")).toBeNull();
});

it("a 200 inspection makes the row a profile row: name, revision chip, Profile chip and inspector", async () => {
  render(
    <RunningWork
      rows={[row()]}
      others={[]}
      inspect={() => Promise.resolve(preview("s1"))}
      nowMs={NOW}
      onOpen={noop}
      onStop={noop}
      onClose={noop}
    />,
  );
  expect(await screen.findByText("Helper")).toBeTruthy();
  const li = screen.getByText("Alpha").closest("li") as HTMLElement;
  const chips = Array.from(li.querySelectorAll(".chip")).map((el) => el.textContent);
  expect(chips).toContain("r2");
  expect(chips).toContain("Profile");
  expect(within(li).getByRole("img", { name: "Helper" })).toBeTruthy();
  const details = within(li).getByText(/Workspace workspace-1/);
  expect(details.textContent).toContain("Revision r2");
  expect(within(li).getByText(/Policy: ordinary/).textContent).toContain("Effort: high");
});

it("draws a row's bot icon when botProfileIdOf names it, and keeps the initial otherwise", async () => {
  render(
    <RunningWork
      rows={[row({ sessionId: "s1", title: "Alpha" }), row({ sessionId: "s2", title: "Beta" })]}
      others={[]}
      inspect={(sessionId) => Promise.resolve(preview(sessionId))}
      nowMs={NOW}
      onOpen={noop}
      onStop={noop}
      onClose={noop}
      botProfileIdOf={(sessionId) => (sessionId === "s1" ? "bot-profile-1" : undefined)}
    />,
  );
  await vi.waitFor(() => expect(screen.getAllByText("Helper")).toHaveLength(2));
  const alpha = screen.getByText("Alpha").closest("li") as HTMLElement;
  const beta = screen.getByText("Beta").closest("li") as HTMLElement;
  expect(alpha.querySelector("svg.bot-icon")).not.toBeNull();
  expect(alpha.querySelector(".avatar-initials")).toBeNull();
  expect(beta.querySelector("svg.bot-icon")).toBeNull();
  expect(beta.querySelector(".avatar-initials")?.textContent).toBe("H");
});

it("a 404 keeps the row ordinary: no chip, no failure message", async () => {
  const inspect = vi.fn(notFound);
  render(
    <RunningWork
      rows={[row()]}
      others={[]}
      inspect={inspect}
      nowMs={NOW}
      onOpen={noop}
      onStop={noop}
      onClose={noop}
    />,
  );
  // Settles the rejection itself (not merely that inspect was called) before asserting, as
  // ProfileSessionReview's 404 test does.
  await act(async () => {});
  expect(inspect).toHaveBeenCalledTimes(1);
  const li = screen.getByText("Alpha").closest("li") as HTMLElement;
  expect(li.querySelector(".chip")).toBeNull();
  expect(screen.queryByRole("status")).toBeNull();
});

it("another inspection failure shows the unavailable message", async () => {
  render(
    <RunningWork
      rows={[row()]}
      others={[]}
      inspect={() => Promise.reject(new Error("boom"))}
      nowMs={NOW}
      onOpen={noop}
      onStop={noop}
      onClose={noop}
    />,
  );
  expect((await screen.findByRole("status")).textContent).toBe("Profile details are unavailable.");
});

it("Open, Stop and Close each call their own callback once with the row's id", () => {
  const onOpen = vi.fn();
  const onStop = vi.fn();
  const onClose = vi.fn();
  render(
    <RunningWork
      rows={[row({ sessionId: "s7", busy: true })]}
      others={[]}
      inspect={notFound}
      nowMs={NOW}
      onOpen={onOpen}
      onStop={onStop}
      onClose={onClose}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Open" }));
  fireEvent.click(screen.getByRole("button", { name: "Stop" }));
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(onOpen).toHaveBeenCalledExactlyOnceWith("s7");
  expect(onStop).toHaveBeenCalledExactlyOnceWith("s7");
  expect(onClose).toHaveBeenCalledExactlyOnceWith("s7");
});

it("Stop is disabled when the row is idle", () => {
  render(
    <RunningWork
      rows={[row({ busy: false })]}
      others={[]}
      inspect={notFound}
      nowMs={NOW}
      onOpen={noop}
      onStop={noop}
      onClose={noop}
    />,
  );
  expect((screen.getByRole("button", { name: "Stop" }) as HTMLButtonElement).disabled).toBe(true);
});

it("others are read-only: entrypoint and title, no actions", () => {
  const other: OtherSession = {
    session_id: "o1",
    entrypoint: "cli",
    pid: 4242,
    title: "Elsewhere",
  };
  render(
    <RunningWork
      rows={[]}
      others={[other]}
      inspect={notFound}
      nowMs={NOW}
      onOpen={noop}
      onStop={noop}
      onClose={noop}
    />,
  );
  const li = screen.getByText("Elsewhere").closest("li") as HTMLElement;
  expect(li.textContent).toContain("cli");
  expect(within(li).queryAllByRole("button")).toHaveLength(0);
});

it("no rows and no others: the empty line", () => {
  render(
    <RunningWork
      rows={[]}
      others={[]}
      inspect={notFound}
      nowMs={NOW}
      onOpen={noop}
      onStop={noop}
      onClose={noop}
    />,
  );
  expect(screen.getByText("No running work")).toBeTruthy();
});

it("inspects each id at most once per mount, even across a re-render with a new rows array", async () => {
  const inspect = vi.fn(notFound);
  const { rerender } = render(
    <RunningWork
      rows={[row()]}
      others={[]}
      inspect={inspect}
      nowMs={NOW}
      onOpen={noop}
      onStop={noop}
      onClose={noop}
    />,
  );
  await vi.waitFor(() => expect(inspect).toHaveBeenCalledTimes(1));
  rerender(
    <RunningWork
      rows={[row()]}
      others={[]}
      inspect={inspect}
      nowMs={NOW + 1000}
      onOpen={noop}
      onStop={noop}
      onClose={noop}
    />,
  );
  expect(inspect).toHaveBeenCalledTimes(1);
});

it("bounds inspection to at most 3 rows in one mount", async () => {
  const inspect = vi.fn(notFound);
  render(
    <RunningWork
      rows={[
        row({ sessionId: "s1" }),
        row({ sessionId: "s2" }),
        row({ sessionId: "s3" }),
        row({ sessionId: "s4" }),
      ]}
      others={[]}
      inspect={inspect}
      nowMs={NOW}
      onOpen={noop}
      onStop={noop}
      onClose={noop}
    />,
  );
  await vi.waitFor(() => expect(inspect).toHaveBeenCalledTimes(3));
  expect(inspect).not.toHaveBeenCalledWith("s4", expect.anything());
});

it("inspects eight advertised rows once per id and routes the eighth row's actions", async () => {
  const rows = Array.from({ length: 8 }, (_, i) =>
    row({ sessionId: `s${i + 1}`, title: `Session ${i + 1}` }),
  );
  const inspect = vi.fn((_id: string, _signal: AbortSignal) => notFound());
  const onOpen = vi.fn();
  const onStop = vi.fn();
  const onClose = vi.fn();
  const props = { rows, others: [], inspect, nowMs: NOW, onOpen, onStop, onClose };
  const { rerender } = render(<RunningWork {...props} maxLiveSessions={8} />);
  await act(async () => {});
  expect(inspect.mock.calls.map(([id]) => id)).toEqual(rows.map((item) => item.sessionId));
  rerender(<RunningWork {...props} rows={[...rows]} maxLiveSessions={8} />);
  await act(async () => {});
  expect(inspect).toHaveBeenCalledTimes(8);
  const eighth = screen.getByText("Session 8").closest("li") as HTMLElement;
  fireEvent.click(within(eighth).getByRole("button", { name: "Open" }));
  fireEvent.click(within(eighth).getByRole("button", { name: "Stop" }));
  fireEvent.click(within(eighth).getByRole("button", { name: "Close" }));
  expect(onOpen).toHaveBeenCalledExactlyOnceWith("s8");
  expect(onStop).toHaveBeenCalledExactlyOnceWith("s8");
  expect(onClose).toHaveBeenCalledExactlyOnceWith("s8");
});

it("a cap increase inspects the remaining ids when rows stay the same", async () => {
  const rows = Array.from({ length: 8 }, (_, i) => row({ sessionId: `s${i + 1}` }));
  const inspect = vi.fn((_id: string, _signal: AbortSignal) => notFound());
  const props = {
    rows,
    others: [],
    inspect,
    nowMs: NOW,
    onOpen: noop,
    onStop: noop,
    onClose: noop,
  };
  const { rerender } = render(<RunningWork {...props} />);
  await act(async () => {});
  expect(inspect).toHaveBeenCalledTimes(3);
  rerender(<RunningWork {...props} maxLiveSessions={8} />);
  await act(async () => {});
  expect(inspect.mock.calls.map(([id]) => id)).toEqual(rows.map((item) => item.sessionId));
});

it("resolves all eight identities without exceeding the shared four-read limit", async () => {
  const rows = Array.from({ length: 8 }, (_, i) => row({ sessionId: `s${i + 1}` }));
  const gated = gatedInspections();
  render(
    <RunningWork
      rows={rows}
      maxLiveSessions={8}
      others={[]}
      inspect={gated.inspect}
      nowMs={NOW}
      onOpen={noop}
      onStop={noop}
      onClose={noop}
    />,
  );
  expect(gated.peak).toBeLessThanOrEqual(4);
  for (const _row of rows) await gated.completeNext();
  expect(gated.peak).toBeLessThanOrEqual(4);
  expect(gated.inspect.mock.calls.map(([id]) => id)).toEqual(rows.map((item) => item.sessionId));
  expect(screen.getAllByText("Helper")).toHaveLength(8);
  expect(screen.queryByRole("status")).toBeNull();
});

it("cap growth and row churn keep one read active and remove departed queued ids", async () => {
  const rows = Array.from({ length: 8 }, (_, i) => row({ sessionId: `s${i + 1}` }));
  const gated = gatedInspections();
  const props = {
    rows,
    others: [],
    inspect: gated.inspect,
    nowMs: NOW,
    onOpen: noop,
    onStop: noop,
    onClose: noop,
  };
  const { rerender } = render(<RunningWork {...props} />);
  rerender(<RunningWork {...props} maxLiveSessions={8} />);
  const replacements = Array.from({ length: 8 }, (_, i) => row({ sessionId: `s${i + 4}` }));
  rerender(<RunningWork {...props} rows={replacements} maxLiveSessions={8} />);
  const current = [rows[0] as RunningRow, ...replacements.slice(0, 7)];
  rerender(<RunningWork {...props} rows={current} maxLiveSessions={8} />);
  rerender(<RunningWork {...props} rows={[...current]} maxLiveSessions={8} />);
  expect(gated.inspect).toHaveBeenCalledTimes(1);
  for (const _row of current) await gated.completeNext();
  expect(gated.peak).toBe(1);
  expect(gated.inspect.mock.calls.map(([id]) => id)).toEqual(current.map((item) => item.sessionId));
  expect(screen.getAllByText("Helper")).toHaveLength(8);
});

it("unmount aborts the active read and stops queued dispatch after a late completion", async () => {
  const gated = gatedInspections();
  const { unmount } = render(
    <RunningWork
      rows={Array.from({ length: 8 }, (_, i) => row({ sessionId: `s${i + 1}` }))}
      maxLiveSessions={8}
      others={[]}
      inspect={gated.inspect}
      nowMs={NOW}
      onOpen={noop}
      onStop={noop}
      onClose={noop}
    />,
  );
  expect(gated.inspect).toHaveBeenCalledTimes(1);
  const signal = gated.inspect.mock.calls[0]?.[1];
  expect(signal?.aborted).toBe(false);
  unmount();
  expect(signal?.aborted).toBe(true);
  await gated.completeNext();
  expect(gated.inspect).toHaveBeenCalledTimes(1);
});

it("StrictMode discards the old queue and ignores its late identity", async () => {
  const rows = Array.from({ length: 8 }, (_, i) => row({ sessionId: `s${i + 1}` }));
  const gated = gatedInspections();
  render(
    <StrictMode>
      <RunningWork
        rows={rows}
        maxLiveSessions={8}
        others={[]}
        inspect={gated.inspect}
        nowMs={NOW}
        onOpen={noop}
        onStop={noop}
        onClose={noop}
      />
    </StrictMode>,
  );
  expect(gated.inspect).toHaveBeenCalledTimes(2);
  expect(gated.inspect.mock.calls[0]?.[1].aborted).toBe(true);
  expect(gated.inspect.mock.calls[1]?.[1].aborted).toBe(false);
  await gated.completeNext("Discarded");
  expect(screen.queryByText("Discarded")).toBeNull();
  expect(gated.inspect).toHaveBeenCalledTimes(2);
  for (const _row of rows) await gated.completeNext();
  expect(gated.inspect.mock.calls.slice(1).map(([id]) => id)).toEqual(
    rows.map((item) => item.sessionId),
  );
  expect(screen.getAllByText("Helper")).toHaveLength(8);
  expect(gated.peak).toBeLessThanOrEqual(4);
});

it("re-reads under StrictMode's development-only remount: a resolved identity still renders", async () => {
  render(
    <StrictMode>
      <RunningWork
        rows={[row()]}
        others={[]}
        inspect={() => Promise.resolve(preview("s1"))}
        nowMs={NOW}
        onOpen={noop}
        onStop={noop}
        onClose={noop}
      />
    </StrictMode>,
  );
  expect(await screen.findByText("Helper")).toBeTruthy();
});

it("inspects a fourth distinct id once an earlier live id leaves rows", async () => {
  const inspect = vi.fn(notFound);
  const { rerender } = render(
    <RunningWork
      rows={[row({ sessionId: "s1" }), row({ sessionId: "s2" }), row({ sessionId: "s3" })]}
      others={[]}
      inspect={inspect}
      nowMs={NOW}
      onOpen={noop}
      onStop={noop}
      onClose={noop}
    />,
  );
  await vi.waitFor(() => expect(inspect).toHaveBeenCalledTimes(3));
  rerender(
    <RunningWork
      rows={[row({ sessionId: "s2" }), row({ sessionId: "s3" }), row({ sessionId: "s4" })]}
      others={[]}
      inspect={inspect}
      nowMs={NOW}
      onOpen={noop}
      onStop={noop}
      onClose={noop}
    />,
  );
  await vi.waitFor(() => expect(inspect).toHaveBeenCalledWith("s4", expect.anything()));
});

it("aborts every outstanding read on unmount", async () => {
  let signal: AbortSignal | undefined;
  const inspect = vi.fn((_id: string, abortSignal: AbortSignal) => {
    signal = abortSignal;
    return new Promise<PreparedProfilePreview>(() => undefined);
  });
  const { unmount } = render(
    <RunningWork
      rows={[row()]}
      others={[]}
      inspect={inspect}
      nowMs={NOW}
      onOpen={noop}
      onStop={noop}
      onClose={noop}
    />,
  );
  expect(signal?.aborted).toBe(false);
  unmount();
  expect(signal?.aborted).toBe(true);
});

it("labels the model: Claude Code's default for null, the model for a ref, never the id", async () => {
  const props = { others: [], nowMs: NOW, onOpen: noop, onStop: noop, onClose: noop };
  const { container } = render(
    <RunningWork {...props} rows={[row()]} inspect={() => Promise.resolve(preview("s1"))} />,
  );
  await screen.findByText("Helper");
  expect(container.textContent).toContain("Model: Claude Code's default.");
  expect(container.textContent).not.toContain("inherited");
  cleanup();
  const pinned = preview("s2", {
    prefs: {
      mode: "plan",
      effort: "high",
      model: "viki-llm/11111111-2222-4333-8444-555555555555/gpt-x",
    },
  });
  render(
    <RunningWork
      {...props}
      rows={[row({ sessionId: "s2" })]}
      inspect={() => Promise.resolve(pinned)}
    />,
  );
  await screen.findByText("Helper");
  expect(document.body.textContent).toContain("Model: gpt-x.");
  expect(document.body.textContent).not.toContain("11111111-2222-4333-8444-555555555555");
});

it("names the saved connection beside the model once the list loads, never the ref id", async () => {
  const ref = "viki-llm/11111111-2222-4333-8444-555555555555/gpt-x";
  const list = vi.fn().mockResolvedValue({
    revision: 1,
    default_model: null,
    connections: [
      { id: "c", model_ref: ref, name: "Work gateway", model: "gpt-x", has_api_key: true },
    ],
  });
  const pinned = preview("s2", { prefs: { mode: "plan", effort: "high", model: ref } });
  render(
    <ModelConnectionsContext.Provider value={{ list } as never}>
      <RunningWork
        rows={[row({ sessionId: "s2" })]}
        others={[]}
        inspect={() => Promise.resolve(pinned)}
        nowMs={NOW}
        onOpen={noop}
        onStop={noop}
        onClose={noop}
      />
    </ModelConnectionsContext.Provider>,
  );
  await vi.waitFor(() =>
    expect(document.body.textContent).toContain("Model: Work gateway · gpt-x."),
  );
  expect(document.body.textContent).not.toContain("11111111-2222-4333-8444-555555555555");
});
