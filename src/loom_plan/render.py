# SPDX-License-Identifier: Apache-2.0
"""Text rendering of unified items (spec §2 principle 6).

The server does not produce markdown tables: it emits one line per item, already ordered
and formatted (local times, « 45 min » durations, explicit sections), so that Claude only
has to lay the lines out.
"""

from __future__ import annotations

from datetime import datetime

from loom_plan.model import Container, FreeSlot, Item, ItemType, NextView, Status
from loom_plan.times import fmt_date, fmt_day, fmt_dt, fmt_minutes, fmt_time

_STATUS_FR = {Status.OPEN: "ouvert", Status.DONE: "fait", Status.CANCELLED: "annulé"}


def render_item(item: Item, *, with_notes: bool = True) -> str:
    parts: list[str] = [_when(item), item.title]
    if item.estimate_min is not None and not item.is_event:
        parts.append(f"~{fmt_minutes(item.estimate_min)}")
    if item.location:
        parts.append(f"lieu : {item.location}")
    parts.append(("agenda : " if item.is_event else "liste : ") + item.container)
    if item.status is not Status.OPEN:
        parts.append(f"statut : {_STATUS_FR[item.status]}")
    if item.recurring:
        parts.append("récurrent (suivi)" if item.tracked else "récurrent")
    parts.append(f"id : {item.id}")
    line = " — ".join(parts)
    if with_notes and item.notes:
        line += "\n    notes : " + item.notes.replace("\n", "\n    ")
    return line


def render_items(items: list[Item], *, empty: str = "(rien)") -> str:
    if not items:
        return empty
    return "\n".join(render_item(i) for i in items)


def render_slot(slot: FreeSlot) -> str:
    same_day = slot.start.date() == slot.end.date()
    end = fmt_time(slot.end) if same_day else fmt_dt(slot.end)
    return f"libre {fmt_dt(slot.start)} → {end} ({fmt_minutes(slot.minutes)})"


def render_slots(slots: list[FreeSlot]) -> str:
    return "\n".join(render_slot(s) for s in slots) if slots else "(aucun créneau libre)"


def render_next(view: NextView) -> str:
    lines = [
        f"now: {fmt_dt(view.now)} ({fmt_day(view.now.date())})",
        f"fenêtre : jusqu'à {fmt_dt(view.window_end)}",
        "",
        f"## En retard ({len(view.overdue)})",
        render_items(view.overdue, empty="(rien en retard)"),
        "",
        f"## Aujourd'hui — événements ({len(view.events)})",
        render_items(view.events, empty="(aucun événement dans la fenêtre)"),
        "",
        f"## Aujourd'hui — créneaux libres ({len(view.free_slots)})",
        render_slots(view.free_slots),
        "",
        f"## À faire ({len(view.tasks)})",
        render_items(view.tasks, empty="(aucune tâche ouverte)"),
    ]
    return "\n".join(lines)


def render_containers(containers: list[Container]) -> str:
    lines: list[str] = ["## Agendas"]
    for c in containers:
        if c.type is ItemType.EVENT:
            flags = [
                f for f, on in (("principal", c.primary), ("lecture seule", not c.writable)) if on
            ]
            suffix = f" ({', '.join(flags)})" if flags else ""
            lines.append(f"- {c.name}{suffix} — id : {c.id}")
    lines.append("## Listes de tâches")
    lines.extend(f"- {c.name} — id : {c.id}" for c in containers if c.type is ItemType.TASK)
    return "\n".join(lines)


def _when(item: Item) -> str:
    if item.is_event:
        assert item.start is not None and item.end is not None
        if item.all_day:
            if item.start.date() == item.end.date():
                return f"{fmt_date(item.start.date())} (journée)"
            return f"{fmt_date(item.start.date())} → {fmt_date(item.end.date())} (journées)"
        end, dur = _end(item.start, item.end), _duration(item.start, item.end)
        return f"{fmt_dt(item.start)} → {end} ({dur})"
    if item.due is not None:
        return f"échéance {fmt_date(item.due)}"
    return "sans échéance"


def _end(start: datetime, end: datetime) -> str:
    return fmt_time(end) if start.date() == end.date() else fmt_dt(end)


def _duration(start: datetime, end: datetime) -> str:
    return fmt_minutes(max(0, int((end - start).total_seconds() // 60)))
