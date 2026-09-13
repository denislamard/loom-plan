# SPDX-License-Identifier: Apache-2.0
"""Raw Google Tasks v1 calls. Returns Google payloads untouched; mapping lives elsewhere."""

from __future__ import annotations

from urllib.parse import quote

from loom_plan.http import TASKS_BASE, GoogleClient, JsonObject


class TasksApi:
    def __init__(self, client: GoogleClient) -> None:
        self._c = client

    async def task_lists(self) -> list[JsonObject]:
        return await self._paginate(f"{TASKS_BASE}/users/@me/lists", {"maxResults": "100"})

    async def list_tasks(self, list_id: str, *, include_completed: bool) -> list[JsonObject]:
        params: dict[str, str] = {
            "maxResults": "100",
            "showCompleted": "true" if include_completed else "false",
            "showHidden": "true" if include_completed else "false",
        }
        return await self._paginate(self._tasks_url(list_id), params)

    async def get_task(self, list_id: str, task_id: str) -> JsonObject:
        return await self._c.get(self._task_url(list_id, task_id))

    async def insert_task(self, list_id: str, body: JsonObject) -> JsonObject:
        return await self._c.post(self._tasks_url(list_id), body)

    async def patch_task(self, list_id: str, task_id: str, body: JsonObject) -> JsonObject:
        return await self._c.patch(self._task_url(list_id, task_id), body)

    async def move_task(self, list_id: str, task_id: str, destination_list_id: str) -> JsonObject:
        url = f"{self._task_url(list_id, task_id)}/move"
        url += f"?destinationTasklist={quote(destination_list_id, safe='')}"
        return await self._c.post(url, {})

    async def delete_task(self, list_id: str, task_id: str) -> None:
        await self._c.delete(self._task_url(list_id, task_id))

    # -- internals ---------------------------------------------------------------------

    @staticmethod
    def _tasks_url(list_id: str) -> str:
        return f"{TASKS_BASE}/lists/{quote(list_id, safe='')}/tasks"

    @classmethod
    def _task_url(cls, list_id: str, task_id: str) -> str:
        return f"{cls._tasks_url(list_id)}/{quote(task_id, safe='')}"

    async def _paginate(self, url: str, params: dict[str, str]) -> list[JsonObject]:
        return await self._c.get_all_pages(url, params)
