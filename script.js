
// ══ LIVEKIT STATE ══
// LiveKit JS SDK loaded via CDN in index.html (window.LivekitClient)
let room = null;
let connected = false, callActive = false;
let sessionStart = null, timerInterval = null;
let transcriptItems = [], fnItems = [];
let stats = { turns: 0, fns: 0, latencies: [] };
let lastUserTime = null;
let appointments = [], dncList = [];

// ══ CONFIG HELPERS ══
function getConfig() {
    return {
        voice_model: document.getElementById('sm-voice')?.value || document.getElementById('cfg-voice')?.value || '',
        stt_model: document.getElementById('sm-stt')?.value || document.getElementById('cfg-stt')?.value || '',
        llm_model: document.getElementById('sm-llm')?.value || document.getElementById('cfg-llm')?.value || '',
        temperature: parseFloat(document.getElementById('sm-temp')?.value || document.getElementById('cfg-temp')?.value || '0.6'),
        system_prompt: document.getElementById('sm-prompt')?.value?.trim() || document.getElementById('cfg-prompt')?.value?.trim() || '',
        greeting: document.getElementById('sm-greeting')?.value?.trim() || document.getElementById('cfg-greeting')?.value?.trim() || '',
        document_id: typeof activeDocId !== 'undefined' ? activeDocId : null,
    };
}

function toggleCfg() {
    const body = document.getElementById('cfg-body');
    const arrow = document.getElementById('cfg-arrow');
    body.classList.toggle('open');
    arrow.classList.toggle('open');
}

// ══ CONFIG LOAD (fetch from /api/models and /api/config) ══
async function loadConfig() {
    try {
        const [modelsRes, defaultsRes] = await Promise.all([
            fetch('/api/models'),
            fetch('/api/config'),
        ]);
        const models = await modelsRes.json();
        const defaults = await defaultsRes.json();

        // Voice dropdown — grouped by family
        const voiceSel = document.getElementById('cfg-voice');
        if (voiceSel) {
            voiceSel.innerHTML = '';
            const families = [...new Set(models.voice.map(v => v.family))];
            families.forEach(family => {
                const grp = document.createElement('optgroup');
                grp.label = family;
                models.voice.filter(v => v.family === family).forEach(v => {
                    const opt = document.createElement('option');
                    opt.value = v.id;
                    opt.textContent = `${v.label} ${v.gender === 'female' ? '♀' : '♂'} — ${v.family}`;
                    if (v.id === defaults.voice_model) opt.selected = true;
                    grp.appendChild(opt);
                });
                voiceSel.appendChild(grp);
            });
        }

        // STT dropdown
        const sttSel = document.getElementById('cfg-stt');
        if (sttSel) {
            sttSel.innerHTML = '';
            models.stt.forEach(m => {
                const opt = document.createElement('option');
                opt.value = m.id; opt.textContent = m.label;
                if (m.id === defaults.stt_model) opt.selected = true;
                sttSel.appendChild(opt);
            });
        }

        // LLM dropdown
        const llmSel = document.getElementById('cfg-llm');
        if (llmSel) {
            llmSel.innerHTML = '';
            models.llm.forEach(m => {
                const opt = document.createElement('option');
                opt.value = m.id; opt.textContent = m.label;
                if (m.id === defaults.llm_model) opt.selected = true;
                llmSel.appendChild(opt);
            });
        }

        const temp = parseFloat(defaults.temperature);
        const tempEl = document.getElementById('cfg-temp');
        const tempValEl = document.getElementById('cfg-temp-val');
        if (tempEl) tempEl.value = temp;
        if (tempValEl) tempValEl.textContent = temp.toFixed(2);
        const greetEl = document.getElementById('cfg-greeting');
        if (greetEl) greetEl.value = defaults.greeting || '';

    } catch (e) {
        console.warn('Config load failed:', e);
    }
}

