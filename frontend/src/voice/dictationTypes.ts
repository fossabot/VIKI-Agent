export const MICROPHONE_KEY = "viki.voice.microphone";
export const BATCH_BYTES = 3_200; // 100 ms of 16 kHz s16le mono
export const PRESTART_AUDIO_BYTES = 320_000; // 10 s before `capturing`
export const MAX_CAPTURE_BYTES = 3_840_000; // voice_engine.MAX_CAPTURE_BYTES
export const MAX_AUDIO_FRAME_BYTES = 16_384; // voice_socket.MAX_AUDIO_BYTES
export type VoiceState = "ready" | "setup_needed" | "backend_missing" | "engine_unavailable";
export type HandsFreeReportEvent = "dropped" | "failed" | "fallback" | "silent";
export interface VoiceFallback {
  backend: string;
  code: string;
  detail: string | null;
}
export interface VoiceStatus {
  state: VoiceState;
  running: boolean;
  model: string | null;
  backend: string;
  device: string | null;
  languages: readonly string[];
  language: string;
  language_unavailable: boolean;
  models: readonly string[];
  backends: readonly string[];
  fallback: VoiceFallback | null;
  cpu_model: string | null;
  slow_on_cpu: boolean;
}
export type VoiceServerMessage =
  | { type: "starting"; capture_id: string }
  | {
      type: "capturing";
      capture_id: string;
      model: string;
      backend: string;
      device: string | null;
      language: string;
      language_unavailable: boolean;
      fallback: VoiceFallback | null;
    }
  | { type: "partial"; capture_id: string; segment: number; revision: number; text: string }
  | { type: "final"; capture_id: string; segment: number; text: string }
  | { type: "stopping"; capture_id: string; reason: "capture_cap" | "overloaded" }
  | { type: "done" | "cancelled"; capture_id: string }
  | { type: "error"; capture_id: string | null; code: string };
export interface VoiceSocket {
  begin(captureId: string): void;
  audio(chunk: ArrayBuffer): void;
  end(captureId: string): void;
  cancel(captureId: string): void;
  close(): void;
}
export interface VoiceSocketHandlers {
  message(message: VoiceServerMessage): void;
  closed(code: number): void;
} // closed: once; 1006 when never open
export interface Microphone {
  deviceId: string;
  label: string;
}
export interface CaptureHandle {
  readonly deviceId: string | null;
  stop(): Promise<void>;
} // flushes the last batch (<= 200 ms), then stops tracks, closes the context
export interface CaptureOptions {
  deviceId: string | null;
  workletUrl: string;
  onAudio(chunk: ArrayBuffer): void;
  onEnded(): void;
}
export type CaptureFactory = (options: CaptureOptions) => Promise<CaptureHandle>; // rejects with getUserMedia's DOMException
export interface DictationApi {
  status(signal?: AbortSignal): Promise<VoiceStatus>;
  clearFallback(signal?: AbortSignal): Promise<VoiceStatus>;
  socket(handlers: VoiceSocketHandlers): VoiceSocket;
  startCapture: CaptureFactory;
  microphones(): Promise<Microphone[]>;
  readonly workletUrl: string;
  report?(event: HandsFreeReportEvent, code: string): void;
}
export type DictationPhase =
  | "unavailable"
  | "idle"
  | "starting"
  | "recording"
  | "finalizing"
  | "cancelling";
export interface DictationView {
  phase: DictationPhase;
  status: VoiceStatus | null;
  provisional: string;
  elapsedMs: number;
  notice: string | null;
  fallback: VoiceFallback | null;
  toggled: boolean;
}
export interface DictationControls {
  press(): void;
  release(): void;
  toggle(): void;
  cancel(): void;
  retryGpu(): void;
  dismiss(): void;
  compositionEnd(): void;
}
