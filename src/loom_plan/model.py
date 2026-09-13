# SPDX-License-Identifier: Apache-2.0
"""Unified model (spec §3) and unified ids.

An id carries its container so that writes know where to go without server-side state:
``evt_<calendarId>/<eventId>`` and ``task_<taskListId>/<taskId>``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


class PlanError(RuntimeError):
    """Plain-language error meant for Claude (spec §7 « Erreurs »)."""


class ItemType(StrEnum):
    EVENT = "event"
    TASK = "task"


class Status(StrEnum):
    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ItemId:
    type: ItemType
    container_id: str
    native_id: str

    def __str__(self) -> str:
        prefix = "evt" if self.type is ItemType.EVENT else "task"
        return f"{prefix}_{self.container_id}/{self.native_id}"

    @classmethod
    def parse(cls, raw: str) -> ItemId:
        if raw.startswith("evt_"):
            kind, rest = ItemType.EVENT, raw[4:]
        elif raw.startswith("task_"):
            kind, rest = ItemType.TASK, raw[5:]
        else:
            raise PlanError(f"id invalide : {raw!r} (attendu evt_… ou task_…)")
        container, sep, native = rest.partition("/")
        if not sep or not container or not native:
            raise PlanError(f"id invalide : {raw!r} (attendu <type>_<conteneur>/<id>)")
        return cls(kind, container, native)


@dataclass(frozen=True, slots=True)
class Item:
    id: ItemId
    type: ItemType
    title: str
    status: Status
    container: str
    updated: datetime | None = None
    # events
    start: datetime | None = None
    end: datetime | None = None
    all_day: bool = False
    recurring: bool = False
    series_id: str | None = None
    tracked: bool = False  # recurring series whose occurrences are things to do
    location: str | None = None
    # tasks
    due: date | None = None
    # both
    notes: str | None = None
    estimate_min: int | None = None

    @property
    def is_event(self) -> bool:
        return self.type is ItemType.EVENT

    def sort_key(self) -> tuple[int, datetime, str]:
        """Chronological key usable across events (start) and tasks (due).

        Datetimes are already local, so comparing them naive keeps a task due on a given
        day (midnight) ahead of that day's events.
        """
        if self.start is not None:
            return (0, self.start.replace(tzinfo=None), self.title)
        if self.due is not None:
            return (0, datetime.combine(self.due, datetime.min.time()), self.title)
        return (1, datetime.max, self.title)


@dataclass(frozen=True, slots=True)
class FreeSlot:
    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass(frozen=True, slots=True)
class Container:
    id: str
    name: str
    type: ItemType
    primary: bool = False
    writable: bool = True


@dataclass(frozen=True, slots=True)
class NextView:
    now: datetime
    window_end: datetime
    overdue: list[Item] = field(default_factory=lambda: list[Item]())
    events: list[Item] = field(default_factory=lambda: list[Item]())
    free_slots: list[FreeSlot] = field(default_factory=lambda: list[FreeSlot]())
    tasks: list[Item] = field(default_factory=lambda: list[Item]())
