# SPDX-License-Identifier: Apache-2.0
"""Service tests with the Google APIs mocked at the HTTP level (respx)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from urllib.parse import quote

import httpx
import pytest
import respx
from conftest import CAL_ID, LIST_ID, TZ, event, task

from loom_plan.config import Config
from loom_plan.http import CALENDAR_BASE, TASKS_BASE, GoogleClient
from loom_plan.model import PlanError, Status
from loom_plan.service import PlanService

EVENTS_URL = f"{CALENDAR_BASE}/calendars/{quote(CAL_ID, safe='')}/events"
TASKS_URL = f"{TASKS_BASE}/lists/{LIST_ID}/tasks"


class _FakeTokens:
    token_path = None

    def access_token(self) -> str:
        return "tok"

    def invalidate(self) -> None:
        pass


@pytest.fixture
async def svc(cfg: Config, now: datetime) -> AsyncIterator[PlanService]:
    client = GoogleClient(_FakeTokens())  # pyright: ignore[reportArgumentType]
    yield PlanService(cfg, client, clock=lambda: now)
    await client.aclose()


def _mock_containers(router: respx.MockRouter) -> None:
    router.get(f"{CALENDAR_BASE}/users/me/calendarList").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {"id": CAL_ID, "summary": "Perso", "primary": True, "accessRole": "owner"},
                    {"id": "holidays", "summary": "Fériés", "accessRole": "reader"},
                ]
            },
        )
    )
    router.get(f"{TASKS_BASE}/users/@me/lists").mock(
        return_value=httpx.Response(200, json={"items": [{"id": LIST_ID, "title": "Ma liste"}]})
    )


async def test_overdue_respects_epoch_and_recurrence(
    svc: PlanService, respx_mock: respx.MockRouter
) -> None:
    _mock_containers(respx_mock)
    respx_mock.get(EVENTS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    event(
                        "old",
                        "Avant epoch",
                        "2026-09-12T10:00:00+02:00",
                        "2026-09-12T11:00:00+02:00",
                    ),
                    event(
                        "late",
                        "Démo agent",
                        "2026-09-14T14:00:00+02:00",
                        "2026-09-14T15:00:00+02:00",
                    ),
                    event(
                        "done",
                        "[fait] Point",
                        "2026-09-14T16:00:00+02:00",
                        "2026-09-14T17:00:00+02:00",
                    ),
                    event(
                        "m_1",
                        "Stand-up",
                        "2026-09-14T09:00:00+02:00",
                        "2026-09-14T09:15:00+02:00",
                        series="m",
                    ),
                    event(
                        "r_1",
                        "[suivi] Relancer devis",
                        "2026-09-14T17:00:00+02:00",
                        "2026-09-14T17:30:00+02:00",
                        series="r",
                    ),
                    event(
                        "r_2",
                        "[fait] [suivi] Relancer devis",
                        "2026-09-07T17:00:00+02:00",
                        "2026-09-07T17:30:00+02:00",
                        series="r",
                    ),
                    event(
                        "running",
                        "En cours",
                        "2026-09-15T10:00:00+02:00",
                        "2026-09-15T11:00:00+02:00",
                    ),
                ]
            },
        )
    )
    respx_mock.get(TASKS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    task("t_old", "Tâche ancienne", due="2026-09-01"),
                    task("t_late", "Relance devis", due="2026-09-14"),
                    task("t_today", "Aujourd'hui", due="2026-09-15"),
                    task("t_none", "Sans date"),
                ]
            },
        )
    )
    items = await svc.overdue()
    # a task due on the 14th (midnight) sorts ahead of that day's events
    # r_1 is a tracked occurrence (always in), r_2 is done, m_1 an ordinary recurrence (out)
    assert [i.title for i in items] == ["Relance devis", "Démo agent", "Relancer devis"]
    assert items[-1].tracked
    with_rec = await svc.overdue(include_recurring=True)
    assert [i.title for i in with_rec] == [
        "Relance devis",
        "Stand-up",
        "Démo agent",
        "Relancer devis",
    ]
    # The reader-only holidays calendar is never queried.
    assert not any("holidays" in str(call.request.url) for call in respx_mock.calls)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType, reportUnknownVariableType]


async def test_next_window_and_free_slots(
    svc: PlanService, now: datetime, respx_mock: respx.MockRouter
) -> None:
    _mock_containers(respx_mock)
    respx_mock.get(EVENTS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    event("a", "Réunion", "2026-09-15T11:00:00+02:00", "2026-09-15T12:00:00+02:00"),
                    event("b", "Salon", "2026-09-15", "2026-09-16", all_day=True),
                    event("c", "Café", "2026-09-15T15:00:00+02:00", "2026-09-15T15:20:00+02:00"),
                ]
            },
        )
    )
    respx_mock.get(TASKS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    task("t1", "Long", due="2026-09-16", notes="~2h"),
                    task("t2", "Court", due="2026-09-16", notes="~15min"),
                    task("t3", "Sans date"),
                ]
            },
        )
    )
    view = await svc.next()
    assert view.window_end == datetime(2026, 9, 15, 18, 0, tzinfo=TZ)
    assert [e.title for e in view.events] == ["Salon", "Réunion", "Café"]
    slots = [(s.start.strftime("%H:%M"), s.end.strftime("%H:%M")) for s in view.free_slots]
    assert slots == [("10:30", "11:00"), ("12:00", "15:00"), ("15:20", "18:00")]
    assert [t.title for t in view.tasks] == ["Court", "Long", "Sans date"]


async def test_next_after_work_hours_goes_to_midnight(cfg: Config) -> None:
    late = datetime(2026, 9, 15, 19, 0, tzinfo=TZ)
    client = GoogleClient(_FakeTokens())  # pyright: ignore[reportArgumentType]
    with respx.mock(assert_all_called=False) as router:
        _mock_containers(router)
        router.get(EVENTS_URL).mock(return_value=httpx.Response(200, json={"items": []}))
        router.get(TASKS_URL).mock(return_value=httpx.Response(200, json={"items": []}))
        view = await PlanService(cfg, client, clock=lambda: late).next()
    await client.aclose()
    assert view.window_end == datetime(2026, 9, 16, 0, 0, tzinfo=TZ)


async def test_free_slots_skip_weekend(svc: PlanService, respx_mock: respx.MockRouter) -> None:
    _mock_containers(respx_mock)
    respx_mock.get(EVENTS_URL).mock(return_value=httpx.Response(200, json={"items": []}))
    sat = datetime(2026, 9, 19, 9, 0, tzinfo=TZ)
    slots = await svc.free_slots(sat, sat + timedelta(days=2))  # sat + sun
    assert slots == []
    mon = datetime(2026, 9, 21, 9, 0, tzinfo=TZ)
    slots = await svc.free_slots(sat, mon + timedelta(hours=2))
    assert len(slots) == 1 and slots[0].minutes == 120


async def test_agenda_window_limit(
    svc: PlanService, now: datetime, respx_mock: respx.MockRouter
) -> None:
    _mock_containers(respx_mock)
    with pytest.raises(PlanError, match="31"):
        await svc.agenda(now, now + timedelta(days=40))


async def test_set_status_on_event_rewrites_title(
    svc: PlanService, respx_mock: respx.MockRouter
) -> None:
    _mock_containers(respx_mock)
    respx_mock.get(f"{EVENTS_URL}/e1").mock(
        return_value=httpx.Response(
            200, json=event("e1", "Démo", "2026-09-14T14:00:00+02:00", "2026-09-14T15:00:00+02:00")
        )
    )
    patch = respx_mock.patch(f"{EVENTS_URL}/e1").mock(
        return_value=httpx.Response(
            200,
            json=event(
                "e1", "[fait] Démo", "2026-09-14T14:00:00+02:00", "2026-09-14T15:00:00+02:00"
            ),
        )
    )
    item = await svc.set_status(f"evt_{CAL_ID}/e1", Status.DONE)
    assert item.status is Status.DONE and item.title == "Démo"
    assert json.loads(patch.calls.last.request.content)["summary"] == "[fait] Démo"

    # a tracked occurrence keeps its [suivi] marker when marked done
    respx_mock.get(f"{EVENTS_URL}/r_1").mock(
        return_value=httpx.Response(
            200,
            json=event(
                "r_1",
                "[suivi] Relancer",
                "2026-09-14T17:00:00+02:00",
                "2026-09-14T17:30:00+02:00",
                series="r",
            ),
        )
    )
    patch_r = respx_mock.patch(f"{EVENTS_URL}/r_1").mock(
        return_value=httpx.Response(
            200,
            json=event(
                "r_1",
                "[fait] [suivi] Relancer",
                "2026-09-14T17:00:00+02:00",
                "2026-09-14T17:30:00+02:00",
                series="r",
            ),
        )
    )
    item = await svc.set_status(f"evt_{CAL_ID}/r_1", Status.DONE)
    assert item.status is Status.DONE and item.tracked and item.title == "Relancer"
    assert json.loads(patch_r.calls.last.request.content)["summary"] == "[fait] [suivi] Relancer"


async def test_set_status_task_cancelled_and_idempotent(
    svc: PlanService, respx_mock: respx.MockRouter
) -> None:
    _mock_containers(respx_mock)
    respx_mock.get(f"{TASKS_URL}/t1").mock(
        return_value=httpx.Response(200, json=task("t1", "Payer"))
    )
    patch = respx_mock.patch(f"{TASKS_URL}/t1").mock(
        return_value=httpx.Response(200, json=task("t1", "[annulé] Payer", status="completed"))
    )
    item = await svc.set_status(f"task_{LIST_ID}/t1", Status.CANCELLED)
    assert item.status is Status.CANCELLED
    assert json.loads(patch.calls.last.request.content)["status"] == "completed"

    respx_mock.get(f"{TASKS_URL}/t2").mock(
        return_value=httpx.Response(200, json=task("t2", "Fait", status="completed"))
    )
    item = await svc.set_status(f"task_{LIST_ID}/t2", Status.DONE)
    assert item.status is Status.DONE and patch.call_count == 1  # no second PATCH


async def test_recurring_requires_scope(svc: PlanService, respx_mock: respx.MockRouter) -> None:
    _mock_containers(respx_mock)
    respx_mock.get(f"{EVENTS_URL}/m_1").mock(
        return_value=httpx.Response(
            200,
            json=event(
                "m_1",
                "Stand-up",
                "2026-09-14T09:00:00+02:00",
                "2026-09-14T09:15:00+02:00",
                series="m",
            ),
        )
    )
    with pytest.raises(PlanError, match="scope"):
        await svc.delete(f"evt_{CAL_ID}/m_1")
    with pytest.raises(PlanError, match="scope"):
        await svc.update_event(f"evt_{CAL_ID}/m_1", title="x")
    delete = respx_mock.delete(f"{EVENTS_URL}/m").mock(return_value=httpx.Response(204))
    await svc.delete(f"evt_{CAL_ID}/m_1", scope="series")
    assert delete.called


async def test_add_task_unknown_list_is_refused(
    svc: PlanService, respx_mock: respx.MockRouter
) -> None:
    _mock_containers(respx_mock)
    with pytest.raises(PlanError, match="Ma liste"):
        await svc.add_task("x", list_name="Inexistante")


async def test_add_task_default_list_and_due(
    svc: PlanService, respx_mock: respx.MockRouter
) -> None:
    _mock_containers(respx_mock)
    post = respx_mock.post(TASKS_URL).mock(
        return_value=httpx.Response(200, json=task("n1", "Relance", due="2026-09-20"))
    )
    item = await svc.add_task("Relance", due=date(2026, 9, 20))
    assert item.due == date(2026, 9, 20)
    assert json.loads(post.calls.last.request.content)["due"] == "2026-09-20T00:00:00.000Z"


async def test_cache_invalidated_by_write(svc: PlanService, respx_mock: respx.MockRouter) -> None:
    _mock_containers(respx_mock)
    listing = respx_mock.get(TASKS_URL).mock(return_value=httpx.Response(200, json={"items": []}))
    respx_mock.post(TASKS_URL).mock(return_value=httpx.Response(200, json=task("n1", "x")))
    await svc.tasks()
    await svc.tasks()
    assert listing.call_count == 1
    await svc.add_task("x")
    await svc.tasks()
    assert listing.call_count == 2


async def test_401_retried_once(svc: PlanService, respx_mock: respx.MockRouter) -> None:
    _mock_containers(respx_mock)
    route = respx_mock.get(TASKS_URL)
    route.side_effect = [httpx.Response(401), httpx.Response(200, json={"items": []})]
    assert await svc.tasks() == []
    assert route.call_count == 2


async def test_add_tracked_recurring_event(svc: PlanService, respx_mock: respx.MockRouter) -> None:
    _mock_containers(respx_mock)
    created = event("m", "[suivi] Malt", "2026-09-16T09:00:00+02:00", "2026-09-16T09:30:00+02:00")
    created["recurrence"] = ["RRULE:FREQ=WEEKLY;BYDAY=WE"]
    post = respx_mock.post(EVENTS_URL).mock(return_value=httpx.Response(200, json=created))
    item = await svc.add_event(
        "Malt",
        datetime(2026, 9, 16, 9, tzinfo=TZ),
        datetime(2026, 9, 16, 9, 30, tzinfo=TZ),
        recurrence="FREQ=WEEKLY;BYDAY=WE",
        tracked=True,
    )
    sent = json.loads(post.calls.last.request.content)
    assert sent["summary"] == "[suivi] Malt"
    assert sent["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=WE"]
    assert item.tracked and item.recurring
    with pytest.raises(PlanError, match="récurrence"):
        await svc.add_event(
            "x",
            datetime(2026, 9, 16, 9, tzinfo=TZ),
            datetime(2026, 9, 16, 10, tzinfo=TZ),
            tracked=True,
        )
