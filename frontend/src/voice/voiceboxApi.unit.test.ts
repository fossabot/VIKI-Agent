import { afterEach, expect, test, vi } from "vitest";
import {
  createVoiceboxApi,
  VOICEBOX_LANGUAGES,
  VoiceboxError,
  voiceboxProblem,
} from "./voiceboxApi";

const PROFILE = { id: "p1", name: "Default", language: "ja" };
const STATUS_BODY = { state: "ready", gpu: true, profiles: [PROFILE] };

const okJson = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
const refusal = (code: string, status = 502) => okJson({ error: { code, message: "x" } }, status);
const wav = (bytes: Uint8Array<ArrayBuffer>, status = 200) =>
  new Response(bytes, { status, headers: { "content-type": "audio/wav" } });

const codeOf = (promise: Promise<unknown>) =>
  promise.then(
    () => null,
    (error: unknown) => (error instanceof VoiceboxError ? error.code : `other:${String(error)}`),
  );

afterEach(() => vi.useRealTimers());

test("VOICEBOX_LANGUAGES has 23 codes, including the documented ones", () => {
  expect(VOICEBOX_LANGUAGES).toHaveLength(23);
  for (const code of ["ar", "zh", "en", "fr", "de", "hi", "it", "ja", "ko", "pt", "ru", "es", "tr"])
    expect(VOICEBOX_LANGUAGES).toContain(code);
});

test("VOICEBOX_LANGUAGES mirrors voicebox_http.LANGUAGES exactly, in order", () => {
  expect(VOICEBOX_LANGUAGES).toEqual([
    "zh",
    "en",
    "ja",
    "ko",
    "de",
    "fr",
    "ru",
    "pt",
    "es",
    "it",
    "he",
    "ar",
    "da",
    "el",
    "fi",
    "hi",
    "ms",
    "nl",
    "no",
    "pl",
    "sv",
    "sw",
    "tr",
  ]);
});

test("status sends a Bearer GET and parses the voicebox envelope", async () => {
  const fetcher = vi.fn<typeof fetch>().mockResolvedValue(okJson({ voicebox: STATUS_BODY }));
  const result = await createVoiceboxApi("tok", fetcher).status();
  expect(result).toEqual(STATUS_BODY);
  const [url, init] = fetcher.mock.calls[0] as unknown as [string, RequestInit];
  expect(url).toBe("/api/v2/voice/voicebox");
  expect(init.method).toBe("GET");
  expect(init.headers).toEqual({ authorization: "Bearer tok" });
});

test("status is strict: an extra key, a bad type, an unknown language or too many profiles refuse", async () => {
  const bad = [
    { ...STATUS_BODY, extra: 1 },
    { ...STATUS_BODY, state: "weird" },
    { ...STATUS_BODY, gpu: "yes" },
    { ...STATUS_BODY, profiles: [{ ...PROFILE, extra: 1 }] },
    { ...STATUS_BODY, profiles: [{ ...PROFILE, language: "xx" }] },
    { ...STATUS_BODY, profiles: Array.from({ length: 201 }, () => PROFILE) },
  ];
  for (const body of bad) {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(okJson({ voicebox: body }));
    expect(await codeOf(createVoiceboxApi("tok", fetcher).status())).toBe("unavailable");
  }
});

test("status refuses a bare body with no voicebox envelope", async () => {
  const fetcher = vi.fn<typeof fetch>().mockResolvedValue(okJson(STATUS_BODY));
  expect(await codeOf(createVoiceboxApi("tok", fetcher).status())).toBe("unavailable");
});

test("status maps a refusal's code, and an unparsable refusal is unavailable", async () => {
  const refused = vi.fn<typeof fetch>().mockResolvedValue(refusal("no_settings", 503));
  expect(await codeOf(createVoiceboxApi("tok", refused).status())).toBe("no_settings");
  const garbled = vi
    .fn<typeof fetch>()
    .mockResolvedValue(new Response("nonsense", { status: 500 }));
  expect(await codeOf(createVoiceboxApi("tok", garbled).status())).toBe("unavailable");
});

test("speech posts Bearer JSON and returns the audio bytes", async () => {
  const fetcher = vi.fn<typeof fetch>().mockResolvedValue(wav(new Uint8Array([1, 2, 3, 4])));
  const bytes = await createVoiceboxApi("tok", fetcher).speech(
    "hello",
    "en",
    new AbortController().signal,
  );
  expect(new Uint8Array(bytes)).toEqual(new Uint8Array([1, 2, 3, 4]));
  const [url, init] = fetcher.mock.calls[0] as unknown as [string, RequestInit];
  expect(url).toBe("/api/v2/voice/voicebox/speech");
  expect(init.method).toBe("POST");
  expect(init.headers).toEqual({
    authorization: "Bearer tok",
    "content-type": "application/json",
  });
  expect(init.body).toBe(JSON.stringify({ text: "hello", language: "en" }));
});

