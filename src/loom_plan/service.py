# SPDX-License-Identifier: Apache-2.0
"""PlanService — the read/write operations behind the MCP tools and the CLI (spec §5 to §7).

Google is the only source of truth. The service keeps a short read cache keyed by the raw
Google request, invalidated by every write, and no other state.
"""

from __future__ import annotations

import time as _time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal

from loom_plan.config import Config
from loom_plan.gcal import CalendarApi
from loom_plan.gtasks import TasksApi
from loom_plan.http import GoogleClient, JsonObject
from loom_plan.mapping import event_body, event_to_item, task_body, task_to_item, with_prefix
from loom_plan.model import Container, FreeSlot, Item, ItemId, ItemType, NextView, PlanError, Status
from loom_plan.times import day_start, next_midnight

AGENDA_MAX_DAYS = 31
FIND_DEFAULT_DAYS = 90

Scope = Literal["this", "series"]
Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class WorkingHours:
    start: time
    end: time


class PlanService:
    def __init__(self, cfg: Config, client: GoogleClient, clock: Clock | None = None) -> None:
        self._cfg = cfg
        self._cal = CalendarApi(client)
        self._tasks = TasksApi(client)
        self._clock: Clock = clock or (lambda: datetime.now(cfg.timezone))
        self._cache: dict[tuple[object, ...], tuple[float, object]] = {}

    # ==================================================================================
    # Containers
    # ==================================================================================

    async def containers(self) -> list[Container]:
        return await self._cached(("containers",), self._fetch_containers)

    async def _fetch_containers(self) -> list[Container]:
        out: list[Container] = []
        for c in await self._cal.calendar_list():
            cid = c.get("id")
            if not isinstance(cid, str):
                continue
            role = c.get("accessRole")
            out.append(
                Container(
                    id=cid,
                    name=_str(c, "summary", cid),
                    type=ItemType.EVENT,
                    primary=bool(c.get("primary")),
                    writable=role in ("owner", "writer"),
                )
            )
        for tl in await self._tasks.task_lists():
            tid = tl.get("id")
            if isinstance(tid, str):
                out.append(Container(id=tid, name=_str(tl, "title", tid), type=ItemType.TASK))
        return out

    async def _calendars(self, names: Iterable[str] | None = None) -> list[Container]:
        """Owned calendars by default (the only ones the token can write), or the given ones."""
        all_ = [c for c in await self.containers() if c.type is ItemType.EVENT]
        if names is None:
            return [c for c in all_ if c.writable]
        return [await self._resolve(n, ItemType.EVENT) for n in names]

    async def _task_lists(self, names: Iterable[str] | None = None) -> list[Container]:
        all_ = [c for c in await self.containers() if c.type is ItemType.TASK]
        if names is None:
            return all_
        return [await self._resolve(n, ItemType.TASK) for n in names]

    async def _resolve(self, name_or_id: str, kind: ItemType) -> Container:
        candidates = [c for c in await self.containers() if c.type is kind]
        wanted = name_or_id.strip().lower()
        if kind is ItemType.EVENT and wanted == "primary":
            for c in candidates:
                if c.primary:
                    return c
        for c in candidates:
            if c.id == name_or_id or c.name.lower() == wanted:
                return c
        label = "l'agenda" if kind is ItemType.EVENT else "la liste"
        names = ", ".join(f"« {c.name} »" for c in candidates) or "(aucun)"
        raise PlanError(f"{label} « {name_or_id} » n'existe pas ; disponibles : {names}")

    async def _container_by_id(self, container_id: str, kind: ItemType) -> Container:
        for c in await self.containers():
            if c.type is kind and c.id == container_id:
                return c
        raise PlanError(f"conteneur inconnu dans l'id : {container_id}")

    # ==================================================================================
    # Reads (spec §5)
    # ==================================================================================

    async def next(self, hours: float | None = None) -> NextView:
        now = self._clock()
        if hours is not None:
            if hours <= 0:
                raise PlanError("hours doit être > 0")
            window_end = now + timedelta(hours=hours)
        else:
            end_of_work = datetime.combine(now.date(), self._cfg.work.end, now.tzinfo)
            window_end = end_of_work if now < end_of_work else next_midnight(now)

        overdue = await self.overdue()
        events = await self._events_in(now, window_end, status=Status.OPEN)
        slots = await self.free_slots(now, window_end)
        tasks = await self.tasks(status=Status.OPEN)
        tasks.sort(key=_task_priority)
        return NextView(
            now=now,
            window_end=window_end,
            overdue=overdue,
            events=events,
            free_slots=slots,
            tasks=tasks,
        )

    async def overdue(self, include_recurring: bool = False) -> list[Item]:
        now = self._clock()
        epoch = day_start(self._cfg.epoch, self._cfg.timezone)
        today = now.date()
        out: list[Item] = []
        if epoch < now:
            for ev in await self._events_in(epoch, now, status=Status.OPEN):
                if ev.recurring and not include_recurring:
                    continue
                if _event_ended(ev, now) and _event_end_exclusive(ev) > epoch:
                    out.append(ev)
        for t in await self.tasks(status=Status.OPEN):
            if t.due is not None and self._cfg.epoch <= t.due < today:
                out.append(t)
        out.sort(key=Item.sort_key)
        return out

    async def agenda(
        self,
        start: datetime,
        end: datetime,
        containers: list[str] | None = None,
        status: Status = Status.OPEN,
    ) -> list[Item]:
        if end <= start:
            raise PlanError("la fin de la fenêtre doit être après son début")
        if end - start > timedelta(days=AGENDA_MAX_DAYS):
            raise PlanError(
                f"fenêtre trop large ({(end - start).days} j) : agenda est limité à "
                f"{AGENDA_MAX_DAYS} jours, utiliser plusieurs appels"
            )
        cal_names = (
            [c for c in containers or [] if await self._is_calendar(c)] if containers else None
        )
        list_names = (
            [c for c in containers or [] if not await self._is_calendar(c)] if containers else None
        )
        items = await self._events_in(start, end, status=status, calendars=cal_names)
        if list_names is None or list_names:
            for t in await self.tasks(status=status, lists=list_names):
                if t.due is not None and start.date() <= t.due <= end.date():
                    items.append(t)
        items.sort(key=Item.sort_key)
        return items

    async def tasks(
        self,
        list_name: str | None = None,
        status: Status = Status.OPEN,
        lists: list[str] | None = None,
    ) -> list[Item]:
        names = [list_name] if list_name else lists
        out: list[Item] = []
        for tl in await self._task_lists(names):
            include_completed = status is not Status.OPEN
            raw = await self._cached(
                ("tasks", tl.id, include_completed),
                lambda tl=tl, inc=include_completed: self._tasks.list_tasks(
                    tl.id, include_completed=inc
                ),
            )
            for payload in raw:
                item = self._task(payload, tl)
                if item.status is status:
                    out.append(item)
        out.sort(key=Item.sort_key)
        return out

    async def find(
        self, query: str, start: datetime | None = None, end: datetime | None = None
    ) -> list[Item]:
        q = query.strip()
        if not q:
            raise PlanError("query vide")
        now = self._clock()
        start = start or now - timedelta(days=FIND_DEFAULT_DAYS)
        end = end or now + timedelta(days=FIND_DEFAULT_DAYS)
        out: list[Item] = []
        for cal in await self._calendars():
            raw = await self._cached(
                ("events", cal.id, start, end, q, False),
                lambda cal=cal: self._cal.list_events(
                    cal.id, time_min=start, time_max=end, query=q
                ),
            )
            out.extend(self._event(p, cal) for p in raw)
        needle = q.lower()
        for tl in await self._task_lists():
            raw = await self._cached(
                ("tasks", tl.id, True),
                lambda tl=tl: self._tasks.list_tasks(tl.id, include_completed=True),
            )
            for payload in raw:
                item = self._task(payload, tl)
                haystack = f"{item.title}\n{item.notes or ''}".lower()
                in_window = item.due is None or start.date() <= item.due <= end.date()
                if needle in haystack and in_window:
                    out.append(item)
        out.sort(key=Item.sort_key)
        return out

    async def free_slots(
        self,
        start: datetime,
        end: datetime,
        min_minutes: int = 30,
        working_hours: WorkingHours | None = None,
    ) -> list[FreeSlot]:
        if end <= start:
            return []
        wh = working_hours or WorkingHours(self._cfg.work.start, self._cfg.work.end)
        busy = [
            (ev.start, ev.end)
            for ev in await self._events_in(start, end, status=None)
            if ev.status is not Status.CANCELLED
            and not ev.all_day
            and ev.start is not None
            and ev.end is not None
        ]
        busy.sort()
        slots: list[FreeSlot] = []
        day = start.date()
        while day <= end.date():
            if day.weekday() in self._cfg.work.days:
                tz = self._cfg.timezone
                win_start = max(start, datetime.combine(day, wh.start, tz))
                win_end = min(end, datetime.combine(day, wh.end, tz))
                slots.extend(_subtract(win_start, win_end, busy, min_minutes))
            day += timedelta(days=1)
        return slots

    # ==================================================================================
    # Writes (spec §6) — every write invalidates the read cache and returns the item
    # ==================================================================================

    async def add_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        container: str | None = None,
        notes: str | None = None,
        location: str | None = None,
        all_day: bool = False,
    ) -> Item:
        if not title.strip():
            raise PlanError("titre vide")
        if all_day and end.date() < start.date():
            raise PlanError("le dernier jour doit être ≥ au premier jour")
        if not all_day and end <= start:
            raise PlanError("la fin doit être après le début")
        cal = await self._resolve(container or self._cfg.primary_calendar, ItemType.EVENT)
        body = event_body(
            title=title,
            start=start,
            end=end,
            all_day=all_day,
            notes=notes,
            location=location,
            tz=self._cfg.timezone,
        )
        payload = await self._cal.insert_event(cal.id, body)
        self._invalidate()
        return self._event(payload, cal)

    async def add_task(
        self,
        title: str,
        list_name: str | None = None,
        due: date | None = None,
        notes: str | None = None,
    ) -> Item:
        if not title.strip():
            raise PlanError("titre vide")
        tl = await self._resolve(list_name or self._cfg.default_task_list, ItemType.TASK)
        payload = await self._tasks.insert_task(tl.id, task_body(title=title, due=due, notes=notes))
        self._invalidate()
        return self._task(payload, tl)

    async def update_event(
        self,
        item_id: str,
        scope: Scope | None = None,
        *,
        title: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        all_day: bool | None = None,
        notes: str | None = None,
        location: str | None = None,
    ) -> Item:
        iid = _expect(item_id, ItemType.EVENT)
        cal = await self._container_by_id(iid.container_id, ItemType.EVENT)
        current = await self._cal.get_event(cal.id, iid.native_id)
        target_id = self._target_for_scope(current, scope, "update_event")
        if (start is None) != (end is None):
            raise PlanError("start et end vont ensemble")
        if start is not None and target_id != iid.native_id:
            raise PlanError(
                "déplacer toute une série (scope=series) n'est pas pris en charge en v1 ; "
                "modifier l'horaire dans Google Agenda ou utiliser scope=this"
            )
        if all_day is None:
            all_day = "date" in _table(current, "start")
        if title is not None:
            existing = self._event(current, cal)
            title = with_prefix(title, existing.status, self._cfg.prefixes)
        body = event_body(
            title=title,
            start=start,
            end=end,
            all_day=all_day,
            notes=notes,
            location=location,
            tz=self._cfg.timezone,
        )
        if not body:
            raise PlanError("aucun champ à modifier")
        payload = await self._cal.patch_event(cal.id, target_id, body)
        self._invalidate()
        return self._event(payload, cal)

    async def update_task(
        self,
        item_id: str,
        *,
        title: str | None = None,
        due: date | None = None,
        clear_due: bool = False,
        notes: str | None = None,
        list_name: str | None = None,
    ) -> Item:
        iid = _expect(item_id, ItemType.TASK)
        tl = await self._container_by_id(iid.container_id, ItemType.TASK)
        if title is not None:
            current = self._task(await self._tasks.get_task(tl.id, iid.native_id), tl)
            title = with_prefix(title, current.status, self._cfg.prefixes)
        body = task_body(title=title, due=due, clear_due=clear_due, notes=notes)
        payload: JsonObject | None = None
        if body:
            payload = await self._tasks.patch_task(tl.id, iid.native_id, body)
        if list_name is not None:
            dest = await self._resolve(list_name, ItemType.TASK)
            if dest.id != tl.id:
                payload = await self._tasks.move_task(tl.id, iid.native_id, dest.id)
                tl = dest
        if payload is None:
            raise PlanError("aucun champ à modifier")
        self._invalidate()
        return self._task(payload, tl)

    async def set_status(self, item_id: str, status: Status) -> Item:
        iid = ItemId.parse(item_id)
        if iid.type is ItemType.EVENT:
            return await self._set_event_status(iid, status)
        return await self._set_task_status(iid, status)

    async def delete(self, item_id: str, scope: Scope | None = None) -> None:
        iid = ItemId.parse(item_id)
        if iid.type is ItemType.EVENT:
            cal = await self._container_by_id(iid.container_id, ItemType.EVENT)
            current = await self._cal.get_event(cal.id, iid.native_id)
            target = self._target_for_scope(current, scope, "delete")
            await self._cal.delete_event(cal.id, target)
        else:
            tl = await self._container_by_id(iid.container_id, ItemType.TASK)
            await self._tasks.delete_task(tl.id, iid.native_id)
        self._invalidate()

    # ==================================================================================
    # Internals
    # ==================================================================================

    async def _set_event_status(self, iid: ItemId, status: Status) -> Item:
        cal = await self._container_by_id(iid.container_id, ItemType.EVENT)
        current_payload = await self._cal.get_event(cal.id, iid.native_id)
        current = self._event(current_payload, cal)
        if current.status is status:
            return current
        body: JsonObject = {"summary": with_prefix(current.title, status, self._cfg.prefixes)}
        body["status"] = "cancelled" if status is Status.CANCELLED else "confirmed"
        # A recurring instance id patches that occurrence only (spec §6 set_status).
        payload = await self._cal.patch_event(cal.id, iid.native_id, body)
        self._invalidate()
        return self._event(payload, cal)

    async def _set_task_status(self, iid: ItemId, status: Status) -> Item:
        tl = await self._container_by_id(iid.container_id, ItemType.TASK)
        current = self._task(await self._tasks.get_task(tl.id, iid.native_id), tl)
        if current.status is status:
            return current
        body: JsonObject = {"title": with_prefix(current.title, status, self._cfg.prefixes)}
        if status is Status.OPEN:
            body["status"] = "needsAction"
            body["completed"] = None
        else:
            body["status"] = "completed"
        payload = await self._tasks.patch_task(tl.id, iid.native_id, body)
        self._invalidate()
        return self._task(payload, tl)

    @staticmethod
    def _target_for_scope(current: JsonObject, scope: Scope | None, op: str) -> str:
        series = current.get("recurringEventId")
        own_id = _str(current, "id", "")
        if isinstance(series, str) and series:
            if scope is None:
                raise PlanError(
                    f"{op} : cet événement est une occurrence d'une série ; préciser "
                    "scope=this (cette occurrence) ou scope=series (toute la série)"
                )
            return series if scope == "series" else own_id
        return own_id

    async def _events_in(
        self,
        start: datetime,
        end: datetime,
        *,
        status: Status | None,
        calendars: list[str] | None = None,
    ) -> list[Item]:
        show_deleted = status is Status.CANCELLED
        out: list[Item] = []
        for cal in await self._calendars(calendars):
            raw = await self._cached(
                ("events", cal.id, start, end, None, show_deleted),
                lambda cal=cal: self._cal.list_events(
                    cal.id, time_min=start, time_max=end, show_deleted=show_deleted
                ),
            )
            for payload in raw:
                item = self._event(payload, cal)
                if status is None or item.status is status:
                    out.append(item)
        out.sort(key=Item.sort_key)
        return out

    async def _is_calendar(self, name: str) -> bool:
        try:
            await self._resolve(name, ItemType.EVENT)
        except PlanError:
            await self._resolve(name, ItemType.TASK)  # raises with the list names if unknown
            return False
        return True

    def _event(self, payload: JsonObject, cal: Container) -> Item:
        return event_to_item(
            payload,
            calendar_id=cal.id,
            calendar_name=cal.name,
            tz=self._cfg.timezone,
            prefixes=self._cfg.prefixes,
            notes_max_chars=self._cfg.notes_max_chars,
        )

    def _task(self, payload: JsonObject, tl: Container) -> Item:
        return task_to_item(
            payload,
            list_id=tl.id,
            list_name=tl.name,
            tz=self._cfg.timezone,
            prefixes=self._cfg.prefixes,
            notes_max_chars=self._cfg.notes_max_chars,
        )

    async def _cached[T](self, key: tuple[object, ...], fetch: Callable[[], Awaitable[T]]) -> T:
        now = _time.monotonic()
        hit = self._cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]  # pyright: ignore[reportReturnType]
        value = await fetch()
        self._cache[key] = (now + self._cfg.cache_ttl_seconds, value)
        return value

    def _invalidate(self) -> None:
        self._cache.clear()


