from __future__ import annotations

from datetime import datetime, timedelta
import os
import re

# ── In-memory stores (replace with DB in production) ──────────────────────────
_appointments: list[dict] = []
_dnc_list: list[dict] = []


# ── Date parsing helper (stdlib only — no external deps) ──────────────────────
def _parse_date(date_str: str) -> str:
    """
    Convert natural language date strings into strict YYYY-MM-DD format.
    Uses only Python stdlib (datetime, timedelta) — no external packages needed.

    Handles:
    - Already-ISO strings:  "2026-03-07"         → "2026-03-07"
    - Relative:             "today", "tomorrow"   → actual date
    - Day names:            "Monday", "next Friday", "this Wednesday"
    - Month names:          "March 7", "7 March 2026"
    - Ordinals:             "7th March", "March 7th"
    """
    raw = date_str.strip()

    # 1. Already ISO 8601 — return as-is
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return raw

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    lower = raw.lower()

    # 2. Absolute relative keywords
    if lower in ("today", "now"):
        return today.strftime("%Y-%m-%d")
    if lower in ("tomorrow", "tmr", "tmrw"):
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    if lower in ("yesterday",):
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")

    # 3. Normalise ordinals: "7th" → "7", "3rd" → "3"
    normalised = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", lower, flags=re.I)

    # 4. Weekday names (e.g. "next Monday", "this Friday", "Monday")
    _WEEKDAYS = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }
    for day_name, day_idx in _WEEKDAYS.items():
        if day_name in normalised:
            current_weekday = today.weekday()
            days_ahead = day_idx - current_weekday
            # "next X" always means the X that is at least 7 days away
            if "next" in normalised:
                days_ahead = days_ahead % 7 or 7
                days_ahead += 7 if days_ahead <= 7 else 0
            else:
                if days_ahead <= 0:
                    days_ahead += 7
            return (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    # 5. Month name + day (e.g. "March 7", "7 March", "March 7 2026")
    _MONTHS = {
        "january": 1,
        "jan": 1,
        "february": 2,
        "feb": 2,
        "march": 3,
        "mar": 3,
        "april": 4,
        "apr": 4,
        "may": 5,
        "june": 6,
        "jun": 6,
        "july": 7,
        "jul": 7,
        "august": 8,
        "aug": 8,
        "september": 9,
        "sep": 9,
        "sept": 9,
        "october": 10,
        "oct": 10,
        "november": 11,
        "nov": 11,
        "december": 12,
        "dec": 12,
    }
    for month_name, month_num in _MONTHS.items():
        if month_name in normalised:
            # Extract digits that could be day or year
            numbers = re.findall(r"\d+", normalised)
            year = today.year
            day = None
            for n in numbers:
                n_int = int(n)
                if 1900 <= n_int <= 2100:
                    year = n_int
                elif 1 <= n_int <= 31:
                    day = n_int
            if day:
                try:
                    candidate = datetime(year, month_num, day)
                    # If the date has passed this year, assume next year
                    if candidate < today and year == today.year:
                        candidate = datetime(year + 1, month_num, day)
                    return candidate.strftime("%Y-%m-%d")
                except ValueError:
                    pass

    # 6. Pure numeric formats: DD/MM/YYYY, MM/DD/YYYY, DD-MM-YYYY
    for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d/%m/%y", "%m/%d/%y"):
        try:
            return datetime.strptime(normalised, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    # 7. Fallback — return the raw string and let the Cal.com API surface any error
    return raw


def _normalise_timezone(tz_str: str) -> str:
    """Map common spoken timezones to IANA equivalents."""
    if not tz_str:
        tz_str = os.environ.get("DEFAULT_TIMEZONE", "Asia/Kolkata")

    _MAP = {
        "IST": "Asia/Kolkata",
        "EST": "America/New_York",
        "CST": "America/Chicago",
        "MST": "America/Denver",
        "PST": "America/Los_Angeles",
        "GMT": "Europe/London",
        "UTC": "UTC",
        "BST": "Europe/London",
        "CET": "Europe/Paris",
        "EET": "Europe/Athens",
        "JST": "Asia/Tokyo",
        "AEST": "Australia/Sydney",
    }
    key = tz_str.strip().upper()
    return _MAP.get(key, tz_str)  # if not mapped, pass through (may be IANA already)


# ── Individual handlers ────────────────────────────────────────────────────────


async def _book_appointment(p: dict) -> dict:
    """Create a booking via Cal.com (async-safe for Gemini Live)."""
    from services.calendar import cal_service

    name = (p.get("name") or "").strip()
    email = (p.get("email") or "").strip()
    date_raw = (p.get("date") or "").strip()
    time_str = (p.get("time") or "").strip()
    tz_raw = p.get("timezone") or os.environ.get("DEFAULT_TIMEZONE", "Asia/Kolkata")

    date = _parse_date(date_raw)
    tz = _normalise_timezone(tz_raw)

    try:
        result = await cal_service.create_booking(
            name=name,
            email=email,
            date=date,
            time=time_str,
            timezone=tz,
        )
    except Exception as exc:
        result = {
            "success": False,
            "error": str(exc),
            "message": (
                "I encountered an unexpected error while booking the appointment. "
                "Please try again in a moment."
            ),
        }

    if result.get("success"):
        _appointments.append({**result, "name": name, "date": date, "time": time_str})

    return result


async def _check_availability(p: dict) -> dict:
    """Fetch available slots from Cal.com (async-safe for Gemini Live)."""
    from services.calendar import cal_service

    date_raw = (p.get("date") or "").strip()
    tz_raw = p.get("timezone") or os.environ.get("DEFAULT_TIMEZONE", "Asia/Kolkata")

    date = _parse_date(date_raw)
    tz = _normalise_timezone(tz_raw)

    try:
        return await cal_service.get_available_slots(date=date, timezone=tz)
    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
            "message": (
                "I had trouble fetching available slots. "
                "Please try again or provide a different date."
            ),
        }


async def _transfer_to_human(p: dict) -> dict:
    return {
        "status": "transferring",
        "message": "Connecting to a human specialist now. Estimated wait: 2-3 minutes.",
    }


async def _mark_dnc(p: dict) -> dict:
    _dnc_list.append(
        {"reason": p.get("reason"), "timestamp": datetime.now().isoformat()}
    )
    return {
        "status": "marked",
        "message": "Your number has been marked as Do Not Call.",
    }


async def _end_call(p: dict) -> dict:
    return {"status": "ending", "reason": p.get("reason", "completed")}


async def _search_company_knowledge(p: dict) -> dict:
    """RAG retrieval tool — searches the ingested document knowledge base."""
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
        from services.retrieval import retrieve_knowledge

        content = await retrieve_knowledge(query, document_id)
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
            "description": (
                "Book an appointment on the calendar once the user confirms their "
                "name, email, date, and time. ALWAYS confirm the email address by "
                "reading it back before calling this tool."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Attendee's full name"},
                    "email": {
                        "type": "string",
                        "description": "Attendee email address for the calendar invite",
                    },
                    "date": {
                        "type": "string",
                        "description": "Date in YYYY-MM-DD or natural language (e.g. 'next Friday')",
                    },
                    "time": {
                        "type": "string",
                        "description": "Time string, e.g. '3:00 PM' or '15:00'",
                    },
                    "timezone": {
                        "type": "string",
                        "description": "IANA or abbreviated timezone, e.g. 'IST', 'Asia/Kolkata'. Default: IST",
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
            "description": (
                "Check available appointment slots for a given date. "
                "Call this before offering or confirming any meeting time. "
                "Present only 2-3 of the returned slots to the user."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Date in YYYY-MM-DD or natural language (e.g. 'tomorrow', 'next Monday')",
                    },
                    "timezone": {
                        "type": "string",
                        "description": "IANA or abbreviated timezone, e.g. 'IST'. Default: IST",
                    },
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
    """Return function schemas formatted for Google GenAI/Gemini Tool."""
    declarations = []
    for entry in FUNCTION_REGISTRY.values():
        # Make a deep copy to avoid modifying the original registry continuously
        import copy

        sch = copy.deepcopy(entry["schema"])

        # Gemini often requires uppercase parameter types (e.g., 'OBJECT', 'STRING')
        if "parameters" in sch:
            sch["parameters"]["type"] = sch["parameters"].get("type", "object").upper()
            if "properties" in sch["parameters"]:
                for prop_name, prop_val in sch["parameters"]["properties"].items():
                    prop_val["type"] = prop_val.get("type", "string").upper()
        declarations.append(sch)

    return [{"function_declarations": declarations}]


async def dispatch(name: str, params: dict) -> dict:
    """Execute a function by name (async). Returns error dict if name is unknown."""
    entry = FUNCTION_REGISTRY.get(name)
    if entry is None:
        return {"error": f"Unknown function: {name}"}
    handler = entry["handler"]
    return await handler(params)
