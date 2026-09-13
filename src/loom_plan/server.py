# SPDX-License-Identifier: Apache-2.0
"""FastMCP server exposing the loom-plan tools (spec §5, §6).

Tool descriptions are written for Claude, in French, and carry the rules of the spec:
free reads, writes only on Denis's explicit request, expected markdown rendering.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from importlib import resources
from typing import Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.session import ServerSession
from mcp.types import ToolAnnotations

from loom_plan.auth import AuthError, TokenStore
from loom_plan.config import ENV_CONFIG, Config, load_config
from loom_plan.http import GoogleApiError, GoogleClient
from loom_plan.model import PlanError, Status
from loom_plan.render import (
    render_containers,
    render_item,
    render_items,
    render_next,
    render_slots,
)
from loom_plan.service import PlanService, Scope, WorkingHours
from loom_plan.times import (
    day_start,
    parse_date_input,
    parse_datetime_input,
    parse_time_input,
)

StatusName = Literal["open", "done", "cancelled"]

WRITE_RULE = (
    "ÉCRITURE : uniquement sur demande explicite de Denis. Ne jamais appeler de sa propre "
    "initiative, ni pour « aider », ni pour corriger. Renvoie l'objet résultant au format unifié."
)
DATETIME_FORMAT = "format « AAAA-MM-JJ HH:MM » en heure locale Europe/Paris"

INSTRUCTIONS = """loom-plan : agenda et tâches de Denis (Google Calendar + Google Tasks).
Lecture libre. Toute écriture (add_*, update_*, set_status, delete) uniquement sur demande
explicite. Les dates sont en heure locale Europe/Paris. Chaque item porte un id
(evt_… ou task_…) à réutiliser tel quel pour les écritures. Pour « qu'est-ce que j'ai
aujourd'hui / à faire », appeler `next`. Conventions de titre (posées ou lues par le
serveur) : [fait] = événement traité, [annulé] = tâche abandonnée, [suivi] = série
récurrente dont chaque occurrence est une chose à faire (remonte en retard si oubliée)."""


@dataclass(frozen=True, slots=True)
class AppState:
    cfg: Config
    service: PlanService


@asynccontextmanager
async def _lifespan(_: FastMCP) -> AsyncGenerator[AppState]:
    cfg = load_config()
    client = GoogleClient(TokenStore(cfg.auth.client_secret, cfg.auth.token))
    try:
        yield AppState(cfg=cfg, service=PlanService(cfg, client))
    finally:
        await client.aclose()


mcp = FastMCP("loom-plan", instructions=INSTRUCTIONS, lifespan=_lifespan)

# -- MCP Apps (spec §11, v2) : the `next` tool carries an HTML view served as a resource ----
NEXT_UI_URI = "ui://loom-plan/next.html"
APP_MIME_TYPE = "text/html;profile=mcp-app"


@mcp.resource(NEXT_UI_URI, name="next-view", mime_type=APP_MIME_TYPE)
def next_view() -> str:
    """Vue interactive de la journée (MCP App), rendue par l'hôte dans la conversation."""
    return resources.files("loom_plan").joinpath("ui/next.html").read_text(encoding="utf-8")


_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)


Ctx = Context[ServerSession, AppState]


def _state(ctx: Ctx) -> AppState:
    return ctx.request_context.lifespan_context


async def _guard[T](run: Callable[[], Awaitable[T]]) -> T:
    """Turn our plain-language errors into ToolError so Claude sees the message."""
    try:
        return await run()
    except (PlanError, GoogleApiError, AuthError) as exc:
        raise ToolError(str(exc)) from exc


# ======================================================================================
# Lecture
# ======================================================================================


@mcp.tool(
    annotations=_READ,
    meta={"ui": {"resourceUri": NEXT_UI_URI}},
    description=(
        "État global de la journée en un seul appel : (1) En retard — tout ce qui est passé et "
        "toujours ouvert ; (2) événements de la fenêtre ; (3) créneaux libres ; (4) tâches "
        "ouvertes, y compris sans date, triées par échéance puis durée estimée. À appeler pour "
        "toute question du type « qu'est-ce que j'ai aujourd'hui / à faire ». Présenter le "
        "résultat en markdown, en trois blocs — **En retard**, **Aujourd'hui**, **À faire** — "
        "chacun sous forme de tableau (heure ou échéance, titre, durée estimée, agenda/liste). "
        "Les créneaux libres s'intercalent dans le tableau Aujourd'hui. Ne pas omettre le bloc "
        "En retard s'il est vide : l'indiquer explicitement. Ne pas afficher les ids sauf si "
        "Denis les demande. `hours` : fenêtre en heures à partir de maintenant ; par défaut "
        "jusqu'à la fin des horaires de travail du jour (ou minuit si on est déjà après)."
    ),
)
async def next(ctx: Ctx, hours: float | None = None) -> str:
    svc = _state(ctx).service
    return render_next(await _guard(lambda: svc.next(hours)))


