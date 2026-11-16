import { expect, test, vi } from "vitest";
import {
  createVoicePackApi,
  installedState,
  type PackAction,
  type PackState,
  speechEngine,
  VoicePackError,
} from "./voicePackApi";

const GOOD_WIRE = {
  state: "installing",
  reason: null,
  error: null,
  progress: { step: "model", done: 3, total: 10 },
  download_bytes: 1000,
  disk_bytes: 2000,
  need_bytes: 3000,
  free_bytes: 400_000,
  gpu: { name: "RTX 4090", memory_mib: 24_576, driver: "560.1" },
  kokoro: true,
  speed: { kokoro: 0.025, qwen: 4.1 },
  fallbacks: [{ code: "voicebox_timeout", at: "2026-10-19T14:45:51Z" }],
};

const GOOD_PARSED = {
  state: "installing",
  reason: null,
  error: null,
  progress: { step: "model", done: 3, total: 10 },
  downloadBytes: 1000,
  diskBytes: 2000,
  needBytes: 3000,
  freeBytes: 400_000,
  gpu: { name: "RTX 4090", memoryMib: 24_576, driver: "560.1" },
  kokoro: true,
  speed: { kokoro: 0.025, qwen: 4.1 },
  fallbacks: [{ code: "voicebox_timeout", at: "2026-10-19T14:45:51Z" }],
};

const okJson = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
const refusal = (code: string, status = 502) => okJson({ error: { code, message: "x" } }, status);

const codeOf = (promise: Promise<unknown>) =>
  promise.then(
    () => null,
    (error: unknown) => (error instanceof VoicePackError ? error.code : `other:${String(error)}`),
  );

test("status sends a Bearer GET and parses the pack envelope", async () => {
  const fetcher = vi.fn<typeof fetch>().mockResolvedValue(okJson({ pack: GOOD_WIRE }));
  const result = await createVoicePackApi("tok", fetcher).status();
  expect(result).toEqual(GOOD_PARSED);
  const [url, init] = fetcher.mock.calls[0] as unknown as [string, RequestInit];
  expect(url).toBe("/api/v2/voice/pack");
  expect(init.method).toBe("GET");
  expect(init.headers).toEqual({ authorization: "Bearer tok" });
});

test("status parses null progress, null gpu, null free_bytes, null speed values and no fallbacks", async () => {
  const wire = {
    ...GOOD_WIRE,
    reason: "no_nvidia",
    error: "boom",
    progress: null,
    free_bytes: null,
    gpu: null,
    kokoro: false,
    speed: { kokoro: null, qwen: null },
    fallbacks: [],
  };
  const fetcher = vi.fn<typeof fetch>().mockResolvedValue(okJson({ pack: wire }));
  const result = await createVoicePackApi("tok", fetcher).status();
  expect(result).toEqual({
    ...GOOD_PARSED,
    reason: "no_nvidia",
    error: "boom",
    progress: null,
    freeBytes: null,
    gpu: null,
    kokoro: false,
    speed: { kokoro: null, qwen: null },
    fallbacks: [],
  });
});

test("status refuses more than three fallbacks but accepts exactly three", async () => {
  const three = [0, 1, 2].map((n) => ({
    code: `voicebox_timeout`,
    at: `2026-10-19T14:5${n}:00Z`,
  }));
  const okFetcher = vi
    .fn<typeof fetch>()
    .mockResolvedValue(okJson({ pack: { ...GOOD_WIRE, fallbacks: three } }));
  const result = await createVoicePackApi("tok", okFetcher).status();
  expect(result.fallbacks).toEqual(three);

  const four = [...three, { code: "voicebox_timeout", at: "2026-10-19T14:59:00Z" }];
  const badFetcher = vi
    .fn<typeof fetch>()
    .mockResolvedValue(okJson({ pack: { ...GOOD_WIRE, fallbacks: four } }));
  expect(await codeOf(createVoicePackApi("tok", badFetcher).status())).toBe("unavailable");
});

test("status refuses a bare body with no pack envelope", async () => {
  const fetcher = vi.fn<typeof fetch>().mockResolvedValue(okJson(GOOD_WIRE));
  expect(await codeOf(createVoicePackApi("tok", fetcher).status())).toBe("unavailable");
});

