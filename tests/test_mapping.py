# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from datetime import date, datetime

import pytest
from conftest import CAL_ID, LIST_ID, TZ, event, task

from loom_plan.config import Config
from loom_plan.mapping import (
    clean_html,
    estimate_minutes,
    event_body,
    event_to_item,
    normalize_rrule,
    parse_title,
    strip_prefix,
    task_to_item,
    truncate_notes,
    with_prefix,
)
from loom_plan.model import ItemId, ItemType, PlanError, Status


def _ev(cfg: Config, payload: dict[str, object]):
    return event_to_item(
        payload,
        calendar_id=CAL_ID,
        calendar_name="Perso",
        tz=cfg.timezone,
        prefixes=cfg.prefixes,
        notes_max_chars=cfg.notes_max_chars,
    )


def _tk(cfg: Config, payload: dict[str, object]):
    return task_to_item(
        payload,
        list_id=LIST_ID,
        list_name="Ma liste",
        tz=cfg.timezone,
        prefixes=cfg.prefixes,
        notes_max_chars=cfg.notes_max_chars,
    )


def test_timed_event_is_local_time(cfg: Config) -> None:
    item = _ev(cfg, event("e1", "Point Malt", "2026-09-14T07:30:00Z", "2026-09-14T08:15:00Z"))
    assert item.start == datetime(2026, 9, 14, 9, 30, tzinfo=TZ)
    assert item.end == datetime(2026, 9, 14, 10, 15, tzinfo=TZ)
    assert item.all_day is False
    assert item.status is Status.OPEN
    assert str(item.id) == f"evt_{CAL_ID}/e1"
    assert item.updated is not None and item.updated.tzinfo is TZ


def test_all_day_end_is_inclusive(cfg: Config) -> None:
    item = _ev(cfg, event("e2", "Salon", "2026-09-14", "2026-09-17", all_day=True))
    assert item.all_day
    assert item.start is not None and item.start.date() == date(2026, 9, 14)
    assert item.end is not None and item.end.date() == date(2026, 9, 16)


def test_single_all_day(cfg: Config) -> None:
    item = _ev(cfg, event("e3", "Férié", "2026-11-11", "2026-11-12", all_day=True))
    assert item.start is not None and item.end is not None
    assert item.start.date() == item.end.date() == date(2026, 11, 11)


def test_done_prefix_stripped(cfg: Config) -> None:
    item = _ev(
        cfg,
        event("e4", "[fait] Démo agent", "2026-09-10T14:00:00+02:00", "2026-09-10T15:00:00+02:00"),
    )
    assert item.title == "Démo agent"
    assert item.status is Status.DONE


def test_cancelled_event_native(cfg: Config) -> None:
    item = _ev(
        cfg,
        event(
            "e5",
            "Annulé par Google",
            "2026-09-10T14:00:00+02:00",
            "2026-09-10T15:00:00+02:00",
            status="cancelled",
        ),
    )
    assert item.status is Status.CANCELLED


def test_recurring_instance(cfg: Config) -> None:
    item = _ev(
        cfg,
        event(
            "m1_20260914T070000Z",
            "Stand-up",
            "2026-09-14T09:00:00+02:00",
            "2026-09-14T09:15:00+02:00",
            series="m1",
        ),
    )
    assert item.recurring and item.series_id == "m1"


def test_task_due_is_a_pure_date(cfg: Config) -> None:
    item = _tk(cfg, task("t1", "Répondre Malt", due="2026-09-14", notes="urgent ~30min"))
    assert item.due == date(2026, 9, 14)  # never shifted by the timezone
    assert item.estimate_min == 30
    assert item.status is Status.OPEN
    assert str(item.id) == f"task_{LIST_ID}/t1"


def test_task_completed_and_cancelled(cfg: Config) -> None:
    done = _tk(cfg, task("t2", "Payer", status="completed"))
    cancelled = _tk(cfg, task("t3", "[annulé] Payer", status="completed"))
    assert done.status is Status.DONE and done.title == "Payer"
    assert cancelled.status is Status.CANCELLED and cancelled.title == "Payer"


def test_undated_task(cfg: Config) -> None:
    item = _tk(cfg, task("t4", "Lire la doc"))
    assert item.due is None


def test_prefix_helpers(cfg: Config) -> None:
    p = cfg.prefixes
    assert strip_prefix("[FAIT]  x", p) == ("x", Status.DONE)
    assert strip_prefix("x", p) == ("x", None)
    assert with_prefix("[fait] x", Status.CANCELLED, p) == "[annulé] x"
    assert with_prefix("[annulé] x", Status.OPEN, p) == "x"


