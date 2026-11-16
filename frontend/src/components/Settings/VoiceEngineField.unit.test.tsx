import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import * as platformModule from "../../platform";
import {
  type PackAction,
  type SpeechEngine,
  VOICEBOX_RELEASES_URL,
  type VoicePackApi,
  VoicePackError,
  type VoicePackServices,
  type VoicePackStatus,
} from "../../voice/voicePackApi";
import { VoiceEngineField } from "./VoiceEngineField";

vi.mock("../../platform", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../platform")>();
  return { ...actual, platformKind: vi.fn(actual.platformKind) };
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});
beforeEach(() => {
  vi.mocked(platformModule.platformKind).mockReturnValue("windows");
});

function status(
  overrides: Partial<VoicePackStatus> & Pick<VoicePackStatus, "state">,
): VoicePackStatus {
  return {
    reason: null,
    error: null,
    progress: null,
    downloadBytes: 5_600_000_000,
    diskBytes: 8_000_000_000,
    needBytes: 8_000_000_000,
    freeBytes: 20_000_000_000,
    gpu: null,
    kokoro: false,
    speed: { kokoro: null, qwen: null },
    fallbacks: [],
    ...overrides,
  };
}

function fakeServices(
  initial: VoicePackStatus,
  onAct?: (action: PackAction) => VoicePackStatus | Promise<VoicePackStatus>,
) {
  const statusFn = vi.fn(async () => initial);
  const actFn = vi.fn(async (action: PackAction) => (onAct ? onAct(action) : initial));
  const openSpeechSettings = vi.fn(async () => {});
  const openUrl = vi.fn();
  const api: VoicePackApi = { status: statusFn, act: actFn, openSpeechSettings };
  const services: VoicePackServices = { api, openUrl };
  return { services, statusFn, actFn, openUrl, openSpeechSettings };
}

async function renderField(
  initial: VoicePackStatus,
  opts: {
    value?: SpeechEngine;
    onAct?: (action: PackAction) => VoicePackStatus | Promise<VoicePackStatus>;
  } = {},
) {
  const onChange = vi.fn();
  const fake = fakeServices(initial, opts.onAct);
  const view = render(
    <VoiceEngineField
      value={opts.value ?? "system"}
      labelId="label"
      readOnlyNoticeId={null}
      onChange={onChange}
      services={fake.services}
    />,
  );
  await act(async () => {});
  return { onChange, ...fake, ...view };
}

/** Every button on the card except the always-present "Get Voicebox" opener. */
function cardButtonNames(): string[] {
  return screen
    .getAllByRole("button")
    .map((button) => button.textContent ?? "")
    .filter((name) => name !== "Get Voicebox");
}

test("no services shows the unavailable note plus the radios", async () => {
  render(
    <VoiceEngineField
      value="system"
      labelId="label"
      readOnlyNoticeId={null}
      onChange={vi.fn()}
      services={null}
    />,
  );
  expect(screen.getByText("Natural voices aren't available in this window.")).toBeTruthy();
  expect(screen.getAllByRole("radio")).toHaveLength(3);
  expect(screen.queryByRole("button")).toBeNull();
});

test("mount reads status, then checks once when unchecked", async () => {
  const statusFn = vi
    .fn()
    .mockResolvedValueOnce(status({ state: "unchecked" }))
    .mockResolvedValue(status({ state: "not_installed" }));
  const actFn = vi.fn(async () => status({ state: "not_installed" }));
  const api: VoicePackApi = {
    status: statusFn,
    act: actFn,
    openSpeechSettings: vi.fn(async () => {}),
  };
  render(
    <VoiceEngineField
      value="system"
      labelId="label"
      readOnlyNoticeId={null}
      onChange={vi.fn()}
      services={{ api, openUrl: vi.fn() }}
    />,
  );
  await act(async () => {});
  expect(statusFn).toHaveBeenCalledTimes(1);
  expect(actFn).toHaveBeenCalledWith("check");
  expect(screen.getByRole("status").textContent).toBe(
    "Natural voices aren't installed. The download is 5.6 GB and needs 8.0 GB free (20.0 GB free now).",
  );
});

