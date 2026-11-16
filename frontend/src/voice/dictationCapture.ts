import type { CaptureFactory, CaptureHandle, Microphone } from "./dictationTypes";
import { MICROPHONE_KEY } from "./dictationTypes";

const FLUSH_WAIT_MS = 200;

const errorName = (error: unknown): unknown =>
  typeof error === "object" && error !== null ? (error as { name?: unknown }).name : undefined;

// Echo cancellation, noise suppression and gain control are off: the engine's accuracy was
// measured on unprocessed audio, and Chromium's processing distorted a test tone.
function openStream(deviceId: string | null): Promise<MediaStream> {
  return navigator.mediaDevices.getUserMedia({
    audio: {
      ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
      channelCount: 1,
      echoCancellation: false,
      noiseSuppression: false,
      autoGainControl: false,
    },
  });
}

export const startCapture: CaptureFactory = async ({ deviceId, workletUrl, onAudio, onEnded }) => {
  let usedId = deviceId;
  let stream: MediaStream;
  try {
    stream = await openStream(usedId);
  } catch (error) {
    const name = errorName(error);
    if (!usedId || (name !== "OverconstrainedError" && name !== "NotFoundError")) throw error;
    // The saved microphone is gone: forget it and use the system default.
    localStorage.removeItem(MICROPHONE_KEY);
    usedId = null;
    stream = await openStream(null);
  }

  let context: AudioContext | null = null;
  try {
    context = new AudioContext({ sampleRate: 16000 });
    await context.audioWorklet.addModule(workletUrl);
  } catch (error) {
    for (const track of stream.getTracks()) track.stop();
    await context?.close().catch(() => undefined);
    throw error;
  }

  const source = context.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(context, "viki-dictation");
  let stopping = false;
  let resolveFlushed: (() => void) | null = null;

  node.port.onmessage = (event: MessageEvent) => {
    if (event.data instanceof ArrayBuffer) onAudio(event.data);
    else if (event.data?.flushed === true) resolveFlushed?.();
  };
  // A track we stop ourselves does not fire `ended`; this guards a fake or a late event.
  let endedReported = false;
  for (const track of stream.getTracks()) {
    track.addEventListener("ended", () => {
      if (stopping || endedReported) return;
      endedReported = true;
      onEnded();
    });
  }
  source.connect(node);
  // The node writes silence to its output; connecting it keeps the graph pulling it.
  node.connect(context.destination);

  let stopped: Promise<void> | null = null;
  const stop = async () => {
    stopping = true;
    await new Promise<void>((resolve) => {
      const timer = setTimeout(resolve, FLUSH_WAIT_MS);
      resolveFlushed = () => {
        clearTimeout(timer);
        resolve();
      };
      node.port.postMessage("flush");
    });
    for (const track of stream.getTracks()) track.stop();
    source.disconnect();
    node.disconnect();
    node.port.onmessage = null;
    await context.close().catch(() => undefined);
  };

  const handle: CaptureHandle = {
    deviceId: usedId,
    stop() {
      stopped ??= stop();
      return stopped;
    },
  };
  return handle;
};

export async function microphones(): Promise<Microphone[]> {
  const devices = await navigator.mediaDevices.enumerateDevices();
  return devices
    .filter((device) => device.kind === "audioinput")
    .map(({ deviceId, label }) => ({ deviceId, label }));
}