// ══ LIVEKIT CONNECTION ══
async function startCall() {
    if (callActive || connected) return; // guard against double-starts
    setCallBtn('connecting');
    setStatus('connecting', '⏳ Connecting...');
    setFooter('Requesting session token...');

    // Use a unique room name per session so prior sessions don't interfere
    const roomName = 'zenisth-room-' + Date.now();

    try {
        // 1. Fetch a short-lived token from our FastAPI backend
        const tokenRes = await fetch('/api/livekit/token', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ room: roomName }),
        });
        const { token, url } = await tokenRes.json();
        if (!token) throw new Error('Token generation failed — check LIVEKIT credentials in .env');

        // 2. Create a LiveKit Room
        room = new LivekitClient.Room({
            adaptiveStream: true,
            dynacast: true,
        });

        // 3. Wire up room events → our existing UI handlers
        room
            .on(LivekitClient.RoomEvent.Connected, () => {
                connected = true; callActive = true;
                setCallBtn('end');
                setStatus('live', '🔴 Live');
                setFooter('Call active — listening...');
                startTimer(); removeEmpty();
                document.getElementById('reset-btn').style.display = '';
                handleEvent({ event: 'connected' });
            })
            .on(LivekitClient.RoomEvent.Disconnected, () => {
                connected = false; callActive = false;
                setCallBtn('start');
                setStatus('idle', '● Idle');
                setFooter('Disconnected');
                stopTimer();
                setAgentState('idle', 'Idle');
                document.getElementById('reset-btn').style.display = '';
                _cleanupAudioElements();
            })
            .on(LivekitClient.RoomEvent.DataReceived, (data) => {
                // LiveKit Agent sends JSON events over the data channel
                try {
                    const msg = JSON.parse(new TextDecoder().decode(data));
                    handleEvent(msg);
                } catch (_) { /* binary audio frames — handled by WebRTC track natively */ }
            })
            .on(LivekitClient.RoomEvent.ActiveSpeakersChanged, (speakers) => {
                const agentSpeaking = speakers.some(s => s.identity === 'zenisth-agent');
                const userSpeaking = speakers.some(s => s.identity !== 'zenisth-agent');
                if (agentSpeaking) handleEvent({ event: 'agent_speaking' });
                else if (userSpeaking) handleEvent({ event: 'user_speaking' });
                else handleEvent({ event: 'agent_done' });
            })
            .on(LivekitClient.RoomEvent.TrackSubscribed, (track, pub, participant) => {
                if (track.kind === LivekitClient.Track.Kind.Audio) {
                    // Attach remote agent audio track directly to the DOM
                    const el = track.attach();
                    el.style.display = 'none';
                    el.setAttribute('data-lk-audio', 'true'); // tag for cleanup
                    document.body.appendChild(el);
                }
            })
            .on(LivekitClient.RoomEvent.TranscriptionReceived, (segments, participant, publication) => {
                const role = participant?.identity === 'zenisth-agent' ? 'agent' : 'user';
                for (const segment of segments) {
                    handleEvent({
                        event: 'live_transcript',
                        role: role,
                        text: segment.text,
                        id: segment.id,
                        final: segment.final
                    });
                }
            })
            .on(LivekitClient.RoomEvent.ConnectionError, (err) => {
                toast('error', '⚠️ Connection Error', err.message || 'LiveKit connection failed.');
                endCall();
            });


        // 4. Connect room + publish local mic track
        // We pass the user-selected models as participant metadata so the agent knows which brain to use.
        await room.connect(url, token, {
            metadata: JSON.stringify(getConfig())
        });
        await room.localParticipant.setMicrophoneEnabled(true);
        setFooter('Connected — agent joining...');

    } catch (e) {
        toast('error', '⚠️ Error', e.message);
        setCallBtn('start');
        setStatus('idle', '● Idle');
        setFooter('Connection failed');
    }
}

function _cleanupAudioElements() {
    document.querySelectorAll('[data-lk-audio="true"]').forEach(el => el.remove());
}

function handleEvent(msg) {
    const ev = msg.event;

    if (ev === 'connected') {
        // handled inline in RoomEvent.Connected above
    }
    else if (ev === 'transcript') {
        const { role, text } = msg;
        if (role === 'user') {
            stats.turns++;
            lastUserTime = Date.now();
            addMsg('user', 'You', text);
            setAgentState('live', 'Listening...');
            updateStats();
        } else {
            addMsg('agent', 'AI Agent', text);
            setAgentState('speaking', 'Speaking...');
        }
    }
    else if (ev === 'live_transcript') {
        updateLiveTranscript(msg.role, msg.text, msg.id, msg.final);
    }
    else if (ev === 'user_speaking') {
        lastUserTime = Date.now();
        setStatus('live', '🔴 You speaking');
        setFooter('You are speaking...');
        setAgentState('live', 'Listening...');
        removeTyping();
    }
    else if (ev === 'agent_thinking') {
        setStatus('thinking', '💭 Thinking');
        setFooter('Agent thinking...');
        setAgentState('thinking', 'Thinking...');
        showTyping();
        if (lastUserTime) {
            stats.latencies.push((Date.now() - lastUserTime) / 1000);
            updateStats();
        }
    }
    else if (ev === 'agent_speaking') {
        setStatus('speaking', '🔊 Speaking');
        setFooter('Agent speaking...');
        setAgentState('speaking', 'Speaking...');
        removeTyping();
    }
    else if (ev === 'agent_done') {
        setStatus('live', '🔴 Live');
        setFooter('Listening...');
        setAgentState('live', 'Listening...');
    }
    else if (ev === 'function_call') {
        stats.fns++;
        updateStats();
        addFnItem(msg.name, msg.params, msg.result);
        addMsg('system', 'System', `fn: ${msg.name}(${JSON.stringify(msg.params)})`);
    }
    else if (ev === 'appointment_booked') {
        const d = msg.data || {};
        appointments.push(d);
        toast('appt', '✅ Demo Booked!', d.message || `Appointment confirmed (#${d.appointment_id})`);
    }
    else if (ev === 'dnc_marked') {
        dncList.push(msg.data);
        toast('dnc', '🚫 DNC Marked', 'Contact added to Do Not Call list.');
    }
    else if (ev === 'transfer_initiated') {
        toast('transfer', '↗️ Transferring', msg.data?.message || 'Connecting to human agent...');
    }
    else if (ev === 'call_ended') {
        setFooter('Call ended by agent');
        setTimeout(() => endCall(), 3500);
    }
    else if (ev === 'error') {
        toast('error', '⚠️ Error', msg.message || 'Unknown error');
        setFooter(`Error: ${msg.message}`);
    }
}

function endCall() {
    if (room) {
        room.disconnect();
        room = null;
    }
    connected = false; callActive = false;
    setCallBtn('start');
    stopTimer();
    _cleanupAudioElements(); // remove stale <audio> elements from DOM
}

function toggleCall() {
    if (callActive || connected) endCall();
    else startCall();
}