test.each([
  ["platform" as const, "Natural voices need Windows on a PC with an NVIDIA graphics card.", null],
  [
    "no_nvidia" as const,
    "VIKI found no NVIDIA graphics driver on this PC, so natural voices can't run here.",
    "Add a Windows voice…",
  ],
  [
    "driver_old" as const,
    "The NVIDIA driver is too old for natural voices; they need driver 570.65 or later.",
    "Check again",
  ],
  ["probe_failed" as const, "VIKI couldn't check the graphics card.", "Check again"],
])("unsupported/%s", async (reason, sentence, button) => {
  await renderField(status({ state: "unsupported", reason }));
  expect(screen.getByRole("status").textContent).toBe(sentence);
  expect(cardButtonNames()).toEqual(button === null ? [] : [button]);
});

test("unsupported/low_vram shows the GB figure", async () => {
  await renderField(
    status({
      state: "unsupported",
      reason: "low_vram",
      gpu: { name: "GPU", memoryMib: 3072, driver: "1" },
    }),
  );
  expect(screen.getByRole("status").textContent).toBe(
    "This graphics card has 3.0 GB of memory; natural voices need 4 GB.",
  );
  expect(cardButtonNames()).toEqual(["Add a Windows voice…"]);
});

test("Add a Windows voice is hidden off Windows", async () => {
  vi.mocked(platformModule.platformKind).mockReturnValue("mac");
  await renderField(status({ state: "unsupported", reason: "no_nvidia" }));
  expect(cardButtonNames()).toEqual([]);
});

