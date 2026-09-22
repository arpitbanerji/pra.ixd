"""Backend for OpenHands Cloud (``app.all-hands.dev``, Bearer auth).

Cloud conversations are provisioned through the app API, which hands back the
agent-server URL and per-conversation session key for the sandbox that hosts
them. After provisioning, the conversation is driven with the exact same
agent-server calls used in local mode.
"""

from __future__ import annotations

import asyncio
import logging

from ohbot.backends.agent_server import AgentServerBackend
from ohbot.backends.base import BackendError, HttpBackend
from ohbot.events import AgentEvent

log = logging.getLogger(__name__)

_READY_STATES = {"READY", "COMPLETED", "SUCCEEDED"}


class CloudBackend(HttpBackend):
    name = "cloud"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://app.all-hands.dev",
        workspace_dir: str = "/workspace",
        max_iterations: int = 200,
        confirmation_policy: str = "never",
        timeout: float = 60.0,
        provision_timeout: float = 300.0,
    ) -> None:
        super().__init__(
            base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout
        )
        self.workspace_dir = workspace_dir
        self.max_iterations = max_iterations
        self.confirmation_policy = confirmation_policy
        self.provision_timeout = provision_timeout
        self._routes: dict[str, AgentServerBackend] = {}
        self._lock = asyncio.Lock()

    async def _route(self, conversation_id: str) -> AgentServerBackend:
        """Return the agent-server client for a conversation, provisioning if needed."""
        async with self._lock:
            if conversation_id in self._routes:
                return self._routes[conversation_id]
            record = await self._json(
                "GET", "/api/v1/app-conversations", params={"ids": conversation_id}
            )
            item = _first_item(record)
            session_key = item.get("session_api_key")
            conversation_url = item.get("conversation_url") or ""
            if not session_key or "/api/conversations" not in conversation_url:
                raise BackendError(
                    "Cloud response did not include an agent server URL and session key. "
                    f"Keys present: {sorted(item)[:20]}"
                )
            agent_server_url = conversation_url.rsplit("/api/conversations", 1)[0]
            backend = AgentServerBackend(
                base_url=agent_server_url,
                session_api_key=str(session_key),
                workspace_dir=self.workspace_dir,
                max_iterations=self.max_iterations,
                confirmation_policy=self.confirmation_policy,
                timeout=self._client.timeout.read or 60.0,
            )
            self._routes[conversation_id] = backend
            return backend

    async def create_conversation(self, initial_text: str) -> str:
        start = await self._json(
            "POST",
            "/api/v1/app-conversations",
            json={
                "initial_message": {"content": [{"type": "text", "text": initial_text}]},
            },
        )
        conversation_id = start.get("app_conversation_id")
        if not conversation_id:
            conversation_id = await self._await_start_task(str(start.get("id") or ""))
        conversation_id = str(conversation_id)
        log.info("cloud_conversation_created", extra={"conversation_id": conversation_id})
        return conversation_id

    async def _await_start_task(self, start_task_id: str) -> str:
        if not start_task_id:
            raise BackendError("Cloud did not return a start task id or app_conversation_id")
        deadline = asyncio.get_running_loop().time() + self.provision_timeout
        while asyncio.get_running_loop().time() < deadline:
            payload = await self._json(
                "GET", "/api/v1/app-conversations/start-tasks", params={"ids": start_task_id}
            )
            task = _first_item(payload)
            if task.get("app_conversation_id"):
                return str(task["app_conversation_id"])
            if str(task.get("status", "")).upper() in {"ERROR", "FAILED"}:
                raise BackendError(f"Cloud conversation failed to start: {task.get('detail')}")
            await asyncio.sleep(3)
        raise BackendError("Timed out waiting for the cloud conversation to start")

    async def send_message(self, conversation_id: str, text: str) -> None:
        await (await self._route(conversation_id)).send_message(conversation_id, text)

    async def get_status(self, conversation_id: str) -> str:
        return await (await self._route(conversation_id)).get_status(conversation_id)

    async def get_events(self, conversation_id: str, limit: int = 60) -> list[AgentEvent]:
        return await (await self._route(conversation_id)).get_events(conversation_id, limit)

    async def get_final_response(self, conversation_id: str) -> str:
        return await (await self._route(conversation_id)).get_final_response(conversation_id)

    async def interrupt(self, conversation_id: str) -> None:
        await (await self._route(conversation_id)).interrupt(conversation_id)

    async def respond_to_confirmation(
        self, conversation_id: str, *, accept: bool, reason: str | None = None
    ) -> None:
        await (await self._route(conversation_id)).respond_to_confirmation(
            conversation_id, accept=accept, reason=reason
        )

    async def aclose(self) -> None:
        for backend in self._routes.values():
            await backend.aclose()
        self._routes.clear()
        await super().aclose()


def _first_item(payload: dict) -> dict:
    for key in ("items", "results", "data"):
        value = payload.get(key)
        if isinstance(value, list) and value:
            return value[0]
    return payload if isinstance(payload, dict) else {}
