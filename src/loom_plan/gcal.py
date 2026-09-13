# SPDX-License-Identifier: Apache-2.0
"""Raw Google Calendar v3 calls. Returns Google payloads untouched; mapping lives elsewhere."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

from loom_plan.http import CALENDAR_BASE, GoogleClient, JsonObject


def _rfc3339(dt: datetime) -> str:
    return dt.isoformat()


class CalendarApi:
    def __init__(self, client: GoogleClient) -> None:
        self._c = client

    async def calendar_list(self) -> list[JsonObject]:
        return await self._paginate(f"{CALENDAR_BASE}/users/me/calendarList", {})

    async def list_events(
        self,
        calendar_id: str,
        *,
        time_min: datetime | None = None,
        time_max: datetime | None = None,
        query: str | None = None,
        show_deleted: bool = False,
    ) -> list[JsonObject]:
        params: dict[str, str] = {
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "250",
            "showDeleted": "true" if show_deleted else "false",
        }
        if time_min is not None:
            params["timeMin"] = _rfc3339(time_min)
        if time_max is not None:
            params["timeMax"] = _rfc3339(time_max)
        if query:
            params["q"] = query
        return await self._paginate(self._events_url(calendar_id), params)

    async def get_event(self, calendar_id: str, event_id: str) -> JsonObject:
        return await self._c.get(self._event_url(calendar_id, event_id))

    async def insert_event(self, calendar_id: str, body: JsonObject) -> JsonObject:
        return await self._c.post(self._events_url(calendar_id), body)

    async def patch_event(self, calendar_id: str, event_id: str, body: JsonObject) -> JsonObject:
        return await self._c.patch(self._event_url(calendar_id, event_id), body)

    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        await self._c.delete(self._event_url(calendar_id, event_id))

    # -- internals ---------------------------------------------------------------------

    @staticmethod
    def _events_url(calendar_id: str) -> str:
        return f"{CALENDAR_BASE}/calendars/{quote(calendar_id, safe='')}/events"

    @classmethod
    def _event_url(cls, calendar_id: str, event_id: str) -> str:
        return f"{cls._events_url(calendar_id)}/{quote(event_id, safe='')}"

    async def _paginate(self, url: str, params: dict[str, str]) -> list[JsonObject]:
        return await self._c.get_all_pages(url, params)
