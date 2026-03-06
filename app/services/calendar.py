"""
app/services/calendar.py

Abstracted calendar service layer.
- BaseCalendarService: ABC defining the interface to ensure future flexibility.
- CalComService: Concrete implementation using the Cal.com API v2 via httpx.
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone

import httpx


# ── Constants ──────────────────────────────────────────────────────────────────
CAL_API_BASE_URL = os.environ.get("CAL_API_BASE_URL", "https://api.cal.com/v2")
CAL_API_KEY = os.environ.get("CAL_API_KEY", "")
CAL_EVENT_TYPE_ID = os.environ.get("CAL_EVENT_TYPE_ID", "")
DEFAULT_TIMEZONE = os.environ.get("DEFAULT_TIMEZONE", "Asia/Kolkata")

# Cal.com API v2 requires 30 minutes minimum slot duration.
SLOT_DURATION_MINUTES = 30

_HEADERS_SLOTS = {
    "cal-api-version": "2024-09-04",
    "Content-Type": "application/json",
}

_HEADERS_BOOKINGS = {
    "cal-api-version": "2024-08-13",
    "Content-Type": "application/json",
}


def _auth_headers(for_booking: bool = False) -> dict:
    h = _HEADERS_BOOKINGS if for_booking else _HEADERS_SLOTS
    return {**h, "Authorization": f"Bearer {CAL_API_KEY}"}


# ── Abstract Base ──────────────────────────────────────────────────────────────
class BaseCalendarService(ABC):
    """Abstract base class for calendar integrations. Swap implementations freely."""

    @abstractmethod
    async def get_available_slots(self, date: str, timezone: str) -> dict:
        """
        Fetch available time slots for a given day.

        Args:
            date: ISO 8601 date string (YYYY-MM-DD)
            timezone: IANA timezone string, e.g. "Asia/Kolkata"

        Returns:
            dict with keys: date, timezone, available_slots (list[str])
        """

    @abstractmethod
    async def create_booking(
        self,
        name: str,
        email: str,
        date: str,
        time: str,
        timezone: str,
    ) -> dict:
        """
        Create a calendar booking.

        Args:
            name: Attendee full name
            email: Attendee email address
            date: ISO 8601 date string (YYYY-MM-DD)
            time: Human readable time string (e.g. "3:00 PM")
            timezone: IANA timezone string

        Returns:
            dict with keys: success, uid, title, start, attendee_email, message
        """


# ── Cal.com Implementation ─────────────────────────────────────────────────────
class CalComService(BaseCalendarService):
    """
    Connects to the Cal.com REST API v2.
    All HTTP calls are async (non-blocking) using httpx.AsyncClient.
    """

    # ── Public Methods ────────────────────────────────────────────────────────

    async def get_available_slots(self, date: str, timezone: str = DEFAULT_TIMEZONE) -> dict:
        """Fetch open slots for `date` from Cal.com."""
        timezone = timezone or DEFAULT_TIMEZONE

        # Cal.com v2 /slots endpoint uses 'start' and 'end' (ISO 8601 strings)
        start = f"{date}T00:00:00Z"
        end = f"{date}T23:59:59Z"

        params = {
            "start": start,
            "end": end,
            "eventTypeId": CAL_EVENT_TYPE_ID,
            "timeZone": timezone,
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{CAL_API_BASE_URL}/slots",
                    params=params,
                    headers=_auth_headers(),
                )
            resp.raise_for_status()
            data = resp.json()

            # Cal.com v2 returns {status:'success', data: { 'YYYY-MM-DD': [{time:'...'}] }}
            # The data dict is keyed by date string
            slots_by_date: dict = data.get("data", {})
            raw_slots: list[dict] = slots_by_date.get(date, [])

            if not raw_slots:
                return {
                    "date": date,
                    "timezone": timezone,
                    "available_slots": [],
                    "message": (
                        f"No available slots found for {date}. "
                        "Please ask the user to try a different date."
                    ),
                }

            # Convert ISO datetime strings → human-readable '3:00 PM'
            # Cal.com v2 slot objects use 'start' key (not 'time')
            slots = [_iso_to_display_time(s.get("start") or s.get("time", ""), timezone) for s in raw_slots]

            return {
                "date": date,
                "timezone": timezone,
                "available_slots": slots,
            }

        except httpx.HTTPStatusError as exc:
            return _api_error("get_available_slots", exc.response.status_code, str(exc))
        except Exception as exc:
            return _api_error("get_available_slots", 500, str(exc))

    async def create_booking(
        self,
        name: str,
        email: str,
        date: str,
        time: str,
        timezone: str = DEFAULT_TIMEZONE,
    ) -> dict:
        """Submit a booking to Cal.com. Returns confirmation or error dict."""
        timezone = timezone or DEFAULT_TIMEZONE

        # Convert "3:00 PM" + "2024-03-07" + timezone → ISO 8601 for Cal.com
        try:
            start_iso = _to_iso(date, time, timezone)
        except Exception as exc:
            return {
                "success": False,
                "error": f"Could not parse date/time: {exc}",
                "message": (
                    "I had trouble understanding that date or time. "
                    "Could you repeat it more clearly?"
                ),
            }

        payload = {
            "eventTypeId": int(CAL_EVENT_TYPE_ID),
            "start": start_iso,
            "attendee": {
                "name": name,
                "email": email,
                "timeZone": timezone,
                "language": "en",
            },
            "metadata": {},
        }

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{CAL_API_BASE_URL}/bookings",
                    json=payload,
                    headers=_auth_headers(for_booking=True),
                )
            resp.raise_for_status()
            data = resp.json()

            booking = data.get("data", {})
            return {
                "success": True,
                "uid": booking.get("uid", ""),
                "title": booking.get("title", "Meeting"),
                "start": booking.get("start", start_iso),
                "attendee_email": email,
                "message": (
                    f"Booking confirmed! A calendar invite has been sent to {email}. "
                    f"Meeting ID: {booking.get('uid', 'N/A')}."
                ),
            }

        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            try:
                detail = exc.response.json().get("message", str(exc))
            except Exception:
                detail = str(exc)

            # Provide voice-friendly error messages for common cases
            if status == 409:
                return {
                    "success": False,
                    "error": "Slot no longer available",
                    "message": (
                        "That slot was just taken. Let me check for another available time — "
                        "could you pick a different slot?"
                    ),
                }
            if status == 422:
                return {
                    "success": False,
                    "error": f"Invalid data: {detail}",
                    "message": (
                        "There was a problem with the booking details, possibly the email address. "
                        "Could you spell out your email address for me to make sure I got it right?"
                    ),
                }
            return _api_error("create_booking", status, detail)

        except Exception as exc:
            return _api_error("create_booking", 500, str(exc))


# ── Internal Helpers ───────────────────────────────────────────────────────────

def _iso_to_display_time(iso_string: str, tz_name: str) -> str:
    """Convert an ISO 8601 datetime string to a display time like '9:00 AM'.
    Uses stdlib zoneinfo (Python 3.9+) — no external packages needed.
    """
    if not iso_string:
        return ""
    try:
        from zoneinfo import ZoneInfo
        iso_clean = iso_string.replace("Z", "+00:00")
        dt_utc = datetime.fromisoformat(iso_clean)
        dt_local = dt_utc.astimezone(ZoneInfo(tz_name))
        time_str = dt_local.strftime("%I:%M %p")  # "09:00 AM"
        return time_str.lstrip("0") or time_str  # "9:00 AM"
    except Exception:
        # Fallback: UTC HH:MM → 12-hour arithmetic (no external deps)
        try:
            hh = int(iso_string[11:13])
            mm = iso_string[14:16]
            ampm = "AM" if hh < 12 else "PM"
            hh12 = hh % 12 or 12
            return f"{hh12}:{mm} {ampm}"
        except Exception:
            return iso_string[11:16] if len(iso_string) >= 16 else iso_string


def _to_iso(date: str, time_str: str, tz_name: str) -> str:
    """
    Combine a YYYY-MM-DD date + '3:00 PM' time string + tz_name
    → ISO 8601 UTC string suitable for Cal.com.
    Uses stdlib zoneinfo — no external packages needed.
    """
    from zoneinfo import ZoneInfo

    # Normalize time_str: "3 PM" → "3:00 PM"
    time_str = time_str.strip().upper()
    if ":" not in time_str and ("AM" in time_str or "PM" in time_str):
        time_str = time_str.replace(" AM", ":00 AM").replace(" PM", ":00 PM")

    fmt = "%Y-%m-%d %I:%M %p" if ("AM" in time_str or "PM" in time_str) else "%Y-%m-%d %H:%M"
    local_dt = datetime.strptime(f"{date} {time_str}", fmt)

    aware_dt = local_dt.replace(tzinfo=ZoneInfo(tz_name))
    utc_dt = aware_dt.astimezone(timezone.utc)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")



def _api_error(source: str, status: int, detail: str) -> dict:
    """Construct a standardised error response dict."""
    msg = (
        f"I'm sorry, I ran into a technical issue while trying to {source.replace('_', ' ')} "
        f"(error {status}). Please try again or contact support if this continues."
    )
    return {"success": False, "error": detail, "message": msg}


# ── Singleton ──────────────────────────────────────────────────────────────────
# A single shared instance avoids re-creating the client on every call.
cal_service: BaseCalendarService = CalComService()
