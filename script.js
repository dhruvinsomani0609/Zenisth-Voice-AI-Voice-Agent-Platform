let ws;
let audioContext;
let audioWorkletNode;
let mediaStream;
let micSource;

const btnStart = document.getElementById("btn-start");
const btnStop = document.getElementById("btn-stop");
const btnClear = document.getElementById("btn-clear");
const btnSettings = document.getElementById("btn-settings");
const btnSettingsClose = document.getElementById("btn-settings-close");
const btnSettingsCancel = document.getElementById("btn-settings-cancel");
const btnSettingsApply = document.getElementById("btn-settings-apply");

const statusPill = document.getElementById("status-pill");
const statusText = document.getElementById("status-text");
const agentStateBadge = document.getElementById("agent-state");
const modeText = document.getElementById("mode-text");
const avatarRing = document.getElementById("avatar-ring");

const transcriptBox = document.getElementById("transcript");
const emptyState = document.getElementById("empty-state");
const fnLog = document.getElementById("fn-log");
const fnCount = document.getElementById("fn-count");

const settingsModal = document.getElementById("settings-modal");
const voiceNameEl = document.getElementById("voice-name");
const systemPromptEl = document.getElementById("system-prompt");

const waveformCanvas = document.getElementById("waveform");
const waveCtx = waveformCanvas?.getContext("2d");

let currentAgentMsgEl = null; // for typing-style updates
let lastAgentAudioAt = 0;
let micAnalyser = null;
let micData = null;
let uiState = "idle"; // idle|connecting|live|thinking|speaking|user_speaking
let toolItems = new Map(); // id -> {el, detailEl}
let userCfg = {
    voice_name: "Kore",
    system_prompt: ""
};

// Session generation counter — incremented on every startCall().
// Any async callback that captures the value at call-start can compare
// against the current counter to detect stale/superseded sessions.
let sessionGen = 0;

/** Time when call went live (status "live"). Interrupt is not sent before this + 2s to avoid accidental disconnect. */
let callLiveAt = 0;

function setUiState(state) {
    uiState = state;
    statusPill.className = "status-pill " + (
        state === "idle" ? "idle" :
            state === "connecting" ? "connecting" :
                state === "thinking" ? "thinking" :
                    state === "speaking" ? "speaking" :
                        state === "user_speaking" ? "speaking" :
                            "live"
    );

    avatarRing.className = "avatar-ring " + (
        state === "thinking" ? "thinking" :
            state === "speaking" ? "speaking" :
                state === "live" ? "live" :
                    state === "user_speaking" ? "speaking" :
                        ""
    );

    const label = (
        state === "idle" ? "Idle" :
            state === "connecting" ? "Connecting..." :
                state === "thinking" ? "Thinking..." :
                    state === "speaking" ? "Agent Speaking" :
                        state === "user_speaking" ? "You Speaking" :
                            "Live"
    );

    statusText.textContent = label;
    agentStateBadge.textContent = label;
    modeText.textContent = label;
}

function ensureEmptyHidden() {
    if (emptyState) emptyState.style.display = "none";
}

function addMsg(role, text, meta = {}) {
    ensureEmptyHidden();
    const msg = document.createElement("div");
    msg.className = `msg ${role}`;

    const dot = document.createElement("div");
    dot.className = "msg-dot";

    const inner = document.createElement("div");
    inner.className = "msg-inner";

    const metaRow = document.createElement("div");
    metaRow.className = "msg-meta";
    const who = document.createElement("strong");
    who.textContent = role === "user" ? "YOU" : role === "agent" ? "AGENT" : "SYSTEM";
    metaRow.appendChild(who);
    if (meta.tag) {
        const tag = document.createElement("span");
        tag.textContent = meta.tag;
        metaRow.appendChild(tag);
    }

    const body = document.createElement("div");
    body.className = "msg-text";
    body.textContent = text;

    inner.appendChild(metaRow);
    inner.appendChild(body);

    msg.appendChild(dot);
    msg.appendChild(inner);

    transcriptBox.appendChild(msg);
    transcriptBox.scrollTop = transcriptBox.scrollHeight;
    return { msg, body };
}

