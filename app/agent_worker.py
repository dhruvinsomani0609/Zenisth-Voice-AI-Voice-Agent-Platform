"""
app/agent_worker.py
===================
Zenisth Voice — LiveKit Agent Worker (Chained Pipeline)

Architecture:
  LiveKit Room ←→ Agent Worker
                    ├── STT  : deepgram/nova-3 (via LiveKit Inference)
                    ├── LLM  : openai/gpt-4o-mini  (via LiveKit Inference)
                    ├── TTS  : cartesia/sonic-3     (via LiveKit Inference)
                    └── VAD  : Silero

All tool calls (RAG, Cal.com, DNC, etc.) are dispatched via LiveKit
FunctionContext and delegate to the existing handlers in app/agent/functions.py.

Run with:
    python -m app.agent_worker dev
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    RoomInputOptions,
)
from livekit.agents import function_tool
from livekit.plugins import noise_cancellation, silero

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
_BASE_DIR = Path(__file__).parent.parent
_SYSTEM_PROMPT_FILE = _BASE_DIR / "system_prompt.txt"

_PROMPT_FALLBACK = """\
You are a helpful AI Voice Assistant. Today is {current_date}.
You help users by answering questions and scheduling appointments.

HOW TO ANSWER QUESTIONS:
- When the user asks ANY question about a topic, product, company, process, or document content,
  ALWAYS call the search_company_knowledge function first to find the answer.
- Base your spoken answer ONLY on what the function returns. Do not guess or hallucinate.
- If the function returns no useful information, say: "I don't have that information in the current document."

SCHEDULING PROTOCOL:
Step 1: Ask for date and call check_availability.
Step 2: Present only 2-3 available slots. Wait for user to choose.
Step 3: Collect full name and email. ALWAYS read email back letter-by-letter before booking.
Step 4: Say "Perfect, locking that in for you now..." then call book_appointment.

VOICE & TONE:
- Short, clear sentences. Conversational and friendly.
- Never read out long lists. Summarize key points naturally.

