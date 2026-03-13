class GeminiAudioProcessor extends AudioWorkletProcessor {
    constructor() {
        super();

        // ── Playback ring buffer (Agent → Speaker) ──────────────────────────
        // Fixed capacity: 2 s of 24 kHz mono audio = 48 000 samples.
        // When the buffer would overflow (audio arrives faster than it is
        // consumed) the oldest samples are silently dropped so playback stays
        // current and latency never accumulates.
        const PLAYBACK_CAP = 48000;
        this._pb    = new Float32Array(PLAYBACK_CAP);
        this._pbR   = 0;   // read  pointer
        this._pbW   = 0;   // write pointer
        this._pbN   = 0;   // number of samples currently stored
        this._pbCap = PLAYBACK_CAP;

        // ── Capture ring buffer (Mic → Agent) ───────────────────────────────
        // Accumulate 2048 samples then send as one Int16 PCM chunk.
        this.recordBuffer = new Float32Array(2048);
        this.recordIndex  = 0;

        this.port.onmessage = (event) => {
            const msg = event.data;

            // Reset command: flush playback buffer on barge-in / session restart.
            if (msg && typeof msg === "object" && msg.type === "reset") {
                this._pbR = 0;
                this._pbW = 0;
                this._pbN = 0;
                return;
            }

            // Enqueue audio chunk into the ring buffer.
            const data = msg; // Float32Array from main thread
            const len  = data.length;

            if (len >= this._pbCap) {
                // Chunk fills the entire ring — keep only the newest _pbCap samples.
                const offset = len - this._pbCap;
                for (let i = 0; i < this._pbCap; i++) {
                    this._pb[i] = data[offset + i];
                }
                this._pbR = 0;
                this._pbW = 0;
                this._pbN = this._pbCap;
                return;
            }

            const avail = this._pbCap - this._pbN;
            if (len > avail) {
                // Drop oldest samples to make room (low-latency overflow policy).
                const drop = len - avail;
                this._pbR = (this._pbR + drop) % this._pbCap;
                this._pbN -= drop;
            }

            for (let i = 0; i < len; i++) {
                this._pb[this._pbW] = data[i];
                this._pbW = (this._pbW + 1) % this._pbCap;
            }
            this._pbN += len;
        };
    }

    process(inputs, outputs) {
        const input  = inputs[0];
        const output = outputs[0];

        // 1. Capture: Mic → main thread → WebSocket
        if (input && input.length > 0) {
            const inputChannel = input[0];
            for (let i = 0; i < inputChannel.length; i++) {
                this.recordBuffer[this.recordIndex++] = inputChannel[i];

                if (this.recordIndex >= this.recordBuffer.length) {
                    const pcm16 = new Int16Array(this.recordBuffer.length);
                    for (let j = 0; j < this.recordBuffer.length; j++) {
                        const s = Math.max(-1, Math.min(1, this.recordBuffer[j]));
                        pcm16[j] = s < 0 ? s * 0x8000 : s * 0x7FFF;
                    }
                    // Transfer buffer ownership to avoid a copy on the message channel.
                    this.port.postMessage(pcm16, [pcm16.buffer]);
                    this.recordIndex = 0;
                }
            }
        }

        // 2. Playback: ring buffer → Speaker
        if (output && output.length > 0) {
            const outputChannel = output[0];
            const needed    = outputChannel.length;
            const available = Math.min(needed, this._pbN);

            for (let i = 0; i < available; i++) {
                outputChannel[i] = this._pb[this._pbR];
                this._pbR = (this._pbR + 1) % this._pbCap;
            }
            this._pbN -= available;

            // Fill remainder with silence on underrun (no data arrived yet).
            for (let i = available; i < needed; i++) {
                outputChannel[i] = 0;
            }
        }

        return true;
    }
}

registerProcessor('gemini-audio-processor', GeminiAudioProcessor);
