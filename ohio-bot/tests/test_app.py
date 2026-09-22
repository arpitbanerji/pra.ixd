"""Tests for bot wiring that do not require a live Telegram connection."""

import asyncio

from support import ScriptedBackend
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

from ohbot.app import Bridge, build_application, serve_health
from ohbot.config import Settings
from ohbot.store import Store


def make_settings(**overrides) -> Settings:
    base = {
        "telegram_bot_token": "123:abc",
        "telegram_allowed_user_ids": [1],
        "openhands_session_api_key": "key",
        "poll_interval_seconds": 0.01,
    }
    return Settings(_env_file=None, **{**base, **overrides})


async def test_health_endpoint_reports_ok():
    server = await serve_health("127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(1024), timeout=5)
        writer.close()
        assert b"200 OK" in response
        assert b'"status": "ok"' in response
    finally:
        server.close()
        await server.wait_closed()


async def test_expected_handlers_are_registered(tmp_path):
    async with Store(tmp_path / "bot.sqlite3") as store:
        bridge = Bridge(make_settings(), ScriptedBackend(statuses=["finished"]), store)
        application = build_application(make_settings(), bridge)

        handlers = [handler for group in application.handlers.values() for handler in group]
        commands: set[str] = set()
        for handler in handlers:
            if isinstance(handler, CommandHandler):
                commands.update(handler.commands)

        assert {"start", "help", "id", "new", "status", "stop", "resume"} <= commands
        assert any(isinstance(handler, CallbackQueryHandler) for handler in handlers)
        assert any(isinstance(handler, MessageHandler) for handler in handlers)


async def test_bridge_binds_the_surface_and_runner(tmp_path):
    async with Store(tmp_path / "bot.sqlite3") as store:
        bridge = Bridge(make_settings(), ScriptedBackend(statuses=["finished"]), store)
        assert bridge.surface is None
        application = build_application(make_settings(), bridge)
        bridge.bind(application)
        assert bridge.surface is not None
        assert bridge.runner is not None
        assert bridge.runner.surface is bridge.surface