STRICT RULES:
- Always use search_company_knowledge before answering content-specific questions.
- Always confirm email addresses before booking.
- You are AI. Never claim to be human.
"""


def _get_system_prompt() -> str:
    """Load custom system_prompt.txt or fall back to the built-in template."""
    if _SYSTEM_PROMPT_FILE.exists():
        content = _SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").strip()
        if content:
            return content
    return _PROMPT_FALLBACK.format(
        current_date=datetime.now().strftime("%A, %B %d, %Y")
    )


# ── Tool Definitions ──────────────────────────────────────────────────────────
# All handlers delegate to app/agent/functions.py — no logic duplication.


@function_tool
async def search_company_knowledge(query: str) -> str:
    """
    Search the Zenisth company knowledge base to answer user questions about
    products, pricing, services, features, compliance, deployment, or any
    company-specific information. Always call this before saying you don't know
    something about the company.
    """
    from app.agent.functions import _search_company_knowledge

    result = await asyncio.get_event_loop().run_in_executor(
        None, _search_company_knowledge, {"query": query}
    )
    return result.get("result", "No information found.")


@function_tool
async def check_availability(date: str, timezone: str = "Asia/Kolkata") -> str:
    """
    Check available appointment slots for a given date. Call this before
    offering or confirming any meeting time. Present only 2-3 of the returned
    slots to the user.
    """
    from app.agent.functions import _check_availability

    result = await asyncio.get_event_loop().run_in_executor(
        None, _check_availability, {"date": date, "timezone": timezone}
    )
    if result.get("success"):
        slots = result.get("slots", [])
        return (
            f"Available slots: {', '.join(slots[:5])}"
            if slots
            else "No slots available."
        )
    return result.get("message", "Could not fetch availability.")


@function_tool
async def book_appointment(
    name: str, email: str, date: str, time: str, timezone: str = "Asia/Kolkata"
) -> str:
    """
    Book an appointment on the calendar once the user confirms their name,
    email, date, and time. ALWAYS confirm the email address by reading it
    back before calling this tool.
    """
    from app.agent.functions import _book_appointment

    result = await asyncio.get_event_loop().run_in_executor(
        None,
        _book_appointment,
        {
            "name": name,
            "email": email,
            "date": date,
            "time": time,
            "timezone": timezone,
        },
    )
    if result.get("success"):
        return (
            f"Appointment confirmed for {name} on {date} at {time}. "
            f"A calendar invite will be sent to {email}."
        )
    return result.get("message", "Booking failed. Please try again.")


@function_tool
async def transfer_to_human(reason: str) -> str:
    """
    Transfer to a human agent when the user requests it or when the question
    is too complex for the AI to handle.
    """
    from app.agent.functions import _transfer_to_human

    result = _transfer_to_human({"reason": reason})
    return result.get("message", "Transferring you now.")


@function_tool
async def mark_dnc(reason: str) -> str:
    """
    Mark this contact as Do Not Call when the user explicitly requests
    removal from the calling list.
    """
    from app.agent.functions import _mark_dnc

    result = _mark_dnc({"reason": reason})
    return result.get("message", "You have been marked as Do Not Call.")


@function_tool
async def end_call(reason: str) -> str:
    """
    End the conversation politely when the interaction is complete.
    Reason must be one of: completed, dnc, transferred, no_interest.
    """
    from app.agent.functions import _end_call

    _end_call({"reason": reason})
    return "Thank you for calling. Have a great day!"


# ── Agent Definition ──────────────────────────────────────────────────────────


class ZenisthAssistant(Agent):
    """Zenisth Voice AI — LiveKit Chained Pipeline (STT → LLM → TTS)."""

    def __init__(self, greeting: str, sys_prompt: str) -> None:
        super().__init__(
            instructions=sys_prompt if sys_prompt else _get_system_prompt(),
            tools=[
                search_company_knowledge,
                check_availability,
                book_appointment,
                transfer_to_human,
                mark_dnc,
                end_call,
            ],
        )
        self.greeting = greeting

    async def on_enter(self) -> None:
        """Play the configured greeting right as the session starts."""
        if self.greeting:
            import logging
            import traceback
            import asyncio
            try:
                # Add a brief delay before using the LLM channel to ensure tracks bind
                await asyncio.sleep(0.5)
                self.session.generate_reply(
                    instructions=f"You must say EXACTLY this phrase and nothing else: '{self.greeting}'. Do not add any greeting of your own."
                )
            except Exception as e:
                logging.error(f"FATAL greeting fail: {e}\n{traceback.format_exc()}")



# ── Server Entry Point ────────────────────────────────────────────────────────

server = AgentServer()


@server.rtc_session(agent_name="zenisth-agent")
async def zenisth_session(ctx: JobContext):
    """
    Called by LiveKit for each new Room participant.
    Starts a chained STT → LLM → TTS session via LiveKit Inference.
    Dynamically swaps models based on frontend configuration sent in metadata.
    """
    
    # Connect the agent to the room first
    await ctx.connect()
    
    # Wait for the first human participant to join so we can read their preferences.
    participant = await ctx.wait_for_participant()
    user_data = {}
    try:
        import json
        if participant.metadata:
            user_data = json.loads(participant.metadata)
    except Exception:
        pass

    # Read the absolute latest backend config (so settings saved cleanly apply)
    import yaml
    from pathlib import Path
    with open(Path(__file__).parent.parent / "config.yaml", encoding="utf-8") as f:
        file_cfg = yaml.safe_load(f)["agent"]["defaults"]

    stt_model = user_data.get("stt_model") or file_cfg.get("stt_model")
    llm_model = user_data.get("llm_model") or file_cfg.get("llm_model")
    tts_model = user_data.get("voice_model") or file_cfg.get("voice_model")
    
    # Expose document_id to RAG tools dynamically
    doc_id = user_data.get("document_id")
    if doc_id:
        os.environ["RAG_DOCUMENT_ID"] = doc_id

    session = AgentSession(
        stt=stt_model,
        llm=llm_model,
        tts=tts_model,
        vad=silero.VAD.load(),
    )

    greeting = user_data.get("greeting") or file_cfg.get("greeting")
    sys_prompt = user_data.get("system_prompt") or ""

    await session.start(
        room=ctx.room,
        agent=ZenisthAssistant(greeting=greeting, sys_prompt=sys_prompt),
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVC(),
        ),
    )


if __name__ == "__main__":
    agents.cli.run_app(server)
