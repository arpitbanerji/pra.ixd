"""End-to-end test against a real OpenHands agent server.

Run with::

    OPENHANDS_LIVE_TEST=1 \
    OPENHANDS_SESSION_API_KEY=$(cat ~/.openhands/agent-canvas/api-key.txt) \
    python -m pytest -m live -v

A local OpenAI-compatible stub stands in for the model provider, so the entire
agent loop (conversation creation, tool selection, finish action, event stream)
executes for real without needing an external API key.
"""

import os

import pytest
from support import RecordingSurface, StubLLM

from ohbot.backends.agent_server import AgentServerBackend
from ohbot.config import Settings
from ohbot.store import Store
from ohbot.worker import ConversationRunner

pytestmark = pytest.mark.live

BASE_URL = os.environ.get("OPENHANDS_BASE_URL", "http://127.0.0.1:18000")
SESSION_KEY = os.environ.get("OPENHANDS_SESSION_API_KEY", "")

requires_server = pytest.mark.skipif(
    os.environ.get("OPENHANDS_LIVE_TEST") != "1" or not SESSION_KEY,
    reason="set OPENHANDS_LIVE_TEST=1 and OPENHANDS_SESSION_API_KEY to run live tests",
)


def make_backend(llm: StubLLM, workspace: str) -> AgentServerBackend:
    return AgentServerBackend(
        base_url=BASE_URL,
        session_api_key=SESSION_KEY,
        workspace_dir=workspace,
        agent_override={
            "kind": "Agent",
            "llm": {
                "model": "openai/stub-model",
                "api_key": "stub",
                "base_url": llm.base_url,
            },
        },
        max_iterations=3,
    )


def make_settings() -> Settings:
    return Settings(
        _env_file=None,
        telegram_bot_token="live-test",
        telegram_allowed_user_ids=[1],
        openhands_session_api_key=SESSION_KEY,
        poll_interval_seconds=0.5,
        run_timeout_seconds=180,
        stream_progress=True,
    )


@requires_server
async def test_worker_delivers_a_real_agent_answer(tmp_path):
    llm = StubLLM(reply="INTEGRATION-OK")
    backend = make_backend(llm, str(tmp_path))
    surface = RecordingSurface()
    try:
        async with Store(tmp_path / "bot.sqlite3") as store:
            runner = ConversationRunner(
                backend=backend, store=store, surface=surface, settings=make_settings()
            )
            await runner.handle_prompt(1, "Say the integration word.")
            assert "INTEGRATION-OK" in surface.last_delivery
            session = await store.get_session(1)
            assert session is not None and session.conversation_id
    finally:
        await backend.aclose()
        llm.close()


@requires_server
async def test_provider_failures_reach_the_user(tmp_path):
    llm = StubLLM(fail_with=403)
    backend = make_backend(llm, str(tmp_path))
    surface = RecordingSurface()
    try:
        async with Store(tmp_path / "bot.sqlite3") as store:
            runner = ConversationRunner(
                backend=backend, store=store, surface=surface, settings=make_settings()
            )
            await runner.handle_prompt(1, "This will fail at the provider.")
            delivered = surface.last_delivery.lower()
            assert "error" in delivered
            assert "403" in delivered or "provider" in delivered
    finally:
        await backend.aclose()
        llm.close()