test("Add a Windows voice opens the OS speech settings", async () => {
  const { openSpeechSettings } = await renderField(
    status({ state: "unsupported", reason: "no_nvidia" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Add a Windows voice…" }));
  await act(async () => {});
  expect(openSpeechSettings).toHaveBeenCalledTimes(1);
});

test("the Natural voices radio is aria-disabled and its pick dropped when the PC is unsupported", async () => {
  const { onChange } = await renderField(status({ state: "unsupported", reason: "no_nvidia" }));
  const radio = screen.getByRole("radio", { name: "Natural voices" });
  const reasonId = screen.getByRole("status").id;
  expect(reasonId).not.toBe("");
  expect(radio.getAttribute("aria-disabled")).toBe("true");
  expect(radio.getAttribute("aria-describedby")).toBe(reasonId);
  fireEvent.click(radio);
  expect(onChange).not.toHaveBeenCalled();
  // The other two radios stay unaffected by the pack's own unsupported state.
  expect(screen.getByRole("radio", { name: "System voices" }).getAttribute("aria-disabled")).toBe(
    null,
  );
});

test("a stored pack value still shows checked when the PC is unsupported", async () => {
  await renderField(status({ state: "unsupported", reason: "platform" }), { value: "pack" });
  const radio = screen.getByRole("radio", { name: "Natural voices" }) as HTMLInputElement;
  expect(radio.checked).toBe(true);
  expect(radio.getAttribute("aria-disabled")).toBe("true");
});

test("shows sizes, licences and free space before Install", async () => {
  await renderField(
    status({
      state: "not_installed",
      downloadBytes: 5_600_000_000,
      needBytes: 4_000_000_000,
      freeBytes: 12_300_000_000,
    }),
  );
  expect(screen.getByRole("status").textContent).toBe(
    "Natural voices aren't installed. The download is 5.6 GB and needs 4.0 GB free (12.3 GB free now).",
  );
  expect(screen.getByText(/Includes the/).textContent).toBe(
    "Includes the Voicebox server 0.5.0 (MIT), NVIDIA CUDA libraries (NVIDIA licence), the Qwen3-TTS 0.6B CustomVoice model (Apache-2.0) and the Kokoro-82M model (Apache-2.0).",
  );
  const install = screen.getByRole("button", {
    name: "Install natural voices",
  }) as HTMLButtonElement;
  expect(install.disabled).toBe(false);
});

test("drops the free-space parenthesis when freeBytes is unknown", async () => {
  await renderField(
    status({
      state: "not_installed",
      downloadBytes: 5_600_000_000,
      needBytes: 4_000_000_000,
      freeBytes: null,
    }),
  );
  expect(screen.getByRole("status").textContent).toBe(
    "Natural voices aren't installed. The download is 5.6 GB and needs 4.0 GB free.",
  );
});

test("Install is disabled with a hint when free space is short", async () => {
  await renderField(
    status({ state: "not_installed", needBytes: 8_000_000_000, freeBytes: 1_000_000_000 }),
  );
  const install = screen.getByRole("button", {
    name: "Install natural voices",
  }) as HTMLButtonElement;
  expect(install.disabled).toBe(true);
  expect(screen.getByText("Free up space first.")).toBeTruthy();
});

test("licence links open the pinned copies", async () => {
  const { openUrl } = await renderField(status({ state: "not_installed" }));
  fireEvent.click(screen.getByRole("link", { name: "Voicebox server 0.5.0" }));
  fireEvent.click(screen.getByRole("link", { name: "NVIDIA CUDA libraries" }));
  fireEvent.click(screen.getByRole("link", { name: "Qwen3-TTS 0.6B CustomVoice model" }));
  fireEvent.click(screen.getByRole("link", { name: "Kokoro-82M model" }));
  expect(openUrl).toHaveBeenNthCalledWith(
    1,
    "https://github.com/jamiepine/voicebox/blob/v0.5.0/LICENSE",
  );
  expect(openUrl).toHaveBeenNthCalledWith(2, "https://docs.nvidia.com/cuda/eula/index.html");
  expect(openUrl).toHaveBeenNthCalledWith(
    3,
    "https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
  );
  expect(openUrl).toHaveBeenNthCalledWith(4, "https://huggingface.co/hexgrad/Kokoro-82M");
});

test("Install sends onChange before act(install)", async () => {
  const calls: string[] = [];
  const onChange = vi.fn((next: SpeechEngine) => calls.push(`onChange:${next}`));
  const api: VoicePackApi = {
    status: vi.fn(async () => status({ state: "not_installed" })),
    act: vi.fn(async (action: PackAction) => {
      calls.push(`act:${action}`);
      return status({ state: "installing", progress: { step: "server", done: 0, total: 10 } });
    }),
    openSpeechSettings: vi.fn(async () => {}),
  };
  render(
    <VoiceEngineField
      value="system"
      labelId="label"
      readOnlyNoticeId={null}
      onChange={onChange}
      services={{ api, openUrl: vi.fn() }}
    />,
  );
  await act(async () => {});
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Install natural voices" }));
  });
  expect(calls).toEqual(["onChange:pack", "act:install"]);
});

test("read-only Install does not call onChange but still installs", async () => {
  const onChange = vi.fn();
  const actFn = vi.fn(async () =>
    status({ state: "installing", progress: { step: "server", done: 0, total: 10 } }),
  );
  const api: VoicePackApi = {
    status: vi.fn(async () => status({ state: "not_installed" })),
    act: actFn,
    openSpeechSettings: vi.fn(async () => {}),
  };
  render(
    <VoiceEngineField
      value="system"
      labelId="label"
      readOnlyNoticeId="notice"
      onChange={onChange}
      services={{ api, openUrl: vi.fn() }}
    />,
  );
  await act(async () => {});
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Install natural voices" }));
  });
  expect(onChange).not.toHaveBeenCalled();
  expect(actFn).toHaveBeenCalledWith("install");
});

