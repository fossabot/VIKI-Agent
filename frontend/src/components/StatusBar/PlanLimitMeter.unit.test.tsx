import { act, cleanup, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { LimitsApi, PlanLimitReading } from "../../clients/conversations/limitsApi";
import { PlanLimitMeter } from "./PlanLimitMeter";

afterEach(cleanup);

const NOW = new Date(2026, 9, 9, 10, 0, 0).getTime(); // Fri 2026-10-09 10:00 local
const FUTURE_FIVE = Math.floor(new Date(2026, 9, 9, 18, 0, 0).getTime() / 1000); // same day, 18:00
const FUTURE_WEEK = Math.floor(new Date(2026, 9, 12, 9, 0, 0).getTime() / 1000); // 3 days out
const PAST = Math.floor(new Date(2026, 9, 9, 9, 0, 0).getTime() / 1000); // same day, before NOW

function reading(over: Partial<PlanLimitReading> = {}): PlanLimitReading {
  return {
    observed_at: new Date(NOW).toISOString(),
    status: "allowed",
    limiting: null,
    utilization: null,
    resets_at: null,
    windows: { five_hour: null, seven_day: null },
    ...over,
  };
}

function fakeApi(get: LimitsApi["get"]): LimitsApi {
  return { get };
}

function limit(container: HTMLElement): HTMLElement | null {
  return container.querySelector(".statusbar-limit");
}

async function flush(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
  });
}

test("renders nothing before the fetch resolves", () => {
  const api = fakeApi(vi.fn(() => new Promise<PlanLimitReading | null>(() => {})));
  const { container } = render(<PlanLimitMeter api={api} nowMs={NOW} />);
  expect(container.firstChild).toBeNull();
});

test("renders nothing when there is no observation", async () => {
  const api = fakeApi(vi.fn().mockResolvedValue(null));
  const { container } = render(<PlanLimitMeter api={api} nowMs={NOW} />);
  await flush();
  expect(container.firstChild).toBeNull();
});

test("renders nothing when the reading carries no window or reset data", async () => {
  const api = fakeApi(vi.fn().mockResolvedValue(reading()));
  const { container } = render(<PlanLimitMeter api={api} nowMs={NOW} />);
  await flush();
  expect(container.firstChild).toBeNull();
});

test("shows the 5-hour window's percentage and a future reset time", async () => {
  const api = fakeApi(
    vi.fn().mockResolvedValue(
      reading({
        windows: { five_hour: { utilization: 0.417, resets_at: FUTURE_FIVE }, seven_day: null },
      }),
    ),
  );
  const { container } = render(<PlanLimitMeter api={api} nowMs={NOW} />);
  await flush();
  expect(limit(container)?.textContent).toBe("5-hour window · 42% · resets 18:00");
});

test("falls back to the top-level reading when windows.five_hour is absent", async () => {
  const api = fakeApi(
    vi.fn().mockResolvedValue(
      reading({
        limiting: "five_hour",
        utilization: 0.1,
        resets_at: FUTURE_FIVE,
        windows: { five_hour: null, seven_day: null },
      }),
    ),
  );
  const { container } = render(<PlanLimitMeter api={api} nowMs={NOW} />);
  await flush();
  expect(limit(container)?.textContent).toBe("5-hour window · 10% · resets 18:00");
});

test("shows the weekly window when there is no five-hour data", async () => {
  const api = fakeApi(
    vi.fn().mockResolvedValue(
      reading({
        windows: { five_hour: null, seven_day: { utilization: 0.5, resets_at: FUTURE_WEEK } },
      }),
    ),
  );
  const { container } = render(<PlanLimitMeter api={api} nowMs={NOW} />);
  await flush();
  expect(limit(container)?.textContent).toMatch(
    /^Weekly window · 50% · resets [A-Za-z]{3} \d{2}:\d{2}$/,
  );
});

test("falls back to a bare plan-limit line when neither window matches what is limiting", async () => {
  const api = fakeApi(
    vi.fn().mockResolvedValue(
      reading({
        limiting: "overage",
        resets_at: FUTURE_FIVE,
        windows: { five_hour: null, seven_day: null },
      }),
    ),
  );
  const { container } = render(<PlanLimitMeter api={api} nowMs={NOW} />);
  await flush();
  expect(limit(container)?.textContent).toBe("Plan limit · resets 18:00");
});

test("drops the percentage and switches to past tense once the reset has passed", async () => {
  const api = fakeApi(
    vi
      .fn()
      .mockResolvedValue(
        reading({ windows: { five_hour: { utilization: 0.9, resets_at: PAST }, seven_day: null } }),
      ),
  );
  const { container } = render(<PlanLimitMeter api={api} nowMs={NOW} />);
  await flush();
  expect(limit(container)?.textContent).toBe("5-hour window · reset at 09:00");
});

