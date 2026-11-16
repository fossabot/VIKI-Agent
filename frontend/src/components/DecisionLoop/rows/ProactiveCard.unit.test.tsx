import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { decisionLoopExamples } from "../../../clients/decisionLoop/fake";
import type { PageView } from "../../../clients/decisionLoop/types";
import { localClock, statusLine } from "../wording";
import { ProactiveCard, type ProactiveCardProps } from "./ProactiveCard";

afterEach(() => {
  cleanup();
});

const NOW = new Date(2026, 9, 21, 23, 50, 0, 0).getTime();
const HOUR = 60 * 60 * 1000;
const PAUSED = "2026-10-22T02:00:00.000Z";
const FOCUSED = "2026-10-22T03:00:00.000Z";

function pageWith(overrides: Partial<PageView> = {}): PageView {
  return { ...decisionLoopExamples().page, ...overrides };
}

const ON = { enabled: true, paused_until: "", focus_until: "" } as const;

function baseProps(overrides: Partial<ProactiveCardProps> = {}): ProactiveCardProps {
  return {
    page: pageWith(ON),
    writable: true,
    patch: vi.fn().mockResolvedValue(undefined),
    onRefresh: vi.fn(),
    nowMs: NOW,
    ...overrides,
  };
}

function button(name: string): HTMLButtonElement {
  return screen.getByRole("button", { name }) as HTMLButtonElement;
}

test("the card is a region named Proactive VIKI", () => {
  render(<ProactiveCard {...baseProps()} />);
  expect(screen.getByRole("region", { name: "Proactive VIKI" })).toBeTruthy();
  expect(screen.getByRole("heading", { level: 2, name: "Proactive VIKI" })).toBeTruthy();
});

test("off shows the off line and one button, no switch", () => {
  render(<ProactiveCard {...baseProps({ page: pageWith({ ...ON, enabled: false }) })} />);
  expect(screen.getByText(/^Off\. VIKI checks nothing and starts nothing/)).toBeTruthy();
  expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual([
    "Turn proactive VIKI on",
  ]);
  expect(screen.queryByRole("switch")).toBeNull();
});

test("Turn proactive VIKI on patches decision.enabled true and refreshes", async () => {
  const patch = vi.fn().mockResolvedValue(undefined);
  const onRefresh = vi.fn();
  render(
    <ProactiveCard
      {...baseProps({ page: pageWith({ ...ON, enabled: false }), patch, onRefresh })}
    />,
  );
  fireEvent.click(button("Turn proactive VIKI on"));
  await waitFor(() => expect(patch).toHaveBeenCalledWith({ "decision.enabled": true }));
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
});

test("on shows the checked switch, the launcher's status line and the hint", () => {
  const page = pageWith(ON);
  render(<ProactiveCard {...baseProps({ page })} />);
  expect(screen.getByRole("switch", { name: "Proactive VIKI" }).getAttribute("aria-checked")).toBe(
    "true",
  );
  const expected = statusLine(
    {
      state: "on",
      paused_until: null,
      focus_until: null,
      checked_at: page.status.checked_at,
      open: page.status.open,
      plans_waiting: 0,
      model: page.model.state,
    },
    NOW,
  );
  expect(screen.getByText(expected)).toBeTruthy();
  expect(
    screen.getByText(
      "Pause: VIKI stops checking and starts nothing. Focus: VIKI keeps watching but holds nudges and alerts.",
    ),
  ).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Turn proactive VIKI on" })).toBeNull();
});

test("the status line says Paused or Focus with the stamp's clock time", () => {
  render(<ProactiveCard {...baseProps({ page: pageWith({ ...ON, paused_until: PAUSED }) })} />);
  expect(screen.getByText(`Paused until ${localClock(PAUSED)}`)).toBeTruthy();
  cleanup();
  render(<ProactiveCard {...baseProps({ page: pageWith({ ...ON, focus_until: FOCUSED }) })} />);
  const focusClause = ` · Focus until ${localClock(FOCUSED)}`;
  expect(
    screen.getByText((text) => text.startsWith("Watching · ") && text.endsWith(focusClause)),
  ).toBeTruthy();
});

