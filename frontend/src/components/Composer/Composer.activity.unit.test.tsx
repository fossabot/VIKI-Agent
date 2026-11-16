import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { narrowBlock, readCss, ruleIn } from "../../testing/css";
import { Composer, type ComposerActivity } from "./Composer";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

test.each<[ComposerActivity["state"], string]>([
  ["idle", "Idle"],
  ["active", "Working"],
  ["waiting", "Waiting"],
  ["offline", "Offline"],
  ["error", "Attention"],
])("the %s dock names observed work beside the beam-decorated composer", (state, caption) => {
  const label = "<img src=x onerror=alert(1)> observed status";
  const { container } = render(
    <Composer activity={{ state, label }} running={false} onSend={vi.fn()} onStop={vi.fn()} />,
  );
  const status = screen.getByRole("status", { name: "Chat activity" });
  const field = screen.getByRole("group", { name: "Chat composer" });
  const host = field.parentElement;
  expect(host?.matches(".composer-beam-host")).toBe(true);
  const beam = host?.querySelector<HTMLDivElement>(".composer-beam");
  if (state === "active") {
    expect(beam?.querySelector(".composer-beam-bloom")).not.toBeNull();
    expect(beam?.getAttribute("aria-hidden")).toBe("true");
  } else {
    expect(beam).toBeNull();
  }
  expect(status.textContent).toContain(caption);
  expect(status.textContent).toContain(label);
  expect(status.querySelector("img, button, a, [tabindex]")).toBeNull();
  expect(status.querySelector(".activity-orb")?.getAttribute("data-state")).toBe(state);
  expect(container.querySelectorAll(".activity-orb")).toHaveLength(1);
});

test("activity transitions retain the editor, draft, focus and Send behavior", async () => {
  const onSend = vi.fn(async () => true);
  const props = { running: false, onSend, onStop: vi.fn() };
  const { rerender } = render(
    <Composer {...props} activity={{ state: "active", label: "Reply in progress" }} />,
  );
  const box = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  fireEvent.change(box, { target: { value: "Keep this draft" } });
  box.focus();
  box.setSelectionRange(4, 8);
  rerender(<Composer {...props} activity={{ state: "waiting", label: "Waiting for approval" }} />);
  expect(screen.getByRole("textbox", { name: "Message" })).toBe(box);
  expect(box.value).toBe("Keep this draft");
  expect(document.activeElement).toBe(box);
  expect([box.selectionStart, box.selectionEnd]).toEqual([4, 8]);
  expect(box.parentElement?.parentElement?.querySelector(".composer-beam")).toBeNull();
  await act(async () => {
    fireEvent.keyDown(box, { key: "Enter" });
  });
  expect(onSend).toHaveBeenCalledExactlyOnceWith("Keep this draft");
});

test("the beam's oscillator stops through unmount while its observed host and editor persist", async () => {
  const initialMotion = document.documentElement.getAttribute("data-motion");
  const observed: Element[] = [];
  vi.stubGlobal(
    "IntersectionObserver",
    class {
      observe(target: Element) {
        observed.push(target);
      }
      disconnect() {}
    },
  );
  const frames = new Map<number, FrameRequestCallback>();
  let nextId = 0;
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    frames.set(++nextId, callback);
    return nextId;
  });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => frames.delete(id));
  const props = { running: false, onSend: vi.fn(), onStop: vi.fn() };
  const { rerender, unmount } = render(
    <Composer {...props} activity={{ state: "active", label: "Reply in progress" }} />,
  );
  const box = screen.getByRole("textbox", { name: "Message" });
  const host = box.parentElement?.parentElement;
  const beam = host?.querySelector<HTMLDivElement>(".composer-beam");
  expect(beam).toBeInstanceOf(HTMLDivElement);
  if (!(beam instanceof HTMLDivElement)) return;
  const width = "--beam-w1";
  const pending = [...frames.values()];
  frames.clear();
  act(() => {
    for (const callback of pending) callback(1000);
  });
  expect(beam.style.getPropertyValue(width)).not.toBe("");
  expect(frames.size).toBe(1);
  expect(observed.filter((element) => element === host)).toHaveLength(1);
  rerender(<Composer {...props} activity={{ state: "waiting", label: "Waiting for approval" }} />);
  expect(host?.querySelector(".composer-beam")).toBeNull();
  expect(frames.size).toBe(0);
  expect(screen.getByRole("textbox", { name: "Message" })).toBe(box);
  expect(box.parentElement?.parentElement).toBe(host);
  rerender(<Composer {...props} activity={{ state: "active", label: "Reply in progress" }} />);
  expect(frames.size).toBe(1);
  expect(host?.querySelector(".composer-beam")).not.toBeNull();
  expect(observed.filter((element) => element === host)).toHaveLength(1);
  try {
    await act(async () => {
      document.documentElement.dataset.motion = "reduce";
    });
    expect(host?.dataset.motion).toBe("reduce");
    expect(host?.querySelector(".composer-beam")).toBeNull();
    expect(frames.size).toBe(0);
  } finally {
    if (initialMotion === null) document.documentElement.removeAttribute("data-motion");
    else document.documentElement.setAttribute("data-motion", initialMotion);
  }
  unmount();
  expect(frames.size).toBe(0);
});