test("appends the weekly percentage when the weekly reset is also in the future", async () => {
  const api = fakeApi(
    vi.fn().mockResolvedValue(
      reading({
        windows: {
          five_hour: { utilization: 0.3, resets_at: FUTURE_FIVE },
          seven_day: { utilization: 0.2, resets_at: FUTURE_WEEK },
        },
      }),
    ),
  );
  const { container } = render(<PlanLimitMeter api={api} nowMs={NOW} />);
  await flush();
  expect(limit(container)?.textContent).toBe("5-hour window · 30% · resets 18:00 · weekly 20%");
});

test("omits the weekly append once the weekly window has already reset", async () => {
  const api = fakeApi(
    vi.fn().mockResolvedValue(
      reading({
        windows: {
          five_hour: { utilization: 0.3, resets_at: FUTURE_FIVE },
          seven_day: { utilization: 0.2, resets_at: PAST },
        },
      }),
    ),
  );
  const { container } = render(<PlanLimitMeter api={api} nowMs={NOW} />);
  await flush();
  expect(limit(container)?.textContent).toBe("5-hour window · 30% · resets 18:00");
});

test("refetches and aborts the fetch in flight when refreshKey changes, and again on unmount", () => {
  const signals: AbortSignal[] = [];
  const get = vi.fn((signal?: AbortSignal) => {
    if (signal) signals.push(signal);
    return new Promise<PlanLimitReading | null>(() => {});
  });
  const view = render(<PlanLimitMeter api={fakeApi(get)} refreshKey={0} nowMs={NOW} />);
  view.rerender(<PlanLimitMeter api={fakeApi(get)} refreshKey={1} nowMs={NOW} />);
  expect(get).toHaveBeenCalledTimes(2);
  expect(signals[0]?.aborted).toBe(true);
  view.unmount();
  expect(signals[1]?.aborted).toBe(true);
});

test("a failed refresh keeps the last reading", async () => {
  const good = reading({
    windows: { five_hour: { utilization: 0.5, resets_at: FUTURE_FIVE }, seven_day: null },
  });
  const get = vi.fn().mockResolvedValueOnce(good).mockRejectedValueOnce(new Error("network"));
  const api = fakeApi(get);
  const view = render(<PlanLimitMeter api={api} refreshKey={0} nowMs={NOW} />);
  await flush();
  expect(limit(view.container)?.textContent).toBe("5-hour window · 50% · resets 18:00");

  view.rerender(<PlanLimitMeter api={api} refreshKey={1} nowMs={NOW} />);
  await flush();
  expect(limit(view.container)?.textContent).toBe("5-hour window · 50% · resets 18:00");
});

test("data-status carries the reading's status, and a warning title suffix", async () => {
  const api = fakeApi(
    vi.fn().mockResolvedValue(
      reading({
        status: "allowed_warning",
        windows: { five_hour: { utilization: 0.9, resets_at: FUTURE_FIVE }, seven_day: null },
      }),
    ),
  );
  const view = render(<PlanLimitMeter api={api} nowMs={NOW} />);
  await flush();
  const span = limit(view.container);
  expect(span?.getAttribute("data-status")).toBe("allowed_warning");
  expect(span?.getAttribute("title")).toBe("Reported by Claude Code at 10:00. Close to the limit.");
});

test("a rejected status gets its own title suffix", async () => {
  const api = fakeApi(
    vi.fn().mockResolvedValue(
      reading({
        status: "rejected",
        windows: { five_hour: { utilization: 1, resets_at: FUTURE_FIVE }, seven_day: null },
      }),
    ),
  );
  const view = render(<PlanLimitMeter api={api} nowMs={NOW} />);
  await flush();
  const span = limit(view.container);
  expect(span?.getAttribute("data-status")).toBe("rejected");
  expect(span?.getAttribute("title")).toBe(
    "Reported by Claude Code at 10:00. The limit is reached.",
  );
});

test("an allowed status carries no title suffix", async () => {
  const api = fakeApi(
    vi.fn().mockResolvedValue(
      reading({
        status: "allowed",
        windows: { five_hour: { utilization: 0.1, resets_at: FUTURE_FIVE }, seven_day: null },
      }),
    ),
  );
  const view = render(<PlanLimitMeter api={api} nowMs={NOW} />);
  await flush();
  const span = limit(view.container);
  expect(span?.getAttribute("data-status")).toBe("allowed");
  expect(span?.getAttribute("title")).toBe("Reported by Claude Code at 10:00");
});
