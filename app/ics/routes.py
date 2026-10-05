"""Read-only calendar feed (spec section 14), for subscribing from Apple or Google Calendar.

One VEVENT per active event, with its RRULE and exception dates passed through. The token
is per household and only its hash is stored.
"""
from typing import Any

from fastapi import APIRouter, HTTPException, Response

from app.core.timeutil import local
from app.db import tx
from app.services import calendar, households

router = APIRouter()
CACHE_SECONDS = 15 * 60
UTC_STAMP = "%Y%m%dT%H%M%SZ"
LOCAL_STAMP = "%Y%m%dT%H%M%S"


def _escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _fold(line: str) -> str:
    """Content lines are at most 75 octets; longer ones continue after CRLF and a space (RFC 5545 3.1)."""
    raw = line.encode()
    parts: list[str] = []
    while raw:
        cut = min(len(raw), 75 if not parts else 74)
        while cut < len(raw) and raw[cut] & 0xC0 == 0x80:   # never split inside a UTF-8 character
            cut -= 1
        parts.append(raw[:cut].decode())
        raw = raw[cut:]
    return "\r\n ".join(parts)


def render(household: dict[str, Any], events: list[dict[str, Any]]) -> str:
    timezone = household["timezone"]
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//household-agent//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(household['name'])}", f"X-WR-TIMEZONE:{timezone}",
    ]
    for event in events:
        begins = local(event["starts_at"], timezone)
        lines += [
            "BEGIN:VEVENT", f"UID:{event['id']}@household-agent", f"DTSTAMP:{event['created_at']:{UTC_STAMP}}",
            f"DTSTART;TZID={timezone}:{begins:{LOCAL_STAMP}}",
        ]
        if event["ends_at"]:
            lines.append(f"DTEND;TZID={timezone}:{local(event['ends_at'], timezone):{LOCAL_STAMP}}")
        if event["rrule"]:
            lines.append(f"RRULE:{event['rrule']}")
        if event["exdates"]:
            # An exception date names the occurrence by its start: that date at the series' local time.
            skipped = ",".join(f"{day:%Y%m%d}T{begins:%H%M%S}" for day in sorted(event["exdates"]))
            lines.append(f"EXDATE;TZID={timezone}:{skipped}")
        lines.append(f"SUMMARY:{_escape(calendar.label(event))}")
        if event["location"]:
            lines.append(f"LOCATION:{_escape(event['location'])}")
        if event["notes"]:
            lines.append(f"DESCRIPTION:{_escape(event['notes'])}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


@router.get("/ics/{token}.ics")
async def feed(token: str) -> Response:
    async with tx() as conn:
        household = await households.for_calendar_token(conn, token)
        if household is None:
            raise HTTPException(status_code=404)
        events = await calendar.active_events(conn, household["id"])
    return Response(render(household, events), media_type="text/calendar",
                    headers={"Cache-Control": f"private, max-age={CACHE_SECONDS}"})