test("while a write is in flight every control is disabled and no second write starts", () => {
  const patch = vi.fn(() => new Promise<void>(() => {}));
  render(<ProactiveCard {...baseProps({ patch })} />);
  fireEvent.click(button("Pause for 1 hour"));
  for (const name of ["Pause for 1 hour", "Pause until tomorrow", "Focus for 1 hour"]) {
    expect(button(name).disabled).toBe(true);
  }
  expect(screen.getByRole("switch").getAttribute("aria-disabled")).toBe("true");
  fireEvent.click(button("Focus for 1 hour"));
  expect(patch).toHaveBeenCalledTimes(1);
});

test("while Turn proactive VIKI on is in flight its button is disabled", () => {
  const patch = vi.fn(() => new Promise<void>(() => {}));
  render(<ProactiveCard {...baseProps({ page: pageWith({ ...ON, enabled: false }), patch })} />);
  fireEvent.click(button("Turn proactive VIKI on"));
  expect(button("Turn proactive VIKI on").disabled).toBe(true);
  fireEvent.click(button("Turn proactive VIKI on"));
  expect(patch).toHaveBeenCalledTimes(1);
});

test("the switch patches decision.enabled false", async () => {
  const patch = vi.fn().mockResolvedValue(undefined);
  render(<ProactiveCard {...baseProps({ patch })} />);
  fireEvent.click(screen.getByRole("switch"));
  await waitFor(() => expect(patch).toHaveBeenCalledWith({ "decision.enabled": false }));
});

