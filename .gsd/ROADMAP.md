# ROADMAP.md — Zenisth LiveKit Migration

> **Current Phase**: Phase 1 (Foundation)
> **Milestone**: v1.0 (LiveKit S2S PoC)

## Must-Haves
- [ ] Sub-500ms Audio-to-Audio latency.
- [ ] Native Gemini Multimodal interruption handling.
- [ ] Supabase RAG function call.
- [ ] Cal.com booking function call.
- [ ] React-based LiveKit frontend dashboard.

## Phases

### Phase 1: Foundation & Agent Worker
**Status**: ⬜ Not Started
**Objective**: Establish a basic LiveKit room and an Agent Worker that can speak.
- Set up LiveKit local server and environment variables.
- Create `app/agent_worker.py` (Simple Gemini S2S assistant).
- Verify basic audio connection (Agent starts/stops on VAD).

### Phase 2: Intelligence & Tools Port
**Status**: ⬜ Not Started
**Objective**: Port existing Zenisth tools to the LiveKit `FunctionContext`.
- Hook up `search_company_knowledge` (Supabase RAG).
- Hook up `check_availability` and `book_appointment` (Cal.com).
- Verify tool execution within the voice loop without dropping the stream.

### Phase 3: Frontend Modernization
**Status**: ⬜ Not Started
**Objective**: Gut the custom WebSocket-audio UI for `@livekit/components-react`.
- Integrate `LiveKitRoom` and `useVoiceAssistant` hooks.
- Apply "Zenisth" custom CSS to standard LiveKit components.
- Implement token generation endpoint in FastAPI.

### Phase 4: Benchmarking & Optimization
**Status**: ⬜ Not Started
**Objective**: Fine-tune for sub-500ms latency and "perfect" barge-in.
- Optimize Gemini model parameters (temperature, voice selection).
- Benchmark latency and jitter using LiveKit telemetry.
- Final verification against success criteria.

### Phase 5: SIP Integration (Future)
**Status**: ⬜ Not Started
**Objective**: Bridge Vobiz outbound calls into LiveKit via SIP.
- Configure LiveKit SIP participant.
- Replace Base64 translation with native telephony-to-agent bridging.