test("status is strict: an extra or missing key, a bad enum, or a wrong type refuses", async () => {
  const { reason: _reason, ...missingReason } = GOOD_WIRE;
  const { kokoro: _kokoro, ...missingKokoro } = GOOD_WIRE;
  const { speed: _speed, ...missingSpeed } = GOOD_WIRE;
  const { fallbacks: _fallbacks, ...missingFallbacks } = GOOD_WIRE;
  const bad: Record<string, unknown>[] = [
    { ...GOOD_WIRE, extra: 1 },
    missingReason,
    { ...GOOD_WIRE, state: "weird" },
    { ...GOOD_WIRE, reason: "weird" },
    { ...GOOD_WIRE, error: 1 },
    { ...GOOD_WIRE, progress: { ...GOOD_WIRE.progress, extra: 1 } },
    { ...GOOD_WIRE, progress: { step: "weird", done: 3, total: 10 } },
    { ...GOOD_WIRE, progress: { step: "model", done: -1, total: 10 } },
    { ...GOOD_WIRE, progress: { step: "model", done: 3, total: 2 } },
    { ...GOOD_WIRE, progress: { step: "model", done: 3.5, total: 10 } },
    { ...GOOD_WIRE, download_bytes: "1000" },
    { ...GOOD_WIRE, free_bytes: "400000" },
    { ...GOOD_WIRE, gpu: { name: "RTX 4090", memory_mib: 24_576 } },
    { ...GOOD_WIRE, gpu: { name: 1, memory_mib: 24_576, driver: "560.1" } },
    missingKokoro,
    { ...GOOD_WIRE, kokoro: 1 },
    missingSpeed,
    { ...GOOD_WIRE, speed: { kokoro: 0.025 } },
    { ...GOOD_WIRE, speed: { kokoro: 0.025, qwen: 4.1, extra: 1 } },
    { ...GOOD_WIRE, speed: { kokoro: "0.025", qwen: 4.1 } },
    { ...GOOD_WIRE, speed: null },
    missingFallbacks,
    { ...GOOD_WIRE, fallbacks: {} },
    { ...GOOD_WIRE, fallbacks: [{ code: "x" }] },
    { ...GOOD_WIRE, fallbacks: [{ code: 1, at: "2026-10-19T00:00:00Z" }] },
    { ...GOOD_WIRE, fallbacks: [{ code: "x", at: "y", extra: 1 }] },
  ];
  for (const wire of bad) {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(okJson({ pack: wire }));
    expect(await codeOf(createVoicePackApi("tok", fetcher).status())).toBe("unavailable");
  }
});

test("status maps a refusal's code, and an unparsable refusal is unavailable", async () => {
  const refused = vi.fn<typeof fetch>().mockResolvedValue(refusal("voicebox_pack_failed", 503));
  expect(await codeOf(createVoicePackApi("tok", refused).status())).toBe("voicebox_pack_failed");
  const garbled = vi
    .fn<typeof fetch>()
    .mockResolvedValue(new Response("nonsense", { status: 500 }));
  expect(await codeOf(createVoicePackApi("tok", garbled).status())).toBe("unavailable");
});

const ACTIONS: readonly PackAction[] = [
  "check",
  "install",
  "repair",
  "cancel",
  "remove",
  "retry",
  "warm",
];

test("act posts {} with Bearer JSON to the action's own path, and parses the pack envelope", async () => {
  for (const action of ACTIONS) {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(okJson({ pack: GOOD_WIRE }));
    const result = await createVoicePackApi("tok", fetcher).act(action);
    expect(result).toEqual(GOOD_PARSED);
    const [url, init] = fetcher.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`/api/v2/voice/pack/${action}`);
    expect(init.method).toBe("POST");
    expect(init.body).toBe("{}");
    expect(init.headers).toEqual({
      authorization: "Bearer tok",
      "content-type": "application/json",
    });
  }
});

/** A fetch that never answers and rejects once its signal aborts, as the platform's does. */
function hangingFetch(): typeof fetch {
  return vi.fn<typeof fetch>(
    (_url, init) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener(
          "abort",
          () => reject(new DOMException("aborted", "AbortError")),
          { once: true },
        );
      }),
  );
}

