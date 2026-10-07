"""Plain words for what the pages show. The stored values are the system's; these are the family's."""
import re
from collections.abc import Callable

from markupsafe import Markup

CHANNELS = {"telegram": "Telegram", "whatsapp": "WhatsApp", "imessage": "iMessage", "playground": "practice chat"}
STOCK = {"in_stock": "In stock", "low": "Low", "out": "Out", "unknown": "Not sure"}
REASONS = {"explicit": "", "finished": "ran out", "low": "running low", "predicted": "usually needed about now"}
EVENTS = {"added": "Added", "used": "Used", "low": "Running low", "finished": "Ran out", "restocked": "Bought",
          "adjusted": "Counted", "discarded": "Thrown away"}
SOURCES = {"message": "from chat", "receipt": "from a receipt", "photo": "from a photo", "prediction": "estimated",
           "shopping": "ticked off the list", "undo": "undone"}
SENDS = {"pending": "Waiting to send", "sending": "Sending", "sent": "Sent", "failed": "Not delivered",
         "cancelled": "Not sent", "simulated": "Practice only"}
PLACES = {"home": "Home", "store": "Shop", "school": "School", "clinic": "Clinic", "other": "Other"}

_EVERY = {"DAILY": ("day", "days"), "WEEKLY": ("week", "weeks"), "MONTHLY": ("month", "months"),
          "YEARLY": ("year", "years")}
_DAYS = {"MO": "Mon", "TU": "Tue", "WE": "Wed", "TH": "Thu", "FR": "Fri", "SA": "Sat", "SU": "Sun"}
_LINE = re.compile(r"([A-Z]+): (.*)")
_RULE = re.compile(r"\b(?:RRULE:)?FREQ=[A-Z0-9=;,+-]+")


def _worded(words: dict[str, str]) -> Callable[[str | None], str]:
    """A filter that gives a stored value its words, or the value itself with spaces if it has none."""
    return lambda value: words.get(value or "", (value or "").replace("_", " "))


def repeat_text(rrule: str | None) -> str:
    """How often a rule repeats: 'FREQ=WEEKLY;BYDAY=TU,TH' is 'every week on Tue, Thu'."""
    parts = dict(part.split("=", 1) for part in (rrule or "").upper().removeprefix("RRULE:").split(";") if "=" in part)
    one, many = _EVERY.get(parts.get("FREQ", ""), ("", ""))
    if not one:
        return "repeats"
    every = parts.get("INTERVAL", "1")
    text = f"every {one}" if every == "1" else f"every {every} {many}"
    days = [_DAYS[day] for day in parts.get("BYDAY", "").split(",") if day in _DAYS]
    return f"{text} on {', '.join(days)}" if days else text


def result_lines(result: str | None) -> list[tuple[str, str]]:
    """A logged result as (kind, words) pairs: 'OK: rice finished' is ('ok', 'rice finished'). A repeat
    rule in it is said in words."""
    lines = []
    for line in _RULE.sub(lambda rule: repeat_text(rule[0]), result or "").splitlines():
        found = _LINE.fullmatch(line)
        lines.append((found[1].lower(), found[2]) if found else ("", line))
    return [(kind, words) for kind, words in lines if words.strip()]


# Line icons, drawn on a 24 by 24 grid and coloured by the text around them.
ICONS = {
    "home": '<path d="M3.5 11 12 4l8.5 7"/><path d="M5.5 9.8V20h13V9.8"/><path d="M10 20v-5.5h4V20"/>',
    "cart": '<path d="M3 4.5h2.2l2.3 10h10.3L20 7.5H6.2"/><circle cx="9" cy="19" r="1.5"/>'
            '<circle cx="17" cy="19" r="1.5"/>',
    "fridge": '<rect x="6" y="3" width="12" height="18" rx="2.5"/><path d="M6 10h12M9.5 6v1.5M9.5 13v3"/>',
    "calendar": '<rect x="3.5" y="5" width="17" height="15.5" rx="2.5"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
    "more": '<g fill="currentColor" stroke="none"><circle cx="5" cy="12" r="1.7"/><circle cx="12" cy="12" r="1.7"/>'
            '<circle cx="19" cy="12" r="1.7"/></g>',
    "people": '<circle cx="9" cy="8" r="3.2"/><path d="M3 20c0-3.3 2.7-5.5 6-5.5s6 2.2 6 5.5"/>'
              '<circle cx="17.2" cy="9" r="2.4"/><path d="M17 14.6c2.4.3 4 2 4 4.9"/>',
    "chat": '<path d="M4.5 5h15A1.5 1.5 0 0 1 21 6.5v9a1.5 1.5 0 0 1-1.5 1.5H12l-4.5 3.5V17h-3A1.5 1.5 0 0 1 3 15.5v-9'
            'A1.5 1.5 0 0 1 4.5 5z"/>',
    "settings": '<path d="M4 7h9M19 7h1M4 17h1M11 17h9"/><circle cx="16" cy="7" r="2.5"/>'
                '<circle cx="8" cy="17" r="2.5"/>',
    "clock": '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    "spark": '<path d="M11 3.5l1.9 5.1 5.1 1.9-5.1 1.9L11 17.5l-1.9-5.1L4 10.5l5.1-1.9z"/>'
             '<path d="M18.5 15.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z"/>',
    "pulse": '<path d="M3 12h4l2.5-6 4 12 2.5-6h5"/>',
    "right": '<path d="M9.5 5.5 16 12l-6.5 6.5"/>',
    "left": '<path d="M14.5 5.5 8 12l6.5 6.5"/>',
    "down": '<path d="M5.5 9.5 12 16l6.5-6.5"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "copy": '<rect x="8.5" y="8.5" width="11.5" height="11.5" rx="2.5"/>'
            '<path d="M15.5 8.5V6.5A2.5 2.5 0 0 0 13 4H6.5A2.5 2.5 0 0 0 4 6.5V13a2.5 2.5 0 0 0 2.5 2.5h2"/>',
    "send": '<path d="M20.5 3.5 10.5 13.5"/><path d="M20.5 3.5 14 20.5l-3.5-7-7-3.5z"/>',
    "logout": '<path d="M10 4H6.5A2.5 2.5 0 0 0 4 6.5v11A2.5 2.5 0 0 0 6.5 20H10"/><path d="M15 8l4 4-4 4M19 12H9.5"/>',
    "bell": '<path d="M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 2h-15z"/><path d="M10 20.7a2.2 2.2 0 0 0 4 0"/>',
    "repeat": '<path d="M4 11V9.5A3.5 3.5 0 0 1 7.5 6H19l-3-3M20 13v1.5a3.5 3.5 0 0 1-3.5 3.5H5l3 3"/>',
    "undo": '<path d="M8.5 5 4.5 9l4 4"/><path d="M4.5 9h9.5a5.5 5.5 0 0 1 0 11h-3"/>',
    "alert": '<circle cx="12" cy="12" r="8.5"/><path d="M12 8v5M12 16.2v.1"/>',
}


def icon(name: str, also: str = "") -> Markup:
    """An icon's SVG, with a further class if one is named: `chev` for the small arrow at the end of a row."""
    return Markup(f'<svg class="{f"icon {also}".strip()}" viewBox="0 0 24 24" aria-hidden="true">{ICONS[name]}</svg>')


FILTERS = {"channel": _worded(CHANNELS), "stock": _worded(STOCK), "reason": _worded(REASONS),
           "happened": _worded(EVENTS), "source": _worded(SOURCES), "send": _worded(SENDS),
           "place": _worded(PLACES), "repeats": repeat_text, "lines": result_lines}