// ══ WAVEFORM (driven by LiveKit audio level, no AudioContext needed) ══
function initWaveform() {
    const c = document.getElementById('waveform');
    if (!c) return;
    c.width = c.offsetWidth * 2; c.height = 104;
}
function drawWaveform() {
    const c = document.getElementById('waveform');
    if (!c) return;
    const ctx = c.getContext('2d');
    const W = c.width, H = c.height;
    ctx.clearRect(0, 0, W, H);

    // Draw a flat idle line when not connected
    const level = (room && connected)
        ? (room.localParticipant?.audioLevel || 0)
        : 0;

    const g = ctx.createLinearGradient(0, 0, W, 0);
    g.addColorStop(0, '#2dd4bf'); g.addColorStop(1, '#a78bfa');
    ctx.strokeStyle = g; ctx.lineWidth = 2.5;
    ctx.beginPath();
    const amp = level * (H / 2);
    for (let x = 0; x < W; x++) {
        const y = H / 2 + Math.sin((x / W) * Math.PI * 8 + Date.now() / 200) * amp;
        x === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.stroke();
    requestAnimationFrame(drawWaveform);
}


// ══ TYPING INDICATOR ══
function showTyping() {
    if (document.getElementById('typing')) return;
    const el = document.createElement('div');
    el.className = 'msg agent'; el.id = 'typing';
    el.innerHTML = `<div class="msg-dot"></div><div class="msg-inner"><div class="msg-meta"><strong>AI Agent</strong></div><div class="typing"><span></span><span></span><span></span></div></div>`;
    const t = document.getElementById('transcript');
    t.appendChild(el); t.scrollTop = t.scrollHeight;
}
function removeTyping() {
    const el = document.getElementById('typing');
    if (el) el.remove();
}

// ══ TRANSCRIPT ══
let activeTranscriptElements = {}; // Tracks live segments by ID

function updateLiveTranscript(role, text, id, isFinal) {
    if (!text || text.trim() === '') return;
    removeEmpty(); removeTyping();
    const t = document.getElementById('transcript');
    let el = activeTranscriptElements[id];

    if (!el) {
        const sender = role === 'agent' ? 'AI Agent' : 'You';
        const time = new Date().toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        el = document.createElement('div');
        el.className = `msg ${role}`;
        el.innerHTML = `<div class="msg-dot"></div>
        <div class="msg-inner">
          <div class="msg-meta"><strong>${esc(sender)}</strong><span>${time}</span></div>
          <div class="msg-text">${esc(text)}</div>
        </div>`;
        t.appendChild(el);
        activeTranscriptElements[id] = el;
    } else {
        const textNode = el.querySelector('.msg-text');
        textNode.innerHTML = esc(text);
    }
    t.scrollTop = t.scrollHeight;

    if (isFinal) {
        const sender = role === 'agent' ? 'AI Agent' : 'You';
        const time = new Date().toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        transcriptItems.push({ type: role, sender, text, time });
        delete activeTranscriptElements[id];

        if (role === 'user') {
            stats.turns++;
            lastUserTime = Date.now();
            setAgentState('live', 'Listening...');
            updateStats();
        } else {
            setAgentState('speaking', 'Speaking...');
        }
    }
}

function addMsg(type, sender, text) {
    removeEmpty();
    removeTyping();
    const t = document.getElementById('transcript');
    const time = new Date().toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    transcriptItems.push({ type, sender, text, time });
    const el = document.createElement('div');
    el.className = `msg ${type}`;
    el.innerHTML = `<div class="msg-dot"></div>
    <div class="msg-inner">
      <div class="msg-meta"><strong>${esc(sender)}</strong><span>${time}</span></div>
      <div class="msg-text">${esc(text)}</div>
    </div>`;
    t.appendChild(el); t.scrollTop = t.scrollHeight;
}

function removeEmpty() {
    const e = document.querySelector('.empty'); if (e) e.remove();
}

// ══ FUNCTION CALLS LOG ══
function addFnItem(name, params, result) {
    const log = document.getElementById('fn-log');
    const empty = log.querySelector('div:not(.fn-item)');
    if (empty) empty.remove();
    document.getElementById('fn-count').textContent = fnItems.length + 1;
    fnItems.push({ name, params, result, time: new Date().toISOString() });
    const id = `fn${Date.now()}`;
    const tag = name === 'book_appointment' ? 'appt' : name === 'mark_dnc' ? 'dnc' : 'call';
    const tagLabel = name === 'book_appointment' ? 'BOOKED' : name === 'mark_dnc' ? 'DNC' : name === 'transfer_to_human' ? 'TRANSFER' : 'CALLED';
    const time = new Date().toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const el = document.createElement('div');
    el.className = 'fn-item';
    el.innerHTML = `
    <div class="fn-hdr" onclick="toggleFn('${id}')">
      <span class="fn-tag ${tag}">${tagLabel}</span>
      <span class="fn-pname">${name}</span>
      <span class="fn-ms">${time}</span>
    </div>
    <div class="fn-detail" id="${id}">
      <pre>PARAMS:\n${esc(JSON.stringify(params, null, 2))}\n\nRESULT:\n${esc(JSON.stringify(result, null, 2))}</pre>
    </div>`;
    log.appendChild(el); log.scrollTop = log.scrollHeight;
}

function toggleFn(id) {
    document.getElementById(id).classList.toggle('open');
}

// ══ TOASTS ══
function toast(type, title, body) {
    const wrap = document.getElementById('toast-wrap');
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.innerHTML = `<div class="toast-title">${title}</div><div class="toast-body">${body}</div>`;
    wrap.appendChild(el);
    setTimeout(() => el.remove(), 5000);
}

// ══ STATS & UI HELPERS ══
function updateStats() {
    document.getElementById('s-turns').textContent = stats.turns;
    document.getElementById('s-fns').textContent = stats.fns;
    if (stats.latencies.length) {
        const avg = (stats.latencies.reduce((a, b) => a + b, 0) / stats.latencies.length).toFixed(2);
        document.getElementById('s-lat').textContent = avg + 's';
    }
}

function setCallBtn(state) {
    const b = document.getElementById('call-btn');
    const i = document.getElementById('btn-icon');
    const l = document.getElementById('btn-label');
    b.className = `call-btn ${state}`;
    if (state === 'end') { i.textContent = '📵'; l.textContent = 'End Call'; }
    else if (state === 'connecting') { i.textContent = '⏳'; l.textContent = 'Connecting...'; }
    else { i.textContent = '📞'; l.textContent = 'Start Call'; }
}

function setStatus(cls, label) {
    const p = document.getElementById('status-pill');
    p.className = `status-pill ${cls}`; p.textContent = label;
}
function setAgentState(cls, text) {
    document.getElementById('avatar-ring').className = `avatar-ring ${cls}`;
    document.getElementById('agent-state').innerHTML = `Status: <em>${text}</em>`;
}
function setFooter(msg) { document.getElementById('footer-msg').textContent = msg; }

function startTimer() {
    sessionStart = Date.now();
    timerInterval = setInterval(() => {
        const s = Math.floor((Date.now() - sessionStart) / 1000);
        const m = Math.floor(s / 60);
        document.getElementById('timer').textContent = `${String(m).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
        document.getElementById('s-dur').textContent = `${m}:${String(s % 60).padStart(2, '0')}`;
    }, 1000);
}
function stopTimer() { clearInterval(timerInterval); }

function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ══ ANALYTICS MODAL ══
function openModal() {
    const dur = sessionStart ? Math.floor((Date.now() - sessionStart) / 1000) : 0;
    const m = Math.floor(dur / 60), s = dur % 60;
    const avgLat = stats.latencies.length ? (stats.latencies.reduce((a, b) => a + b, 0) / stats.latencies.length).toFixed(2) + 's' : '—';
    const apptRows = appointments.map(a => `
    <div class="appt-row">
      <div class="ar-name">📅 ${a.name || 'Prospect'} — ${a.appointment_id || ''}</div>
      <div class="ar-detail">${a.date || ''} at ${a.time || ''} ${a.timezone || 'IST'} · ${a.email || ''}</div>
    </div>`).join('') || '<p style="color:var(--t3);font-size:.8rem">No appointments booked</p>';

    document.getElementById('modal-body').innerHTML = `
    <div class="ag">
      <div class="ac t"><div class="av">${stats.turns}</div><div class="al">Conversation Turns</div></div>
      <div class="ac v"><div class="av">${stats.fns}</div><div class="al">Function Calls</div></div>
      <div class="ac g"><div class="av">${m}:${String(s).padStart(2, '0')}</div><div class="al">Duration</div></div>
    </div>
    <div class="ag" style="grid-template-columns:1fr 1fr;margin-bottom:18px">
      <div class="ac t"><div class="av">${avgLat}</div><div class="al">Avg LLM Latency</div></div>
      <div class="ac v"><div class="av">${dncList.length}</div><div class="al">DNC Requests</div></div>
    </div>
    <div class="sec">Booked Demos</div>
    <div class="appts-list">${apptRows}</div>
    <div class="sec">Export</div>
    <div class="dl-row">
      <button class="btn-dl" onclick="dlJSON()">📦 Full Session JSON</button>
      <button class="btn-dl" onclick="dlText()">📄 Transcript TXT</button>
    </div>`;
    document.getElementById('modal').classList.add('open');
}
function closeModal() { document.getElementById('modal').classList.remove('open'); }

// ══ DOWNLOADS ══
function dlJSON() {
    download('zenisth-session.json', JSON.stringify({
        session: { start: sessionStart ? new Date(sessionStart).toISOString() : null, end: new Date().toISOString(), stats },
        transcript: transcriptItems,
        function_calls: fnItems,
        appointments, dnc_list: dncList
    }, null, 2), 'application/json');
}
function dlText() {
    const lines = transcriptItems.map(i => `[${i.time}] ${i.sender}: ${i.text}`).join('\n');
    download('zenisth-transcript.txt', lines, 'text/plain');
}
function download(name, content, type) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([content], { type }));
    a.download = name; a.click();
}

// ══ RESET ══
function resetAll() {
    if (callActive) endCall();
    transcriptItems = []; fnItems = []; appointments = []; dncList = [];
    stats = { turns: 0, fns: 0, latencies: [] };
    agentAudioQueue = []; isPlayingAudio = false; nextPlayTime = 0;
    document.getElementById('transcript').innerHTML = `<div class="empty"><div class="icon">🎙️</div><p>Press <strong>Start Call</strong> to begin.<br>Live transcript will appear here.</p></div>`;
    document.getElementById('fn-log').innerHTML = '<div style="color:var(--t3);font-size:.78rem;text-align:center;padding:24px">No actions yet</div>';
    document.getElementById('fn-count').textContent = '0';
    document.getElementById('timer').textContent = '00:00';
    ['s-turns', 's-fns'].forEach(id => document.getElementById(id).textContent = '0');
    document.getElementById('s-dur').textContent = '0:00';
    document.getElementById('s-lat').textContent = '—';
    setCallBtn('start'); setStatus('idle', '● Idle');
    setAgentState('', 'Idle'); setFooter('Session reset');
}

window.onload = () => { initWaveform(); drawWaveform(); loadConfig(); };
window.onresize = initWaveform;

// ══ SETTINGS MODAL ══
let activeDocId = localStorage.getItem('activeDocId') || '';

let editingAgentId = null;

function openSettingsModal() {
    document.getElementById('settings-modal').classList.add('open');
    populateSettingsDropdowns();
    loadDocuments();
    loadAgents();
    fetch('/api/config').then(r => r.json()).then(d => {
        document.getElementById('sm-prompt').value = d.system_prompt || '';
        updateCharCount();
    }).catch(() => { });
    document.getElementById('sm-prompt').addEventListener('input', updateCharCount);
    showLibraryView();
}

function showLibraryView() {
    const lib = document.getElementById('view-library');
    const ed  = document.getElementById('view-editor');
    ed.classList.remove('active');  ed.style.display = 'none';
    lib.classList.add('active');    lib.style.display = '';
    document.getElementById('settings-header').textContent = '📂 Agent Library';
    editingAgentId = null;
    loadAgents();
}

function showEditorView(title) {
    const lib = document.getElementById('view-library');
    const ed  = document.getElementById('view-editor');
    lib.classList.remove('active'); lib.style.display = 'none';
    ed.classList.add('active');     ed.style.display = '';
    document.getElementById('settings-header').textContent = title;
    switchTab('voice'); // default tab
}

function closeSettingsModal() {
    document.getElementById('settings-modal').classList.remove('open');
}

function switchTab(name) {
    document.querySelectorAll('.sm-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.sm-pane').forEach(p => p.classList.remove('active'));
    document.getElementById('tab-' + name).classList.add('active');
    document.getElementById('pane-' + name).classList.add('active');
}

async function populateSettingsDropdowns() {
    try {
        const [mRes, cRes] = await Promise.all([fetch('/api/models'), fetch('/api/config')]);
        const models = await mRes.json();
        const defaults = await cRes.json();

        // Voice
        const vs = document.getElementById('sm-voice');
        vs.innerHTML = '';
        const families = [...new Set(models.voice.map(v => v.family))];
        families.forEach(f => {
            const g = document.createElement('optgroup'); g.label = f;
            models.voice.filter(v => v.family === f).forEach(v => {
                const o = document.createElement('option');
                o.value = v.id; o.textContent = `${v.label} ${v.gender === 'female' ? '♀' : '♂'} — ${v.family}`;
                if (v.id === defaults.voice_model) o.selected = true;
                g.appendChild(o);
            }); vs.appendChild(g);
        });

        // STT
        const ss = document.getElementById('sm-stt'); ss.innerHTML = '';
        models.stt.forEach(m => {
            const o = document.createElement('option'); o.value = m.id; o.textContent = m.label;
            if (m.id === defaults.stt_model) o.selected = true; ss.appendChild(o);
        });

        // LLM
        const ls = document.getElementById('sm-llm'); ls.innerHTML = '';
        models.llm.forEach(m => {
            const o = document.createElement('option'); o.value = m.id; o.textContent = m.label;
            if (m.id === defaults.llm_model) o.selected = true; ls.appendChild(o);
        });

        // Scalars — Temperature
        const t = parseFloat(defaults.temperature) || 0.7;
        document.getElementById('sm-temp').value = t;
        document.getElementById('sm-temp-val').textContent = t.toFixed(2);
        document.getElementById('sm-greeting').value = defaults.greeting || '';

        // ── New fields — prefer localStorage (user-saved) over config defaults ──
        const savedSpeed = parseFloat(localStorage.getItem('voice_speed') ?? defaults.voice_speed ?? 1.0);
        document.getElementById('sm-voice-speed').value = savedSpeed;
        document.getElementById('sm-voice-speed-val').textContent = savedSpeed.toFixed(2) + '×';

        const savedEmotion = localStorage.getItem('voice_emotion') || defaults.voice_emotion || 'helpful';
        document.getElementById('sm-voice-emotion').value = savedEmotion;

        const savedBargeIn = localStorage.getItem('barge_in');
        const bargeInVal = savedBargeIn !== null ? savedBargeIn !== 'false' : (defaults.barge_in !== false);
        document.getElementById('sm-barge-in').checked = bargeInVal;

        const savedEndpointing = parseInt(localStorage.getItem('endpointing') ?? defaults.endpointing ?? 800, 10);
        document.getElementById('sm-endpointing').value = savedEndpointing;
        document.getElementById('sm-endpointing-val').textContent = savedEndpointing + 'ms';

        const savedFiller = localStorage.getItem('filler_audio');
        const fillerVal = savedFiller !== null ? savedFiller === 'true' : (defaults.filler_audio === true);
        document.getElementById('sm-filler-audio').checked = fillerVal;

        // Custom API Key
        document.getElementById('sm-api-key').value = localStorage.getItem('deepgram_api_key') || '';

    } catch (e) { console.warn('Settings load failed:', e); }
}

async function saveSettings() {
    const statusEl = document.getElementById('save-status');
    try {
        const voiceSpeed = parseFloat(document.getElementById('sm-voice-speed').value);
        const voiceEmotion = document.getElementById('sm-voice-emotion').value;
        const bargeIn = document.getElementById('sm-barge-in').checked;
        const endpointing = parseInt(document.getElementById('sm-endpointing').value, 10);
        const fillerAudio = document.getElementById('sm-filler-audio').checked;

        const payload = {
            voice_model: document.getElementById('sm-voice').value,
            stt_model: document.getElementById('sm-stt').value,
            llm_model: document.getElementById('sm-llm').value,
            temperature: parseFloat(document.getElementById('sm-temp').value),
            greeting: document.getElementById('sm-greeting').value.trim(),
            system_prompt: document.getElementById('sm-prompt').value.trim(),
            voice_speed: voiceSpeed,
            voice_emotion: voiceEmotion,
            barge_in: bargeIn,
            endpointing: endpointing,
            filler_audio: fillerAudio,
        };
        const r = await fetch('/api/config/save', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const d = await r.json();
        if (d.error) throw new Error(d.error);

        // Persist new fields to localStorage for use during next call
        localStorage.setItem('voice_speed', voiceSpeed);
        localStorage.setItem('voice_emotion', voiceEmotion);
        localStorage.setItem('barge_in', bargeIn);
        localStorage.setItem('endpointing', endpointing);
        localStorage.setItem('filler_audio', fillerAudio);
        localStorage.setItem('deepgram_api_key', document.getElementById('sm-api-key').value.trim());

        // Sync left panel dropdowns
        ['voice', 'stt', 'llm'].forEach(k => {
            const el = document.getElementById('cfg-' + k);
            if (el) el.value = payload[k + '_model'] || payload[k];
        });
        document.getElementById('cfg-greeting').value = payload.greeting;
        document.getElementById('cfg-prompt').value = payload.system_prompt;

        statusEl.textContent = '✓ Saved successfully';
        statusEl.className = 'save-status ok';
        statusEl.style.display = 'inline-flex';
        setTimeout(() => { statusEl.style.display = 'none'; }, 3000);
    } catch (e) {
        statusEl.textContent = '✗ ' + e.message;
        statusEl.className = 'save-status err';
        statusEl.style.display = 'inline-flex';
    }
}

function updateCharCount() {
    const len = document.getElementById('sm-prompt').value.length;
    document.getElementById('sm-char-count').textContent = len ? `${len.toLocaleString()} chars` : '';
}

function resetPrompt() {
    document.getElementById('sm-prompt').value = '';
    updateCharCount();
}

// ── Knowledge Upload ──────────────────────────────────────────────────────
// Track which doc is being activated (shows spinner per card)
const _activatingDocs = new Set();

function handleFileSelect(e) {
    const file = e.target.files[0];
    if (!file) return;
    uploadDocument(file);
    // Reset input so same file can be re-uploaded
    e.target.value = '';
}

// Drag & drop support
const uz = document.getElementById('upload-zone');
if (uz) {
    uz.addEventListener('dragover', e => { e.preventDefault(); uz.classList.add('drag-over'); });
    uz.addEventListener('dragleave', () => uz.classList.remove('drag-over'));
    uz.addEventListener('drop', e => {
        e.preventDefault(); uz.classList.remove('drag-over');
        const f = e.dataTransfer.files[0];
        const validExt = ['.pdf', '.docx', '.txt'];
        const ext = f && f.name.includes('.') ? '.' + f.name.split('.').pop().toLowerCase() : '';
        if (f && validExt.includes(ext)) uploadDocument(f);
    });
}

// Phase 1: Upload file only — no ingestion yet
async function uploadDocument(file) {
    const prog = document.getElementById('upload-progress');
    const fill = document.getElementById('upload-fill');
    const label = document.getElementById('upload-label');

    prog.style.display = 'block';
    fill.style.width = '30%'; fill.style.background = ''; label.textContent = 'Uploading file…';

    const fd = new FormData();
    fd.append('file', file);
    // No document_id field — server derives it from filename

    try {
        fill.style.width = '80%';
        const r = await fetch('/api/upload', { method: 'POST', body: fd });
        const d = await r.json();
        if (d.error) throw new Error(d.error);

        fill.style.width = '100%';
        label.textContent = `✓ "${d.filename}" uploaded — click Mark as Active to use it`;
        setTimeout(() => { prog.style.display = 'none'; fill.style.width = '0'; }, 4000);
        loadDocuments();
    } catch (e) {
        fill.style.width = '100%'; fill.style.background = 'var(--err)';
        label.textContent = '✗ ' + e.message;
        setTimeout(() => { prog.style.display = 'none'; fill.style.background = ''; fill.style.width = '0'; }, 5000);
    }
}

// Phase 2: Activate = ingest (if needed) + set as active doc for calls
async function activateDocument(docId, filename) {
    if (_activatingDocs.has(docId)) return; // prevent double-click
    _activatingDocs.add(docId);
    loadDocuments(); // re-render to show spinner

    try {
        const r = await fetch('/api/activate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ document_id: docId, filename: filename })
        });
        const d = await r.json();
        if (d.error) throw new Error(d.error);

        // Set as active document in session
        activeDocId = docId;
        localStorage.setItem('activeDocId', docId);
        showToast(`✓ "${docId}" is now active (${d.node_count || '?'} sections indexed)`, 'ok');
    } catch (e) {
        showToast('✗ Activation failed: ' + e.message, 'err');
    } finally {
        _activatingDocs.delete(docId);
        loadDocuments();
    }
}

// Set already-ingested doc as active without re-ingesting
function setActiveDocument(docId) {
    activeDocId = docId;
    localStorage.setItem('activeDocId', docId);
    loadDocuments();
}

async function loadDocuments() {
    const body = document.getElementById('doc-list-body');
    try {
        const r = await fetch('/api/documents');
        const docs = await r.json();
        if (!Array.isArray(docs) || !docs.length) {
            body.innerHTML = '<div class="doc-empty">No documents yet. Upload a PDF, DOCX, or TXT above.</div>';
            return;
        }

        body.innerHTML = docs.map(d => {
            const isActive = d.document_id === activeDocId;
            const isActivating = _activatingDocs.has(d.document_id);
            const isReady = d.status === 'ready';
            const dateStr = d.created_at ? new Date(d.created_at).toLocaleDateString() : '';
            const sizeStr = d.file_size ? _fmtSize(d.file_size) : '';

            // Status badge
            let statusBadge;
            if (isActivating) {
                statusBadge = `<span class="doc-status-badge activating">⏳ Ingesting…</span>`;
            } else if (isReady) {
                statusBadge = `<span class="doc-status-badge ready">✓ Ready · ${d.node_count} sections${dateStr ? ' · ' + dateStr : ''}</span>`;
            } else {
                statusBadge = `<span class="doc-status-badge uploaded">Not Ingested${sizeStr ? ' · ' + sizeStr : ''}</span>`;
            }

            // Action button
            let actionBtn;
            if (isActive && isReady) {
                actionBtn = `<button class="doc-active-badge" disabled>✓ Active</button>`;
            } else if (isActivating) {
                actionBtn = `<button class="doc-active-badge inactive activating-btn" disabled>⏳</button>`;
            } else if (isReady) {
                actionBtn = `<button class="doc-active-badge inactive" onclick="setActiveDocument('${escHtml(d.document_id)}')">Set Active</button>`;
            } else {
                actionBtn = `<button class="doc-activate-btn" onclick="activateDocument('${escHtml(d.document_id)}','${escHtml(d.filename)}')">▶ Mark as Active</button>`;
            }

            return `<div class="doc-item ${isActive ? 'active-doc' : ''}" id="docitem-${d.document_id}">
            <span class="doc-icon">${_docIcon(d.filename)}</span>
            <div class="doc-info">
              <div class="doc-name">${escHtml(d.filename || d.document_id)}</div>
              <div class="doc-meta">${escHtml(d.document_id)}</div>
            </div>
            ${statusBadge}
            ${actionBtn}
            <button class="doc-del" title="Delete" onclick="deleteDocument('${escHtml(d.document_id)}','${escHtml(d.filename)}')">🗑</button>
          </div>`;
        }).join('');
    } catch (e) {
        body.innerHTML = `<div class="doc-empty" style="color:var(--err)">Error loading documents: ${e.message}</div>`;
    }
}

async function deleteDocument(docId, filename) {
    if (!confirm(`Delete "${filename || docId}" from library?\nThis removes it from disk and the knowledge base.`)) return;
    try {
        await fetch('/api/documents', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ document_id: docId, filename: filename || '' })
        });
        if (activeDocId === docId) { activeDocId = ''; localStorage.removeItem('activeDocId'); }
        loadDocuments();
    } catch (e) { alert('Delete failed: ' + e.message); }
}

function _docIcon(filename) {
    if (!filename) return '📄';
    const ext = filename.split('.').pop().toLowerCase();
    if (ext === 'pdf') return '📕';
    if (ext === 'docx' || ext === 'doc') return '📘';
    return '📄';
}

function _fmtSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

function showToast(msg, type) {
    const wrap = document.getElementById('toast-wrap');
    if (!wrap) return;
    const t = document.createElement('div');
    t.className = 'toast ' + (type || '');
    t.textContent = msg;
    wrap.appendChild(t);
    setTimeout(() => t.remove(), 4500);
}

function escHtml(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
function escAttr(s) { return escHtml(s).replace(/'/g, '&#39;'); }

// Close settings modal on backdrop click
document.getElementById('settings-modal').addEventListener('click', function (e) {
    if (e.target === this) closeSettingsModal();
});

// ── Agent Library ──────────────────────────────────────────────────────────────
let activeAgentId = localStorage.getItem('activeAgentId') || '';
let _agentsCache = [];
let _initialFormState = '';

function getFormState() {
    return [
        document.getElementById('sm-voice').value,
        document.getElementById('sm-stt').value,
        document.getElementById('sm-llm').value,
        document.getElementById('sm-temp').value,
        document.getElementById('sm-greeting').value,
        document.getElementById('sm-prompt').value,
        activeDocId || ''
    ].join('|');
}

function cancelEdit() {
    if (_initialFormState !== getFormState()) {
        if (!confirm("You have unsaved changes. Are you sure you want to discard them?")) return;
    }
    showLibraryView();
}

async function loadAgents() {
    const list = document.getElementById('agent-list-body');
    if (!list) return;
    try {
        const r = await fetch('/api/agents');
        _agentsCache = await r.json();

        // Update the Active Agent Banner
        const banner = document.getElementById('active-agent-banner');
        if (activeAgentId && _agentsCache.length) {
            const act = _agentsCache.find(x => x.id === activeAgentId);
            if (act) {
                document.getElementById('aab-name').textContent = act.name;
                document.getElementById('aab-desc').textContent = `${act.voice_model || 'default voice'} · ${act.llm_model || 'default llm'}`;
                banner.style.display = 'flex';
            } else { banner.style.display = 'none'; }
        } else { banner.style.display = 'none'; }

        if (!_agentsCache || !_agentsCache.length) {
            list.innerHTML = '<div class="doc-empty">No saved agents. Click "+ Create New Agent".</div>';
            return;
        }

        list.innerHTML = _agentsCache.map(a => {
            const isActive = a.id === activeAgentId;
            let activateBtn = isActive
                ? `<button class="doc-active-badge" disabled style="min-width:80px;">✓ Active</button>`
                : `<button class="doc-activate-btn" style="min-width:80px;background:var(--agent);color:var(--bg)" onclick="applyAgent('${a.id}')">▶ Activate</button>`;

            return `
            <div class="doc-item ${isActive ? 'active-doc' : ''}" id="agent-${a.id}">
                <span class="doc-icon">🤖</span>
                <div class="doc-info">
                    <div class="doc-name">${escHtml(a.name)}</div>
                    <div class="doc-meta">${escHtml(a.voice_model || '')} · ${escHtml(a.llm_model || '')}</div>
                </div>
                <span class="doc-status-badge ready" style="margin-right:8px;">${a.document_id ? '📚 Linked KB' : 'No KB'}</span>
                ${activateBtn}
                <button class="doc-activate-btn" style="background:var(--s3);color:var(--ttext);margin-left:4px;" onclick="editAgent('${a.id}')">✏️ Edit</button>
            </div>
            `;
        }).join('');

    } catch (e) {
        list.innerHTML = `<div class="doc-empty" style="color:var(--err)">Error loading agents: ${e.message}</div>`;
    }
}

function editAgent(id) {
    const a = _agentsCache.find(x => x.id === id);
    if (!a) return;

    // Load config into DOM
    if (a.voice_model) document.getElementById('sm-voice').value = a.voice_model;
    if (a.stt_model) document.getElementById('sm-stt').value = a.stt_model;
    if (a.llm_model) document.getElementById('sm-llm').value = a.llm_model;
    if (a.temperature) {
        document.getElementById('sm-temp').value = a.temperature;
        document.getElementById('sm-temp-val').textContent = parseFloat(a.temperature).toFixed(2);
    }
    document.getElementById('sm-greeting').value = a.greeting || '';
    document.getElementById('sm-prompt').value = a.system_prompt || '';
    updateCharCount();

    if (a.document_id) { setActiveDocument(a.document_id); }
    else { activeDocId = ''; localStorage.removeItem('activeDocId'); loadDocuments(); }

    editingAgentId = id;
    document.getElementById('btn-update-agent').style.display = 'inline-block';

    showEditorView(`Editing: ${a.name}`);
    _initialFormState = getFormState();
}

function createNewAgent() {
    editingAgentId = null;
    document.getElementById('sm-greeting').value = '';
    document.getElementById('sm-prompt').value = 'You are a helpful AI assistant...';
    document.getElementById('btn-update-agent').style.display = 'none';
    showEditorView('Creating New Agent');
    _initialFormState = getFormState();
}

function applyAgent(id) {
    const a = _agentsCache.find(x => x.id === id);
    if (!a) return;

    // We still load the physical defaults into the DOM just so the global state matches
    editAgent(id);

    activeAgentId = id;
    localStorage.setItem('activeAgentId', id);

    // Save physical yaml config for the python worker
    saveSettings();

    // Trigger micro-animation
    const banner = document.getElementById('active-agent-banner');
    if (banner) {
        banner.classList.add('activating');
        setTimeout(() => banner.classList.remove('activating'), 300);
    }

    // Go immediately back to Library to show it's active
    showLibraryView();
    showToast(`✓ "${a.name}" is now the active system!`, 'ok');
}

async function commitAgentSave(mode) {
    let finalName = '';
    let finalId = null;

    if (mode === 'update' && editingAgentId) {
        const a = _agentsCache.find(x => x.id === editingAgentId);
        if (!a) return;
        finalName = a.name;
        finalId = editingAgentId;
    } else {
        const n = prompt("Enter a name for this new Agent Configuration:");
        if (!n) return;
        finalName = n.trim();
    }

    const payload = {
        name: finalName,
        voice_model: document.getElementById('sm-voice').value,
        stt_model: document.getElementById('sm-stt').value,
        llm_model: document.getElementById('sm-llm').value,
        temperature: parseFloat(document.getElementById('sm-temp').value),
        greeting: document.getElementById('sm-greeting').value.trim(),
        system_prompt: document.getElementById('sm-prompt').value.trim(),
        document_id: activeDocId || null
    };

    if (finalId) { payload.id = finalId; }

    const btn = mode === 'update' ? document.getElementById('btn-update-agent') : document.getElementById('btn-save-new');
    const oldText = btn.textContent;
    btn.textContent = 'Saving...';

    try {
        const r = await fetch('/api/agents', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const d = await r.json();
        if (d.error) throw new Error(d.error);

        showToast(`Agent "${finalName}" saved successfully!`, 'ok');

        // Auto-activate if we just explicitly edited it or made a new one
        activeAgentId = d.id;
        localStorage.setItem('activeAgentId', d.id);

        saveSettings();
        showLibraryView();
    } catch (e) {
        showToast('Error saving agent: ' + e.message, 'err');
    } finally {
        btn.textContent = oldText;
    }
}

async function deleteAgent(id, name) {
    if (!confirm(`Delete abstract agent "${name}" permanently?`)) return;
    try {
        await fetch(`/api/agents/${id}`, { method: 'DELETE' });
        if (activeAgentId === id) {
            activeAgentId = '';
            localStorage.removeItem('activeAgentId');
        }
        loadAgents();
    } catch (e) {
        alert('Delete failed: ' + e.message);
    }
}

// ── Filler Audio relay helper ──────────────────────────────────────────────────
// Called by handleEvent when a function_call event fires, if filler audio is ON,
// the server relay already injected the phrase. This is a no-op stub for future
// client-side filler injection if needed.
function _maybeInjectFiller() {
    // Intentionally empty — filler is handled server-side in server.py relay
}