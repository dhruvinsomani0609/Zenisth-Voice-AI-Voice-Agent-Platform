from app.config import cfg
from app.agent.functions import get_schemas

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

TOOLS AVAILABLE:
- search_company_knowledge: Search the uploaded knowledge document for relevant information.
- check_availability: Check available appointment slots for a given date. Always call this first.
- book_appointment: Schedule an appointment once details are confirmed.
- transfer_to_human: Connect the user to a human agent if requested.
- mark_dnc: Mark the user as Do Not Contact if they request it.
- end_call: End the conversation when complete.

STRICT RULES:
- Always use search_company_knowledge before answering content-specific questions.
- Never claim to know something that was not returned by the tool.
- Always confirm email addresses before booking.
- You are AI. Never claim to be human.
"""


def build_settings(user_cfg: dict) -> dict:
    d = cfg.defaults
    voice_model = user_cfg.get("voice_model") or d["voice_model"]
    stt_model = user_cfg.get("stt_model") or d["stt_model"]
    llm_model = user_cfg.get("llm_model") or d["llm_model"]
    temperature = float(
        user_cfg.get("temperature")
        if user_cfg.get("temperature") is not None
        else d["temperature"]
    )
    greeting = (user_cfg.get("greeting") or "").strip() or d["greeting"]

    # ── New fields ────────────────────────────────────────────────────────────
    voice_speed = float(
        user_cfg.get("voice_speed")
        if user_cfg.get("voice_speed") is not None
        else d.get("voice_speed", 1.0)
    )

    # barge_in can come as bool True/False or string "true"/"false"
    _barge_raw = user_cfg.get("barge_in")
    if _barge_raw is None:
        _barge_raw = d.get("barge_in", True)
    barge_in = _barge_raw if isinstance(_barge_raw, bool) else str(_barge_raw).lower() != "false"

    endpointing = int(
        user_cfg.get("endpointing")
        if user_cfg.get("endpointing") is not None
        else d.get("endpointing", 800)
    )

    from datetime import datetime

    custom_prompt = (user_cfg.get("system_prompt") or "").strip()
    prompt = (
        custom_prompt
        if custom_prompt
        else _PROMPT_TEMPLATE.format(
            current_date=datetime.now().strftime("%A, %B %d, %Y"),
        )
    )

    return {
        "type": "Settings",
        "audio": cfg.audio_config,
        "agent": {
            "listen": {
                "provider": {
                    "type": "deepgram",
                    "model": stt_model,
                    "endpointing": endpointing,
                }
            },
            "think": {
                "provider": {
                    "type": "open_ai",
                    "model": llm_model,
                    "temperature": temperature,
                },
                "prompt": prompt,
                "functions": get_schemas(),
            },
            "speak": {
                "provider": {
                    "type": "deepgram",
                    "model": voice_model,
                }
            },
            "greeting": greeting,
        },
    }