# -- module helpers --------------------------------------------------------------------


def _expect(item_id: str, kind: ItemType) -> ItemId:
    iid = ItemId.parse(item_id)
    if iid.type is not kind:
        wanted = "un événement (evt_…)" if kind is ItemType.EVENT else "une tâche (task_…)"
        raise PlanError(f"{item_id} n'est pas {wanted}")
    return iid


def _event_end_exclusive(ev: Item) -> datetime:
    assert ev.end is not None
    return ev.end + timedelta(days=1) if ev.all_day else ev.end


def _event_ended(ev: Item, now: datetime) -> bool:
    return _event_end_exclusive(ev) <= now


def _task_priority(t: Item) -> tuple[int, date, int, str]:
    return (
        0 if t.due is not None else 1,
        t.due or date.max,
        t.estimate_min if t.estimate_min is not None else 10**6,
        t.title,
    )


def _subtract(
    win_start: datetime,
    win_end: datetime,
    busy: list[tuple[datetime, datetime]],
    min_minutes: int,
) -> list[FreeSlot]:
    slots: list[FreeSlot] = []
    cursor = win_start
    for b_start, b_end in busy:
        if b_end <= cursor or b_start >= win_end:
            continue
        if b_start > cursor:
            _emit(slots, cursor, min(b_start, win_end), min_minutes)
        cursor = max(cursor, b_end)
        if cursor >= win_end:
            break
    if cursor < win_end:
        _emit(slots, cursor, win_end, min_minutes)
    return slots


def _emit(slots: list[FreeSlot], start: datetime, end: datetime, min_minutes: int) -> None:
    slot = FreeSlot(start, end)
    if slot.minutes >= min_minutes:
        slots.append(slot)


def _str(payload: JsonObject, key: str, default: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else default


def _table(payload: JsonObject, key: str) -> JsonObject:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}  # pyright: ignore[reportUnknownVariableType, reportReturnType]
