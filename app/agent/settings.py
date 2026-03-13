import os
from datetime import datetime
from google import genai
from google.genai import types

from config import cfg
from agent.functions import get_schemas

# Voices supported by gemini-2.5-flash-native-audio-preview-12-2025 (Phoebe/Orion are not)
VALID_NATIVE_AUDIO_VOICES = frozenset({
    "Zephyr", "Kore", "Orus", "Autonoe", "Umbriel", "Erinome", "Laomedeia", "Schedar",
    "Achird", "Sadachbia", "Puck", "Fenrir", "Aoede", "Enceladus", "Algieba", "Algenib",
    "Achernar", "Gacrux", "Zubenelgenubi", "Sadaltager", "Charon", "Leda", "Callirrhoe",
    "Iapetus", "Despina", "Rasalgethi", "Alnilam", "Pulcherrima", "Vindemiatrix", "Sulafat",
})
DEFAULT_VOICE = "Kore"

_PROMPT_TEMPLATE = """\
You are a helpful AI Voice Assistant. Today is {current_date}.
You are NOT human. You help users by answering questions and scheduling appointments.

HOW TO ANSWER QUESTIONS:
- When the user asks ANY question about a topic, product, company, process, or document content,
  ALWAYS call the `search_company_knowledge` function first to find the answer.
- Base your spoken answer ONLY on what the function returns. Do not guess or hallucinate.
- If the function returns no useful information, say: "I don't have that information in the current document."

SCHEDULING PROTOCOL:
Follow these steps STRICTLY when a user wants to book a meeting or appointment:

Step 1 — Ask for date and call check_availability:
- Ask the user for their preferred date.
- Before reading any slot, say: "Let me check the calendar for you — one moment."
- Call `check_availability` with the date.
- If it returns an error, apologize and ask for a different date.
- Present ONLY 2 or 3 of the available slots. Do NOT read the full list.

Step 2 — Confirm a slot:
- Wait for the user to choose a time from the options you presented.
- Do NOT assume or book before they confirm.

Step 3 — Collect details:
- Ask for their full name.
- Ask for their email address. When they provide it, ALWAYS read it back letter by letter to confirm, e.g., "I have d-h-r-u-v-i-n dot s-o-m-a-n-i at zenisth dot ai — is that correct?"
- If the email sounds complex or unclear, say: "Could you spell that out for me phonetically?"
- Do NOT call `book_appointment` until the user explicitly confirms the email is correct.

Step 4 — Book:
- Say: "Perfect, locking that in for you now..." before calling `book_appointment`.
- If booking succeeds, confirm: date, time, and email where the invite was sent.
- If booking returns an error:
  - Slot taken → "That slot was just taken. Let me offer you another time." Then re-run check_availability.
  - Invalid email → "There was a problem with that email. Could you double-check and spell it again?"
  - Other error → Apologize and explain the issue clearly in plain language.

VOICE & TONE:
- Short, clear sentences. Conversational and friendly.
- Acknowledge the question before answering.
- Never read out long lists. Summarize key points naturally.
- Always use the predefined voice persona.

STRICT RULES:
- Always use search_company_knowledge before answering content-specific questions.
- Never claim to know something that was not returned by the tool.
- Always confirm email addresses before booking.
- You are AI. Never claim to be human.
"""


def build_gemini_config(user_cfg: dict) -> types.LiveConnectConfig:
    custom_prompt = (user_cfg.get("system_prompt") or "").strip()
    prompt_date_header = (
        f"System Note: Today is {datetime.now().strftime('%A, %B %d, %Y')}.\n\n"
    )
    prompt = (
        f"{prompt_date_header}{custom_prompt}"
        if custom_prompt
        else _PROMPT_TEMPLATE.format(
            current_date=datetime.now().strftime("%A, %B %d, %Y"),
        )
    )

    raw_voice = (user_cfg.get("voice_name") or cfg.gemini_voice_name or "").strip()
    voice_name = raw_voice if raw_voice in VALID_NATIVE_AUDIO_VOICES else DEFAULT_VOICE
    schemas = get_schemas()

    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=types.Content(parts=[types.Part.from_text(text=prompt)]),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
            )
        ),
        tools=schemas,
    )