test.each([
  [
    { step: "server", done: 1_000_000_000, total: 2_000_000_000 } as const,
    "Downloading the voice server… 1.0 of 2.0 GB",
  ],
  [
    { step: "model", done: 500_000_000, total: 1_000_000_000 } as const,
    "Downloading the voice model… 0.5 of 1.0 GB",
  ],
  [{ step: "unpack", done: 30, total: 100 } as const, "Unpacking… 30%"],
])("installing/%#", async (progress, sentence) => {
  const { container } = await renderField(status({ state: "installing", progress }));
  expect(screen.getByRole("status").textContent).toBe(sentence);
  expect(container.querySelector("progress")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Cancel" })).toBeTruthy();
});

test("Cancel sends act(cancel)", async () => {
  const { actFn } = await renderField(
    status({ state: "installing", progress: { step: "server", done: 1, total: 2 } }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  await act(async () => {});
  expect(actFn).toHaveBeenCalledWith("cancel");
});

test("shows the percent beside the sentence, aria-hidden, while progress is known", async () => {
  await renderField(
    status({
      state: "installing",
      progress: { step: "server", done: 1_000_000_000, total: 2_000_000_000 },
    }),
  );
  const pct = screen.getByText("50%", { selector: ".voice-pack-pct" });
  expect(pct.getAttribute("aria-hidden")).toBe("true");
});

test("shows a progressbar with no value while the size is unknown", async () => {
  const { container } = await renderField(status({ state: "installing", progress: null }));
  expect(screen.getByRole("status").textContent).toBe("Installing…");
  const bar = screen.getByRole("progressbar");
  expect(bar.getAttribute("aria-valuenow")).toBeNull();
  expect(container.querySelector("progress")).toBeNull();
});

test("no time left is shown before 3 s have passed", async () => {
  vi.useFakeTimers();
  const statusFn = vi
    .fn()
    .mockResolvedValueOnce(
      status({ state: "installing", progress: { step: "server", done: 0, total: 1_000_000_000 } }),
    )
    .mockResolvedValue(
      status({
        state: "installing",
        progress: { step: "server", done: 100_000_000, total: 1_000_000_000 },
      }),
    );
  const api: VoicePackApi = {
    status: statusFn,
    act: vi.fn(async () => status({ state: "installing" })),
    openSpeechSettings: vi.fn(async () => {}),
  };
  render(
    <VoiceEngineField
      value="system"
      labelId="label"
      readOnlyNoticeId={null}
      onChange={vi.fn()}
      services={{ api, openUrl: vi.fn() }}
    />,
  );
  await act(async () => {});
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(screen.getByRole("status").textContent).not.toMatch(/left/);
});

test("shows about 2 min left once two samples 10 s apart show a steady rate", async () => {
  vi.useFakeTimers();
  const statusFn = vi
    .fn()
    .mockResolvedValueOnce(
      status({ state: "installing", progress: { step: "server", done: 0, total: 1_000_000_000 } }),
    )
    .mockResolvedValue(
      status({
        state: "installing",
        progress: { step: "server", done: 100_000_000, total: 1_000_000_000 },
      }),
    );
  const api: VoicePackApi = {
    status: statusFn,
    act: vi.fn(async () => status({ state: "installing" })),
    openSpeechSettings: vi.fn(async () => {}),
  };
  render(
    <VoiceEngineField
      value="system"
      labelId="label"
      readOnlyNoticeId={null}
      onChange={vi.fn()}
      services={{ api, openUrl: vi.fn() }}
    />,
  );
  await act(async () => {});
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_000);
  });
  expect(screen.getByRole("status").textContent).toBe(
    "Downloading the voice server… 0.1 of 1.0 GB · about 2 min left",
  );
});

test.each([
  [
    "cancelled" as const,
    "The download is paused. It continues where it stopped.",
    ["Resume", "Remove"],
  ],
  ["network" as const, "The download stopped because the network failed.", ["Resume"]],
  ["disk_full" as const, "The disk is full. Free up space, then resume.", ["Resume"]],
  [
    "hash_mismatch" as const,
    "A downloaded file didn't match VIKI's pinned copy, so it was deleted.",
    ["Try again"],
  ],
  [
    "remove_failed" as const,
    "VIKI couldn't remove every natural-voice file. Close any program using them, then select Remove again.",
    ["Remove"],
  ],
  [
    "weird_code" as const,
    "VIKI couldn't finish installing natural voices (weird_code).",
    ["Try again", "Remove"],
  ],
])("install_failed/%s", async (error, sentence, buttons) => {
  await renderField(status({ state: "install_failed", error }));
  expect(screen.getByRole("status").textContent).toBe(sentence);
  expect(cardButtonNames()).toEqual(buttons);
});

