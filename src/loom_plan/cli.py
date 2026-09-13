# SPDX-License-Identifier: Apache-2.0
"""Command-line entry point (spec §10 step 2): manual validation against the real account.

Commands: auth, check, containers, next, overdue, agenda, tasks, find, free, add-task,
add-event, set-status, delete.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import timedelta
from typing import cast

from loom_plan.auth import AuthError, TokenStore
from loom_plan.config import Config, ConfigError, load_config
from loom_plan.http import GoogleApiError, GoogleClient
from loom_plan.model import PlanError, Status
from loom_plan.render import render_containers, render_item, render_items, render_next, render_slots
from loom_plan.service import PlanService, Scope
from loom_plan.times import day_start, parse_date_input, parse_datetime_input


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        cfg = load_config()
        if args.command == "auth":
            return _cmd_auth(cfg)
        if args.command == "serve":
            from loom_plan.server import serve

            serve()
            return 0
        return asyncio.run(_run(cfg, args))
    except (ConfigError, AuthError, GoogleApiError, PlanError) as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="loom-plan")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("auth", help="lance le consentement OAuth et enregistre token.json")
    sub.add_parser("check", help="vérifie le token : liste les agendas et les listes Tasks")
    sub.add_parser("containers", help="agendas et listes disponibles")
    sub.add_parser("serve", help="lance le serveur MCP (stdio) pour Claude Desktop")

    s = sub.add_parser("next", help="état de la journée : retards, événements, créneaux, tâches")
    s.add_argument("--hours", type=float, help="fenêtre en heures (défaut : fin de journée)")

    s = sub.add_parser("overdue", help="tout ce qui est passé et toujours ouvert")
    s.add_argument("--include-recurring", action="store_true")

    s = sub.add_parser("agenda", help="événements + tâches datées sur une fenêtre (≤ 31 j)")
    s.add_argument("start", help="AAAA-MM-JJ[ HH:MM]")
    s.add_argument("end", nargs="?", help="AAAA-MM-JJ[ HH:MM] (défaut : start + 1 jour)")
    s.add_argument("--status", choices=[s.value for s in Status], default="open")
    s.add_argument("--container", action="append", help="agenda ou liste (répétable)")

    s = sub.add_parser("tasks", help="tâches, datées ou non")
    s.add_argument("--list", dest="list_name")
    s.add_argument("--status", choices=[s.value for s in Status], default="open")

    s = sub.add_parser("find", help="recherche texte (titre, notes), ±90 j par défaut")
    s.add_argument("query")
    s.add_argument("--from", dest="start")
    s.add_argument("--to", dest="end")

    s = sub.add_parser("free", help="créneaux libres sur une fenêtre")
    s.add_argument("start")
    s.add_argument("end")
    s.add_argument("--min", type=int, default=30, help="durée minimale en minutes")

    s = sub.add_parser("add-task", help="crée une tâche")
    s.add_argument("title")
    s.add_argument("--list", dest="list_name")
    s.add_argument("--due", help="AAAA-MM-JJ")
    s.add_argument("--notes")

    s = sub.add_parser("add-event", help="crée un événement")
    s.add_argument("title")
    s.add_argument("start", help="AAAA-MM-JJ HH:MM (ou AAAA-MM-JJ avec --all-day)")
    s.add_argument("end")
    s.add_argument("--calendar")
    s.add_argument("--notes")
    s.add_argument("--location")
    s.add_argument("--all-day", action="store_true")
    s.add_argument("--recurrence", help="règle RRULE, ex. FREQ=WEEKLY;BYDAY=WE")
    s.add_argument("--tracked", action="store_true", help="pose [suivi] (récurrence seulement)")

    s = sub.add_parser("set-status", help="open | done | cancelled")
    s.add_argument("id")
    s.add_argument("status", choices=[s.value for s in Status])

    s = sub.add_parser("delete", help="suppression définitive (préférer set-status cancelled)")
    s.add_argument("id")
    s.add_argument("--scope", choices=["this", "series"])
    return p


def _store(cfg: Config) -> TokenStore:
    return TokenStore(cfg.auth.client_secret, cfg.auth.token)


def _cmd_auth(cfg: Config) -> int:
    store = _store(cfg)
    print("Ouverture du navigateur pour le consentement Google…")
    store.authorize()
    print(f"Token enregistré : {store.token_path}")
    return 0


async def _run(cfg: Config, args: argparse.Namespace) -> int:
    tz = cfg.timezone
    async with GoogleClient(_store(cfg)) as client:
        svc = PlanService(cfg, client)
        cmd = cast(str, args.command)

        if cmd in ("check", "containers"):
            print(render_containers(await svc.containers()))

        elif cmd == "next":
            print(render_next(await svc.next(cast(float | None, args.hours))))

        elif cmd == "overdue":
            print(render_items(await svc.overdue(cast(bool, args.include_recurring))))

        elif cmd == "agenda":
            start = parse_datetime_input(cast(str, args.start), tz)
            end_raw = cast(str | None, args.end)
            end = parse_datetime_input(end_raw, tz) if end_raw else start + timedelta(days=1)
            items = await svc.agenda(
                start, end, cast(list[str] | None, args.container), Status(cast(str, args.status))
            )
            print(render_items(items))

        elif cmd == "tasks":
            items = await svc.tasks(
                cast(str | None, args.list_name), Status(cast(str, args.status))
            )
            print(render_items(items))

        elif cmd == "find":
            start_raw, end_raw = cast(str | None, args.start), cast(str | None, args.end)
            print(
                render_items(
                    await svc.find(
                        cast(str, args.query),
                        parse_datetime_input(start_raw, tz) if start_raw else None,
                        parse_datetime_input(end_raw, tz) if end_raw else None,
                    )
                )
            )

        elif cmd == "free":
            start = parse_datetime_input(cast(str, args.start), tz)
            end = parse_datetime_input(cast(str, args.end), tz)
            print(render_slots(await svc.free_slots(start, end, cast(int, args.min))))

        elif cmd == "add-task":
            due_raw = cast(str | None, args.due)
            item = await svc.add_task(
                cast(str, args.title),
                cast(str | None, args.list_name),
                parse_date_input(due_raw) if due_raw else None,
                cast(str | None, args.notes),
            )
            print(render_item(item))

        elif cmd == "add-event":
            all_day = cast(bool, args.all_day)
            if all_day:
                start = day_start(parse_date_input(cast(str, args.start)), tz)
                end = day_start(parse_date_input(cast(str, args.end)), tz)
            else:
                start = parse_datetime_input(cast(str, args.start), tz)
                end = parse_datetime_input(cast(str, args.end), tz)
            item = await svc.add_event(
                cast(str, args.title),
                start,
                end,
                cast(str | None, args.calendar),
                cast(str | None, args.notes),
                cast(str | None, args.location),
                all_day,
                cast(str | None, args.recurrence),
                cast(bool, args.tracked),
            )
            print(render_item(item))

        elif cmd == "set-status":
            print(
                render_item(
                    await svc.set_status(cast(str, args.id), Status(cast(str, args.status)))
                )
            )

        elif cmd == "delete":
            await svc.delete(cast(str, args.id), cast(Scope | None, args.scope))
            print(f"supprimé : {cast(str, args.id)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
