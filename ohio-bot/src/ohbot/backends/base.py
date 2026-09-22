"""Backend interface for driving an OpenHands conversation."""

from __future__ import annotations

import abc
import asyncio
import logging
from collections.abc import Mapping

import httpx

from ohbot.events import AgentEvent, parse_event

log = logging.getLogger(__name__)

_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class BackendError(RuntimeError):
    """Raised when the OpenHands backend returns an unrecoverable error."""


class Backend(abc.ABC):
    """The operations the bot needs from an OpenHands deployment."""

    name: str = "backend"

    @abc.abstractmethod
    async def create_conversation(self, initial_text: str) -> str:
        """Start a fresh conversation seeded with ``initial_text``; return its id."""

    @abc.abstractmethod
    async def send_message(self, conversation_id: str, text: str) -> None:
        """Append a user message and run the agent loop."""

    @abc.abstractmethod
    async def get_status(self, conversation_id: str) -> str:
        """Return the conversation's ``execution_status``."""

    @abc.abstractmethod
    async def get_events(self, conversation_id: str, limit: int = 60) -> list[AgentEvent]:
        """Return recent events, oldest first."""

    @abc.abstractmethod
    async def get_final_response(self, conversation_id: str) -> str:
        """Return the agent's answer text for the most recent turn."""

    @abc.abstractmethod
    async def interrupt(self, conversation_id: str) -> None:
        """Ask the agent to stop the current run."""

    async def respond_to_confirmation(
        self, conversation_id: str, *, accept: bool, reason: str | None = None
    ) -> None:
        """Approve or reject a pending action. Only meaningful with confirmations on."""
        raise NotImplementedError(f"{self.name} backend does not support confirmations")

    @abc.abstractmethod
    async def aclose(self) -> None:
        """Release network resources."""


class HttpBackend(Backend):
    """Shared HTTP plumbing: one pooled client plus retry on transient failures.

    A Telegram turn can run for many minutes; a single dropped keep-alive
    connection must not abort it, hence the retry policy. Client errors (4xx)
    are surfaced immediately because retrying them cannot help.
    """

    def __init__(self, *, base_url: str, headers: Mapping[str, str], timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Accept": "application/json", **headers},
            timeout=httpx.Timeout(timeout, connect=15.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self, method: str, path: str, *, attempts: int = 3, **kwargs: object
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.request(method, path, **kwargs)  # type: ignore[arg-type]
            except httpx.TransportError as exc:  # DNS, connect, read timeouts, resets
                last_error = exc
                log.warning("backend_transport_error", extra={"path": path, "attempt": attempt})
            else:
                if response.status_code not in _RETRY_STATUS:
                    if response.status_code >= 400:
                        raise BackendError(
                            f"{method} {path} -> {response.status_code}: {response.text[:400]}"
                        )
                    return response
                last_error = BackendError(
                    f"{method} {path} -> {response.status_code}: {response.text[:200]}"
                )
                log.warning(
                    "backend_retryable_status",
                    extra={"path": path, "status": response.status_code, "attempt": attempt},
                )
            if attempt < attempts:
                await asyncio.sleep(min(2 ** attempt, 8))
        raise BackendError(f"{method} {path} failed after {attempts} attempts: {last_error}")

    async def _json(self, method: str, path: str, **kwargs: object) -> dict:
        response = await self._request(method, path, **kwargs)
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise BackendError(f"{method} {path} returned non-JSON body") from exc
        return payload if isinstance(payload, dict) else {"items": payload}

    async def _events(self, path: str, limit: int) -> list[AgentEvent]:
        payload = await self._json("GET", path, params={"limit": limit, "sort_order": "TIMESTAMP_DESC"})
        items = payload.get("items") or []
        # Requested newest-first for a cheap "latest" query; return oldest-first.
        return [parse_event(item) for item in reversed(items)]
