from datetime import datetime
import asyncio
import os
import nest_asyncio

nest_asyncio.apply()

# ── In-memory stores (replace with DB in production) ──────────────────────────
_appointments: list[dict] = []
_dnc_list: list[dict] = []


# ── Individual handlers ────────────────────────────────────────────────────────
def _book_appointment(p: dict) -> dict:
    appt = {
        **p,
        "appointment_id": f"DEMO{len(_appointments) + 1:04d}",
        "booked_at": datetime.now().isoformat(),
        "status": "confirmed",
        "timezone": p.get("timezone", "IST"),
    }
    _appointments.append(appt)
    return {
        "success": True,
        "appointment_id": appt["appointment_id"],
        "message": (
            f"Demo confirmed for {p.get('date')} at {p.get('time')} "
            f"{p.get('timezone', 'IST')}. Invite sent to {p.get('email')}."
        ),
    }


def _check_availability(p: dict) -> dict:
    return {
        "date": p.get("date"),
        "timezone": "IST",
        "available_slots": [
            "10:00 AM",
            "11:00 AM",
            "12:00 PM",
            "2:00 PM",
            "3:00 PM",
            "4:00 PM",
        ],
    }


def _transfer_to_human(p: dict) -> dict:
    return {
        "status": "transferring",
        "message": "Connecting to a human specialist now. Estimated wait: 2-3 minutes.",
    }


def _mark_dnc(p: dict) -> dict:
    _dnc_list.append(
        {"reason": p.get("reason"), "timestamp": datetime.now().isoformat()}
    )
    return {
        "status": "marked",
        "message": "Your number has been marked as Do Not Call.",
    }


def _end_call(p: dict) -> dict:
    return {"status": "ending", "reason": p.get("reason", "completed")}


def _search_company_knowledge(p: dict) -> dict:
    """
    RAG retrieval tool — searches the ingested document knowledge base.
    Uses asyncio.run() to execute the async retrieval synchronously so
    it works within Deepgram's sync FunctionCallRequest dispatch.
    """
    query = p.get("query", "")
    document_id = p.get("_document_id") or os.environ.get(
        "RAG_DOCUMENT_ID", "zenisth_prd"
    )

    print(
        f"[functions] search_company_knowledge called | doc_id='{document_id}' | query='{query[:80]}'"
    )
    if not query:
        return {"result": "No query provided."}

    try:
        from app.services.retrieval import retrieve_knowledge

        # Using loop.run_until_complete with nest_asyncio is safer inside existing loops
        loop = asyncio.get_event_loop()
        content = loop.run_until_complete(retrieve_knowledge(query, document_id))
        return {"result": content}
    except Exception as exc:
        print(f"[functions] RAG Error: {exc}")
        return {"result": f"Knowledge base unavailable: {str(exc)}"}


# ── Registry: name → {schema, handler} ────────────────────────────────────────
# To add a new function: add one entry here. Nothing else changes.
FUNCTION_REGISTRY: dict[str, dict] = {
    "book_appointment": {
        "schema": {
            "name": "book_appointment",
            "description": "Book a 15-minute demo when the prospect agrees.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Prospect's full name"},
                    "email": {
                        "type": "string",
                        "description": "Email for calendar invite",
                    },
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "time": {"type": "string", "description": "e.g. '3:00 PM'"},
                    "timezone": {
                        "type": "string",
                        "description": "e.g. 'IST'. Default: IST",
                    },
                },
                "required": ["name", "email", "date", "time"],
            },
        },
        "handler": _book_appointment,
    },
    "check_availability": {
        "schema": {
            "name": "check_availability",
            "description": "Check available demo slots before offering specific times.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                },
                "required": ["date"],
            },
        },
        "handler": _check_availability,
    },
    "transfer_to_human": {
        "schema": {
            "name": "transfer_to_human",
            "description": "Transfer to a human agent when user asks or for complex questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Reason for transfer"},
                },
                "required": ["reason"],
            },
        },
        "handler": _transfer_to_human,
    },
    "mark_dnc": {
        "schema": {
            "name": "mark_dnc",
            "description": "Mark contact as Do Not Call when user requests removal.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                },
                "required": ["reason"],
            },
        },
        "handler": _mark_dnc,
    },
    "end_call": {
        "schema": {
            "name": "end_call",
            "description": "End the call politely when conversation is complete.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "enum": ["completed", "dnc", "transferred", "no_interest"],
                    },
                },
                "required": ["reason"],
            },
        },
        "handler": _end_call,
    },
    "search_company_knowledge": {
        "schema": {
            "name": "search_company_knowledge",
            "description": (
                "Search the Zenisth company knowledge base to answer user questions about "
                "products, pricing, services, features, compliance, deployment, or any "
                "company-specific information not already in your prompt. Always call this "
                "before saying you don't know something about Zenisth."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The user's question to look up in the knowledge base.",
                    }
                },
                "required": ["query"],
            },
        },
        "handler": _search_company_knowledge,
    },
}


def get_schemas() -> list[dict]:
    """Return function schemas for Deepgram Settings."""
    return [entry["schema"] for entry in FUNCTION_REGISTRY.values()]


def dispatch(name: str, params: dict) -> dict:
    """Execute a function by name. Returns error dict if name is unknown."""
    entry = FUNCTION_REGISTRY.get(name)
    if entry is None:
        return {"error": f"Unknown function: {name}"}
    return entry["handler"](params)
