# SPDX-License-Identifier: Apache-2.0
"""Server configuration (spec §9).

The config file path comes from ``LOOM_PLAN_CONFIG`` (default: ``loom-plan.toml`` in the
current directory). Relative paths inside the file are resolved against the file's own
directory, so the repo can be launched from anywhere.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

ENV_CONFIG = "LOOM_PLAN_CONFIG"
DEFAULT_CONFIG_NAME = "loom-plan.toml"

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


class ConfigError(RuntimeError):
    """Configuration file missing or invalid."""


@dataclass(frozen=True, slots=True)
class AuthConfig:
    client_secret: Path
    token: Path


@dataclass(frozen=True, slots=True)
class WorkConfig:
    start: time
    end: time
    days: frozenset[int]  # 0 = Monday … 6 = Sunday


@dataclass(frozen=True, slots=True)
class StatusPrefixes:
    done: str
    cancelled: str
    tracked: str  # on a recurring series: its occurrences are things to do (spec §4)


@dataclass(frozen=True, slots=True)
class Config:
    path: Path
    auth: AuthConfig
    timezone: ZoneInfo
    epoch: date
    primary_calendar: str
    default_task_list: str
    work: WorkConfig
    prefixes: StatusPrefixes
    notes_max_chars: int
    cache_ttl_seconds: float


def config_path() -> Path:
    return Path(os.environ.get(ENV_CONFIG, DEFAULT_CONFIG_NAME)).expanduser().resolve()


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.is_file():
        raise ConfigError(
            f"fichier de configuration introuvable : {path} "
            f"(définir {ENV_CONFIG} ou créer {DEFAULT_CONFIG_NAME})"
        )
    with path.open("rb") as fh:
        raw = cast(dict[str, object], tomllib.load(fh))
    return parse_config(raw, path)


def parse_config(raw: dict[str, object], path: Path) -> Config:
    base = path.parent
    auth = _table(raw, "auth", path)
    general = _table(raw, "general", path)
    work = _table(raw, "work", path)
    status = _table(raw, "status", path)

    tz_name = _str(general, "timezone", "Europe/Paris")
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:  # ZoneInfoNotFoundError or ValueError
        raise ConfigError(f"[general].timezone invalide : {tz_name!r}") from exc

    epoch_raw = general.get("epoch")
    if not isinstance(epoch_raw, date):
        raise ConfigError("[general].epoch est obligatoire (date TOML, ex. 2026-09-13)")

    primary = _str(general, "primary_calendar", "primary")
    default_list = general.get("default_task_list")
    if not isinstance(default_list, str) or not default_list:
        raise ConfigError("[general].default_task_list est obligatoire (nom ou id de la liste)")

    days_raw = work.get("days", ["mon", "tue", "wed", "thu", "fri"])
    if not isinstance(days_raw, list):
        raise ConfigError('[work].days doit être une liste (ex. ["mon", "tue"])')
    days: set[int] = set()
    for d in cast(list[object], days_raw):
        if not isinstance(d, str) or d.lower() not in _WEEKDAYS:
            raise ConfigError(f"[work].days : jour inconnu {d!r} (attendu mon…sun)")
        days.add(_WEEKDAYS[d.lower()])
    work_cfg = WorkConfig(
        start=_time(work, "start", time(9, 0)),
        end=_time(work, "end", time(18, 0)),
        days=frozenset(days),
    )
    if work_cfg.start >= work_cfg.end:
        raise ConfigError("[work].start doit précéder [work].end")

    return Config(
        path=path,
        auth=AuthConfig(
            client_secret=_path(auth, "client_secret", "client_secret.json", base),
            token=_path(auth, "token", "token.json", base),
        ),
        timezone=tz,
        epoch=epoch_raw,
        primary_calendar=primary,
        default_task_list=default_list,
        work=work_cfg,
        prefixes=StatusPrefixes(
            done=_str(status, "done_prefix", "[fait]"),
            cancelled=_str(status, "cancelled_prefix", "[annulé]"),
            tracked=_str(status, "tracked_prefix", "[suivi]"),
        ),
        notes_max_chars=_int(general, "notes_max_chars", 500),
        cache_ttl_seconds=float(_int(general, "cache_ttl_seconds", 30)),
    )


# -- helpers ---------------------------------------------------------------------------


def _table(raw: dict[str, object], name: str, path: Path) -> dict[str, object]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: la section [{name}] doit être une table")
    return cast(dict[str, object], value)


def _str(table: dict[str, object], key: str, default: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{key} doit être une chaîne")
    return value


def _int(table: dict[str, object], key: str, default: int) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} doit être un entier")
    return value


def _time(table: dict[str, object], key: str, default: time) -> time:
    value = table.get(key, default)
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        try:
            return time.fromisoformat(value)
        except ValueError as exc:
            raise ConfigError(f"[work].{key} : heure invalide {value!r} (ex. 09:00)") from exc
    raise ConfigError(f"[work].{key} doit être une heure (ex. 09:00)")


def _path(table: dict[str, object], key: str, default: str, base: Path) -> Path:
    value = _str(table, key, default)
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p).resolve()