def test_estimate_parsing() -> None:
    assert estimate_minutes("~30min") == 30
    assert estimate_minutes("prévoir ~ 45 min") == 45
    assert estimate_minutes("~1h30") == 90
    assert estimate_minutes("~2h") == 120
    assert estimate_minutes("rien") is None
    assert estimate_minutes(None) is None


def test_truncate_notes() -> None:
    assert truncate_notes("a" * 10, 500) == "a" * 10
    out = truncate_notes("b" * 600, 500)
    assert out is not None and out.endswith("[…tronqué]") and len(out) < 600
    assert truncate_notes("", 500) is None


def test_event_body_all_day_exclusive_end() -> None:
    body = event_body(
        title="Salon",
        start=datetime(2026, 9, 14, tzinfo=TZ),
        end=datetime(2026, 9, 16, tzinfo=TZ),
        all_day=True,
        tz=TZ,
    )
    assert body["start"] == {"date": "2026-09-14"}
    assert body["end"] == {"date": "2026-09-17"}


def test_event_body_timed_local() -> None:
    body = event_body(
        start=datetime(2026, 9, 14, 9, 30, tzinfo=TZ),
        end=datetime(2026, 9, 14, 10, 0, tzinfo=TZ),
        all_day=False,
        tz=TZ,
    )
    assert body["start"] == {"dateTime": "2026-09-14T09:30:00+02:00", "timeZone": "Europe/Paris"}


def test_item_id_roundtrip() -> None:
    iid = ItemId.parse("evt_denis@example.com/abc_20260914T070000Z")
    assert iid.type is ItemType.EVENT
    assert iid.container_id == "denis@example.com"
    assert iid.native_id == "abc_20260914T070000Z"
    assert str(iid) == "evt_denis@example.com/abc_20260914T070000Z"


def test_clean_html() -> None:
    raw = '<a target="_blank">06 15 69 27 78</a><br>Code &amp; badge<p>Suite</p>'
    assert clean_html(raw) == "06 15 69 27 78\nCode & badge\nSuite"
    assert clean_html("Tel. : 03 80\n06 59") == "Tel. : 03 80\n06 59"
    assert truncate_notes("<b></b>", 500) is None


def test_tracked_series_prefix(cfg: Config) -> None:
    p = cfg.prefixes
    parsed = parse_title("[suivi] Relancer les devis", p)
    assert parsed.tracked and parsed.status is None and parsed.title == "Relancer les devis"
    parsed = parse_title("[fait] [suivi] Relancer les devis", p)
    assert parsed.tracked and parsed.status is Status.DONE and parsed.title == "Relancer les devis"
    parsed = parse_title("[SUIVI] [Fait] x", p)
    assert parsed.tracked and parsed.status is Status.DONE and parsed.title == "x"
    # rebuilding keeps [suivi] whatever the status, and never duplicates it
    assert with_prefix("[suivi] x", Status.DONE, p) == "[fait] [suivi] x"
    assert with_prefix("[fait] [suivi] x", Status.OPEN, p) == "[suivi] x"
    assert with_prefix("x", Status.DONE, p, tracked=True) == "[fait] [suivi] x"
    assert with_prefix("x", Status.DONE, p) == "[fait] x"
    item = _ev(
        cfg,
        event(
            "m_1",
            "[suivi] Relancer",
            "2026-09-11T17:00:00+02:00",
            "2026-09-11T17:30:00+02:00",
            series="m",
        ),
    )
    assert item.tracked and item.recurring and item.status is Status.OPEN
    assert item.title == "Relancer"


def test_rrule_normalized_and_validated() -> None:
    assert normalize_rrule("FREQ=WEEKLY;BYDAY=WE") == "RRULE:FREQ=WEEKLY;BYDAY=WE"
    assert normalize_rrule("rrule:freq=monthly;bymonthday=1") == "RRULE:FREQ=MONTHLY;BYMONTHDAY=1"
    with pytest.raises(PlanError):
        normalize_rrule("tous les mercredis")
    body = event_body(
        title="x",
        start=datetime(2026, 9, 16, 9, tzinfo=TZ),
        end=datetime(2026, 9, 16, 9, 30, tzinfo=TZ),
        all_day=False,
        recurrence="FREQ=WEEKLY;BYDAY=WE",
        tz=TZ,
    )
    assert body["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=WE"]


def test_series_master_is_recurring(cfg: Config) -> None:
    payload = event("m", "[suivi] Malt", "2026-09-16T09:00:00+02:00", "2026-09-16T09:30:00+02:00")
    payload["recurrence"] = ["RRULE:FREQ=WEEKLY;BYDAY=WE"]
    item = _ev(cfg, payload)
    assert item.recurring and item.series_id == "m" and item.tracked
