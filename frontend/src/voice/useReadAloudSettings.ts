/**
 * The read-aloud settings App hands every reply and hands-free: the stored rate,
 * voices and notice flag, read off the effective settings, with the window's own controller. Also
 * today's Voicebox choice: an api, url and engine when a pack or Voicebox profile is chosen,
 * else null. `noteFallback` tracks which Voicebox failure codes this window has already shown a
 * note for (cleared once a Voicebox call succeeds again), and `openVoiceSettings` hands the reply
 * a stable way to open Settings › Chat regardless of how often the caller's own callback changes.
 */
import { useContext, useMemo, useRef, useState } from "react";
import type { ReadAloudSettings } from "../components/Conversation/ReadAloud";
import { packLanguages, parsePackVoices } from "./packVoices";
import { parseVoiceMap, readAloud } from "./readAloud";
import { type VoiceboxApi, VoiceboxContext } from "./voiceboxApi";
import { speechEngine } from "./voicePackApi";

const DEFAULT_VOICEBOX_URL = "http://127.0.0.1:17493";

export function useReadAloudSettings(
  effective: Readonly<Record<string, unknown>> | undefined,
  store: { patch(changes: Record<string, unknown>): Promise<unknown> },
  openVoiceSettings?: () => void,
): ReadAloudSettings {
  const rate = effective?.["voice.read_aloud_rate"];
  const voices = effective?.["voice.read_aloud_voices"];
  const noticeSeen = effective?.["voice.read_aloud_notice_seen"];
  const profile = effective?.["voice.voicebox_profile"];
  const url = effective?.["voice.voicebox_url"];
  const speakWhileWriting = effective?.["voice.speak_while_writing"];
  const packVoices = effective?.["voice.pack_voices"];
  const engine = speechEngine(effective ?? {});
  const api = useContext(VoiceboxContext);
  // This window remembers a confirmed notice itself, so a lost save never asks again here.
  const [acked, setAcked] = useState(false);
  // Codes already noted for a Voicebox fallback this window, so a repeat failure stays quiet;
  // cleared whenever a Voicebox call succeeds again (the problem evidently cleared up).
  const fallbackSeen = useRef<Set<string>>(new Set());
  const trackedApi = useMemo<VoiceboxApi | null>(() => {
    if (api === null) return null;
    return {
      status: api.status,
      speech: async (text, language, signal) => {
        const audio = await api.speech(text, language, signal);
        fallbackSeen.current.clear();
        return audio;
      },
    };
  }, [api]);
  // Kept fresh every render so the memo below never has to depend on the caller's own closure
  // identity (a fresh arrow function each App render would otherwise churn it every time).
  const openVoiceSettingsRef = useRef(openVoiceSettings);
  openVoiceSettingsRef.current = openVoiceSettings;
  return useMemo<ReadAloudSettings>(
    () => ({
      controller: readAloud,
      rate: typeof rate === "number" ? rate : 1,
      voices: parseVoiceMap(voices),
      noticeSeen: noticeSeen === true || acked,
      voicebox:
        engine === "pack" && trackedApi !== null
          ? {
              api: trackedApi,
              url: "",
              speakWhileWriting: speakWhileWriting === true,
              engine: "pack",
              languages: packLanguages(parsePackVoices(packVoices)),
            }
          : engine === "voicebox" && trackedApi !== null && typeof profile === "string"
            ? {
                api: trackedApi,
                url: typeof url === "string" ? url : DEFAULT_VOICEBOX_URL,
                speakWhileWriting: speakWhileWriting === true,
                engine: "voicebox",
              }
            : null,
      noteFallback: (code: string) => {
        if (fallbackSeen.current.has(code)) return false;
        fallbackSeen.current.add(code);
        return true;
      },
      openVoiceSettings: () => openVoiceSettingsRef.current?.(),
      acknowledge: () => {
        setAcked(true);
        void store.patch({ "voice.read_aloud_notice_seen": true }).catch(() => undefined);
      },
    }),
    [
      rate,
      voices,
      noticeSeen,
      acked,
      store,
      engine,
      trackedApi,
      profile,
      url,
      speakWhileWriting,
      packVoices,
    ],
  );
}
