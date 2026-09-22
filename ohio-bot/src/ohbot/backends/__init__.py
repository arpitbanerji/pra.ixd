"""Backend selection."""

from __future__ import annotations

from ohbot.backends.agent_server import AgentServerBackend
from ohbot.backends.base import Backend, BackendError
from ohbot.backends.cloud import CloudBackend
from ohbot.config import BackendMode, Settings


def build_backend(settings: Settings) -> Backend:
    if settings.openhands_mode is BackendMode.CLOUD:
        return CloudBackend(
            api_key=settings.openhands_cloud_api_key or "",
            base_url=settings.openhands_base_url,
            workspace_dir=settings.openhands_workspace_dir,
            max_iterations=settings.openhands_max_iterations,
            confirmation_policy=settings.openhands_confirmation_policy.value,
            timeout=settings.openhands_request_timeout,
        )
    return AgentServerBackend(
        base_url=settings.openhands_base_url,
        session_api_key=settings.openhands_session_api_key or "",
        workspace_dir=settings.openhands_workspace_dir,
        agent_profile_id=settings.openhands_agent_profile_id,
        agent_override=settings.agent_override(),
        max_iterations=settings.openhands_max_iterations,
        confirmation_policy=settings.openhands_confirmation_policy.value,
        timeout=settings.openhands_request_timeout,
    )


__all__ = ["Backend", "BackendError", "AgentServerBackend", "CloudBackend", "build_backend"]
