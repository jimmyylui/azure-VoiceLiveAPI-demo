class PcmCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.frameSize = Math.round(sampleRate / 10);
    this.samples = new Float32Array(this.frameSize);
    this.sampleCount = 0;
  }

  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input) return true;

    let inputOffset = 0;
    while (inputOffset < input.length) {
      const copyLength = Math.min(
        input.length - inputOffset,
        this.frameSize - this.sampleCount,
      );
      this.samples.set(
        input.subarray(inputOffset, inputOffset + copyLength),
        this.sampleCount,
      );
      this.sampleCount += copyLength;
      inputOffset += copyLength;

      if (this.sampleCount === this.frameSize) {
        const outputLength = Math.round(this.frameSize * 24000 / sampleRate);
        const output = new Int16Array(outputLength);
        for (let index = 0; index < outputLength; index++) {
          const sample = this.samples[Math.floor(index * sampleRate / 24000)];
          const value = Math.max(-1, Math.min(1, sample));
          output[index] = value < 0 ? value * 32768 : value * 32767;
        }
        this.port.postMessage(output.buffer, [output.buffer]);
        this.sampleCount = 0;
      }
    }
    return true;
  }
}

registerProcessor("pcm-capture", PcmCapture);