test("not paused shows both Pause buttons and no Resume", () => {
  render(<ProactiveCard {...baseProps()} />);
  expect(button("Pause for 1 hour")).toBeTruthy();
  expect(button("Pause until tomorrow")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Resume" })).toBeNull();
});

test("Pause for 1 hour patches a stamp exactly one hour after nowMs", async () => {
  const patch = vi.fn().mockResolvedValue(undefined);
  render(<ProactiveCard {...baseProps({ patch })} />);
  fireEvent.click(button("Pause for 1 hour"));
  await waitFor(() =>
    expect(patch).toHaveBeenCalledWith({
      "decision.paused_until": new Date(NOW + HOUR).toISOString(),
    }),
  );
});

test("Pause until tomorrow patches 08:00 local the next day", async () => {
  const patch = vi.fn().mockResolvedValue(undefined);
  render(<ProactiveCard {...baseProps({ patch })} />);
  fireEvent.click(button("Pause until tomorrow"));
  await waitFor(() =>
    expect(patch).toHaveBeenCalledWith({
      "decision.paused_until": new Date(2026, 9, 22, 8, 0, 0, 0).toISOString(),
    }),
  );
});

test("paused shows Resume only, and Resume clears decision.paused_until", async () => {
  const patch = vi.fn().mockResolvedValue(undefined);
  render(
    <ProactiveCard {...baseProps({ page: pageWith({ ...ON, paused_until: PAUSED }), patch })} />,
  );
  expect(screen.queryByRole("button", { name: "Pause for 1 hour" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Pause until tomorrow" })).toBeNull();
  fireEvent.click(button("Resume"));
  await waitFor(() => expect(patch).toHaveBeenCalledWith({ "decision.paused_until": "" }));
});

test("Focus for 1 hour patches a stamp one hour after nowMs", async () => {
  const patch = vi.fn().mockResolvedValue(undefined);
  render(<ProactiveCard {...baseProps({ patch })} />);
  fireEvent.click(button("Focus for 1 hour"));
  await waitFor(() =>
    expect(patch).toHaveBeenCalledWith({
      "decision.focus_until": new Date(NOW + HOUR).toISOString(),
    }),
  );
});

test("End focus clears decision.focus_until, paused or not", async () => {
  const patch = vi.fn().mockResolvedValue(undefined);
  render(
    <ProactiveCard
      {...baseProps({
        page: pageWith({ ...ON, paused_until: PAUSED, focus_until: FOCUSED }),
        patch,
      })}
    />,
  );
  expect(screen.queryByRole("button", { name: "Focus for 1 hour" })).toBeNull();
  fireEvent.click(button("End focus"));
  await waitFor(() => expect(patch).toHaveBeenCalledWith({ "decision.focus_until": "" }));
});

// --- focus hand-off: rerender with the page the write produced, then read document.activeElement

test("Turn on hands focus to the switch once the page shows it", async () => {
  const onRefresh = vi.fn();
  const off = pageWith({ ...ON, enabled: false });
  const { rerender } = render(<ProactiveCard {...baseProps({ page: off, onRefresh })} />);
  fireEvent.click(button("Turn proactive VIKI on"));
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
  rerender(<ProactiveCard {...baseProps({ page: pageWith(ON), onRefresh })} />);
  await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("switch")));
});

test("the switch hands focus to Turn proactive VIKI on", async () => {
  const onRefresh = vi.fn();
  const { rerender } = render(<ProactiveCard {...baseProps({ onRefresh })} />);
  fireEvent.click(screen.getByRole("switch"));
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
  rerender(
    <ProactiveCard {...baseProps({ page: pageWith({ ...ON, enabled: false }), onRefresh })} />,
  );
  await waitFor(() => expect(document.activeElement).toBe(button("Turn proactive VIKI on")));
});

test.each(["Pause for 1 hour", "Pause until tomorrow"])(
  "%s hands focus to Resume",
  async (name) => {
    const onRefresh = vi.fn();
    const { rerender } = render(<ProactiveCard {...baseProps({ onRefresh })} />);
    fireEvent.click(button(name));
    await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
    rerender(
      <ProactiveCard
        {...baseProps({ page: pageWith({ ...ON, paused_until: PAUSED }), onRefresh })}
      />,
    );
    await waitFor(() => expect(document.activeElement).toBe(button("Resume")));
  },
);

test("Resume hands focus to Pause for 1 hour", async () => {
  const onRefresh = vi.fn();
  const paused = pageWith({ ...ON, paused_until: PAUSED });
  const { rerender } = render(<ProactiveCard {...baseProps({ page: paused, onRefresh })} />);
  fireEvent.click(button("Resume"));
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
  rerender(<ProactiveCard {...baseProps({ page: pageWith(ON), onRefresh })} />);
  await waitFor(() => expect(document.activeElement).toBe(button("Pause for 1 hour")));
});

test("the Focus button keeps its element, so a Focus write moves focus nowhere", async () => {
  const onRefresh = vi.fn();
  const { rerender } = render(<ProactiveCard {...baseProps({ onRefresh })} />);
  const focus = button("Focus for 1 hour");
  focus.focus();
  fireEvent.click(focus);
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
  rerender(
    <ProactiveCard
      {...baseProps({ page: pageWith({ ...ON, focus_until: FOCUSED }), onRefresh })}
    />,
  );
  await waitFor(() => expect(button("End focus")).toBe(focus));
  expect(document.activeElement).toBe(focus);
});

test("a failed write hands focus to nothing", async () => {
  const patch = vi.fn().mockRejectedValue(new Error("boom"));
  const { rerender } = render(<ProactiveCard {...baseProps({ patch })} />);
  fireEvent.click(button("Pause for 1 hour"));
  await screen.findByText("Could not save. Try again.");
  rerender(
    <ProactiveCard {...baseProps({ page: pageWith({ ...ON, paused_until: PAUSED }), patch })} />,
  );
  expect(document.activeElement).not.toBe(button("Resume"));
});

// --- not writable and errors

test("on, not writable: the switch is aria-disabled, every button is disabled, nothing is written", () => {
  const patch = vi.fn().mockResolvedValue(undefined);
  render(<ProactiveCard {...baseProps({ writable: false, patch })} />);
  const switchEl = screen.getByRole("switch");
  expect(switchEl.getAttribute("aria-disabled")).toBe("true");
  fireEvent.click(switchEl);
  for (const name of ["Pause for 1 hour", "Pause until tomorrow", "Focus for 1 hour"]) {
    expect(button(name).disabled).toBe(true);
    fireEvent.click(button(name));
  }
  expect(patch).not.toHaveBeenCalled();
});

test("off, not writable: Turn proactive VIKI on is disabled and writes nothing", () => {
  const patch = vi.fn().mockResolvedValue(undefined);
  render(
    <ProactiveCard
      {...baseProps({ writable: false, page: pageWith({ ...ON, enabled: false }), patch })}
    />,
  );
  expect(button("Turn proactive VIKI on").disabled).toBe(true);
  fireEvent.click(button("Turn proactive VIKI on"));
  expect(patch).not.toHaveBeenCalled();
  expect(screen.queryByText("Settings aren't ready to change right now.")).toBeNull();
});

test("a rejected patch shows the error and the button is enabled again", async () => {
  const patch = vi.fn().mockRejectedValue(new Error("boom"));
  render(<ProactiveCard {...baseProps({ patch })} />);
  fireEvent.click(button("Pause for 1 hour"));
  const error = await screen.findByText("Could not save. Try again.");
  expect(error.getAttribute("role")).toBe("status");
  expect(button("Pause for 1 hour").disabled).toBe(false);
});