test.each([
  [
    "write_failed" as const,
    "VIKI couldn't write the voice files to disk.",
    ["Try again", "Remove"],
  ],
  ["not_regular" as const, "VIKI couldn't write the voice files to disk.", ["Try again", "Remove"]],
  [
    "private_folder" as const,
    "VIKI couldn't make its private voice folder.",
    ["Try again", "Remove"],
  ],
  ["setup_failed" as const, "The installer stopped unexpectedly.", ["Try again", "Remove"]],
  ["protocol_error" as const, "The installer stopped unexpectedly.", ["Try again", "Remove"]],
])("install_failed/%s plain words", async (error, sentence, buttons) => {
  await renderField(status({ state: "install_failed", error }));
  expect(screen.getByRole("status").textContent).toBe(sentence);
  expect(cardButtonNames()).toEqual(buttons);
});

test("Resume retries the install", async () => {
  const { actFn } = await renderField(status({ state: "install_failed", error: "network" }));
  fireEvent.click(screen.getByRole("button", { name: "Resume" }));
  await act(async () => {});
  expect(actFn).toHaveBeenCalledWith("install");
});

test("stopped state offers Remove", async () => {
  await renderField(status({ state: "stopped" }));
  expect(screen.getByRole("status").textContent).toBe(
    "Natural voices are installed. They start when VIKI reads a reply.",
  );
  expect(cardButtonNames()).toEqual(["Remove"]);
});

test("stopped state drops the GPU part when gpu is null", async () => {
  await renderField(status({ state: "stopped", diskBytes: 7_900_000_000, gpu: null }));
  expect(screen.getByText("Uses 7.9 GB on disk")).toBeTruthy();
});

test("starting state has no buttons", async () => {
  await renderField(status({ state: "starting" }));
  expect(screen.getByRole("status").textContent).toBe("Natural voices are starting…");
  expect(cardButtonNames()).toEqual([]);
});

test("ready state offers Remove", async () => {
  await renderField(status({ state: "ready" }));
  expect(screen.getByRole("status").textContent).toBe("Natural voices are ready.");
  expect(cardButtonNames()).toEqual(["Remove"]);
});

test("ready state shows disk use and the GPU name", async () => {
  await renderField(
    status({
      state: "ready",
      diskBytes: 7_900_000_000,
      gpu: { name: "NVIDIA GeForce RTX 4070", memoryMib: 12288, driver: "570.65" },
    }),
  );
  expect(screen.getByText("Uses 7.9 GB on disk · NVIDIA GeForce RTX 4070")).toBeTruthy();
});

test("removing state has no buttons", async () => {
  await renderField(status({ state: "removing" }));
  expect(screen.getByRole("status").textContent).toBe("Removing natural voices…");
  expect(cardButtonNames()).toEqual([]);
});

test("failed state offers Try again, Repair and Remove", async () => {
  const { actFn } = await renderField(status({ state: "failed", error: "crash" }));
  expect(screen.getByRole("status").textContent).toBe("Natural voices stopped working (crash).");
  expect(cardButtonNames()).toEqual(["Try again", "Repair", "Remove"]);
  fireEvent.click(screen.getByRole("button", { name: "Repair" }));
  await act(async () => {});
  expect(actFn).toHaveBeenCalledWith("repair");
});

test.each([
  ["start_timeout" as const, "Natural voices took too long to start."],
  ["crashed" as const, "Natural voices stopped unexpectedly."],
  ["missing_exe" as const, "Some natural-voice files are missing. Repair restores them."],
  ["port_taken" as const, "Another program took the port natural voices were starting on."],
])("failed/%s plain words", async (error, sentence) => {
  await renderField(status({ state: "failed", error }));
  expect(screen.getByRole("status").textContent).toBe(sentence);
  expect(cardButtonNames()).toEqual(["Try again", "Repair", "Remove"]);
});

