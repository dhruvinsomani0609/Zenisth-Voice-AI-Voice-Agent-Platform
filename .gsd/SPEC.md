# SPEC.md — Zenisth LiveKit Voice Agent Migration

> **Status**: `FINALIZED`
> **Project**: Zenisth Voice AI Agent Platform (LiveKit Migration)
> **Goal**: Sub-500ms latency Speech-to-Speech (S2S) via Gemini & LiveKit

## Vision
Transform the Zenisth Voice Agent from a custom WebSocket-relay architecture to a native WebRTC-based LiveKit Agent system using a **Chained Pipeline** (STT -> LLM -> TTS). This migration aims to maintain high granular control over voice and synthesis while leveraging the LiveKit Agent framework.

## Goals
1. **Low Latency Pipeline**: Optimize the hand-off between STT, Gemini LLM, and TTS.
2. **Standard Interruption**: Implement robust turn-detection and interruption handling via LiveKit's Silero VAD.
3. **Advanced Tooling**: Port Vectorless RAG (Supabase) and Scheduling (Cal.com) into the LiveKit `FunctionContext`.
4. **Modern Frontend**: Replace custom audio buffering with `@livekit/components-react`.
5. **Decoupled Architecture**: Separate the Agent Worker (WebRTC loop) from the FastAPI Backend (Auth/Management).

## Success Criteria
- [ ] **Latency**: Sustained <500ms response time in local/cloud tests.
- [ ] **Barge-in**: Agent immediately stops speaking upon user interruption without "residual speech" artifacts.
- [ ] **RAG Integrity**: The agent successfully retrieves and synthesizes knowledge from Supabase during a live session.
- [ ] **Booking Flow**: A complete end-to-end booking flow (Check Avail → Book Slot) handled via voice.
- [ ] **Stable WebRTC**: Connection remains open and clean throughout 10+ minute sessions.

## Non-Goals (Out of Scope)
- **SIP Integration**: Telephony bridging (Vobiz/SIP) is deferred to a subsequent milestone after WebRTC validation.
- **Voice Cloning**: Precise voice cloning (Cartesia/11Labs) is out of scope for the Gemini S2S PoC phase.
- **Multi-region deployment**: Initial PoC will be focused on Localhub/One-region Cloud.

## Users
- **Businesses**: Integrating automated voice receptionists into their dashboards.
- **Lead Gen Teams**: Running high-volume outbound campaigns with AI-to-human hands-off.

## Constraints
- **Model**: Must use `google.GeminiModel` (Multimodal Live API) via `livekit-plugins-google`.
- **Backend**: Python 3.10+ (LiveKit Agent SDK).
- **Frontend**: React (LiveKit Components).
- **Network**: WebRTC (UDP) for low-latency audio.
