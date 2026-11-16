import { act, cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { type ReactElement, useMemo, useState } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { type RunsApi, RunsApiError } from "../../clients/conversations/runsApi";
import { RunStop, STOP_CONFIRM_MS, type StopPhase, useRunStops } from "./RunStop";

interface Deferred {
  resolve: () => void;
  reject: (error: unknown) => void;
}

interface Call {
  sessionId: string;
  blockId: string;
}

interface Props {
  keys: ReadonlySet<string>;
}

function fakeApi(): { api: RunsApi; calls: Call[]; pending: Deferred[] } {
  const calls: Call[] = [];
  const pending: Deferred[] = [];
  const api: RunsApi = {
    async requests() {
      throw new Error("requests() is not used by useRunStops");
    },
    stop(sessionId, blockId) {
      calls.push({ sessionId, blockId });
      return new Promise<void>((resolve, reject) => pending.push({ resolve, reject }));
    },
  };
  return { api, calls, pending };
}

/** Renders the hook behind a tiny wrapper so every test shares the same typed rerender(). */
function renderStops(api: RunsApi | undefined, initialProps: Props, now?: () => number) {
  return renderHook(({ keys }: Props) => useRunStops(api, keys, now), { initialProps });
}

async function settle(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

// --- useRunStops -------------------------------------------------------------------------

test("a fresh key is idle until asked", () => {
  const { result } = renderStops(undefined, { keys: new Set(["k1"]) });
  expect(result.current.phase("k1")).toBeUndefined();
});

test("ask opens confirm; cancel returns to idle", () => {
  const { result } = renderStops(undefined, { keys: new Set(["k1"]) });
  act(() => result.current.ask("k1"));
  expect(result.current.phase("k1")).toEqual({ kind: "confirm" });
  act(() => result.current.cancel("k1"));
  // Explicitly idle, not the same "no entry at all" `undefined` a fresh key starts as: see
  // RunStop's own module doc for why the two render alike but only one autofocuses.
  expect(result.current.phase("k1")).toEqual({ kind: "idle" });
});

test("send moves to sending and calls stop once with the session and block ids", () => {
  const { api, calls } = fakeApi();
  const { result } = renderStops(api, { keys: new Set(["k1"]) });
  act(() => result.current.send("k1", "s1", "b1"));
  expect(result.current.phase("k1")).toEqual({ kind: "sending" });
  expect(calls).toEqual([{ sessionId: "s1", blockId: "b1" }]);
});

test("a resolved send becomes sent, stamped by `now`", async () => {
  const { api, pending } = fakeApi();
  const { result } = renderStops(api, { keys: new Set(["k1"]) }, () => 555);
  act(() => result.current.send("k1", "s1", "b1"));
  pending[0]?.resolve();
  await settle();
  expect(result.current.phase("k1")).toEqual({ kind: "sent", at: 555 });
});

test("a send already in flight for a key is not sent twice", () => {
  const { api, calls } = fakeApi();
  const { result } = renderStops(api, { keys: new Set(["k1"]) });
  act(() => {
    result.current.send("k1", "s1", "b1");
    result.current.send("k1", "s1", "b1");
  });
  expect(calls).toHaveLength(1);
});

test("a rejected send with a RunsApiError becomes failed with its code", async () => {
  const { api, pending } = fakeApi();
  const { result } = renderStops(api, { keys: new Set(["k1"]) });
  act(() => result.current.send("k1", "s1", "b1"));
  pending[0]?.reject(new RunsApiError("timeout"));
  await settle();
  expect(result.current.phase("k1")).toEqual({ kind: "failed", code: "timeout" });
});

test("a rejected send with an unrecognised error fails as unavailable", async () => {
  const { api, pending } = fakeApi();
  const { result } = renderStops(api, { keys: new Set(["k1"]) });
  act(() => result.current.send("k1", "s1", "b1"));
  pending[0]?.reject(new Error("boom"));
  await settle();
  expect(result.current.phase("k1")).toEqual({ kind: "failed", code: "unavailable" });
});

test("with no api, send does nothing", () => {
  const { result } = renderStops(undefined, { keys: new Set(["k1"]) });
  act(() => result.current.send("k1", "s1", "b1"));
  expect(result.current.phase("k1")).toBeUndefined();
});

test("a key that leaves runningKeys drops its phase", () => {
  const { result, rerender } = renderStops(undefined, { keys: new Set(["k1"]) });
  act(() => result.current.ask("k1"));
  expect(result.current.phase("k1")).toEqual({ kind: "confirm" });

  rerender({ keys: new Set() });
  expect(result.current.phase("k1")).toBeUndefined();
});

test("a send that settles after its key left runningKeys is ignored", async () => {
  const { api, pending } = fakeApi();
  const { result, rerender } = renderStops(api, { keys: new Set(["k1"]) });
  act(() => result.current.send("k1", "s1", "b1"));

  rerender({ keys: new Set() });
  pending[0]?.resolve();
  await settle();

  expect(result.current.phase("k1")).toBeUndefined();
});

test("a send that settles after unmount changes nothing and raises nothing", async () => {
  const { api, pending } = fakeApi();
  const { result, unmount } = renderStops(api, { keys: new Set(["k1"]) });
  act(() => result.current.send("k1", "s1", "b1"));

  unmount();
  pending[0]?.resolve();
  await settle(); // proves only that no "update after unmount" warning/throw happens
});

test("the default `now` reads the real (faked) clock", async () => {
  vi.setSystemTime(4321);
  const { api, pending } = fakeApi();
  const { result } = renderHook(() => useRunStops(api, new Set(["k1"])));
  act(() => result.current.send("k1", "s1", "b1"));
  pending[0]?.resolve();
  await settle();
  expect(result.current.phase("k1")).toEqual({ kind: "sent", at: 4321 });
});

// --- RunStop -------------------------------------------------------------------------------

/** Wires RunStop to a tiny local phase so a click can drive a real transition, the same as the
 * real AgentRuns+useRunStops pairing — proving focus moves only on the user's own click, not
 * the mount-time phase. `onCancel`/`onSend` default to the
 * same idle/sending transitions `useRunStops` would make; a test overrides either with a spy
 * when it only needs to check wiring, not a further transition. */
function Harness({
  onCancel,
  onSend,
}: {
  onCancel?: () => void;
  onSend?: () => void;
}): ReactElement {
  const [phase, setPhase] = useState<StopPhase | undefined>(undefined);
  return (
    <RunStop
      agent="Scout"
      chat="Research"
      phase={phase}
      nowMs={0}
      onAsk={() => setPhase({ kind: "confirm" })}
      onCancel={onCancel ?? (() => setPhase({ kind: "idle" }))}
      onSend={onSend ?? (() => setPhase({ kind: "sending" }))}
    />
  );
}

/** Like `Harness`, but backed by the real `useRunStops` hook so a send can resolve into a
 * stamped "sent" — needed to drive a stale "sent" for the Try again / probe G test below. */
function StopsHarness({
  api,
  nowMs,
  now,
}: {
  api: RunsApi;
  nowMs: number;
  now?: () => number;
}): ReactElement {
  const keys = useMemo(() => new Set(["k1"]), []);
  const stops = useRunStops(api, keys, now);
  return (
    <RunStop
      agent="Scout"
      chat="Research"
      phase={stops.phase("k1")}
      nowMs={nowMs}
      onAsk={() => stops.ask("k1")}
      onCancel={() => stops.cancel("k1")}
      onSend={() => stops.send("k1", "s1", "b1")}
    />
  );
}

test("idle shows a Stop button that asks", () => {
  const onAsk = vi.fn();
  render(
    <RunStop
      agent="Scout"
      chat="Research"
      phase={undefined}
      nowMs={0}
      onAsk={onAsk}
      onCancel={vi.fn()}
      onSend={vi.fn()}
    />,
  );
  const button = screen.getByRole("button", { name: "Stop Scout in Research" });
  expect(button.className).toBe("run-stop");
  expect(button.textContent).toBe("Stop");
  fireEvent.click(button);
  expect(onAsk).toHaveBeenCalledTimes(1);
});

test("confirm asks to stop, focuses Keep running, and wires Stop run/Keep running/Escape", () => {
  const onCancel = vi.fn();
  const onSend = vi.fn();
  const { container } = render(<Harness onCancel={onCancel} onSend={onSend} />);
  fireEvent.click(screen.getByRole("button", { name: "Stop Scout in Research" }));
  expect(screen.getByText("Stop Scout now?")).toBeTruthy();
  const keep = screen.getByRole("button", { name: "Keep running" });
  expect(document.activeElement).toBe(keep);

  fireEvent.click(screen.getByRole("button", { name: "Stop run" }));
  expect(onSend).toHaveBeenCalledTimes(1);

  fireEvent.click(keep);
  expect(onCancel).toHaveBeenCalledTimes(1);

  fireEvent.keyDown(container.querySelector(".run-stop-confirm") as Element, { key: "Escape" });
  expect(onCancel).toHaveBeenCalledTimes(2);
});

test("confirm's group is named by its own question, not left anonymous", () => {
  render(
    <RunStop
      agent="Scout"
      chat="Research"
      phase={{ kind: "confirm" }}
      nowMs={0}
      onAsk={vi.fn()}
      onCancel={vi.fn()}
      onSend={vi.fn()}
    />,
  );
  expect(screen.getByRole("group", { name: "Stop Scout now?" })).toBeTruthy();
});

test("idle after an explicit cancel focuses the Stop button; a fresh, never-asked key does not", () => {
  render(<Harness />);
  const stopButton = () => screen.getByRole("button", { name: "Stop Scout in Research" });
  expect(document.activeElement).not.toBe(stopButton());

  fireEvent.click(stopButton());
  fireEvent.click(screen.getByRole("button", { name: "Keep running" }));
  expect(document.activeElement).toBe(stopButton());
});

test("sending reads Stopping…, with no button, and takes focus on its own tabIndex=-1 status", () => {
  render(<Harness />);
  fireEvent.click(screen.getByRole("button", { name: "Stop Scout in Research" }));
  fireEvent.click(screen.getByRole("button", { name: "Stop run" }));

  const status = screen.getByRole("status");
  expect(status.textContent).toBe("Stopping…");
  expect(screen.queryAllByRole("button")).toHaveLength(0);
  expect(document.activeElement).toBe(status);
});

test("a sent phase under STOP_CONFIRM_MS still reads Stopping…, with no button", () => {
  render(
    <RunStop
      agent="Scout"
      chat="Research"
      phase={{ kind: "sent", at: 1000 }}
      nowMs={1000 + STOP_CONFIRM_MS - 1}
      onAsk={vi.fn()}
      onCancel={vi.fn()}
      onSend={vi.fn()}
    />,
  );
  expect(screen.getByRole("status").textContent).toBe("Stopping…");
  expect(screen.queryAllByRole("button")).toHaveLength(0);
});

test("a sent phase at or past STOP_CONFIRM_MS offers Try again, which sends again", () => {
  const onSend = vi.fn();
  render(
    <RunStop
      agent="Scout"
      chat="Research"
      phase={{ kind: "sent", at: 1000 }}
      nowMs={1000 + STOP_CONFIRM_MS}
      onAsk={vi.fn()}
      onCancel={vi.fn()}
      onSend={onSend}
    />,
  );
  expect(screen.getByText("Stop sent; Claude Code has not confirmed it yet.")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(onSend).toHaveBeenCalledTimes(1);
});

test("Try again from a stale sent phase moves focus to the status, not the page", async () => {
  const { api, pending } = fakeApi();
  const { rerender } = render(<StopsHarness api={api} nowMs={0} now={() => 1_000} />);
  fireEvent.click(screen.getByRole("button", { name: "Stop Scout in Research" }));
  fireEvent.click(screen.getByRole("button", { name: "Stop run" }));
  pending[0]?.resolve();
  await settle();

  // Time passes with no confirmation: "sent" goes stale and offers Try again.
  rerender(<StopsHarness api={api} nowMs={1_000 + STOP_CONFIRM_MS} now={() => 1_000} />);
  const retry = screen.getByRole("button", { name: "Try again" });
  retry.focus();
  fireEvent.click(retry);

  // The retry button that held focus unmounts into the same "Stopping…" status paragraph:
  // focus must land back on it, never fall through to the page.
  const status = screen.getByRole("status");
  expect(status.textContent).toBe("Stopping…");
  expect(document.activeElement).toBe(status);
});

const FAILURE_CASES: ReadonlyArray<readonly [string, string]> = [
  ["unknown_run", "This run is no longer running in its chat's Claude Code process."],
  ["not_running", "Claude Code is not running for this chat."],
  ["not_found", "This chat is not open any more."],
  ["refused", "Claude Code did not stop this run."],
  ["timeout", "Claude Code did not answer in time. Try again."],
  ["overloaded", "Too many stops at once. Try again."],
  ["something_else_entirely", "Could not stop this run. Try again."],
];

test.each(FAILURE_CASES)(
  "failed(%s) reads its own sentence, with Try again and Dismiss",
  (code, sentence) => {
    const onSend = vi.fn();
    const onCancel = vi.fn();
    render(
      <RunStop
        agent="Scout"
        chat="Research"
        phase={{ kind: "failed", code }}
        nowMs={0}
        onAsk={vi.fn()}
        onCancel={onCancel}
        onSend={onSend}
      />,
    );
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(screen.getByText(sentence)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onCancel).toHaveBeenCalledTimes(1);
  },
);