function updateAgentTyping(fragment) {
    ensureEmptyHidden();
    if (!currentAgentMsgEl) {
        const { msg, body } = addMsg("agent", "");
        currentAgentMsgEl = { msg, body, text: "" };
    }
    currentAgentMsgEl.text += fragment;
    currentAgentMsgEl.body.textContent = currentAgentMsgEl.text;
    transcriptBox.scrollTop = transcriptBox.scrollHeight;
}

function finalizeAgentMsg() {
    currentAgentMsgEl = null;
}

function toast(title, body, kind = "") {
    const wrap = document.getElementById("toast-wrap");
    if (!wrap) return;
    const t = document.createElement("div");
    t.className = "toast " + (kind || "");
    const tt = document.createElement("div");
    tt.className = "toast-title";
    tt.textContent = title;
    const tb = document.createElement("div");
    tb.className = "toast-body";
    tb.textContent = body;
    t.appendChild(tt);
    t.appendChild(tb);
    wrap.appendChild(t);
    setTimeout(() => t.remove(), 3600);
}

function setFnCount() {
    fnCount.textContent = String(toolItems.size);
}

function renderToolItem(id, name, args) {
    const el = document.createElement("div");
    el.className = "fn-item";

    const hdr = document.createElement("div");
    hdr.className = "fn-hdr";

    const tag = document.createElement("div");
    tag.className = "fn-tag call";
    tag.textContent = "CALL";

    const pname = document.createElement("div");
    pname.className = "fn-pname";
    pname.textContent = name;

    const ms = document.createElement("div");
    ms.className = "fn-ms";
    ms.textContent = "…";

    const detail = document.createElement("div");
    detail.className = "fn-detail";
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify({ args }, null, 2);
    detail.appendChild(pre);

    hdr.appendChild(tag);
    hdr.appendChild(pname);
    hdr.appendChild(ms);
    el.appendChild(hdr);
    el.appendChild(detail);

    hdr.addEventListener("click", () => {
        detail.classList.toggle("open");
    });

    fnLog.prepend(el);
    toolItems.set(id, { el, detail, tag, ms, pre, name });
    setFnCount();
}

function updateToolResult(id, result) {
    const item = toolItems.get(id);
    if (!item) return;
    item.tag.className = "fn-tag ok";
    item.tag.textContent = "OK";
    item.ms.textContent = "done";
    item.pre.textContent = JSON.stringify(result, null, 2);
}

function drawWaveform() {
    if (!waveCtx || !micAnalyser || !micData) return;
    micAnalyser.getByteTimeDomainData(micData);

    waveCtx.clearRect(0, 0, waveformCanvas.width, waveformCanvas.height);
    waveCtx.lineWidth = 2;

    const now = performance.now();
    const agentSpeaking = now - lastAgentAudioAt < 250;
    const isUserSpeaking = estimateMicSpeaking();

    // Color by who is dominant
    waveCtx.strokeStyle = agentSpeaking ? "#2dd4bf" : isUserSpeaking ? "#38bdf8" : "#243040";

    waveCtx.beginPath();
    const slice = waveformCanvas.width / micData.length;
    let x = 0;
    for (let i = 0; i < micData.length; i++) {
        const v = micData[i] / 128.0;
        const y = (v * waveformCanvas.height) / 2;
        if (i === 0) waveCtx.moveTo(x, y);
        else waveCtx.lineTo(x, y);
        x += slice;
    }
    waveCtx.lineTo(waveformCanvas.width, waveformCanvas.height / 2);
    waveCtx.stroke();

    requestAnimationFrame(drawWaveform);
}

function estimateMicSpeaking() {
    if (!micAnalyser) return false;
    const fft = new Uint8Array(micAnalyser.frequencyBinCount);
    micAnalyser.getByteFrequencyData(fft);
    let sum = 0;
    for (let i = 0; i < fft.length; i++) sum += fft[i];
    const avg = sum / fft.length;
    return avg > 22; // heuristic
}