@mcp.tool(
    annotations=_READ,
    description=(
        "Tout ce qui est passé et toujours ouvert (tâches à échéance dépassée, événements "
        "terminés non marqués faits), du plus ancien au plus récent. Sert seul pour « qu'est-ce "
        "que je n'ai pas fait » ; sinon son contenu est déjà dans `next`. Les occurrences "
        "d'événements récurrents sont exclues sauf `include_recurring=true` — à l'exception des "
        "séries dont le titre commence par « [suivi] » (convention posée par Denis dans Google "
        "Agenda : chaque occurrence est une chose à faire), toujours incluses."
    ),
)
async def overdue(ctx: Ctx, include_recurring: bool = False) -> str:
    svc = _state(ctx).service
    return render_items(
        await _guard(lambda: svc.overdue(include_recurring)), empty="(rien en retard)"
    )


@mcp.tool(
    annotations=_READ,
    description=(
        "Événements et tâches datées fusionnés et triés sur une fenêtre (31 jours maximum). "
        f"`start`/`end` : {DATETIME_FORMAT}, ou « AAAA-MM-JJ » (minuit). `end` absent : start + "
        "1 jour. `containers` : noms d'agendas ou de listes pour restreindre (voir `containers`). "
        "`status` : open (défaut), done ou cancelled. Présenter en tableau markdown."
    ),
)
async def agenda(
    ctx: Ctx,
    start: str,
    end: str | None = None,
    containers: list[str] | None = None,
    status: StatusName = "open",
) -> str:
    st = _state(ctx)
    s = parse_datetime_input(start, st.cfg.timezone)
    e = parse_datetime_input(end, st.cfg.timezone) if end else s + timedelta(days=1)
    return render_items(await _guard(lambda: st.service.agenda(s, e, containers, Status(status))))


@mcp.tool(
    annotations=_READ,
    description=(
        "Tâches Google Tasks, datées ou non — le seul tool qui remonte les tâches sans date en "
        "dehors de `next`. `list_name` : nom d'une liste (défaut : toutes). `status` : open "
        "(défaut), done ou cancelled."
    ),
)
async def tasks(ctx: Ctx, list_name: str | None = None, status: StatusName = "open") -> str:
    svc = _state(ctx).service
    return render_items(await _guard(lambda: svc.tasks(list_name, Status(status))))


@mcp.tool(
    annotations=_READ,
    description=(
        "Recherche texte (titre, notes) sur événements et tâches, tous statuts. Sans fenêtre : "
        f"90 jours en arrière et 90 en avant. `start`/`end` : {DATETIME_FORMAT}."
    ),
)
async def find(ctx: Ctx, query: str, start: str | None = None, end: str | None = None) -> str:
    st = _state(ctx)
    s = parse_datetime_input(start, st.cfg.timezone) if start else None
    e = parse_datetime_input(end, st.cfg.timezone) if end else None
    return render_items(
        await _guard(lambda: st.service.find(query, s, e)), empty="(aucun résultat)"
    )


@mcp.tool(
    annotations=_READ,
    description=(
        "Créneaux libres entre `start` et `end`, calculés à partir des événements uniquement "
        "(les tâches ne bloquent pas de temps), sur les jours ouvrés, dans les horaires de "
        f"travail (9h-18h par défaut ; `work_start`/`work_end` en « HH:MM »). {DATETIME_FORMAT}. "
        "`min_minutes` : durée minimale d'un créneau."
    ),
)
async def free_slots(
    ctx: Ctx,
    start: str,
    end: str,
    min_minutes: int = 30,
    work_start: str | None = None,
    work_end: str | None = None,
) -> str:
    st = _state(ctx)
    s = parse_datetime_input(start, st.cfg.timezone)
    e = parse_datetime_input(end, st.cfg.timezone)
    wh = None
    if work_start or work_end:
        wh = WorkingHours(
            parse_time_input(work_start) if work_start else st.cfg.work.start,
            parse_time_input(work_end) if work_end else st.cfg.work.end,
        )
    return render_slots(await _guard(lambda: st.service.free_slots(s, e, min_minutes, wh)))


@mcp.tool(
    annotations=_READ,
    description=(
        "Agendas Google Calendar et listes Google Tasks disponibles (nom, id, principal, lecture "
        "seule). Sert à ranger correctement à l'écriture (`container` / `list_name`)."
    ),
)
async def containers(ctx: Ctx) -> str:
    svc = _state(ctx).service
    return render_containers(await _guard(svc.containers))


# ======================================================================================
# Écriture
# ======================================================================================


