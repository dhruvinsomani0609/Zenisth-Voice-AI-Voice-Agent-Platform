from app.config import cfg
from app.agent.functions import get_schemas

_PROMPT_TEMPLATE = """\
You are a helpful AI Voice Assistant. Today is {current_date}.
You are NOT human. You help users by answering questions based on the knowledge document they have uploaded.

HOW TO ANSWER QUESTIONS:
- When the user asks ANY question about a topic, product, company, process, or document content,
  ALWAYS call the `search_company_knowledge` function first to find the answer.
- Base your spoken answer ONLY on what the function returns. Do not guess or hallucinate.
- If the function returns no useful information, say: "I don't have that information in the current document."

VOICE & TONE:
- Short, clear sentences. Conversational and friendly.
- Acknowledge the question before answering.
- Never read out long lists. Summarize key points naturally.

TOOLS AVAILABLE:
- search_company_knowledge: Search the uploaded knowledge document for relevant information.
- book_appointment: Schedule a meeting or demo if the user requests it.
- check_availability: Check available time slots for scheduling.
- transfer_to_human: Connect the user to a human agent if requested.
- mark_dnc: Mark the user as Do Not Contact if they request it.
- end_call: End the conversation when complete.

STRICT RULES:
- Always use search_company_knowledge before answering content-specific questions.
- Never claim to know something that was not returned by the tool.
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
            "listen": {"provider": {"type": "deepgram", "model": stt_model}},
            "think": {
                "provider": {
                    "type": "open_ai",
                    "model": llm_model,
                    "temperature": temperature,
                },
                "prompt": prompt,
                "functions": get_schemas(),
            },
            "speak": {"provider": {"type": "deepgram", "model": voice_model}},
            "greeting": greeting,
        },
    }