test("Remove shows an inline confirm naming the download size, then removes", async () => {
  const { actFn } = await renderField(status({ state: "ready", downloadBytes: 5_600_000_000 }));
  fireEvent.click(screen.getByRole("button", { name: "Remove" }));
  expect(screen.getByRole("status").textContent).toBe(
    "Remove natural voices? Installing them again downloads 5.6 GB.",
  );
  fireEvent.click(screen.getByRole("button", { name: "Remove" }));
  await act(async () => {});
  expect(actFn).toHaveBeenCalledWith("remove");
});

test("Keep cancels the remove confirm", async () => {
  await renderField(status({ state: "ready" }));
  fireEvent.click(screen.getByRole("button", { name: "Remove" }));
  fireEvent.click(screen.getByRole("button", { name: "Keep" }));
  expect(screen.getByRole("status").textContent).toBe("Natural voices are ready.");
});

test("a rejected action shows an error line without losing the card", async () => {
  const api: VoicePackApi = {
    status: vi.fn(async () => status({ state: "stopped" })),
    act: vi.fn(async () => {
      throw new VoicePackError("boom");
    }),
    openSpeechSettings: vi.fn(async () => {}),
  };
  render(
    <VoiceEngineField
      value="system"
      labelId="label"
      readOnlyNoticeId={null}
      onChange={vi.fn()}
      services={{ api, openUrl: vi.fn() }}
    />,
  );
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "Remove" }));
  fireEvent.click(screen.getByRole("button", { name: "Remove" }));
  await act(async () => {});
  expect(screen.getByRole("alert").textContent).toBe("That didn't work (boom). Try again.");
  expect(screen.getByRole("status").textContent).toBe(
    "Natural voices are installed. They start when VIKI reads a reply.",
  );
});

test.each([
  ["pack_busy" as const, "Natural voices are busy with another change. Try again in a moment."],
  ["pack_unsupported" as const, "This PC can't run natural voices."],
])("action error %s shows its plain sentence", async (code, sentence) => {
  const api: VoicePackApi = {
    status: vi.fn(async () => status({ state: "stopped" })),
    act: vi.fn(async () => {
      throw new VoicePackError(code);
    }),
    openSpeechSettings: vi.fn(async () => {}),
  };
  render(
    <VoiceEngineField
      value="system"
      labelId="label"
      readOnlyNoticeId={null}
      onChange={vi.fn()}
      services={{ api, openUrl: vi.fn() }}
    />,
  );
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "Remove" }));
  fireEvent.click(screen.getByRole("button", { name: "Remove" }));
  await act(async () => {});
  expect(screen.getByRole("alert").textContent).toBe(sentence);
});

test("a rejected remove re-reads status; a poll state clears the error and the poll continues", async () => {
  vi.useFakeTimers();
  const statusFn = vi
    .fn()
    .mockResolvedValueOnce(status({ state: "ready" }))
    .mockResolvedValueOnce(status({ state: "removing" }))
    .mockResolvedValue(status({ state: "removing" }));
  const api: VoicePackApi = {
    status: statusFn,
    act: vi.fn(async () => {
      throw new VoicePackError("unavailable");
    }),
    openSpeechSettings: vi.fn(async () => {}),
  };
  render(
    <VoiceEngineField
      value="system"
      labelId="label"
      readOnlyNoticeId={null}
      onChange={vi.fn()}
      services={{ api, openUrl: vi.fn() }}
    />,
  );
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "Remove" }));
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
  });
  expect(statusFn).toHaveBeenCalledTimes(2);
  expect(screen.getByRole("status").textContent).toBe("Removing natural voices…");
  expect(screen.queryByRole("alert")).toBeNull();
  // The re-read landed on a poll state, so the poll effect keeps re-reading on its own.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1000);
  });
  expect(statusFn).toHaveBeenCalledTimes(3);
});