@mcp.tool(
    annotations=_WRITE,
    description=(
        f"{WRITE_RULE} Crée un événement. `start`/`end` : {DATETIME_FORMAT} ; avec "
        "`all_day=true`, « AAAA-MM-JJ » du premier et du dernier jour (inclus). `container` : "
        "nom de l'agenda (défaut : agenda principal). `recurrence` : règle RRULE sans le "
        "préfixe, `start`/`end` donnant la première occurrence (ex. `FREQ=WEEKLY;BYDAY=WE`, "
        "`FREQ=MONTHLY;BYMONTHDAY=1`, `FREQ=WEEKLY;BYDAY=MO,FR;COUNT=8`). `tracked=true` "
        "(récurrence seulement) pose « [suivi] » : chaque occurrence est une chose à faire et "
        "remonte en retard si elle n'est pas marquée faite."
    ),
)
async def add_event(
    ctx: Ctx,
    title: str,
    start: str,
    end: str,
    container: str | None = None,
    notes: str | None = None,
    location: str | None = None,
    all_day: bool = False,
    recurrence: str | None = None,
    tracked: bool = False,
) -> str:
    st = _state(ctx)
    tz = st.cfg.timezone
    if all_day:
        s, e = day_start(parse_date_input(start), tz), day_start(parse_date_input(end), tz)
    else:
        s, e = parse_datetime_input(start, tz), parse_datetime_input(end, tz)
    item = await _guard(
        lambda: st.service.add_event(
            title, s, e, container, notes, location, all_day, recurrence, tracked
        )
    )
    return render_item(item)


@mcp.tool(
    annotations=_WRITE,
    description=(
        f"{WRITE_RULE} Crée une tâche. `due` : « AAAA-MM-JJ », optionnel. `list_name` : nom de "
        "la liste (défaut : liste configurée) ; une liste inexistante est refusée, jamais créée. "
        "Convention : « ~30min » ou « ~1h30 » dans `notes` donne une durée estimée."
    ),
)
async def add_task(
    ctx: Ctx,
    title: str,
    list_name: str | None = None,
    due: str | None = None,
    notes: str | None = None,
) -> str:
    svc = _state(ctx).service
    d = parse_date_input(due) if due else None
    return render_item(await _guard(lambda: svc.add_task(title, list_name, d, notes)))


@mcp.tool(
    annotations=_WRITE,
    description=(
        f"{WRITE_RULE} Modifie un événement (titre, horaire, notes, lieu). `scope` obligatoire "
        "pour un événement récurrent : « this » (cette occurrence) ou « series » (toute la "
        "série) — absent sur un récurrent, le serveur refuse. `start` et `end` vont ensemble ; "
        f"{DATETIME_FORMAT}."
    ),
)
async def update_event(
    ctx: Ctx,
    id: str,
    scope: Scope | None = None,
    title: str | None = None,
    start: str | None = None,
    end: str | None = None,
    notes: str | None = None,
    location: str | None = None,
    all_day: bool | None = None,
) -> str:
    st = _state(ctx)
    tz = st.cfg.timezone
    s = parse_datetime_input(start, tz) if start else None
    e = parse_datetime_input(end, tz) if end else None
    item = await _guard(
        lambda: st.service.update_event(
            id,
            scope,
            title=title,
            start=s,
            end=e,
            all_day=all_day,
            notes=notes,
            location=location,
        )
    )
    return render_item(item)


@mcp.tool(
    annotations=_WRITE,
    description=(
        f"{WRITE_RULE} Modifie une tâche : titre, notes, échéance (`due` « AAAA-MM-JJ », ou "
        "`clear_due=true` pour la retirer), liste (`list_name` : déplacement)."
    ),
)
async def update_task(
    ctx: Ctx,
    id: str,
    title: str | None = None,
    due: str | None = None,
    clear_due: bool = False,
    notes: str | None = None,
    list_name: str | None = None,
) -> str:
    svc = _state(ctx).service
    d = parse_date_input(due) if due else None
    item = await _guard(
        lambda: svc.update_task(
            id, title=title, due=d, clear_due=clear_due, notes=notes, list_name=list_name
        )
    )
    return render_item(item)


@mcp.tool(
    annotations=_WRITE,
    description=(
        f"{WRITE_RULE} Change le statut d'un événement ou d'une tâche : open, done, cancelled. "
        "Idempotent. Pour un événement récurrent, agit sur l'occurrence seulement. C'est le "
        "tool à utiliser pour « c'est fait » / « je ne le ferai pas » (préférer cancelled à "
        "`delete` : on garde la trace)."
    ),
)
async def set_status(ctx: Ctx, id: str, status: StatusName) -> str:
    svc = _state(ctx).service
    return render_item(await _guard(lambda: svc.set_status(id, Status(status))))


@mcp.tool(
    annotations=_DESTRUCTIVE,
    description=(
        f"{WRITE_RULE} Suppression définitive, réservée aux erreurs de saisie. Pour « ne plus "
        "faire », utiliser `set_status(cancelled)`. `scope` obligatoire pour un événement "
        "récurrent (this / series)."
    ),
)
async def delete(ctx: Ctx, id: str, scope: Scope | None = None) -> str:
    svc = _state(ctx).service
    await _guard(lambda: svc.delete(id, scope))
    return f"supprimé : {id}"


def serve() -> None:
    mcp.run(transport="stdio")


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point of the ``loom-plan-mcp`` script (same shape as loom-fs-mcp / loom-memory-mcp)."""
    parser = argparse.ArgumentParser(prog="loom-plan-mcp")
    parser.add_argument("--config", help=f"chemin de loom-plan.toml (sinon ${ENV_CONFIG})")
    args = parser.parse_args(argv)
    config = args.config
    if isinstance(config, str):
        os.environ[ENV_CONFIG] = config
    serve()
