# PLAN.md — Phase 1: Foundation (LiveKit Managed Models)

> **Objective**: Establish a LiveKit Room connection and an Agent Worker using **LiveKit Cloud Managed Models** (Chained STT -> LLM -> TTS).
> **Outcome**: An agent that joins the room and handles voice interaction using unified LiveKit Cloud credentials.

## 🌊 Wave 1: Environment & Dependencies
**Goal**: Prepare the local system for LiveKit development.

<task type="auto">
  <name>Install Unified Plugins</name>
  <files>requirements.txt</files>
  <action>
    Add the following to requirements.txt:
    livekit-agents
    livekit-plugins-silero
    livekit-plugins-noise-cancellation
    Then run: pip install -r requirements.txt
    (Note: LiveKit Inference handles Deepgram, Google, and Cartesia natively without their specific plugins!)
  </action>
  <verify>pip show livekit-agents</verify>
  <done>Dependencies installed successfully.</done>
</task>

<task type="manual">
  <name>Configure Unified Credentials</name>
  <files>.env.local</files>
  <action>
    Create .env.local with your LiveKit Cloud keys:
    LIVEKIT_URL (wss://...)
    LIVEKIT_API_KEY (devkey)
    LIVEKIT_API_SECRET (secret)
    # Note: No separate Deepgram/Google keys needed for Managed Models.
  </action>
  <verify>Confirm LiveKit Cloud connection works via CLI.</verify>
  <done>.env.local set up for unified access.</done>
</task>

## 🌊 Wave 2: Agent Worker Implementation
**Goal**: Create a separate process for the WebRTC agent loop using a Chained Pipeline via LiveKit Cloud.

<task type="auto">
  <name>Create Agent Worker Script (Inference Managed)</name>
  <files>app/agent_worker.py</files>
  <action>
    Implement a LiveKit Agent Server with VoicePipelineAgent using LiveKit Inference descriptors.
    1. STT: stt="deepgram/nova-3:multi"
    2. LLM: llm="google/gemini-2.0-flash" (or openai/gpt-4o-mini depending on fallback)
    3. TTS: tts="cartesia/sonic-3:9626c31c-bec5-4cca-baa8-f8ba9e84c8bc"
    4. VAD: silero.VAD.load()
    NO plugins are required for inference. Pass these strings directly to AgentSession or VoicePipelineAgent initialization.
  </action>
  <verify>python -m app.agent_worker dev</verify>
  <done>Worker starts using built-in LiveKit inference and listens for incoming jobs.</done>
</task>

## 🌊 Wave 3: Verification & Smoke Test
**Goal**: Prove end-to-end audio flow.

<task type="manual">
  <name>Verify Turnaround</name>
  <files>app/agent_worker.py</files>
  <action>
    1. Start the Agent Worker.
    2. Join the same room via LiveKit dashboard.
    3. Confirm audio Flow: STT(DG) -> LLM(Gemini) -> TTS(Cartesia).
  </action>
  <verify>Check logs for successful turn completion.</verify>
  <done>Stable voice interaction with managed components confirmed.</done>
</task>
