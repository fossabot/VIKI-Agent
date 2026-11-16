/**
 * `useModelChoice` and the note slot it feeds `ModelChip`:
 * pending at the pick, applied once `info` catches up (gone after
 * `APPLIED_NOTE_MS`), failed on a rejected receipt or a fresh error; effort pending only once
 * its own receipt is `applied`; a same-value pick with nothing of that kind pending, a repeat of
 * the pending value, and a session switch are no-ops/clears — a same-value pick while one is
 * pending is a revert instead, sent like any pick.
 */
import { act, cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { ErrorData, ModelOption } from "../../protocol";
import type { ControlOutcome } from "../../state/viki";
import { ModelChip } from "./ModelChip";
import type { ChipNote, Schedule } from "./modelChoice";
import { APPLIED_NOTE_MS, useModelChoice } from "./modelChoice";

afterEach(cleanup);

test("an uninitialized session explicitly saves its current model and confirms only its applied receipt", async () => {
  const receipt = deferred<ControlOutcome>();
  const setModel = vi.fn(() => receipt.promise);
  const { result } = renderHook(() =>
    useModelChoice({
      sessionId: "s1",
      model: "sonnet",
      effort: "high",
      error: null,
      setModel,
      setEffort: vi.fn(),
      labelOf,
      allowCurrentModel: true,
      schedule: fakeSchedule().schedule,
    }),
  );
  act(() => result.current.pickModel("sonnet"));
  act(() => result.current.pickModel("sonnet"));
  expect(setModel).toHaveBeenCalledExactlyOnceWith("s1", "sonnet");
  expect(result.current.note?.kind).toBe("pending");
  await act(async () => receipt.resolve("applied"));
  expect(result.current.note).toEqual({ kind: "applied", text: "Now using Sonnet 5" });
});

const LABELS: Record<string, string> = { fable: "Fable 5.1", sonnet: "Sonnet 5", opus: "Opus 5" };
const labelOf = (value: string): string => LABELS[value] ?? value;

/** A fake `Schedule`: `fire` runs every still-pending callback once, synchronously. */
function fakeSchedule() {
  let nextId = 1;
  const tasks = new Map<number, () => void>();
  let lastMs: number | undefined;
  const schedule: Schedule = (callback, ms) => {
    const id = nextId++;
    tasks.set(id, callback);
    lastMs = ms;
    return () => tasks.delete(id);
  };
  return {
    schedule,
    ms: () => lastMs,
    pending: () => tasks.size,
    fire(): void {
      const due = [...tasks.values()];
      tasks.clear();
      for (const callback of due) callback();
    },
  };
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

/** Exposes the hook's result through plain DOM text, and a button per pick this suite needs. */
function Harness({
  sessionId,
  model,
  effort,
  error,
  setModel,
  setEffort,
  schedule,
}: {
  sessionId: string;
  model: string;
  effort: string;
  error: ErrorData | null;
  setModel: (sessionId: string, value: string) => Promise<ControlOutcome>;
  setEffort: (sessionId: string, value: string) => Promise<ControlOutcome>;
  schedule: Schedule;
}) {
  const { note, pickModel, pickEffort } = useModelChoice({
    sessionId,
    model,
    effort,
    error,
    setModel,
    setEffort,
    labelOf,
    schedule,
  });
  return (
    <div>
      <span data-testid="kind">{note?.kind ?? ""}</span>
      <span data-testid="text">{note?.text ?? ""}</span>
      <button type="button" onClick={() => pickModel("sonnet")}>
        pick sonnet
      </button>
      <button type="button" onClick={() => pickModel("opus")}>
        pick opus
      </button>
      <button type="button" onClick={() => pickEffort("max")}>
        pick max
      </button>
      <button type="button" onClick={() => pickEffort("low")}>
        pick low
      </button>
    </div>
  );
}

/** Same wiring as `Harness`, but `labelOf` is a fresh arrow created inline on every render —
 * what App.tsx actually passes (it reads `info.models` inline at the call site) — instead of
 * the suite's own stable module-level `labelOf`. Proves the hook itself, not the caller, keeps
 * the confirm effect from re-running on every one of its own renders. */
function UnstableHarness({
  sessionId,
  model,
  effort,
  error,
  setModel,
  setEffort,
}: {
  sessionId: string;
  model: string;
  effort: string;
  error: ErrorData | null;
  setModel: (sessionId: string, value: string) => Promise<ControlOutcome>;
  setEffort: (sessionId: string, value: string) => Promise<ControlOutcome>;
}) {
  const { note, pickModel, pickEffort } = useModelChoice({
    sessionId,
    model,
    effort,
    error,
    setModel,
    setEffort,
    labelOf: (value: string) => LABELS[value] ?? value, // a new arrow every render, as App.tsx passes
  });
  return (
    <div>
      <span data-testid="kind">{note?.kind ?? ""}</span>
      <span data-testid="text">{note?.text ?? ""}</span>
      <button type="button" onClick={() => pickModel("sonnet")}>
        pick sonnet
      </button>
      <button type="button" onClick={() => pickModel("opus")}>
        pick opus
      </button>
      <button type="button" onClick={() => pickEffort("max")}>
        pick max
      </button>
      <button type="button" onClick={() => pickEffort("low")}>
        pick low
      </button>
    </div>
  );
}

const ERROR_A: ErrorData = { code: "bad_request", message: "a", recoverable: true };
const ERROR_B: ErrorData = { code: "bad_request", message: "b", recoverable: true };

test("the applied note's own hide delay is 4000ms", () => {
  expect(APPLIED_NOTE_MS).toBe(4000);
});

test("picking a different model notes pending at once, worded from labelOf", () => {
  const setModel = vi.fn(() => new Promise<ControlOutcome>(() => {}));
  render(
    <Harness
      sessionId="s1"
      model="fable"
      effort="xhigh"
      error={null}
      setModel={setModel}
      setEffort={vi.fn()}
      schedule={fakeSchedule().schedule}
    />,
  );
  fireEvent.click(screen.getByText("pick sonnet"));
  expect(screen.getByTestId("kind").textContent).toBe("pending");
  expect(screen.getByTestId("text").textContent).toBe("Switching to Sonnet 5…");
  expect(setModel).toHaveBeenCalledWith("s1", "sonnet");
});

test("set_model's own receipt leaves it pending; info.model catching up applies it, then hides", async () => {
  const pending = deferred<ControlOutcome>();
  const setModel = vi.fn(() => pending.promise);
  const fake = fakeSchedule();
  const { rerender } = render(
    <Harness
      sessionId="s1"
      model="fable"
      effort="xhigh"
      error={null}
      setModel={setModel}
      setEffort={vi.fn()}
      schedule={fake.schedule}
    />,
  );
  fireEvent.click(screen.getByText("pick sonnet"));
  await act(async () => {
    pending.resolve("unknown"); // facts: set_model's receipt is unknown/native_unconfirmed
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(screen.getByTestId("kind").textContent).toBe("pending"); // the receipt alone never confirms it
  rerender(
    <Harness
      sessionId="s1"
      model="sonnet"
      effort="xhigh"
      error={null}
      setModel={setModel}
      setEffort={vi.fn()}
      schedule={fake.schedule}
    />,
  );
  expect(screen.getByTestId("kind").textContent).toBe("applied");
  expect(screen.getByTestId("text").textContent).toBe("Now using Sonnet 5");
  expect(fake.ms()).toBe(APPLIED_NOTE_MS);
  act(() => fake.fire());
  expect(screen.getByTestId("kind").textContent).toBe("");
});

test("a rejected receipt fails a pending model pick", async () => {
  const setModel = vi.fn(async () => "rejected" as ControlOutcome);
  render(
    <Harness
      sessionId="s1"
      model="fable"
      effort="xhigh"
      error={null}
      setModel={setModel}
      setEffort={vi.fn()}
      schedule={fakeSchedule().schedule}
    />,
  );
  await act(async () => {
    fireEvent.click(screen.getByText("pick sonnet"));
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(screen.getByTestId("kind").textContent).toBe("failed");
  expect(screen.getByTestId("text").textContent).toBe("Couldn't switch to Sonnet 5");
});

test("a fresh error after the pick fails it even though the receipt itself was not rejected", async () => {
  const pending = deferred<ControlOutcome>();
  const setModel = vi.fn(() => pending.promise);
  const fake = fakeSchedule();
  const { rerender } = render(
    <Harness
      sessionId="s1"
      model="fable"
      effort="xhigh"
      error={null}
      setModel={setModel}
      setEffort={vi.fn()}
      schedule={fake.schedule}
    />,
  );
  fireEvent.click(screen.getByText("pick sonnet"));
  await act(async () => {
    pending.resolve("unknown");
    await Promise.resolve();
    await Promise.resolve();
  });
  rerender(
    <Harness
      sessionId="s1"
      model="fable"
      effort="xhigh"
      error={ERROR_A}
      setModel={setModel}
      setEffort={vi.fn()}
      schedule={fake.schedule}
    />,
  );
  expect(screen.getByTestId("kind").textContent).toBe("failed");
  expect(screen.getByTestId("text").textContent).toBe("Couldn't switch to Sonnet 5");
});

test("an error already standing before the pick does not by itself fail it", () => {
  const setModel = vi.fn(() => new Promise<ControlOutcome>(() => {}));
  const { rerender } = render(
    <Harness
      sessionId="s1"
      model="fable"
      effort="xhigh"
      error={ERROR_A}
      setModel={setModel}
      setEffort={vi.fn()}
      schedule={fakeSchedule().schedule}
    />,
  );
  fireEvent.click(screen.getByText("pick sonnet"));
  expect(screen.getByTestId("kind").textContent).toBe("pending");
  rerender(
    <Harness
      sessionId="s1"
      model="fable"
      effort="xhigh"
      error={ERROR_A}
      setModel={setModel}
      setEffort={vi.fn()}
      schedule={fakeSchedule().schedule}
    />,
  );
  expect(screen.getByTestId("kind").textContent).toBe("pending"); // the same object, not a fresh refusal
  rerender(
    <Harness
      sessionId="s1"
      model="fable"
      effort="xhigh"
      error={ERROR_B}
      setModel={setModel}
      setEffort={vi.fn()}
      schedule={fakeSchedule().schedule}
    />,
  );
  expect(screen.getByTestId("kind").textContent).toBe("failed"); // a genuinely different object now
});

test("an effort pick the receipt applies notes pending-saved; info.effort catching up applies it", async () => {
  const setEffort = vi.fn(async () => "applied" as ControlOutcome);
  const fake = fakeSchedule();
  const { rerender } = render(
    <Harness
      sessionId="s1"
      model="fable"
      effort="high"
      error={null}
      setModel={vi.fn()}
      setEffort={setEffort}
      schedule={fake.schedule}
    />,
  );
  await act(async () => {
    fireEvent.click(screen.getByText("pick max"));
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(setEffort).toHaveBeenCalledWith("s1", "max");
  expect(screen.getByTestId("kind").textContent).toBe("pending");
  expect(screen.getByTestId("text").textContent).toBe("max from your next message");
  rerender(
    <Harness
      sessionId="s1"
      model="fable"
      effort="max"
      error={null}
      setModel={vi.fn()}
      setEffort={setEffort}
      schedule={fake.schedule}
    />,
  );
  expect(screen.getByTestId("kind").textContent).toBe("applied");
  expect(screen.getByTestId("text").textContent).toBe("Now at max effort");
  expect(fake.ms()).toBe(APPLIED_NOTE_MS);
  act(() => fake.fire());
  expect(screen.getByTestId("kind").textContent).toBe("");
});

test("an effort pick the receipt does not apply fails at once", async () => {
  const setEffort = vi.fn(async () => "rejected" as ControlOutcome);
  render(
    <Harness
      sessionId="s1"
      model="fable"
      effort="high"
      error={null}
      setModel={vi.fn()}
      setEffort={setEffort}
      schedule={fakeSchedule().schedule}
    />,
  );
  await act(async () => {
    fireEvent.click(screen.getByText("pick max"));
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(screen.getByTestId("kind").textContent).toBe("failed");
  expect(screen.getByTestId("text").textContent).toBe("Couldn't switch to max effort");
});

test("a same-value pick does nothing, for either control", () => {
  const setModel = vi.fn();
  const setEffort = vi.fn();
  render(
    <Harness
      sessionId="s1"
      model="sonnet"
      effort="max"
      error={null}
      setModel={setModel}
      setEffort={setEffort}
      schedule={fakeSchedule().schedule}
    />,
  );
  fireEvent.click(screen.getByText("pick sonnet"));
  fireEvent.click(screen.getByText("pick max"));
  expect(setModel).not.toHaveBeenCalled();
  expect(setEffort).not.toHaveBeenCalled();
  expect(screen.getByTestId("kind").textContent).toBe("");
});

test("picking the value already pending again does nothing, for either control", () => {
  const setModel = vi.fn(() => new Promise<ControlOutcome>(() => {}));
  const setEffort = vi.fn(() => new Promise<ControlOutcome>(() => {}));
  render(
    <Harness
      sessionId="s1"
      model="fable"
      effort="high"
      error={null}
      setModel={setModel}
      setEffort={setEffort}
      schedule={fakeSchedule().schedule}
    />,
  );
  fireEvent.click(screen.getByText("pick sonnet"));
  fireEvent.click(screen.getByText("pick sonnet")); // same pick again, still pending
  expect(setModel).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByText("pick max"));
  fireEvent.click(screen.getByText("pick max")); // same pick again, still pending
  expect(setEffort).toHaveBeenCalledTimes(1);
});

test("picking the running effort again while a pick is pending reverts it; an applied receipt clears the note at once", async () => {
  const setEffort = vi.fn(async () => "applied" as ControlOutcome);
  render(
    <Harness
      sessionId="s1"
      model="fable"
      effort="max"
      error={null}
      setModel={vi.fn()}
      setEffort={setEffort}
      schedule={fakeSchedule().schedule}
    />,
  );
  await act(async () => {
    fireEvent.click(screen.getByText("pick low"));
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(screen.getByTestId("text").textContent).toBe("low from your next message");
  await act(async () => {
    fireEvent.click(screen.getByText("pick max")); // back to what's already running: a revert
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(setEffort).toHaveBeenLastCalledWith("s1", "max");
  // nothing pending, nothing changed (the save already matches what's running), so no note
  expect(screen.getByTestId("kind").textContent).toBe("");
  expect(screen.getByTestId("text").textContent).toBe("");
});

test("a model revert sent before the first switch lands ends applied for the reverted value, never for the abandoned one", () => {
  const setModel = vi.fn(() => new Promise<ControlOutcome>(() => {}));
  const { rerender } = render(
    <Harness
      sessionId="s1"
      model="sonnet"
      effort="xhigh"
      error={null}
      setModel={setModel}
      setEffort={vi.fn()}
      schedule={fakeSchedule().schedule}
    />,
  );
  fireEvent.click(screen.getByText("pick opus")); // switching away, pending
  expect(screen.getByTestId("text").textContent).toBe("Switching to Opus 5…");
  fireEvent.click(screen.getByText("pick sonnet")); // back to what's running: a revert, sent like any pick
  expect(setModel).toHaveBeenLastCalledWith("s1", "sonnet");
  expect(screen.getByTestId("text").textContent).toBe("Switching to Sonnet 5…");
  // the abandoned switch's own confirmation lands first: must not be mistaken for the revert applying
  rerender(
    <Harness
      sessionId="s1"
      model="opus"
      effort="xhigh"
      error={null}
      setModel={setModel}
      setEffort={vi.fn()}
      schedule={fakeSchedule().schedule}
    />,
  );
  expect(screen.getByTestId("kind").textContent).toBe("pending");
  // the revert's own confirmation lands: applied, for the reverted value
  rerender(
    <Harness
      sessionId="s1"
      model="sonnet"
      effort="xhigh"
      error={null}
      setModel={setModel}
      setEffort={vi.fn()}
      schedule={fakeSchedule().schedule}
    />,
  );
  expect(screen.getByTestId("kind").textContent).toBe("applied");
  expect(screen.getByTestId("text").textContent).toBe("Now using Sonnet 5");
});

test("an unstable labelOf (a fresh arrow every render, as App.tsx passes it) must not let the confirm effect mistake an effort revert for its own receipt landing", async () => {
  const pickMax = deferred<ControlOutcome>();
  const pickLow = deferred<ControlOutcome>();
  const setEffort = vi
    .fn<(sessionId: string, value: string) => Promise<ControlOutcome>>()
    .mockReturnValueOnce(pickMax.promise)
    .mockReturnValueOnce(pickLow.promise);
  render(
    <UnstableHarness
      sessionId="s1"
      model="fable"
      effort="low"
      error={null}
      setModel={vi.fn()}
      setEffort={setEffort}
    />,
  );
  await act(async () => {
    fireEvent.click(screen.getByText("pick max"));
    pickMax.resolve("applied");
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(screen.getByTestId("kind").textContent).toBe("pending");
  expect(screen.getByTestId("text").textContent).toBe("max from your next message");

  fireEvent.click(screen.getByText("pick low")); // low is still running: a revert, sent like any pick
  expect(setEffort).toHaveBeenLastCalledWith("s1", "low");
  // the revert's own receipt has not landed yet: an unstable labelOf must not let the confirm
  // effect mistake "low already running" for that receipt and jump straight to applied
  expect(screen.getByTestId("kind").textContent).toBe("");

  await act(async () => {
    pickLow.resolve("rejected");
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(screen.getByTestId("kind").textContent).toBe("failed");
  expect(screen.getByTestId("text").textContent).toBe("Couldn't switch to low effort");
});

test("an unstable labelOf (a fresh arrow every render, as App.tsx passes it) still ends a model revert applied only for the reverted value, never for the abandoned one", () => {
  const setModel = vi.fn(() => new Promise<ControlOutcome>(() => {}));
  const { rerender } = render(
    <UnstableHarness
      sessionId="s1"
      model="sonnet"
      effort="xhigh"
      error={null}
      setModel={setModel}
      setEffort={vi.fn()}
    />,
  );
  fireEvent.click(screen.getByText("pick opus")); // switching away, pending
  expect(screen.getByTestId("text").textContent).toBe("Switching to Opus 5…");
  fireEvent.click(screen.getByText("pick sonnet")); // back to what's running: a revert, sent like any pick
  expect(setModel).toHaveBeenLastCalledWith("s1", "sonnet");
  // must still read the revert's own pending note, not jump to "Now using..." just because
  // sonnet is already what's running
  expect(screen.getByTestId("kind").textContent).toBe("pending");
  expect(screen.getByTestId("text").textContent).toBe("Switching to Sonnet 5…");
  // the abandoned opus switch's own confirmation lands first
  rerender(
    <UnstableHarness
      sessionId="s1"
      model="opus"
      effort="xhigh"
      error={null}
      setModel={setModel}
      setEffort={vi.fn()}
    />,
  );
  expect(screen.getByTestId("kind").textContent).toBe("pending");
  // the revert's own confirmation lands: applied, for the reverted value
  rerender(
    <UnstableHarness
      sessionId="s1"
      model="sonnet"
      effort="xhigh"
      error={null}
      setModel={setModel}
      setEffort={vi.fn()}
    />,
  );
  expect(screen.getByTestId("kind").textContent).toBe("applied");
  expect(screen.getByTestId("text").textContent).toBe("Now using Sonnet 5");
});

test("a session switch clears the note and abandons the in-flight pick for good", () => {
  const setModel = vi.fn(() => new Promise<ControlOutcome>(() => {}));
  const { rerender } = render(
    <Harness
      sessionId="s1"
      model="fable"
      effort="xhigh"
      error={null}
      setModel={setModel}
      setEffort={vi.fn()}
      schedule={fakeSchedule().schedule}
    />,
  );
  fireEvent.click(screen.getByText("pick sonnet"));
  expect(screen.getByTestId("kind").textContent).toBe("pending");
  rerender(
    <Harness
      sessionId="s2"
      model="fable"
      effort="xhigh"
      error={null}
      setModel={setModel}
      setEffort={vi.fn()}
      schedule={fakeSchedule().schedule}
    />,
  );
  expect(screen.getByTestId("kind").textContent).toBe("");
  // The new session's own info later happening to read "sonnet" must not resurrect a note that
  // belonged to the session the user already left.
  rerender(
    <Harness
      sessionId="s2"
      model="sonnet"
      effort="xhigh"
      error={null}
      setModel={setModel}
      setEffort={vi.fn()}
      schedule={fakeSchedule().schedule}
    />,
  );
  expect(screen.getByTestId("kind").textContent).toBe("");
});

const MODELS: ModelOption[] = [
  { value: "fable", label: "Fable 5.1", description: "Most capable" },
  { value: "sonnet", label: "Sonnet 5", description: "Efficient for routine tasks" },
];

test("ModelChip draws the note as a status span beside the trigger; the label still reads what runs", () => {
  const note: ChipNote = { kind: "pending", text: "Switching to Sonnet 5…" };
  const { container } = render(
    <ModelChip
      models={MODELS}
      model="fable"
      effort="xhigh"
      running={false}
      onModel={vi.fn()}
      onEffort={vi.fn()}
      note={note}
    />,
  );
  const chip = container.querySelector(".model-chip") as HTMLElement;
  expect(chip.textContent).toBe("Fable 5.1 · xhigh ▾"); // unmoved by the pending note beside it
  const shown = screen.getByRole("status");
  expect(shown.className).toBe("chip-note");
  expect(shown.textContent).toBe("Switching to Sonnet 5…");
  expect(shown.getAttribute("data-kind")).toBe("pending");
});

test("ModelChip renders no note span when note is omitted", () => {
  render(
    <ModelChip
      models={MODELS}
      model="fable"
      effort="xhigh"
      running={false}
      onModel={vi.fn()}
      onEffort={vi.fn()}
    />,
  );
  expect(screen.queryByRole("status")).toBeNull();
});

test("the disabled chip carries a title naming when it will be available", () => {
  const { rerender, container } = render(
    <ModelChip
      models={MODELS}
      model="fable"
      effort="xhigh"
      running={true}
      onModel={vi.fn()}
      onEffort={vi.fn()}
    />,
  );
  const chip = container.querySelector(".model-chip") as HTMLElement;
  expect(chip.title).toBe("Available when this reply finishes");
  rerender(
    <ModelChip
      models={MODELS}
      model="fable"
      effort="xhigh"
      running={false}
      onModel={vi.fn()}
      onEffort={vi.fn()}
    />,
  );
  expect(chip.title).toBe("Fable 5.1 · xhigh\nfable");
});
