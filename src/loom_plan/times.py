# SPDX-License-Identifier: Apache-2.0
"""Local-time helpers: parsing what Claude/the CLI send, formatting what they get back."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from loom_plan.model import PlanError

_DAYS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def parse_datetime_input(raw: str, tz: ZoneInfo) -> datetime:
    """Accept ``2026-09-14 09:30``, ``2026-09-14T09:30``, ISO with offset, or a bare date
    (→ midnight). Naive inputs are local (spec §7)."""
    text = raw.strip()
    try:
        if len(text) == 10:
            return datetime.combine(date.fromisoformat(text), time.min, tz)
        dt = datetime.fromisoformat(text.replace(" ", "T", 1))
    except ValueError as exc:
        raise PlanError(
            f"date/heure invalide : {raw!r} (attendu « AAAA-MM-JJ HH:MM » ou « AAAA-MM-JJ »)"
        ) from exc
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)


def parse_date_input(raw: str) -> date:
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise PlanError(f"date invalide : {raw!r} (attendu « AAAA-MM-JJ »)") from exc


def parse_time_input(raw: str) -> time:
    try:
        return time.fromisoformat(raw.strip())
    except ValueError as exc:
        raise PlanError(f"heure invalide : {raw!r} (attendu « HH:MM »)") from exc


def day_start(d: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, time.min, tz)


def next_midnight(dt: datetime) -> datetime:
    return day_start(dt.date() + timedelta(days=1), _tz(dt))


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def fmt_time(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def fmt_date(d: date) -> str:
    return d.isoformat()


def fmt_day(d: date) -> str:
    return _DAYS_FR[d.weekday()]


def fmt_minutes(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    h, m = divmod(minutes, 60)
    return f"{h} h" if m == 0 else f"{h} h {m:02d}"


def _tz(dt: datetime) -> ZoneInfo:
    if isinstance(dt.tzinfo, ZoneInfo):
        return dt.tzinfo
    raise PlanError("datetime sans fuseau ZoneInfo (bug interne)")