test("legacy callers show neutral idle, running and blocked observations", () => {
  const props = { onSend: vi.fn(), onStop: vi.fn() };
  const { rerender } = render(<Composer {...props} running={false} />);
  expect(screen.getByRole("status", { name: "Chat activity" }).textContent).toContain(
    "No reply running",
  );
  rerender(<Composer {...props} running />);
  expect(screen.getByRole("status", { name: "Chat activity" }).textContent).toContain(
    "Reply in progress",
  );
  rerender(<Composer {...props} running blockedReason="Waiting for approval" />);
  expect(screen.getByRole("status", { name: "Chat activity" }).textContent).toContain(
    "Send unavailable",
  );
});

const css = readCss(import.meta.url, "Composer.css").replace(/\/\*[\s\S]*?\*\//g, "");
const beamCss = readCss(import.meta.url, "ComposerBeam.css").replace(/\/\*[\s\S]*?\*\//g, "");

test("focus stays visible and the stable beam host keeps the decoration aligned", () => {
  expect(ruleIn(css, ".composer:focus-within")).toContain("outline: 2px solid var(--t2)");
  expect(ruleIn(css, ".composer:focus-within")).toContain("outline-offset: 2px");
  expect(narrowBlock(css, "@media (forced-colors: active)")).toContain("outline-color: Highlight");
  expect(ruleIn(css, ".composer-beam-host")).toContain("position: relative");
  expect(ruleIn(css, ".composer-beam-host")).toContain("width: 100%");
  expect(ruleIn(beamCss, ".composer-beam")).toContain("position: absolute");
  expect(ruleIn(beamCss, ".composer-beam")).toContain("inset: 0");
  expect(ruleIn(beamCss, ".composer-beam")).toContain("pointer-events: none");
  expect(css).not.toContain("composer-beam-fade-out");
  expect(css).not.toContain("animation-play-state: paused, running");
  expect(
    narrowBlock(
      beamCss,
      "@media (prefers-reduced-motion: reduce), (prefers-contrast: more), (forced-colors: active)",
    ),
  ).toContain("visibility: hidden");
  expect(ruleIn(css, ".composer-activity-copy")).toContain("min-width: 0");
  expect(ruleIn(css, ".composer-activity-label")).toContain("overflow-wrap: anywhere");
});

// --- working line: the label span's shimmer, remount and elapsed clock --------------------------

test("the label shimmers only while active", () => {
  const props = { running: false, onSend: vi.fn(), onStop: vi.fn() };
  const { container, rerender } = render(
    <Composer {...props} activity={{ state: "waiting", label: "Waiting" }} />,
  );
  expect(container.querySelector(".composer-activity-text.working-shimmer")).toBeNull();
  expect(container.querySelector(".composer-activity-text")).not.toBeNull();
  rerender(<Composer {...props} activity={{ state: "active", label: "Writing" }} />);
  expect(container.querySelector(".composer-activity-text.working-shimmer")).not.toBeNull();
});

test("the label span re-mounts on a new label", () => {
  const props = { running: false, onSend: vi.fn(), onStop: vi.fn() };
  const { container, rerender } = render(
    <Composer {...props} activity={{ state: "active", label: "Thinking" }} />,
  );
  const first = container.querySelector(".composer-activity-text");
  rerender(<Composer {...props} activity={{ state: "active", label: "Writing" }} />);
  const second = container.querySelector(".composer-activity-text");
  expect(second?.textContent).toBe("Writing");
  expect(second).not.toBeNull();
  expect(second).not.toBe(first);
});

test("elapsed time stays silent under 10 s, then ticks visibly as aria-hidden", () => {
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
  const since = Date.now();
  const props = { running: false, onSend: vi.fn(), onStop: vi.fn() };
  const { container } = render(
    <Composer {...props} activity={{ state: "active", label: "Writing", since }} />,
  );
  act(() => vi.advanceTimersByTime(9000));
  expect(container.querySelector(".composer-activity-elapsed")).toBeNull();
  act(() => vi.advanceTimersByTime(1000));
  const elapsed = container.querySelector(".composer-activity-elapsed");
  expect(elapsed?.textContent).toBe("10 s");
  expect(elapsed?.getAttribute("aria-hidden")).toBe("true");
});

test("no elapsed time renders without `since`, however long the wait", () => {
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
  const props = { running: false, onSend: vi.fn(), onStop: vi.fn() };
  const { container } = render(
    <Composer {...props} activity={{ state: "active", label: "Writing" }} />,
  );
  act(() => vi.advanceTimersByTime(20_000));
  expect(container.querySelector(".composer-activity-elapsed")).toBeNull();
});

test("the label fades and rises, and the shimmer and the fade both turn off under reduced motion", () => {
  expect(ruleIn(css, ".composer-activity-text")).toContain("composer-activity-in");
  expect(ruleIn(css, ".composer-activity-text.working-shimmer")).toContain("background-clip: text");
  expect(ruleIn(css, ".composer-activity-text.working-shimmer")).toContain(
    "composer-activity-shimmer",
  );
  // The trailing brace picks out the standalone reduced-motion query, not a combined one.
  const reduced = narrowBlock(css, "@media (prefers-reduced-motion: reduce) {");
  expect(reduced).toContain(".composer-activity-text");
  expect(reduced).toContain("animation: none");
  expect(css).toContain('html[data-motion="reduce"] .composer-activity-text');
});
