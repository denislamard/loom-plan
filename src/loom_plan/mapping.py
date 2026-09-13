# SPDX-License-Identifier: Apache-2.0
"""Google payloads ↔ unified model (spec §3, §4, §7).

Rules implemented here:
- all datetimes come out in the configured timezone, never UTC;
- an all-day event's ``end.date`` is exclusive at Google: we expose the real last day;
- a task's ``due`` is a date; its fake ``T00:00:00.000Z`` time is ignored (never converted);
- status prefixes (``[fait]`` / ``[annulé]``) are stripped from titles and turned into
  ``status``; the reverse helpers put them back for writes;
- ``~30min`` / ``~1h30`` in notes gives ``estimate_min``.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from loom_plan.config import StatusPrefixes
from loom_plan.http import JsonObject
from loom_plan.model import Item, ItemId, ItemType, PlanError, Status

_ESTIMATE_RE = re.compile(r"~\s*(?:(\d+)\s*h\s*(\d{1,2})?|(\d+)\s*min)", re.IGNORECASE)
_BR_RE = re.compile(r"<\s*/?\s*(br|p|div|li)\b[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_RRULE_RE = re.compile(r"FREQ=(DAILY|WEEKLY|MONTHLY|YEARLY)(;[A-Z]+=[A-Z0-9,+-]+)*")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


# -- primitives ------------------------------------------------------------------------


def parse_datetime(raw: str, tz: ZoneInfo) -> datetime:
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def parse_due(raw: str) -> date:
    """Google Tasks ``due`` is ``YYYY-MM-DDT00:00:00.000Z``: the time part is meaningless."""
    return date.fromisoformat(raw[:10])


def format_due(d: date) -> str:
    return f"{d.isoformat()}T00:00:00.000Z"


@dataclass(frozen=True, slots=True)
class ParsedTitle:
    title: str
    status: Status | None  # status carried by a prefix, if any
    tracked: bool


def parse_title(title: str, prefixes: StatusPrefixes) -> ParsedTitle:
    """Strip every leading status/tracking prefix, in any order (``[fait] [suivi] x``)."""
    rest = title.strip()
    status: Status | None = None
    tracked = False
    markers = (
        (prefixes.done, Status.DONE),
        (prefixes.cancelled, Status.CANCELLED),
        (prefixes.tracked, None),
    )
    progress = True
    while progress:
        progress = False
        for prefix, marked in markers:
            if prefix and rest.lower().startswith(prefix.lower()):
                rest = rest[len(prefix) :].strip()
                if marked is None:
                    tracked = True
                elif status is None:
                    status = marked
                progress = True
    return ParsedTitle(rest, status, tracked)


def strip_prefix(title: str, prefixes: StatusPrefixes) -> tuple[str, Status | None]:
    parsed = parse_title(title, prefixes)
    return parsed.title, parsed.status


def with_prefix(
    title: str, status: Status, prefixes: StatusPrefixes, *, tracked: bool = False
) -> str:
    """Rebuild a Google title: status prefix first, then ``[suivi]`` if the item is tracked."""
    parsed = parse_title(title, prefixes)
    parts: list[str] = []
    if status is Status.DONE:
        parts.append(prefixes.done)
    elif status is Status.CANCELLED:
        parts.append(prefixes.cancelled)
    if tracked or parsed.tracked:
        parts.append(prefixes.tracked)
    parts.append(parsed.title)
    return " ".join(parts)


def estimate_minutes(notes: str | None) -> int | None:
    if not notes:
        return None
    m = _ESTIMATE_RE.search(notes)
    if m is None:
        return None
    hours, minutes, only_minutes = m.group(1), m.group(2), m.group(3)
    if only_minutes is not None:
        return int(only_minutes)
    return int(hours) * 60 + (int(minutes) if minutes else 0)


def clean_html(text: str) -> str:
    """Google Calendar descriptions are HTML: keep the text, turn breaks into newlines."""
    if "<" not in text and "&" not in text:
        return text
    text = _BR_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANK_LINES_RE.sub("\n\n", text).strip()


def truncate_notes(notes: str | None, max_chars: int) -> str | None:
    if not notes:
        return None
    notes = clean_html(notes).strip()
    if not notes:
        return None
    if len(notes) <= max_chars:
        return notes
    return notes[:max_chars].rstrip() + " […tronqué]"


# -- Calendar → Item -------------------------------------------------------------------


def event_to_item(
    payload: JsonObject,
    *,
    calendar_id: str,
    calendar_name: str,
    tz: ZoneInfo,
    prefixes: StatusPrefixes,
    notes_max_chars: int,
) -> Item:
    native_id = _s(payload, "id")
    parsed = parse_title(_s(payload, "summary", "(sans titre)"), prefixes)
    title, marked = parsed.title, parsed.status

    if _s(payload, "status") == "cancelled":
        status = Status.CANCELLED
    elif marked is not None:
        status = marked
    else:
        status = Status.OPEN

    start_raw = _table(payload, "start")
    end_raw = _table(payload, "end")
    all_day = "date" in start_raw
    if all_day:
        start = datetime.combine(date.fromisoformat(_s(start_raw, "date")), datetime.min.time(), tz)
        end_excl = date.fromisoformat(_s(end_raw, "date", _s(start_raw, "date")))
        end = datetime.combine(end_excl - timedelta(days=1), datetime.min.time(), tz)
        if end < start:
            end = start
    else:
        start = parse_datetime(_s(start_raw, "dateTime"), tz)
        end = parse_datetime(_s(end_raw, "dateTime", _s(start_raw, "dateTime")), tz)

    series = payload.get("recurringEventId")
    if not isinstance(series, str) and isinstance(payload.get("recurrence"), list):
        series = native_id  # the series master itself
    notes_raw = payload.get("description")
    notes_str = notes_raw if isinstance(notes_raw, str) else None
    updated_raw = payload.get("updated")

    return Item(
        id=ItemId(ItemType.EVENT, calendar_id, native_id),
        type=ItemType.EVENT,
        title=title,
        status=status,
        container=calendar_name,
        updated=parse_datetime(updated_raw, tz) if isinstance(updated_raw, str) else None,
        start=start,
        end=end,
        all_day=all_day,
        recurring=isinstance(series, str) and bool(series),
        series_id=series if isinstance(series, str) and series else None,
        tracked=parsed.tracked,
        location=_opt_s(payload, "location"),
        notes=truncate_notes(notes_str, notes_max_chars),
        estimate_min=estimate_minutes(notes_str),
    )


def event_body(
    *,
    title: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    all_day: bool | None = None,
    notes: str | None = None,
    location: str | None = None,
    recurrence: str | None = None,
    tz: ZoneInfo,
) -> JsonObject:
    """Build the fields of an ``events.insert``/``patch`` body from unified values.

    ``recurrence`` is an RFC 5545 rule, with or without the ``RRULE:`` prefix
    (``FREQ=WEEKLY;BYDAY=WE``).
    """
    body: JsonObject = {}
    if title is not None:
        body["summary"] = title
    if recurrence is not None:
        body["recurrence"] = [normalize_rrule(recurrence)]
    if notes is not None:
        body["description"] = notes
    if location is not None:
        body["location"] = location
    if start is not None and end is not None:
        if all_day:
            body["start"] = {"date": start.date().isoformat()}
            # inclusive last day → exclusive end for Google
            body["end"] = {"date": (end.date() + timedelta(days=1)).isoformat()}
        else:
            body["start"] = {"dateTime": _local(start, tz).isoformat(), "timeZone": tz.key}
            body["end"] = {"dateTime": _local(end, tz).isoformat(), "timeZone": tz.key}
    return body


def normalize_rrule(rule: str) -> str:
    text = rule.strip().upper()
    text = text.removeprefix("RRULE:")
    if not _RRULE_RE.fullmatch(text):
        raise PlanError(
            f"règle de récurrence invalide : {rule!r} (attendu par ex. FREQ=WEEKLY;BYDAY=WE, "
            "FREQ=MONTHLY;BYMONTHDAY=1, FREQ=DAILY;COUNT=10)"
        )
    return f"RRULE:{text}"


# -- Tasks → Item ----------------------------------------------------------------------


def task_to_item(
    payload: JsonObject,
    *,
    list_id: str,
    list_name: str,
    tz: ZoneInfo,
    prefixes: StatusPrefixes,
    notes_max_chars: int,
) -> Item:
    native_id = _s(payload, "id")
    title, marked = strip_prefix(_s(payload, "title", "(sans titre)"), prefixes)
    google_status = _s(payload, "status", "needsAction")
    if google_status == "completed":
        status = Status.CANCELLED if marked is Status.CANCELLED else Status.DONE
    else:
        status = Status.OPEN

    due_raw = payload.get("due")
    notes_raw = payload.get("notes")
    notes_str = notes_raw if isinstance(notes_raw, str) else None
    updated_raw = payload.get("updated")

    return Item(
        id=ItemId(ItemType.TASK, list_id, native_id),
        type=ItemType.TASK,
        title=title,
        status=status,
        container=list_name,
        updated=parse_datetime(updated_raw, tz) if isinstance(updated_raw, str) else None,
        due=parse_due(due_raw) if isinstance(due_raw, str) and due_raw else None,
        notes=truncate_notes(notes_str, notes_max_chars),
        estimate_min=estimate_minutes(notes_str),
    )


def task_body(
    *,
    title: str | None = None,
    due: date | None = None,
    clear_due: bool = False,
    notes: str | None = None,
) -> JsonObject:
    body: JsonObject = {}
    if title is not None:
        body["title"] = title
    if notes is not None:
        body["notes"] = notes
    if due is not None:
        body["due"] = format_due(due)
    elif clear_due:
        body["due"] = None
    return body


# -- helpers ---------------------------------------------------------------------------


def _local(dt: datetime, tz: ZoneInfo) -> datetime:
    return dt.astimezone(tz) if dt.tzinfo is not None else dt.replace(tzinfo=tz)


def _s(payload: JsonObject, key: str, default: str | None = None) -> str:
    value = payload.get(key)
    if isinstance(value, str):
        return value
    if default is not None:
        return default
    raise ValueError(f"champ Google manquant : {key!r}")


def _opt_s(payload: JsonObject, key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _table(payload: JsonObject, key: str) -> JsonObject:
    value = payload.get(key)
    return cast(JsonObject, value) if isinstance(value, dict) else {}
