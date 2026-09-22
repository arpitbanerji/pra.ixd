"""Backend for a self-hosted OpenHands agent server (``X-Session-API-Key`` auth)."""

from __future__ import annotations

import asyncio
import logging

from ohbot.backends.base import BackendError, HttpBackend
from ohbot.events import AgentEvent, latest_answer

log = logging.getLogger(__name__)

_CONFIRMATION_KINDS = {"never": "NeverConfirm", "always": "AlwaysConfirm"}


class AgentServerBackend(HttpBackend):
    """Talks to ``/api/...`` on an agent server you run yourself."""

    name = "local"

    def __init__(
        self,
        *,
        base_url: str,
        session_api_key: str,
        workspace_dir: str,
        agent_profile_id: str | None = None,
        agent_override: dict | None = None,
        max_iterations: int = 200,
        confirmation_policy: str = "never",
        timeout: float = 60.0,
    ) -> None:
        super().__init__(
            base_url=base_url,
            headers={"X-Session-API-Key": session_api_key},
            timeout=timeout,
        )
        self.workspace_dir = workspace_dir
        self.max_iterations = max_iterations
        self.confirmation_policy = confirmation_policy
        self._agent_profile_id = agent_profile_id
        self._agent_override = agent_override
        self._profile_lock = asyncio.Lock()

    async def resolve_agent_profile_id(self) -> str | None:
        """Return the configured profile, else the server's active profile.

        The server rejects a create without one of ``agent`` / ``agent_settings``
        / ``agent_profile_id``, and reusing the active profile means the bot
        inherits whatever the operator already configured instead of embedding
        model credentials in the bot's own config.
        """
        if self._agent_profile_id:
            return self._agent_profile_id
        async with self._profile_lock:
            if not self._agent_profile_id:
                settings = await self._json("GET", "/api/settings")
                self._agent_profile_id = settings.get("active_agent_profile_id")
            return self._agent_profile_id

    async def create_conversation(self, initial_text: str) -> str:
        body: dict[str, object] = {
            "workspace": {"kind": "LocalWorkspace", "working_dir": self.workspace_dir},
            "max_iterations": self.max_iterations,
            "confirmation_policy": {
                "kind": _CONFIRMATION_KINDS.get(self.confirmation_policy, "NeverConfirm")
            },
            "initial_message": {
                "content": [{"type": "text", "text": initial_text}],
                "run": True,
            },
        }
        if self._agent_override:
            # Inline agent/LLM config (e.g. a self-hosted model endpoint) wins
            # over profile lookup, which also means no extra round trip.
            body["agent"] = self._agent_override
        else:
            profile_id = await self.resolve_agent_profile_id()
            if profile_id:
                body["agent_profile_id"] = profile_id
        data = await self._json("POST", "/api/conversations", json=body)
        conversation_id = data.get("id")
        if not conversation_id:
            raise BackendError(f"agent server did not return a conversation id: {data}")
        log.info("conversation_created", extra={"conversation_id": conversation_id})
        return str(conversation_id)

    async def send_message(self, conversation_id: str, text: str) -> None:
        await self._json(
            "POST",
            f"/api/conversations/{conversation_id}/events",
            json={
                "role": "user",
                "run": True,
                "content": [{"type": "text", "text": text}],
            },
        )

    async def get_status(self, conversation_id: str) -> str:
        data = await self._json("GET", f"/api/conversations/{conversation_id}")
        return str(data.get("execution_status") or "")

    async def get_events(self, conversation_id: str, limit: int = 60) -> list[AgentEvent]:
        return await self._events(f"/api/conversations/{conversation_id}/events/search", limit)

    async def get_final_response(self, conversation_id: str) -> str:
        data = await self._json(
            "GET", f"/api/conversations/{conversation_id}/agent_final_response"
        )
        text = str(data.get("response") or "").strip()
        if text:
            return text
        # No explicit finish message yet: fall back to the last assistant prose.
        return latest_answer(await self.get_events(conversation_id, limit=100)) or ""

    async def interrupt(self, conversation_id: str) -> None:
        await self._json("POST", f"/api/conversations/{conversation_id}/interrupt")

    async def respond_to_confirmation(
        self, conversation_id: str, *, accept: bool, reason: str | None = None
    ) -> None:
        await self._json(
            "POST",
            f"/api/conversations/{conversation_id}/events/respond_to_confirmation",
            json={"accept": accept, "reason": reason or "Responded from Telegram."},
        )
