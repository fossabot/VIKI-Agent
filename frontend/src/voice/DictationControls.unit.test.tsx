import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { DictationStrip, MicButton } from "./DictationControls";
import type { DictationControls, DictationPhase, DictationView } from "./dictationTypes";

afterEach(() => {
  cleanup();
});

function view(phase: DictationPhase, extra: Partial<DictationView> = {}): DictationView {
  return {
    phase,
    status: null,
    provisional: "",
    elapsedMs: 0,
    notice: null,
    fallback: null,
    toggled: false,
    ...extra,
  };
}

function controls(): DictationControls {
  return {
    press: vi.fn(),
    release: vi.fn(),
    toggle: vi.fn(),
    cancel: vi.fn(),
    retryGpu: vi.fn(),
    dismiss: vi.fn(),
    compositionEnd: vi.fn(),
  };
}

const mic = (): HTMLElement => screen.getByRole("button", { name: "Dictation" });

test("a ready idle mic is an unpressed, enabled button that says it runs on this computer", () => {
  render(<MicButton view={view("idle")} controls={controls()} />);
  expect(mic().className).toContain("voice-mic-ready");
  expect(mic().getAttribute("aria-pressed")).toBe("false");
  expect(mic().hasAttribute("aria-disabled")).toBe(false);
  expect(mic().title).toContain("on this computer");
});

test("an unavailable mic is unready", () => {
  render(<MicButton view={view("unavailable")} controls={controls()} />);
  expect(mic().className).toContain("voice-mic-unready");
  expect(mic().className).not.toContain("voice-mic-ready");
});

test.each(["starting", "recording"] as const)("%s is aria-pressed", (phase) => {
  render(<MicButton view={view(phase)} controls={controls()} />);
  expect(mic().getAttribute("aria-pressed")).toBe("true");
});

test("a recording mic shows a stop square", () => {
  const { container } = render(<MicButton view={view("recording")} controls={controls()} />);
  expect(container.querySelector(".voice-mic-stop")).toBeTruthy();
  expect(container.querySelector("svg")).toBeNull();
});

test("a finalizing mic shows a spin", () => {
  const { container } = render(<MicButton view={view("finalizing")} controls={controls()} />);
  expect(container.querySelector(".voice-mic-spin")).toBeTruthy();
  expect(container.querySelector("svg.ico")).toBeNull();
});

test("a ready idle mic shows the svg", () => {
  const { container } = render(<MicButton view={view("idle")} controls={controls()} />);
  expect(container.querySelector("svg")).toBeTruthy();
  expect(container.querySelector(".voice-mic-stop")).toBeNull();
  expect(container.querySelector(".voice-mic-spin")).toBeNull();
});

test.each(["finalizing", "cancelling"] as const)("%s is aria-disabled and not pressed", (phase) => {
  render(<MicButton view={view(phase)} controls={controls()} />);
  expect(mic().getAttribute("aria-disabled")).toBe("true");
  expect(mic().getAttribute("aria-pressed")).toBe("false");
});

test("the CPU badge shows only during a fallback", () => {
  const { rerender, container } = render(<MicButton view={view("idle")} controls={controls()} />);
  expect(container.querySelector(".voice-mic-badge")).toBeNull();
  rerender(
    <MicButton
      view={view("idle", { fallback: { backend: "cuda", code: "backend_missing", detail: null } })}
      controls={controls()}
    />,
  );
  expect(container.querySelector(".voice-mic-badge")?.textContent).toBe("CPU");
});

test("pointerdown presses without taking the box's focus, pointerup releases", () => {
  const c = controls();
  render(<MicButton view={view("idle")} controls={c} />);
  // fireEvent returns false when the handler called preventDefault.
  expect(fireEvent.pointerDown(mic())).toBe(false);
  expect(c.press).toHaveBeenCalledTimes(1);
  fireEvent.pointerUp(mic());
  expect(c.release).toHaveBeenCalledTimes(1);
});

test("pointerdown captures the pointer so a release off the button still arrives", () => {
  const c = controls();
  render(<MicButton view={view("idle")} controls={c} />);
  const capture = vi.fn();
  mic().setPointerCapture = capture;
  fireEvent.pointerDown(mic(), { pointerId: 7 });
  expect(capture).toHaveBeenCalledWith(7);
});

test("pointercancel releases once", () => {
  const c = controls();
  render(<MicButton view={view("recording")} controls={c} />);
  fireEvent.pointerCancel(mic());
  expect(c.release).toHaveBeenCalledTimes(1);
});

test("pointerdown without setPointerCapture still presses", () => {
  const c = controls();
  render(<MicButton view={view("idle")} controls={c} />);
  expect("setPointerCapture" in mic()).toBe(false);
  expect(() => fireEvent.pointerDown(mic())).not.toThrow();
  expect(c.press).toHaveBeenCalledTimes(1);
});

test("a keyboard click (detail 0) toggles, a pointer click (detail 1) does nothing", () => {
  const c = controls();
  render(<MicButton view={view("idle")} controls={c} />);
  fireEvent.click(mic(), { detail: 1 });
  expect(c.toggle).not.toHaveBeenCalled();
  fireEvent.click(mic(), { detail: 0 });
  expect(c.toggle).toHaveBeenCalledTimes(1);
});

test("Escape on the mic cancels", () => {
  const c = controls();
  render(<MicButton view={view("recording")} controls={c} />);
  fireEvent.keyDown(mic(), { key: "Escape" });
  expect(c.cancel).toHaveBeenCalledTimes(1);
});

test("the strip draws nothing when idle or unavailable without a notice", () => {
  const { container, rerender } = render(
    <DictationStrip view={view("idle")} controls={controls()} />,
  );
  expect(container.firstChild).toBeNull();
  rerender(<DictationStrip view={view("unavailable")} controls={controls()} />);
  expect(container.firstChild).toBeNull();
  rerender(<DictationStrip view={view("cancelling")} controls={controls()} />);
  expect(container.firstChild).toBeNull();
});

test("a recording strip shows the timer, the provisional text as text, and Cancel", () => {
  const c = controls();
  render(
    <DictationStrip
      view={view("recording", { elapsedMs: 65_400, provisional: "<b>hello</b>" })}
      controls={c}
    />,
  );
  expect(screen.getByRole("timer").textContent).toBe("1:05");
  const live = screen.getByText("<b>hello</b>");
  expect(live.getAttribute("aria-live")).toBe("polite");
  expect(live.className).toBe("voice-provisional");
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(c.cancel).toHaveBeenCalledTimes(1);
});

test("a finalizing strip still offers Cancel", () => {
  render(<DictationStrip view={view("finalizing")} controls={controls()} />);
  expect(screen.getByRole("button", { name: "Cancel" })).toBeTruthy();
});

test("the fallback line names the code and Try GPU again calls retryGpu", () => {
  const c = controls();
  render(
    <DictationStrip
      view={view("recording", {
        fallback: { backend: "cuda", code: "backend_missing", detail: null },
      })}
      controls={c}
    />,
  );
  expect(screen.getByText(/backend_missing/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Try GPU again" }));
  expect(c.retryGpu).toHaveBeenCalledTimes(1);
});

test("an idle strip with a notice shows it, and Dismiss calls dismiss", () => {
  const c = controls();
  render(<DictationStrip view={view("idle", { notice: "Microphone denied" })} controls={c} />);
  expect(screen.getByText(/Microphone denied/)).toBeTruthy();
  expect(screen.queryByRole("timer")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
  expect(c.dismiss).toHaveBeenCalledTimes(1);
});
