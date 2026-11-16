import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { DictationSettingsPanel, useDictationStatus, voiceOptions } from "./DictationSettingsPanel";
import { type DictationApi, MICROPHONE_KEY, type VoiceStatus } from "./dictationTypes";

beforeEach(() => {
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  localStorage.clear();
});

const READY: VoiceStatus = {
  state: "ready",
  running: false,
  model: "large-v3-turbo-q8_0",
  backend: "cuda",
  device: "RTX",
  languages: ["auto", "en", "ja"],
  language: "ja",
  language_unavailable: false,
  models: ["large-v3-turbo-q8_0", "small-q8_0"],
  backends: ["cpu", "cuda"],
  fallback: null,
  cpu_model: "small-q8_0",
  slow_on_cpu: false,
};

function fakeApi(status: VoiceStatus = READY): DictationApi {
  return {
    status: vi.fn(async () => status),
    clearFallback: vi.fn(async () => ({ ...status, fallback: null })),
    socket: vi.fn(),
    startCapture: vi.fn(),
    microphones: vi.fn(async () => [
      { deviceId: "a", label: "Desk mic" },
      { deviceId: "b", label: "" },
    ]),
    workletUrl: "/w.js",
  };
}

function panel(status: VoiceStatus | null, extra: { error?: boolean; retry?: () => void } = {}) {
  render(
    <DictationSettingsPanel
      api={fakeApi()}
      status={status}
      error={extra.error ?? false}
      onRetryGpu={extra.retry ?? (() => {})}
    />,
  );
}

test("voiceOptions: null status keeps the key's own list, else installed options", () => {
  expect(voiceOptions(null, "voice.model")).toBeUndefined();
  expect(voiceOptions(READY, "voice.model")).toEqual(["auto", "large-v3-turbo-q8_0", "small-q8_0"]);
  expect(voiceOptions(READY, "voice.backend")).toEqual(["auto", "cpu", "cuda"]);
  expect(voiceOptions(READY, "voice.dictation_language")).toEqual(["auto", "en", "ja"]);
  expect(voiceOptions(READY, "chat.default_model")).toBeUndefined();
});

test("ready: the model, backend and device, the language named, the privacy sentence", async () => {
  panel(READY);
  expect(document.querySelector('[data-setting-key="voice.status"]')).not.toBeNull();
  expect(screen.getByText("Ready: large-v3-turbo-q8_0, cuda, RTX")).toBeTruthy();
  expect(screen.getByText("Dictation language: Japanese")).toBeTruthy();
  expect(screen.getByText(/Audio stays on this computer/)).toBeTruthy();
  expect(screen.queryByText(/No small model/)).toBeNull();
  expect(screen.queryByText(/15 seconds/)).toBeNull();
  await screen.findByRole("option", { name: "Desk mic" });
});

test("setup needed shows the command; an engine problem says so", () => {
  panel({ ...READY, state: "setup_needed" });
  expect(screen.getByText("uv run python scripts/voice_setup.py")).toBeTruthy();
  cleanup();
  panel({ ...READY, state: "engine_unavailable" });
  expect(screen.getByText(/Speech recognition is not available/)).toBeTruthy();
});

test("a fallback names its code and Try GPU again calls onRetryGpu", () => {
  const retry = vi.fn();
  panel(
    { ...READY, fallback: { backend: "cuda", code: "backend_missing", detail: null } },
    { retry },
  );
  expect(screen.getByText(/backend_missing/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Try GPU again" }));
  expect(retry).toHaveBeenCalledTimes(1);
});

test("the CPU-model hint shows off the CPU when none is installed", () => {
  panel({ ...READY, cpu_model: null });
  expect(
    screen.getByText("uv run python scripts/voice_setup.py --model small-q8_0 --backend cpu"),
  ).toBeTruthy();
  cleanup();
  panel({ ...READY, backend: "cpu", cpu_model: null });
  expect(screen.queryByText(/No small model/)).toBeNull();
});

test("the slow-on-CPU line and the unavailable-language note", () => {
  panel({ ...READY, slow_on_cpu: true, language_unavailable: true, language: "auto" });
  expect(screen.getByText(/about 15 seconds per update/)).toBeTruthy();
  expect(screen.getByText(/saved language is not available/)).toBeTruthy();
  expect(screen.queryByText(/Dictation language:/)).toBeNull();
});

test("auto language reads Auto, and an unread state shows the error line", () => {
  panel({ ...READY, language: "auto" });
  expect(screen.getByText("Dictation language: Auto")).toBeTruthy();
  cleanup();
  panel(null, { error: true });
  expect(screen.getByText(/could not be read/)).toBeTruthy();
});

test("the microphone select saves and removes the key", async () => {
  panel(READY);
  await screen.findByRole("option", { name: "Desk mic" });
  expect(screen.getByRole("option", { name: "Microphone 2" })).toBeTruthy();
  const select = screen.getByRole("combobox") as HTMLSelectElement;
  fireEvent.change(select, { target: { value: "a" } });
  expect(localStorage.getItem(MICROPHONE_KEY)).toBe("a");
  fireEvent.change(select, { target: { value: "" } });
  expect(localStorage.getItem(MICROPHONE_KEY)).toBeNull();
});

test("the select starts on the saved microphone", async () => {
  localStorage.setItem(MICROPHONE_KEY, "a");
  panel(READY);
  await screen.findByRole("option", { name: "Desk mic" });
  expect((screen.getByRole("combobox") as HTMLSelectElement).value).toBe("a");
});

test("useDictationStatus reads, refreshes and replaces the status on retryGpu", async () => {
  const api = fakeApi();
  const { result } = renderHook(() => useDictationStatus(api, true));
  await waitFor(() => expect(result.current.status).toEqual(READY));
  expect(api.status).toHaveBeenCalledTimes(1);
  act(() => result.current.refresh());
  await waitFor(() => expect(api.status).toHaveBeenCalledTimes(2));
  await act(async () => result.current.retryGpu());
  expect(api.clearFallback).toHaveBeenCalledTimes(1);
});

test("useDictationStatus reads nothing when disabled or without an api, and flags a failure", async () => {
  const api = fakeApi();
  renderHook(() => useDictationStatus(api, false));
  renderHook(() => useDictationStatus(null, true));
  expect(api.status).not.toHaveBeenCalled();
  const failing = fakeApi();
  failing.status = vi.fn(async () => {
    throw new Error("down");
  });
  const { result } = renderHook(() => useDictationStatus(failing, true));
  await waitFor(() => expect(result.current.error).toBe(true));
  expect(result.current.status).toBeNull();
});
