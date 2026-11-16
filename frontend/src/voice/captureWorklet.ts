import { PcmBatcher } from "./capturePcm";

// The AudioWorkletGlobalScope has these; tsconfig's DOM lib does not, and a tsconfig change is
// not worth three declarations.
declare class AudioWorkletProcessor {
  readonly port: MessagePort;
}
declare function registerProcessor(name: string, processor: new () => AudioWorkletProcessor): void;

// Built by Vite's `?worker&url` into one standalone module that `audioWorklet.addModule` loads
// under the app's CSP. The context runs at 16 kHz, so batches go out as the frames arrive.
class DictationProcessor extends AudioWorkletProcessor {
  private readonly batcher = new PcmBatcher();

  constructor() {
    super();
    this.port.onmessage = (event: MessageEvent) => {
      if (event.data !== "flush") return;
      const rest = this.batcher.flush();
      if (rest) this.port.postMessage(rest, [rest]);
      // Posted after the rest on the same port, so the main thread has the last batch first.
      this.port.postMessage({ flushed: true });
    };
  }

  process(inputs: Float32Array[][]): boolean {
    const channels = inputs[0];
    if (channels) {
      for (const batch of this.batcher.push(channels)) this.port.postMessage(batch, [batch]);
    }
    return true;
  }
}

registerProcessor("viki-dictation", DictationProcessor);
