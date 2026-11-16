/**
 * App-level wiring for useModelChoice: modelChoice.unit.test.tsx already pins the
 * hook and ModelChip through every outcome (pending/applied/failed, same-value and
 * session-switch no-ops). This file is the one part only a full App render can prove — that
 * App actually feeds the hook real session/model state and the real control actions, so picking
 * a model through the chip notes pending, and a server-confirmed switch (a fresh session.started
 * carrying the new model) notes it applied, all while the chip's own label keeps reading `info`
 * and never jumps to the pick ahead of the server.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, GENERATION } from "../../accept/m3b/fakes";
import type { Envelope, EventMap, ModelOption } from "../../protocol";
import { fakeDurable } from "../../testDurable";

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
});

const MODELS: ModelOption[] = [
  { value: "sonnet", label: "Sonnet 5", description: "Efficient for routine tasks" },
  { value: "opus", label: "Opus 5", description: "Best for everyday, complex tasks" },
];

function frame<K extends keyof EventMap>(
  sessionId: string,
  seq: number,
  type: K,
  data: EventMap[K],
): Envelope {
  return {
    v: 2,
    generation: GENERATION,
    request_id: "",
    type,
    session_id: sessionId,
    seq,
    data,
  } as Envelope;
}

function started(sessionId: string, seq: number, model: string): Envelope {
  return frame(sessionId, seq, "session.started", {
    model,
    mode: "auto",
    effort: "low",
    commands: [],
    agents: [],
    models: MODELS,
    cli_version: "2.1.259",
    resumed: true,
  });
}

test("picking a model through the chip notes pending, then applied once a fresh session.started confirms it; the label only ever reads info", async () => {
  const rest = fakeRest();
  const { viki, socket } = fakeViki();
  const { container } = render(<App durable={fakeDurable()} viki={viki} rest={rest} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  fireEvent.click(screen.getByText("New session"));
  const opened = socket.sent.find((f) => f.type === "session.open") as { session_id: string };
  const sessionId = opened.session_id;
  act(() => socket.receive(started(sessionId, 1, "sonnet")));

  const chip = container.querySelector(".model-chip") as HTMLElement;
  expect(chip.textContent).toBe("Sonnet 5 · low ▾");

  fireEvent.click(chip);
  fireEvent.click(within(screen.getByRole("menu")).getByText("Sonnet 5"));
  expect(socket.sent.filter((m) => m.type === "session.set_model")).toHaveLength(0);

  fireEvent.click(chip);
  fireEvent.click(within(screen.getByRole("menu")).getByText("Opus 5"));

  // The pick notes pending at once; the label is unmoved by it (the label always shows info).
  // (Not screen.getByRole("status"): a fresh session's own draft-loading notice carries that role too.)
  const note = () => container.querySelector(".chip-note") as HTMLElement;
  expect(note().textContent).toBe("Switching to Opus 5…");
  expect(note().getAttribute("data-kind")).toBe("pending");
  expect(chip.textContent).toBe("Sonnet 5 · low ▾");
  expect(socket.sent.some((m) => m.type === "session.set_model" && m.model === "opus")).toBe(true);

  // The server confirms the live switch the same way it reports every other session start.
  act(() => socket.receive(started(sessionId, 2, "opus")));

  expect(note().textContent).toBe("Now using Opus 5");
  expect(note().getAttribute("data-kind")).toBe("applied");
  expect(chip.textContent).toBe("Opus 5 · low ▾");
});

// App.tsx passes useModelChoice a new `labelOf` arrow every render (it reads `info.models`
// inline), which used to be an effect dependency and made the confirm effect re-run after every
// App render -- including the render its own pending-note state change causes. A revert already
// satisfies `effort === pick.value`, so that stray re-run mistook the revert for its own receipt
// landing and confirmed it "applied" before the real receipt arrived, swallowing a later
// rejection. This drives the real App end to end (no harness-level labelOf to go stale) so the
// fix lives in the hook, not in how App happens to call it.
test("an effort revert through the real chip notes pending-saved on an applied receipt, then failed on the revert's own rejected receipt -- never stuck applied", async () => {
  const rest = fakeRest();
  const { viki, socket } = fakeViki();
  const { container } = render(<App durable={fakeDurable()} viki={viki} rest={rest} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  fireEvent.click(screen.getByText("New session"));
  const opened = socket.sent.find((f) => f.type === "session.open") as { session_id: string };
  const sessionId = opened.session_id;
  act(() => socket.receive(started(sessionId, 1, "sonnet"))); // effort: "low" runs

  const chip = container.querySelector(".model-chip") as HTMLElement;
  const note = () => container.querySelector(".chip-note") as HTMLElement | null;

  fireEvent.click(chip);
  fireEvent.click(within(screen.getByRole("menu")).getByText("max"));
  await waitFor(() => {
    expect(note()?.textContent).toBe("max from your next message");
    expect(note()?.getAttribute("data-kind")).toBe("pending");
  });
  expect(socket.sent.filter((m) => m.type === "session.set_effort")).toHaveLength(1);

  socket.controlStatus = "rejected"; // the revert's own receipt, picked next, fails
  fireEvent.click(chip);
  fireEvent.click(within(screen.getByRole("menu")).getByText("low")); // back to what's running: a revert
  expect(socket.sent.filter((m) => m.type === "session.set_effort")).toHaveLength(2);

  await waitFor(() => {
    expect(note()?.textContent).toBe("Couldn't switch to low effort");
    expect(note()?.getAttribute("data-kind")).toBe("failed");
  });
});
