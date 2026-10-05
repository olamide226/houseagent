"""Household time. Timestamps are UTC everywhere; conversion happens only at the edges."""
from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(UTC)


def local(moment: datetime, timezone: str) -> datetime:
    return moment.astimezone(ZoneInfo(timezone))
