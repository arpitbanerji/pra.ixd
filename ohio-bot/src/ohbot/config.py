"""Configuration for the Telegram <-> OpenHands bridge.

All settings come from environment variables (or a local ``.env`` file).
Validation happens once at startup so the process fails fast on misconfiguration
instead of failing mid-conversation.
"""

from __future__ import annotations

import json
import logging
from enum import Enum
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

log = logging.getLogger(__name__)


class BackendMode(str, Enum):
    """Which OpenHands deployment the bot drives."""

    LOCAL = "local"  # an agent-server you run yourself (X-Session-API-Key auth)
    CLOUD = "cloud"  # OpenHands Cloud app server (Bearer auth)


class ConfirmationPolicy(str, Enum):
    NEVER = "never"  # agent runs fully autonomously
    ALWAYS = "always"  # agent pauses for approval; surfaced as Telegram buttons


def _split_ints(value: object) -> list[int]:
    """Parse an allowlist from the shapes an env var actually arrives in.

    The field is annotated ``NoDecode`` so pydantic-settings hands us the raw
    string. Without that, its env source calls ``json.loads`` on the value
    *before* validators run, which rejects the two most natural inputs:
    an empty value (``json.loads("")``) and a comma-separated list
    (``json.loads("1,2")``). Both raised SettingsError at startup.
    """
    if value is None or value == "":
        return []
    if isinstance(value, int):  # a single id, e.g. TELEGRAM_ALLOWED_USER_IDS=123
        return [value]
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):  # a JSON list, e.g. "[1, 2]"
            try:
                return [int(part) for part in json.loads(text)]
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"TELEGRAM_ALLOWED_USER_IDS is not a valid list: {value!r}"
                ) from exc
        parts = [part for part in text.replace(" ", "").split(",") if part]
        try:
            return [int(part) for part in parts]
        except ValueError as exc:
            raise ValueError(
                f"TELEGRAM_ALLOWED_USER_IDS must be comma-separated integers, got {value!r}"
            ) from exc
    return list(value)  # already a sequence (e.g. a JSON list or init kwarg)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # ---- Telegram -------------------------------------------------------
    telegram_bot_token: str = Field(..., description="From @BotFather")
    telegram_allowed_user_ids: Annotated[list[int], NoDecode] = Field(
        default_factory=list,
        description=(
            "Allowlist of Telegram user IDs permitted to drive the agent. "
            "Accepts '1,2,3' or '[1, 2, 3]', or empty for none."
        ),
    )
    telegram_allow_all: bool = Field(
        default=False,
        description="Explicit opt-in to serve every Telegram user. Dangerous; off by default.",
    )
    telegram_proxy: str | None = Field(
        default=None, description="Optional proxy URL, e.g. socks5://host:1080"
    )

    # ---- OpenHands backend ---------------------------------------------
    openhands_mode: BackendMode = BackendMode.LOCAL
    openhands_base_url: str = "http://host.docker.internal:18000"
    openhands_session_api_key: str | None = None
    openhands_cloud_api_key: str | None = None
    openhands_agent_profile_id: str | None = Field(
        default=None,
        description="Agent profile UUID. Auto-detected from /api/settings when omitted.",
    )
    openhands_agent_json: str | None = Field(
        default=None,
        description=(
            "Advanced: inline JSON agent config sent to POST /api/conversations, e.g. "
            '{"kind":"Agent","llm":{"model":"openai/x","api_key":"y","base_url":"z"}}. '
            "Takes precedence over OPENHANDS_AGENT_PROFILE_ID."
        ),
    )
    openhands_workspace_dir: str = "/workspace"
    openhands_max_iterations: int = 200
    openhands_confirmation_policy: ConfirmationPolicy = ConfirmationPolicy.NEVER
    openhands_request_timeout: float = 60.0

    # ---- Conversation lifecycle ----------------------------------------
    poll_interval_seconds: float = 2.0
    run_timeout_seconds: float = 1800.0
    stream_progress: bool = True
    progress_edit_interval_seconds: float = 3.0

    # ---- Storage / ops --------------------------------------------------
    db_path: Path = Path("/data/ohbot.sqlite3")
    log_level: str = "INFO"
    log_json: bool = True
    health_host: str = "0.0.0.0"
    health_port: int = 8080

    @field_validator("telegram_allowed_user_ids", mode="before")
    @classmethod
    def _parse_ids(cls, value: object) -> object:
        return _split_ints(value)

    @field_validator("openhands_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        if self.openhands_mode is BackendMode.LOCAL and not self.openhands_session_api_key:
            raise ValueError(
                "OPENHANDS_SESSION_API_KEY is required in local mode "
                "(read it from the agent server's ~/.openhands/agent-canvas/api-key.txt)"
            )
        if self.openhands_mode is BackendMode.CLOUD and not self.openhands_cloud_api_key:
            raise ValueError("OPENHANDS_CLOUD_API_KEY is required in cloud mode")
        if not self.telegram_allowed_user_ids and not self.telegram_allow_all:
            # Deliberately not fatal. An empty allowlist still grants nobody
            # access, and refusing to start would make bootstrapping impossible:
            # a user cannot ask the bot for their own id if the bot cannot run.
            log.warning(
                "no_allowlist_configured",
                extra={
                    "detail": (
                        "TELEGRAM_ALLOWED_USER_IDS is empty: every request will be "
                        "rejected. Message the bot to learn your numeric id, then set it."
                    )
                },
            )
        return self

    @property
    def allowlist_configured(self) -> bool:
        return bool(self.telegram_allowed_user_ids) or self.telegram_allow_all

    def is_authorized(self, user_id: int) -> bool:
        if self.telegram_allow_all:
            return True
        return user_id in self.telegram_allowed_user_ids

    def agent_override(self) -> dict | None:
        """Parse ``OPENHANDS_AGENT_JSON`` into the dict the API expects."""
        if not self.openhands_agent_json:
            return None
        try:
            parsed = json.loads(self.openhands_agent_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"OPENHANDS_AGENT_JSON is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("OPENHANDS_AGENT_JSON must be a JSON object")
        return parsed
