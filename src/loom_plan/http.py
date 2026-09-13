# SPDX-License-Identifier: Apache-2.0
"""Thin async HTTP layer over the Google REST APIs.

Adds the bearer token, retries exactly once after a 401 (token refreshed under lock, spec §7
« double instance »), and turns Google error payloads into plain-language ``GoogleApiError``.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import httpx

from loom_plan.auth import TokenStore

CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"
TASKS_BASE = "https://tasks.googleapis.com/tasks/v1"

JsonObject = dict[str, Any]


class GoogleApiError(RuntimeError):
    """Plain-language error for Claude; ``status`` is 0 when Google could not be reached."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Google API {status} : {message}" if status else message)
        self.status = status
        self.message = message


class GoogleClient:
    def __init__(self, tokens: TokenStore, *, timeout: float = 15.0) -> None:
        self._tokens = tokens
        self._http = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> GoogleClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def get(self, url: str, params: dict[str, str] | None = None) -> JsonObject:
        return await self._request("GET", url, params=params)

    async def post(self, url: str, body: JsonObject) -> JsonObject:
        return await self._request("POST", url, json=body)

    async def patch(self, url: str, body: JsonObject) -> JsonObject:
        return await self._request("PATCH", url, json=body)

    async def delete(self, url: str) -> None:
        await self._request("DELETE", url)

    async def get_all_pages(self, url: str, params: dict[str, str]) -> list[JsonObject]:
        """Follow ``nextPageToken`` and concatenate every page's ``items``."""
        out: list[JsonObject] = []
        token: str | None = None
        while True:
            page_params = dict(params)
            if token:
                page_params["pageToken"] = token
            page = await self.get(url, page_params)
            items = page.get("items", [])
            if isinstance(items, list):
                out.extend(
                    cast(JsonObject, i) for i in cast(list[object], items) if isinstance(i, dict)
                )
            next_token = page.get("nextPageToken")
            if not isinstance(next_token, str) or not next_token:
                return out
            token = next_token

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json: JsonObject | None = None,
    ) -> JsonObject:
        for attempt in (1, 2):
            token = await asyncio.to_thread(self._tokens.access_token)
            try:
                resp = await self._http.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                raise GoogleApiError(
                    0, f"Google injoignable ({type(exc).__name__}: {exc})"
                ) from exc
            if resp.status_code == 401 and attempt == 1:
                self._tokens.invalidate()
                continue
            if resp.status_code >= 400:
                raise GoogleApiError(resp.status_code, _error_message(resp))
            if resp.status_code == 204 or not resp.content:
                return {}
            data: object = resp.json()
            return cast(JsonObject, data) if isinstance(data, dict) else {"value": data}
        raise AssertionError("unreachable")


def _error_message(resp: httpx.Response) -> str:
    try:
        payload: object = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(payload, dict):
        err = cast(dict[str, object], payload).get("error")
        if isinstance(err, dict):
            msg = cast(dict[str, object], err).get("message")
            if isinstance(msg, str):
                return msg
    return resp.text[:300]