test("a pack_busy refusal whose status re-read shows another window's install clears the error", async () => {
  const statusFn = vi
    .fn()
    .mockResolvedValueOnce(status({ state: "stopped" }))
    .mockResolvedValueOnce(
      status({
        state: "installing",
        progress: { step: "server", done: 1_000_000_000, total: 2_000_000_000 },
      }),
    );
  const api: VoicePackApi = {
    status: statusFn,
    act: vi.fn(async () => {
      throw new VoicePackError("pack_busy");
    }),
    openSpeechSettings: vi.fn(async () => {}),
  };
  render(
    <VoiceEngineField
      value="system"
      labelId="label"
      readOnlyNoticeId={null}
      onChange={vi.fn()}
      services={{ api, openUrl: vi.fn() }}
    />,
  );
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "Remove" }));
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
  });
  expect(screen.getByRole("status").textContent).toBe(
    "Downloading the voice server… 1.0 of 2.0 GB",
  );
  expect(screen.queryByRole("alert")).toBeNull();
});

test("a plain action failure whose status re-read is unchanged keeps the error line", async () => {
  const statusFn = vi
    .fn()
    .mockResolvedValueOnce(status({ state: "ready" }))
    .mockResolvedValueOnce(status({ state: "ready" }));
  const api: VoicePackApi = {
    status: statusFn,
    act: vi.fn(async () => {
      throw new VoicePackError("boom");
    }),
    openSpeechSettings: vi.fn(async () => {}),
  };
  render(
    <VoiceEngineField
      value="system"
      labelId="label"
      readOnlyNoticeId={null}
      onChange={vi.fn()}
      services={{ api, openUrl: vi.fn() }}
    />,
  );
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "Remove" }));
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
  });
  expect(statusFn).toHaveBeenCalledTimes(2);
  expect(screen.getByRole("alert").textContent).toBe("That didn't work (boom). Try again.");
  expect(screen.getByRole("status").textContent).toBe("Natural voices are ready.");
});

test("polls every second while installing, and stops on unmount", async () => {
  vi.useFakeTimers();
  const statusFn = vi.fn(async () =>
    status({ state: "installing", progress: { step: "server", done: 1, total: 10 } }),
  );
  const api: VoicePackApi = {
    status: statusFn,
    act: vi.fn(async () => status({ state: "installing" })),
    openSpeechSettings: vi.fn(async () => {}),
  };
  const { unmount } = render(
    <VoiceEngineField
      value="system"
      labelId="label"
      readOnlyNoticeId={null}
      onChange={vi.fn()}
      services={{ api, openUrl: vi.fn() }}
    />,
  );
  await act(async () => {});
  expect(statusFn).toHaveBeenCalledTimes(1);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1000);
  });
  expect(statusFn).toHaveBeenCalledTimes(2);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1000);
  });
  expect(statusFn).toHaveBeenCalledTimes(3);
  unmount();
  await vi.advanceTimersByTimeAsync(2000);
  expect(statusFn).toHaveBeenCalledTimes(3);
});

test("picking a radio sends its value", async () => {
  const onChange = vi.fn();
  render(
    <VoiceEngineField
      value="system"
      labelId="label"
      readOnlyNoticeId={null}
      onChange={onChange}
      services={null}
    />,
  );
  fireEvent.click(screen.getByRole("radio", { name: "Natural voices" }));
  expect(onChange).toHaveBeenCalledWith("pack");
  fireEvent.click(screen.getByRole("radio", { name: "My own Voicebox app (advanced)" }));
  expect(onChange).toHaveBeenCalledWith("voicebox");
});

test("read-only keeps radios focusable, names the notice, and drops changes", async () => {
  const onChange = vi.fn();
  render(
    <VoiceEngineField
      value="system"
      labelId="label"
      readOnlyNoticeId="notice"
      onChange={onChange}
      services={null}
    />,
  );
  const radio = screen.getByRole("radio", { name: "Natural voices" });
  expect(radio.getAttribute("aria-disabled")).toBe("true");
  expect(radio.getAttribute("aria-describedby")).toBe("notice");
  fireEvent.click(radio);
  expect(onChange).not.toHaveBeenCalled();
});

test("the Voicebox option explains itself and opens the releases page", async () => {
  const { openUrl } = await renderField(status({ state: "ready" }));
  expect(screen.getByText("Uses the Voicebox app you install and run yourself.")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Get Voicebox" }));
  expect(openUrl).toHaveBeenCalledWith(VOICEBOX_RELEASES_URL);
});
