from datetime import datetime, timedelta
import asyncio
import os
import re
import nest_asyncio

nest_asyncio.apply()

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
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
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
        "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
        "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6,
        "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
        "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
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

def _book_appointment(p: dict) -> dict:
    """
    Creates a real booking via Cal.com.
    Runs the async call synchronously within Deepgram's sync dispatch.
    """
    from app.services.calendar import cal_service

    name = p.get("name", "")
    email = p.get("email", "")
    date_raw = p.get("date", "")
    time_str = p.get("time", "")
    tz_raw = p.get("timezone", os.environ.get("DEFAULT_TIMEZONE", "Asia/Kolkata"))

    date = _parse_date(date_raw)
    tz = _normalise_timezone(tz_raw)

    print(
        f"[functions] book_appointment | name={name!r} email={email!r} "
        f"date={date!r} time={time_str!r} tz={tz!r}"
    )

    try:
        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(
            cal_service.create_booking(
                name=name,
                email=email,
                date=date,
                time=time_str,
                timezone=tz,
            )
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


def _check_availability(p: dict) -> dict:
    """
    Checks real available slots from Cal.com for a given date.
    Date parsing via dateparser handles sloppy LLM input ("next Tuesday", "tomorrow").
    """
    from app.services.calendar import cal_service

    date_raw = p.get("date", "")
    tz_raw = p.get("timezone", os.environ.get("DEFAULT_TIMEZONE", "Asia/Kolkata"))

    date = _parse_date(date_raw)
    tz = _normalise_timezone(tz_raw)

    print(f"[functions] check_availability | date={date!r} tz={tz!r}")

    try:
        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(
            cal_service.get_available_slots(date=date, timezone=tz)
        )
    except Exception as exc:
        result = {
            "success": False,
            "error": str(exc),
            "message": (
                "I had trouble fetching available slots. "
                "Please try again or provide a different date."
            ),
        }

    return result


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
    """Return function schemas for Deepgram Settings."""
    return [entry["schema"] for entry in FUNCTION_REGISTRY.values()]


def dispatch(name: str, params: dict) -> dict:
    """Execute a function by name. Returns error dict if name is unknown."""
    entry = FUNCTION_REGISTRY.get(name)
    if entry is None:
        return {"error": f"Unknown function: {name}"}
    return entry["handler"](params)