async function startCall() {
    // Prevent double-start while a call is already in progress.
    if (uiState !== "idle") return;

    // Capture this call's generation.  Any async continuation below checks
    // this value; if it no longer matches sessionGen the call was superseded.
    const myGen = ++sessionGen;

    try {
        setUiState("connecting");
        addMsg("system", "Requesting microphone access...");

        // 1. Get microphone access at a high quality sample rate (typically natively 44.1kHz/48kHz, we'll resample in Worklet/Backend)
        mediaStream = await navigator.mediaDevices.getUserMedia({
            audio: {
                channelCount: 1,
                echoCancellation: true,
                autoGainControl: true,
                noiseSuppression: true
            }
        });

        // Abort if a newer session was started (or stopCall was called) while we awaited.
        if (myGen !== sessionGen) { mediaStream.getTracks().forEach(t => t.stop()); return; }

        audioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });

        // 2. Load the Audio Worklet Processor with cache busting
        await audioContext.audioWorklet.addModule(`./audio-worklet.js?t=${Date.now()}`);

        if (myGen !== sessionGen) {
            // Stop media tracks too so the mic indicator light turns off.
            mediaStream.getTracks().forEach(t => t.stop());
            audioContext.close();
            return;
        }

        // 3. Setup WebSocket connection
        const wsInstance = new WebSocket("ws://127.0.0.1:9050");
        // Receive binary audio as ArrayBuffer directly (no extra Blob→ArrayBuffer conversion).
        wsInstance.binaryType = "arraybuffer";
        ws = wsInstance;

        wsInstance.onopen = () => {
            if (myGen !== sessionGen) { wsInstance.close(); return; }
            addMsg("system", "WebSocket connected. Establishing S2S...");
            btnStart.disabled = true;
            btnStop.disabled = false;
            // Do not send config on open — it triggers an immediate reconnect and unstable session.
            // Backend uses config.yaml defaults; user can Apply in Settings to hot-reload voice/prompt.
        };

        // 4. Handle incoming messages
        wsInstance.onmessage = async (event) => {
            if (myGen !== sessionGen) return; // Stale session — ignore.
            try {
            if (typeof event.data === "string") {
                // Handle JSON (transcript, system messages)
                const data = JSON.parse(event.data);
                if (data.type === "transcript") {
                    // Gemini emits incremental fragments; we treat it like typing.
                    updateAgentTyping(data.text);
                } else if (data.type === "system") {
                    addMsg("system", data.message);
                } else if (data.type === "status") {
                    if (data.state === "connecting") {
                        setUiState("connecting");
                    } else if (data.state === "live") {
                        callLiveAt = Date.now();
                        setUiState("live");
                    }
                } else if (data.type === "error") {
                    addMsg("system", data.message, { tag: "ERROR" });
                    toast("Error", data.message, "error");
                    // Transition out of "Connecting…" so the UI is never stuck.
                    if (uiState === "connecting") setUiState("idle");
                } else if (data.type === "reset_playback") {
                    if (audioWorkletNode) {
                        audioWorkletNode.port.postMessage({ type: "reset" });
                    }
                    // Visually "cut off" the current agent message
                    if (currentAgentMsgEl?.msg) currentAgentMsgEl.msg.style.opacity = "0.55";
                    finalizeAgentMsg();
                    setUiState("user_speaking");
                } else if (data.type === "tool_call") {
                    setUiState("thinking");
                    renderToolItem(data.id, data.name, data.args || {});
                } else if (data.type === "tool_result") {
                    updateToolResult(data.id, data.result);
                    setUiState("live");
                }
            } else {
                // Handle Binary Data (Audio from Gemini).
                // binaryType is 'arraybuffer' so event.data is already an ArrayBuffer.
                const int16Array = new Int16Array(event.data);
                const float32Array = new Float32Array(int16Array.length);
                for (let i = 0; i < int16Array.length; i++) {
                    float32Array[i] = int16Array[i] / 32768.0;
                }

                if (audioWorkletNode) {
                    audioWorkletNode.port.postMessage(float32Array);
                }
                lastAgentAudioAt = performance.now();
                setUiState("speaking");
            }
            } catch (err) {
                console.error("WS onmessage error:", err);
            }
        };

        // onclose fires after onerror too; one stopCall invocation is enough.
        wsInstance.onclose = () => {
            if (myGen !== sessionGen) return; // Already cleaned up by a newer session.
            stopCall("Connection closed.");
        };

        wsInstance.onerror = () => {
            // onclose will follow and invoke stopCall; just surface a toast here.
            if (myGen !== sessionGen) return;
            addMsg("system", "WebSocket error — connection failed.", { tag: "ERROR" });
        };

        // 5. Setup Audio Graph
        micSource = audioContext.createMediaStreamSource(mediaStream);
        audioWorkletNode = new AudioWorkletNode(audioContext, 'gemini-audio-processor');

        // Route Mic to Worklet
        micSource.connect(audioWorkletNode);

        // Mic analyser for waveform + speaking detection
        micAnalyser = audioContext.createAnalyser();
        micAnalyser.fftSize = 1024;
        micData = new Uint8Array(micAnalyser.fftSize);
        micSource.connect(micAnalyser);

        // Route Worklet to Speaker (for audio playback)
        audioWorkletNode.connect(audioContext.destination);

        // 6. Handle Mic Data from Worklet and send over WebSocket
        audioWorkletNode.port.onmessage = (event) => {
            if (myGen !== sessionGen) return;
            // Receive Int16 PCM data from the worklet (transferred, zero-copy)
            const pcm16Buffer = event.data;
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(pcm16Buffer.buffer);
            }
        };

        // Kick off waveform render loop
        requestAnimationFrame(drawWaveform);

    } catch (err) {
        if (myGen !== sessionGen) return; // Superseded; ignore.
        console.error(err);
        addMsg("system", "Failed to start: " + err.message, { tag: "ERROR" });
        toast("Start failed", err.message, "error");
        stopCall();
    }
}

