class GeminiAudioProcessor extends AudioWorkletProcessor {
    constructor() {
        super();
        // For Playback (Agent -> Speaker)
        this.playbackBuffer = [];

        // For Recording (Mic -> Agent)
        // Accumulate 2048 samples before triggering a send
        this.recordBuffer = new Float32Array(2048);
        this.recordIndex = 0;

        // Listen for incoming audio from the main thread
        this.port.onmessage = (event) => {
            const msg = event.data;
            if (msg && typeof msg === "object" && msg.type === "reset") {
                this.playbackBuffer = [];
                return;
            }

            const newAudioData = msg;
            for (let i = 0; i < newAudioData.length; i++) {
                this.playbackBuffer.push(newAudioData[i]);
            }
        };
    }

    process(inputs, outputs, parameters) {
        const input = inputs[0];
        const output = outputs[0];

        // 1. Process Input from Microphone (Record)
        if (input && input.length > 0) {
            const inputChannel = input[0]; // Mono
            for (let i = 0; i < inputChannel.length; i++) {
                if (this.recordIndex < this.recordBuffer.length) {
                    this.recordBuffer[this.recordIndex++] = inputChannel[i];
                }

                if (this.recordIndex >= this.recordBuffer.length) {
                    // Convert Float32 [-1, 1] to Int16 PCM
                    const pcm16 = new Int16Array(this.recordBuffer.length);
                    for (let j = 0; j < this.recordBuffer.length; j++) {
                        let s = Math.max(-1, Math.min(1, this.recordBuffer[j]));
                        pcm16[j] = s < 0 ? s * 0x8000 : s * 0x7FFF;
                    }

                    // Post back to main thread to send over WS
                    this.port.postMessage(pcm16);
                    this.recordIndex = 0;
                }
            }
        }

        // 2. Process Output to Speaker (Playback)
        if (output && output.length > 0) {
            const outputChannel = output[0];
            for (let i = 0; i < outputChannel.length; i++) {
                if (this.playbackBuffer.length > 0) {
                    // Pop the oldest sample from the front
                    outputChannel[i] = this.playbackBuffer.shift();
                } else {
                    // Padding with silence if buffer is empty
                    outputChannel[i] = 0;
                }
            }
        }

        return true;
    }
}

registerProcessor('gemini-audio-processor', GeminiAudioProcessor);