test("speech refuses an oversize or a non-WAV answer", async () => {
  const over = vi.fn<typeof fetch>().mockResolvedValue(wav(new Uint8Array(8 * 1024 * 1024 + 1)));
  expect(
    await codeOf(createVoiceboxApi("tok", over).speech("hi", "en", new AbortController().signal)),
  ).toBe("voicebox_invalid");
  const wrongType = vi
    .fn<typeof fetch>()
    .mockResolvedValue(
      new Response(new Uint8Array([1]), { headers: { "content-type": "text/plain" } }),
    );
  expect(
    await codeOf(
      createVoiceboxApi("tok", wrongType).speech("hi", "en", new AbortController().signal),
    ),
  ).toBe("voicebox_invalid");
});

test("speech maps the server's typed codes, and an unparsable refusal is unavailable", async () => {
  for (const code of ["voicebox_off", "voicebox_busy", "voicebox_profile_missing"]) {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(refusal(code, 409));
    expect(
      await codeOf(
        createVoiceboxApi("tok", fetcher).speech("hi", "en", new AbortController().signal),
      ),
    ).toBe(code);
  }
  const garbled = vi
    .fn<typeof fetch>()
    .mockResolvedValue(new Response("nonsense", { status: 502 }));
  expect(
    await codeOf(
      createVoiceboxApi("tok", garbled).speech("hi", "en", new AbortController().signal),
    ),
  ).toBe("unavailable");
});

test("speech tells the caller's abort from its own timeout", async () => {
  const fetcher = vi.fn<typeof fetch>((_url, init) => {
    return new Promise((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(init.signal?.reason));
    });
  });
  const api = createVoiceboxApi("tok", fetcher);

  const cancel = new AbortController();
  const cancelling = expect(api.speech("hi", "en", cancel.signal)).rejects.toThrow(/aborted/i);
  cancel.abort();
  await cancelling;

  vi.useFakeTimers();
  const timingOut = expect(
    api.speech("hi", "en", new AbortController().signal),
  ).rejects.toMatchObject({ code: "voicebox_timeout" });
  await vi.advanceTimersByTimeAsync(330_000);
  await timingOut;
});

test("voiceboxProblem gives an English note per code, and a default for the rest", () => {
  expect(voiceboxProblem("voicebox_unreachable", "http://127.0.0.1:17493")).toBe(
    "Voicebox isn't answering at http://127.0.0.1:17493. Start the Voicebox app, then try again.",
  );
  expect(voiceboxProblem("voicebox_off", "http://127.0.0.1:17493")).toBe(
    "Choose a Voicebox voice in Settings › Chat.",
  );
  expect(voiceboxProblem("unavailable", "http://127.0.0.1:17493")).toBe(
    "Voicebox couldn't make the speech.",
  );
});

test("voiceboxProblem gives every engine the pack install/repair codes", () => {
  for (const engine of ["voicebox", "pack"] as const) {
    expect(voiceboxProblem("voicebox_pack_missing", "u", engine)).toBe(
      "Natural voices aren't installed yet. Install them in Settings › Chat.",
    );
    expect(voiceboxProblem("voicebox_pack_failed", "u", engine)).toBe(
      "Natural voices stopped working. Repair them in Settings › Chat.",
    );
  }
});

test("voiceboxProblem re-words the shared codes for engine pack", () => {
  expect(voiceboxProblem("voicebox_unreachable", "u", "pack")).toBe(
    "The natural voice didn't answer in time. Try again in a moment.",
  );
  expect(voiceboxProblem("voicebox_timeout", "u", "pack")).toBe(
    "The natural voice didn't answer in time. Try again in a moment.",
  );
  expect(voiceboxProblem("voicebox_busy", "u", "pack")).toBe(
    "The natural voice is busy with another reading. Try again in a moment.",
  );
  expect(voiceboxProblem("voicebox_refused", "u", "pack")).toBe(
    "The natural voice couldn't read this reply.",
  );
  expect(voiceboxProblem("voicebox_invalid", "u", "pack")).toBe(
    "The natural voice couldn't read this reply.",
  );
  expect(voiceboxProblem("voicebox_profile_missing", "u", "pack")).toBe(
    "Natural voices don't speak this language.",
  );
  expect(voiceboxProblem("voicebox_model_missing", "u", "pack")).toBe(
    "The natural voice model is missing. Repair natural voices in Settings › Chat.",
  );
  // A code pack has no wording for falls through to the shared, voicebox-worded table.
  expect(voiceboxProblem("voicebox_off", "u", "pack")).toBe(
    "Choose a Voicebox voice in Settings › Chat.",
  );
});
