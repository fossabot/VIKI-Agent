import { act, cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import {
  PACK_LANGUAGES,
  type PackAction,
  type VoicePackApi,
  type VoicePackStatus,
} from "../../voice/voicePackApi";
import { PackVoiceNotes, usePackStatus } from "./PackVoiceNotes";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function status(
  overrides: Partial<VoicePackStatus> & Pick<VoicePackStatus, "state">,
): VoicePackStatus {
  return {
    reason: null,
    error: null,
    progress: null,
    downloadBytes: 0,
    diskBytes: 0,
    needBytes: 0,
    freeBytes: null,
    gpu: null,
    kokoro: false,
    speed: { kokoro: null, qwen: null },
    fallbacks: [],
    ...overrides,
  };
}

/** Every `PACK_LANGUAGES` entry set to "system" except the ones named, so only those stay on
 * their model's default — keeps each fixture's affected-language list short and exact. */
function systemExcept(keep: string[]): Record<string, string> {
  const map: Record<string, string> = {};
  for (const lang of PACK_LANGUAGES) if (!keep.includes(lang)) map[lang] = "system";
  return map;
}

function renderNotes(
  st: VoicePackStatus | null,
  opts: {
    map?: Record<string, string>;
    onChange?: (next: string | null) => void;
    act?: (action: PackAction) => void;
  } = {},
) {
  const onChange = opts.onChange ?? vi.fn();
  const act = opts.act ?? vi.fn();
  const view = render(
    <PackVoiceNotes status={st} map={opts.map ?? {}} onChange={onChange} act={act} />,
  );
  return { onChange, act, ...view };
}

// ---- PackVoiceNotes: the installedState gate ----------------------------------------------

test("renders nothing when status is null", () => {
  const { container } = renderNotes(null);
  expect(container.firstChild).toBeNull();
});

test("renders nothing before the pack is installed", () => {
  const { container } = renderNotes(status({ state: "not_installed" }));
  expect(container.firstChild).toBeNull();
});

test("renders nothing while the first install is still running", () => {
  const { container } = renderNotes(
    status({ state: "installing", progress: { step: "model", done: 1, total: 2 } }),
  );
  expect(container.firstChild).toBeNull();
});

// ---- (1) Add the fast voice / downloading it -----------------------------------------------

test("offers to add the fast voice when Kokoro is missing", () => {
  const { act: actFn } = renderNotes(status({ state: "stopped", kokoro: false }));
  expect(
    screen.getByText(
      "Add the fast voice: Kokoro (0.35 GB) reads English, Chinese, French, Portuguese, Spanish and Italian much faster than Qwen3.",
    ),
  ).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Add the fast voice" }));
  expect(actFn).toHaveBeenCalledWith("install");
});

test("shows neither the add line nor progress once Kokoro is installed", () => {
  renderNotes(status({ state: "ready", kokoro: true }));
  expect(screen.queryByText(/Add the fast voice:/)).toBeNull();
  expect(screen.queryByText(/Downloading the fast voice/)).toBeNull();
});

// ---- (2) Speed ------------------------------------------------------------------------------

test("shows both models' measured speed, Kokoro faster and Qwen3 slower", () => {
  // Qwen3's own languages (ja/ko/de/ru) forced to "system" so neither model carries an active
  // warning (b) here — that interaction, and the dropped speed line it causes, has its own test.
  renderNotes(status({ state: "ready", kokoro: true, speed: { kokoro: 0.025, qwen: 4.1 } }), {
    map: systemExcept(["en", "zh", "fr", "pt", "es", "it"]),
  });
  expect(screen.getByText("Kokoro: about 40 times faster than speech on this PC.")).toBeTruthy();
  expect(screen.getByText("Qwen3: about 4.1 times slower than speech on this PC.")).toBeTruthy();
});

test("says speed isn't measured yet when neither model has run", () => {
  renderNotes(status({ state: "ready" }));
  expect(
    screen.getByText("Speed on this PC: not measured yet. Preview a voice to measure it."),
  ).toBeTruthy();
});

test("shows only the measured model's line when just one has run", () => {
  renderNotes(status({ state: "ready", kokoro: true, speed: { kokoro: 0.025, qwen: null } }));
  expect(screen.getByText("Kokoro: about 40 times faster than speech on this PC.")).toBeTruthy();
  expect(screen.queryByText(/Qwen3:/)).toBeNull();
  expect(
    screen.queryByText("Speed on this PC: not measured yet. Preview a voice to measure it."),
  ).toBeNull();
});

// ---- (3) Warning (a): a Kokoro pick with Kokoro missing --------------------------------------

test("warns when a pick names the fast voice but it isn't downloaded", () => {
  const { act: actFn } = renderNotes(status({ state: "stopped", kokoro: false }), {
    map: { en: "af_heart" },
  });
  const warning = screen.getByText(
    "Some languages are set to the fast voice, which isn't downloaded, so the system voice reads them.",
  );
  expect(warning.className).toContain("pack-voice-warning");
  const buttons = screen.getAllByRole("button", { name: "Add the fast voice" });
  fireEvent.click(buttons[buttons.length - 1] as HTMLButtonElement);
  expect(actFn).toHaveBeenCalledWith("install");
});

test("does not warn about a Kokoro pick once Kokoro is installed", () => {
  renderNotes(status({ state: "ready", kokoro: true }), { map: { en: "af_heart" } });
  expect(screen.queryByText(/isn't downloaded/)).toBeNull();
});

test("does not warn when no pick names a Kokoro voice", () => {
  renderNotes(status({ state: "stopped", kokoro: false }), { map: { en: "Ryan" } });
  expect(screen.queryByText(/isn't downloaded/)).toBeNull();
});

// ---- (4) Warning (b): a slow model in effective use -------------------------------------------

test("Kokoro installed: Use the fast voice drops only that model's Kokoro-language picks", () => {
  const onChange = vi.fn();
  // kokoroInstalled: zh/fr/pt/es/it default to Kokoro; en is pinned to Qwen's Ryan; ja/ko/de/ru
  // have no Kokoro voice at all, so they stay on Qwen's own defaults regardless.
  renderNotes(status({ state: "ready", kokoro: true, speed: { kokoro: 0.025, qwen: 4.1 } }), {
    map: { en: "Ryan" },
    onChange,
  });
  expect(
    screen.getByText(
      "Qwen3 makes speech about 4.1 times slower than it plays on this PC, so replies in English, Japanese, Korean, German, and Russian wait for it, and hands-free answers come from the system voice.",
    ),
  ).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Use the fast voice" }));
  expect(onChange).toHaveBeenCalledWith("{}");
});

test("Kokoro missing, a Kokoro-capable language affected: no button of its own, the add offer covers it", () => {
  const { act: actFn } = renderNotes(
    status({ state: "stopped", kokoro: false, speed: { kokoro: null, qwen: 4.1 } }),
    { map: systemExcept(["en", "de"]) },
  );
  expect(
    screen.getByText(
      "Qwen3 makes speech about 4.1 times slower than it plays on this PC, so replies in English and German wait for it, and hands-free answers come from the system voice.",
    ),
  ).toBeTruthy();
  const buttons = screen.getAllByRole("button", { name: "Add the fast voice" });
  expect(buttons).toHaveLength(1); // only the add offer's, not a second one from this warning
  fireEvent.click(buttons[0] as HTMLButtonElement);
  expect(actFn).toHaveBeenCalledWith("install");
});

test("Kokoro missing, only Kokoro-incapable languages affected: Use the system voice for them", () => {
  const onChange = vi.fn();
  renderNotes(status({ state: "stopped", kokoro: false, speed: { kokoro: null, qwen: 4.1 } }), {
    map: systemExcept(["de"]),
    onChange,
  });
  expect(
    screen.getByText(
      "Qwen3 makes speech about 4.1 times slower than it plays on this PC, so replies in German wait for it.",
    ),
  ).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Use the system voice for them" }));
  expect(onChange).toHaveBeenCalledWith(JSON.stringify({ ...systemExcept(["de"]), de: "system" }));
});

test("no slow-model warning at exactly SLOW_FACTOR, or when nothing effectively uses it", () => {
  renderNotes(status({ state: "ready", kokoro: true, speed: { kokoro: 0.025, qwen: 0.75 } }), {
    map: systemExcept([]),
  });
  expect(screen.queryByText(/makes speech about/)).toBeNull();
});

test("warning paragraphs carry a class the other notes don't", () => {
  const { container } = renderNotes(
    status({ state: "ready", kokoro: false, speed: { kokoro: null, qwen: 4.1 } }),
    { map: { en: "af_heart", ...systemExcept(["en", "de"]) } },
  );
  const warnings = container.querySelectorAll("p.pack-voice-warning");
  expect(warnings.length).toBe(2);
  for (const p of container.querySelectorAll("p")) {
    expect(p.children.length).toBe(0); // text nodes only, every <p> in this component
  }
});

// ---- (5) Recent hands-free fallbacks ----------------------------------------------------------

test("lists recent fallbacks, mapping voicebox_* codes and leaving others generic", () => {
  const fallbacks = [
    { code: "voicebox_timeout", at: "2026-10-19T05:51:00.000Z" },
    { code: "voicebox_busy", at: "2026-10-19T05:48:00.000Z" },
    { code: "something_else", at: "2026-10-19T05:45:51.000Z" },
  ];
  const { container } = renderNotes(status({ state: "ready", fallbacks }));
  expect(screen.getByText("Recent hands-free answers the system voice read:")).toBeTruthy();
  const reasons: Record<string, string> = {
    voicebox_timeout: "The natural voice took too long to start, so the system voice read it.",
    voicebox_busy: "The natural voice is busy with another reading. Try again in a moment.",
    something_else: "The natural voice didn't play.",
  };
  for (const fb of fallbacks) {
    const time = new Date(fb.at).toLocaleTimeString();
    expect(screen.getByText(`${time} · ${reasons[fb.code]}`)).toBeTruthy();
  }
  expect(container.querySelectorAll("ul.pack-voice-fallbacks > li")).toHaveLength(3);
});

test("hides the fallback block when there are none", () => {
  renderNotes(status({ state: "ready", fallbacks: [] }));
  expect(screen.queryByText("Recent hands-free answers the system voice read:")).toBeNull();
});

// ---- Fix buttons, speed lines and fallback wording -------------------------------------------

test("Kokoro installed but none of the slow languages is Kokoro's: Use the system voice for them", () => {
  const onChange = vi.fn();
  // kokoroInstalled: ja/ko/de/ru have no Kokoro voice at all, so Qwen3 stays their only model —
  // "Use the fast voice" would drop nothing, as the before-fix button did.
  renderNotes(status({ state: "ready", kokoro: true, speed: { kokoro: 0.025, qwen: 4.1 } }), {
    onChange,
  });
  expect(screen.queryByRole("button", { name: "Use the fast voice" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Use the system voice for them" }));
  expect(onChange).toHaveBeenCalledWith(
    JSON.stringify({ ja: "system", ko: "system", de: "system", ru: "system" }),
  );
});

test("one Add button total when the add offer and warning (a) overlap", () => {
  renderNotes(status({ state: "stopped", kokoro: false }), { map: { en: "af_heart" } });
  expect(screen.getAllByRole("button", { name: "Add the fast voice" })).toHaveLength(1);
});

test("a model with an active warning gets no separate speed line", () => {
  renderNotes(status({ state: "ready", kokoro: true, speed: { kokoro: 0.025, qwen: 4.1 } }));
  expect(screen.getByText("Kokoro: about 40 times faster than speech on this PC.")).toBeTruthy();
  expect(screen.queryByText("Qwen3: about 4.1 times slower than speech on this PC.")).toBeNull();
  expect(screen.getByText(/^Qwen3 makes speech about 4\.1 times slower/)).toBeTruthy();
});

test("the notes are wrapped in the styled pack-voice-notes container", () => {
  const { container } = renderNotes(
    status({ state: "ready", kokoro: true, speed: { kokoro: 0.025, qwen: null } }),
  );
  expect(container.querySelector("div.pack-voice-notes")).toBeTruthy();
});

test("a timeout fallback reads as a timing note, with the new heading and a list item", () => {
  const { container } = renderNotes(
    status({
      state: "ready",
      fallbacks: [{ code: "voicebox_timeout", at: "2026-10-19T05:51:00.000Z" }],
    }),
  );
  expect(screen.getByText("Recent hands-free answers the system voice read:")).toBeTruthy();
  const time = new Date("2026-10-19T05:51:00.000Z").toLocaleTimeString();
  expect(
    screen.getByText(
      `${time} · The natural voice took too long to start, so the system voice read it.`,
    ),
  ).toBeTruthy();
  expect(container.querySelector("ul.pack-voice-fallbacks > li")).toBeTruthy();
});

test("a model's figure living only in its warning still counts as measured", () => {
  // Qwen3 is measured (4.1) but its speed line is dropped because warning (b) carries the figure;
  // Kokoro genuinely isn't measured (null). Neither is "not measured" — one of them is.
  renderNotes(status({ state: "stopped", kokoro: false, speed: { kokoro: null, qwen: 4.1 } }));
  expect(
    screen.queryByText("Speed on this PC: not measured yet. Preview a voice to measure it."),
  ).toBeNull();
  expect(screen.getByText(/^Qwen3 makes speech about 4\.1 times slower/)).toBeTruthy();
});

test("a slow model covering every language says 'every reply' instead of listing ten", () => {
  renderNotes(status({ state: "stopped", kokoro: false, speed: { kokoro: null, qwen: 4.1 } }));
  expect(
    screen.getByText(
      "Qwen3 makes speech about 4.1 times slower than it plays on this PC, so every reply waits for it, and hands-free answers come from the system voice.",
    ),
  ).toBeTruthy();
});

// ---- usePackStatus ----------------------------------------------------------------------------

test("usePackStatus reads status once on mount", async () => {
  const statusFn = vi.fn(async () => status({ state: "stopped" }));
  const api: VoicePackApi = {
    status: statusFn,
    act: vi.fn(),
    openSpeechSettings: vi.fn(async () => {}),
  };
  const services = { api, openUrl: vi.fn() };
  const { result } = renderHook(() => usePackStatus(services));
  await act(async () => {});
  expect(statusFn).toHaveBeenCalledTimes(1);
  expect(result.current.status).toEqual(status({ state: "stopped" }));
});

test("usePackStatus returns null status and skips reads with no services", async () => {
  const { result } = renderHook(() => usePackStatus(null));
  await act(async () => {});
  expect(result.current.status).toBeNull();
});

test("usePackStatus polls every 5s, and every 1s once it sees 'installing'", async () => {
  vi.useFakeTimers();
  const statusFn = vi
    .fn()
    .mockResolvedValueOnce(status({ state: "stopped" }))
    .mockResolvedValueOnce(
      status({ state: "installing", progress: { step: "model", done: 1, total: 2 } }),
    )
    .mockResolvedValue(
      status({ state: "installing", progress: { step: "model", done: 2, total: 2 } }),
    );
  const api: VoicePackApi = {
    status: statusFn,
    act: vi.fn(),
    openSpeechSettings: vi.fn(async () => {}),
  };
  const services = { api, openUrl: vi.fn() };
  renderHook(() => usePackStatus(services));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
  expect(statusFn).toHaveBeenCalledTimes(1); // mount

  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(statusFn).toHaveBeenCalledTimes(2); // 5s poll picks up "installing"

  await act(async () => {
    await vi.advanceTimersByTimeAsync(999);
  });
  expect(statusFn).toHaveBeenCalledTimes(2); // not yet 1s since the switch

  await act(async () => {
    await vi.advanceTimersByTimeAsync(1);
  });
  expect(statusFn).toHaveBeenCalledTimes(3); // 1s cadence while installing
});

test("act() always re-reads status, discarding its own resolved answer", async () => {
  const actReturn = status({ state: "installing", progress: { step: "model", done: 0, total: 1 } });
  const reread = status({ state: "stopped", kokoro: true });
  const statusFn = vi
    .fn()
    .mockResolvedValueOnce(status({ state: "stopped" })) // mount
    .mockResolvedValueOnce(reread); // after act
  const actFn = vi.fn(async () => actReturn);
  const api: VoicePackApi = {
    status: statusFn,
    act: actFn,
    openSpeechSettings: vi.fn(async () => {}),
  };
  const services = { api, openUrl: vi.fn() };
  const { result } = renderHook(() => usePackStatus(services));
  await act(async () => {});

  await act(async () => {
    result.current.act("install");
    for (let turn = 0; turn < 10; turn += 1) await Promise.resolve();
  });
  expect(actFn).toHaveBeenCalledWith("install");
  expect(statusFn).toHaveBeenCalledTimes(2);
  expect(result.current.status).toEqual(reread);
});

test("act() still re-reads status after a rejected action, and a failed read keeps the last status", async () => {
  const stopped = status({ state: "stopped" });
  const statusFn = vi
    .fn()
    .mockResolvedValueOnce(stopped) // mount
    .mockRejectedValueOnce(new Error("boom")); // re-read after the failed act
  const actFn = vi.fn(async () => {
    throw new Error("nope");
  });
  const api: VoicePackApi = {
    status: statusFn,
    act: actFn,
    openSpeechSettings: vi.fn(async () => {}),
  };
  const services = { api, openUrl: vi.fn() };
  const { result } = renderHook(() => usePackStatus(services));
  await act(async () => {});

  await act(async () => {
    result.current.act("install");
    for (let turn = 0; turn < 10; turn += 1) await Promise.resolve();
  });
  expect(actFn).toHaveBeenCalledWith("install");
  expect(statusFn).toHaveBeenCalledTimes(2);
  expect(result.current.status).toEqual(stopped); // the failed re-read kept the last status
});
