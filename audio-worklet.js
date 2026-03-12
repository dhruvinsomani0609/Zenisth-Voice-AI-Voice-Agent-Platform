/**
 * audio-worklet.js
 *
 * Two processors in one file:
 *
 *  1. MicProcessor  — captures Float32 mic samples, converts to Int16 PCM,
 *                     and posts chunks to the main thread for WebSocket sending.
 *
 *  2. PlaybackProcessor — receives Int16 PCM chunks from the main thread via a
 *                         shared ring buffer and plays them seamlessly off the
 *                         main thread with zero blocking or popping.
 */

// ─────────────────────────────────────────────────────────────────────────────
// 1. MicProcessor
// ─────────────────────────────────────────────────────────────────────────────
class MicProcessor extends AudioWorkletProcessor {
    constructor() {
        super();
        this._buffer = [];        // accumulated samples
        this._chunkSize = 2048;   // samples per chunk (~128ms @ 16 kHz)
    }

    process(inputs) {
        const input = inputs[0];
        if (!input || !input[0]) return true;

        const samples = input[0]; // Float32Array, mono

        // Accumulate
        for (let i = 0; i < samples.length; i++) {
            this._buffer.push(samples[i]);
        }

        // Flush full chunks
        while (this._buffer.length >= this._chunkSize) {
            const chunk = this._buffer.splice(0, this._chunkSize);
            const int16 = this._toInt16(chunk);
            // Transfer ownership of the underlying buffer for zero-copy
            this.port.postMessage(int16.buffer, [int16.buffer]);
        }

        return true; // keep processor alive
    }

    /** Convert Float32 samples [-1, 1] to Int16 PCM. */
    _toInt16(floats) {
        const out = new Int16Array(floats.length);
        for (let i = 0; i < floats.length; i++) {
            const s = Math.max(-1, Math.min(1, floats[i]));
            out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
        }
        return out;
    }
}

registerProcessor("mic-processor", MicProcessor);


// ─────────────────────────────────────────────────────────────────────────────
// 2. PlaybackProcessor
// ─────────────────────────────────────────────────────────────────────────────
const RING_SIZE = 96000; // ~4 seconds of audio @ 24 kHz

class PlaybackProcessor extends AudioWorkletProcessor {
    constructor() {
        super();
        this._ring = new Int16Array(RING_SIZE); // ring buffer
        this._writePos = 0;
        this._readPos = 0;
        this._buffered = 0;     // samples currently in buffer

        this.port.onmessage = (e) => {
            if (e.data === "flush") {
                // Hard interrupt — clear the queue immediately
                this._writePos = 0;
                this._readPos = 0;
                this._buffered = 0;
                return;
            }
            // Int16Array chunk received from main thread
            const chunk = new Int16Array(e.data);
            this._write(chunk);
        };
    }

    _write(chunk) {
        for (let i = 0; i < chunk.length; i++) {
            if (this._buffered >= RING_SIZE) break; // drop if overrun
            this._ring[this._writePos] = chunk[i];
            this._writePos = (this._writePos + 1) % RING_SIZE;
            this._buffered++;
        }
    }

    process(_inputs, outputs) {
        const output = outputs[0][0]; // Float32Array, mono channel
        const len = output.length;

        for (let i = 0; i < len; i++) {
            if (this._buffered > 0) {
                const sample = this._ring[this._readPos];
                this._readPos = (this._readPos + 1) % RING_SIZE;
                this._buffered--;
                // Normalize Int16 → Float32
                output[i] = sample / (sample < 0 ? 0x8000 : 0x7fff);
            } else {
                output[i] = 0; // silence while waiting
            }
        }

        return true;
    }
}

registerProcessor("playback-processor", PlaybackProcessor);
