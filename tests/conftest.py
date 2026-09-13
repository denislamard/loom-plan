# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from loom_plan.config import Config, parse_config

TZ = ZoneInfo("Europe/Paris")
CAL_ID = "denis@example.com"
LIST_ID = "LIST1"


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    raw: dict[str, object] = {
        "auth": {},
        "general": {
            "epoch": date(2026, 9, 13),
            "primary_calendar": CAL_ID,
            "default_task_list": "Ma liste",
        },
        "work": {"start": "09:00", "end": "18:00"},
    }
    return parse_config(raw, tmp_path / "loom-plan.toml")


@pytest.fixture
def now() -> datetime:
    # Tuesday 2026-09-15 10:30 local
    return datetime(2026, 9, 15, 10, 30, tzinfo=TZ)


def event(
    eid: str,
    summary: str,
    start: str,
    end: str,
    *,
    all_day: bool = False,
    status: str = "confirmed",
    series: str | None = None,
    description: str | None = None,
) -> dict[str, object]:
    key = "date" if all_day else "dateTime"
    payload: dict[str, object] = {
        "id": eid,
        "summary": summary,
        "status": status,
        "start": {key: start},
        "end": {key: end},
        "updated": "2026-09-10T08:00:00.000Z",
    }
    if series:
        payload["recurringEventId"] = series
    if description:
        payload["description"] = description
    return payload


def task(
    tid: str,
    title: str,
    *,
    due: str | None = None,
    status: str = "needsAction",
    notes: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": tid,
        "title": title,
        "status": status,
        "updated": "2026-09-10T08:00:00.000Z",
    }
    if due:
        payload["due"] = f"{due}T00:00:00.000Z"
    if notes:
        payload["notes"] = notes
    return payload