// check runs nvidia-smi for up to 15 s, repair stops a running server first, and remove answers
// only once about 7.9 GB is deleted, so those three outlast the 15 s every other call gets.
const BUDGETS: [PackAction | "status", number][] = [
  ["status", 15_000],
  ["install", 15_000],
  ["cancel", 15_000],
  ["retry", 15_000],
  ["warm", 15_000],
  ["check", 30_000],
  ["repair", 30_000],
  ["remove", 120_000],
];

test.each(BUDGETS)("%s gives up after %i ms, not before", async (action, budget) => {
  vi.useFakeTimers();
  try {
    const api = createVoicePackApi("tok", hangingFetch());
    let settled = false;
    (action === "status" ? api.status() : api.act(action))
      .catch(() => {})
      .finally(() => {
        settled = true;
      });
    await vi.advanceTimersByTimeAsync(budget - 1);
    expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(settled).toBe(true);
  } finally {
    vi.useRealTimers();
  }
});

test("act maps a refusal's code", async () => {
  const fetcher = vi.fn<typeof fetch>().mockResolvedValue(refusal("voicebox_pack_missing", 409));
  expect(await codeOf(createVoicePackApi("tok", fetcher).act("install"))).toBe(
    "voicebox_pack_missing",
  );
});

test("openSpeechSettings posts {} and resolves once the server confirms it opened", async () => {
  const fetcher = vi.fn<typeof fetch>().mockResolvedValue(okJson({ opened: true }));
  await expect(createVoicePackApi("tok", fetcher).openSpeechSettings()).resolves.toBeUndefined();
  const [url, init] = fetcher.mock.calls[0] as unknown as [string, RequestInit];
  expect(url).toBe("/api/v2/voice/speech-settings");
  expect(init.method).toBe("POST");
  expect(init.body).toBe("{}");
  expect(init.headers).toEqual({
    authorization: "Bearer tok",
    "content-type": "application/json",
  });
});

test("openSpeechSettings refuses a false or malformed opened answer", async () => {
  const falsey = vi.fn<typeof fetch>().mockResolvedValue(okJson({ opened: false }));
  expect(await codeOf(createVoicePackApi("tok", falsey).openSpeechSettings())).toBe("unavailable");
  const extra = vi.fn<typeof fetch>().mockResolvedValue(okJson({ opened: true, extra: 1 }));
  expect(await codeOf(createVoicePackApi("tok", extra).openSpeechSettings())).toBe("unavailable");
});

test("installedState is true once a server exists to talk to, even a failed one", () => {
  const expected: Record<PackState, boolean> = {
    unchecked: false,
    checking: false,
    unsupported: false,
    not_installed: false,
    installing: false,
    install_failed: false,
    removing: false,
    stopped: true,
    starting: true,
    ready: true,
    failed: true,
  };
  for (const [state, want] of Object.entries(expected))
    expect(installedState(state as PackState)).toBe(want);
});

test.each([
  {
    when: "a valid engine with a profile",
    engine: "system",
    profile: "p",
    want: "system",
  },
  { when: "pack with no profile", engine: "pack", profile: null, want: "pack" },
  {
    when: "voicebox with no profile",
    engine: "voicebox",
    profile: null,
    want: "voicebox",
  },
  {
    when: "no engine but a profile",
    engine: null,
    profile: "p",
    want: "voicebox",
  },
  {
    when: "neither engine nor profile",
    engine: null,
    profile: null,
    want: "system",
  },
  {
    when: "an invalid engine with a profile",
    engine: "bogus",
    profile: "p",
    want: "voicebox",
  },
  {
    when: "the engine key missing and a non-string profile",
    engine: undefined,
    profile: 7,
    want: "system",
  },
] as const)("speechEngine: $when -> $want", ({ engine, profile, want }) => {
  const settings: Record<string, unknown> =
    engine === undefined
      ? { "voice.voicebox_profile": profile }
      : { "voice.speech_engine": engine, "voice.voicebox_profile": profile };
  expect(speechEngine(settings)).toBe(want);
});
