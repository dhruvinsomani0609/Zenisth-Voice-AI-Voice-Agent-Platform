"""
app/bot.py
==========
Zenisth Voice — Pipecat Pipeline with Vectorless RAG Tool

Registers the `search_company_knowledge` function tool in the Pipecat
orchestration loop. When called by the LLM:

  1. Immediately yields a TextFrame filler ("Let me pull up that information...")
     so TTS has something to say while the async retrieval runs — preventing
     dead silence on the WebRTC audio stream.
  2. Awaits retrieve_knowledge() to query Redis → Groq router → Supabase.
  3. Calls result_callback(content) to pass the retrieved text back to the LLM.

This module is the Pipecat entry-point and is independent of server.py
(the legacy Deepgram WebSocket relay). It is intended for future WebRTC
deployments using Pipecat's transport layer.

Run with:
    python app/bot.py
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

load_dotenv()

# ── Pipecat imports ────────────────────────────────────────────────────────────
from pipecat.frames.frames import (
    EndFrame,
    LLMFullResponseEndFrame,
    TextFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_response import (
    LLMAssistantResponseAggregator,
    LLMUserResponseAggregator,
)
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.cartesia import CartesiaTTSService
from pipecat.services.deepgram import DeepgramSTTService
from pipecat.services.groq import GroqLLMService
from pipecat.transports.services.daily import DailyParams, DailyTransport

from app.services.retrieval import retrieve_knowledge


# ── Constants ──────────────────────────────────────────────────────────────────

DOCUMENT_ID = os.environ.get("RAG_DOCUMENT_ID", "zenisth_prd")

SYSTEM_PROMPT = (
    "You are a helpful AI voice assistant for Zenisth Solutions. "
    "When users ask about company information, products, pricing, or services, "
    "call the search_company_knowledge tool to retrieve accurate information before answering. "
    "Always keep your spoken responses concise and natural."
)

# Function schema for the Groq LLM
SEARCH_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_company_knowledge",
        "description": (
            "Search the Zenisth company knowledge base to answer user questions about "
            "products, pricing, services, compliance, or any company-specific information."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The user question to look up in the knowledge base.",
                }
            },
            "required": ["query"],
        },
    },
}


# ── RAG Tool Handler ───────────────────────────────────────────────────────────


async def search_company_knowledge(
    function_name: str,
    tool_call_id: str,
    args: dict,
    llm: GroqLLMService,
    context,
    result_callback,
):
    """
    Pipecat function tool handler for `search_company_knowledge`.

    CRITICAL: We push a TextFrame filler immediately via llm.push_frame()
    so the TTS pipeline has audio to play while the async retrieval runs.
    Without this, the user hears silence (~1-2 seconds) during Supabase/Redis I/O.
    """
    user_query: str = args.get("query", "")
    print(f"[bot] Tool called: search_company_knowledge | query='{user_query}'")

    # ── Filler frame: prevent TTS silence ──────────────────────────────────────
    # Push a spoken filler immediately so the user hears something right away.
    await llm.push_frame(TextFrame("Let me pull up that information for you..."))

    # ── RAG retrieval ──────────────────────────────────────────────────────────
    try:
        content = await retrieve_knowledge(
            user_query=user_query,
            document_id=DOCUMENT_ID,
        )
    except Exception as exc:
        print(f"[bot] Retrieval error: {exc}")
        content = "I'm having trouble accessing the knowledge base right now. Please try again in a moment."

    print(f"[bot] Retrieved {len(content)} chars for query: '{user_query}'")

    # ── Return content to LLM ──────────────────────────────────────────────────
    await result_callback(content)


# ── Pipeline Factory ───────────────────────────────────────────────────────────


async def build_pipeline(room_url: str, room_token: str) -> PipelineTask:
    """
    Construct a full Pipecat pipeline:
      DailyTransport (WebRTC) → DeepgramSTT → GroqLLM (with RAG tool)
        → CartesiaTTS → DailyTransport (output)
    """
    transport = DailyTransport(
        room_url,
        room_token,
        "Zenisth AI Assistant",
        DailyParams(
            audio_out_enabled=True,
            transcription_enabled=True,
            vad_enabled=True,
        ),
    )

    stt = DeepgramSTTService(
        api_key=os.environ["DEEPGRAM_API_KEY"],
    )

    llm = GroqLLMService(
        api_key=os.environ["GROQ_API_KEY"],
        model="llama-3.1-8b-instant",
    )

    # Register the RAG tool with the LLM service
    llm.register_function(
        function_name="search_company_knowledge",
        callback=search_company_knowledge,
    )

    tts = CartesiaTTSService(
        api_key=os.environ.get("CARTESIA_API_KEY", ""),
        voice_id=os.environ.get(
            "CARTESIA_VOICE_ID", "a0e99841-438c-4a64-b679-ae501e7d6091"
        ),
    )

    user_aggregator = LLMUserResponseAggregator()
    assistant_aggregator = LLMAssistantResponseAggregator()

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(allow_interruptions=True),
    )

    # Seed the LLM context with system prompt and tool schema
    from pipecat.processors.aggregators.llm_response import LLMMessagesFrame

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]
    await task.queue_frames([LLMMessagesFrame(messages)])
    await llm.set_tools([SEARCH_TOOL_SCHEMA])

    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant):
        transport.capture_participant_transcription(participant["id"])
        print(f"[bot] Participant joined: {participant['id']}")

    @transport.event_handler("on_participant_left")
    async def on_participant_left(transport, participant, reason):
        print(f"[bot] Participant left: {participant['id']}")
        await task.queue_frame(EndFrame())

    return task


async def main():
    room_url = os.environ.get("DAILY_ROOM_URL", "")
    room_token = os.environ.get("DAILY_ROOM_TOKEN", "")

    if not room_url:
        raise RuntimeError(
            "DAILY_ROOM_URL environment variable is required to run bot.py"
        )

    print(f"[bot] Connecting to Daily room: {room_url}")
    print(f"[bot] RAG document_id: {DOCUMENT_ID}")

    task = await build_pipeline(room_url, room_token)
    runner = PipelineRunner()
    await runner.run(task)


if __name__ == "__main__":
    asyncio.run(main())