function stopCall(reason = "Call ended by user.") {
    // Bump the generation counter so any in-flight startCall() async continuation
    // detects that this session is over and does not touch the (now reset) state.
    sessionGen++;

    if (ws) { ws.close(); ws = null; }
    if (audioWorkletNode) { audioWorkletNode.disconnect(); audioWorkletNode = null; }
    if (micSource) { micSource.disconnect(); micSource = null; }
    if (mediaStream) {
        mediaStream.getTracks().forEach(track => track.stop());
        mediaStream = null;
    }
    if (audioContext && audioContext.state !== 'closed') {
        audioContext.close();
        audioContext = null;
    }

    micAnalyser = null;
    micData = null;
    callLiveAt = 0;
    setUiState("idle");
    btnStart.disabled = false;
    btnStop.disabled = true;
    addMsg("system", reason);
}

let isSpeaking = false;
// Barge-in: Space sends interrupt only after we are live and 2s have passed (avoids disconnect on connect).
document.addEventListener('keydown', (e) => {
    if (e.code !== 'Space' || !ws || ws.readyState !== WebSocket.OPEN) return;
    if (!callLiveAt) return;  // not live yet — never send interrupt before "live" status
    if ((Date.now() - callLiveAt) < 2000) return;
    addMsg("system", "Interrupt (barge-in) sent.");
    ws.send(JSON.stringify({ type: "interrupt" }));
});

btnStart.addEventListener("click", startCall);
btnStop.addEventListener("click", () => stopCall());

btnClear?.addEventListener("click", () => {
    transcriptBox.innerHTML = "";
    fnLog.innerHTML = "";
    toolItems.clear();
    setFnCount();
    transcriptBox.appendChild(emptyState);
    emptyState.style.display = "block";
});

function openSettings(open) {
    if (!settingsModal) return;
    settingsModal.classList.toggle("open", !!open);
    settingsModal.setAttribute("aria-hidden", open ? "false" : "true");
}

btnSettings?.addEventListener("click", () => openSettings(true));
btnSettingsClose?.addEventListener("click", () => openSettings(false));
btnSettingsCancel?.addEventListener("click", () => openSettings(false));

// Settings modal: tab switching (Engine / Prompt)
settingsModal?.addEventListener("click", (e) => {
    const tab = e.target.closest(".sm-tab");
    if (!tab) return;
    const paneId = tab.getAttribute("data-pane");
    if (!paneId) return;
    settingsModal.querySelectorAll(".sm-tab").forEach(t => t.classList.remove("active"));
    settingsModal.querySelectorAll(".sm-pane").forEach(p => p.classList.remove("active"));
    tab.classList.add("active");
    const pane = document.getElementById("pane-" + paneId);
    if (pane) pane.classList.add("active");
});

btnSettingsApply?.addEventListener("click", () => {
    userCfg.voice_name = voiceNameEl.value;
    userCfg.system_prompt = systemPromptEl.value || "";
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "config", config: userCfg }));
        toast("Settings", "Applying to live session…", "ok");
    } else {
        toast("Settings", "Applied locally. Start the call to take effect.", "ok");
    }
    openSettings(false);
});