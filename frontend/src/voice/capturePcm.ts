// Pure PCM helpers shared by the AudioWorklet and the tests. The worklet bundle imports this file,
// so it must not touch the DOM.

export function toInt16(x: number): number {
  return Math.round(Math.max(-1, Math.min(1, x)) * 32767);
}

// Averages the channels to mono, converts to s16le and cuts fixed-size batches (1,600 samples is
// 100 ms at 16 kHz). The context already runs at 16 kHz, so there is no resampling here.
export class PcmBatcher {
  private readonly batchSamples: number;
  private pending: Int16Array<ArrayBuffer>;
  private filled = 0;

  constructor(batchSamples = 1600) {
    this.batchSamples = batchSamples;
    this.pending = new Int16Array(batchSamples);
  }

  push(channels: Float32Array[]): ArrayBuffer[] {
    const first = channels[0];
    if (!first) return [];
    const batches: ArrayBuffer[] = [];
    for (let i = 0; i < first.length; i++) {
      let sum = 0;
      for (const channel of channels) sum += channel[i] ?? 0;
      this.pending[this.filled++] = toInt16(sum / channels.length);
      if (this.filled === this.batchSamples) {
        batches.push(this.pending.buffer);
        this.pending = new Int16Array(this.batchSamples);
        this.filled = 0;
      }
    }
    return batches;
  }

  flush(): ArrayBuffer | null {
    if (this.filled === 0) return null;
    const rest = this.pending.slice(0, this.filled).buffer;
    this.filled = 0;
    return rest;
  }
}
