"""Shared test helpers.

``StubLLM`` is a real HTTP server speaking the OpenAI chat-completions API, so
integration tests exercise the entire agent loop without needing a paid model.

``ScriptedBackend`` and ``RecordingSurface`` are deliberately small doubles: the
agent server's terminal states (timeouts, mid-run errors, idle-before-start) and
the Telegram API cannot be produced on demand, so the polling edge cases are
tested deterministically here while ``tests/test_integration_live.py`` covers the
real HTTP path.
"""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Sequence

from ohbot.backends.base import Backend
from ohbot.events import AgentEvent

SHORT_POLL_SETTINGS = {
    "telegram_bot_token": "test-token",
    "telegram_allowed_user_ids": [1],
    "openhands_session_api_key": "test-key",
    "poll_interval_seconds": 0.01,
    "progress_edit_interval_seconds": 0.0,
    "run_timeout_seconds": 5.0,
    "stream_progress": False,
}


class StubLLM:
    """OpenAI-compatible endpoint that always answers by calling the finish tool."""

    def __init__(self, reply: str = "STUB-ANSWER", fail_with: int | None = None) -> None:
        self.reply = reply
        self.fail_with = fail_with
        self.requests: list[dict] = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # /v1/models
                self._send(200, {"object": "list", "data": [{"id": "stub-model"}]})

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                owner.requests.append(payload)
                if owner.fail_with:
                    self._send(owner.fail_with, {"error": {"message": "stub provider failure"}})
                    return
                finish = "finish"
                for tool in payload.get("tools") or []:
                    name = (tool.get("function") or {}).get("name", "")
                    if name.lower() in ("finish", "finishtool"):
                        finish = name
                self._send(
                    200,
                    {
                        "id": "chatcmpl-stub",
                        "object": "chat.completion",
                        "created": 0,
                        "model": payload.get("model", "stub-model"),
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "tool_calls",
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call_1",
                                            "type": "function",
                                            "function": {
                                                "name": finish,
                                                "arguments": json.dumps({"message": owner.reply}),
                                            },
                                        }
                                    ],
                                },
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "total_tokens": 2,
                        },
                    },
                )

            def log_message(self, *args: object) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class ScriptedBackend(Backend):
    """Returns a canned status sequence and event list."""

    name = "scripted"

    def __init__(
        self,
        *,
        statuses: Sequence[str],
        events: Sequence[AgentEvent] = (),
        events_reveal_after: int = 0,
        final_response: str = "",
        conversation_id: str = "conv-scripted",
    ) -> None:
        self.statuses = list(statuses)
        self.events = list(events)
        self.events_reveal_after = events_reveal_after
        self.final_response = final_response
        self.conversation_id = conversation_id
        self.created: list[str] = []
        self.sent: list[tuple[str, str]] = []
        self.interrupts = 0
        self.final_response_calls = 0
        self._index = 0
        self._event_calls = 0

    async def create_conversation(self, initial_text: str) -> str:
        self.created.append(initial_text)
        return self.conversation_id

    async def send_message(self, conversation_id: str, text: str) -> None:
        self.sent.append((conversation_id, text))

    async def get_status(self, conversation_id: str) -> str:
        status = self.statuses[min(self._index, len(self.statuses) - 1)]
        self._index += 1
        return status

    async def get_events(self, conversation_id: str, limit: int = 60) -> list[AgentEvent]:
        self._event_calls += 1
        if self._event_calls <= self.events_reveal_after:
            return []
        return list(self.events)

    async def get_final_response(self, conversation_id: str) -> str:
        self.final_response_calls += 1
        return self.final_response

    async def interrupt(self, conversation_id: str) -> None:
        self.interrupts += 1

    async def aclose(self) -> None:
        pass


class RecordingSurface:
    """In-memory stand-in for :class:`ohbot.surface.TelegramSurface`."""

    def __init__(self) -> None:
        self._next_id = 100
        self.sent: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str, object]] = []
        self.replacements: list[tuple[int, int, str]] = []
        self.typing_calls = 0

    async def send_markdown(self, chat_id: int, markdown: str, *, buttons=None) -> list[int]:
        message_id = self._next_id
        self._next_id += 1
        self.sent.append((chat_id, markdown))
        return [message_id]

    async def edit_markdown(
        self, chat_id: int, message_id: int, markdown: str, *, buttons=None
    ) -> None:
        self.edits.append((chat_id, message_id, markdown, buttons))

    async def replace_with_markdown(
        self, chat_id: int, message_id: int, markdown: str, *, buttons=None
    ) -> list[int]:
        self.replacements.append((chat_id, message_id, markdown))
        return [message_id]

    async def send_typing(self, chat_id: int) -> None:
        self.typing_calls += 1

    @property
    def last_delivery(self) -> str:
        if self.replacements:
            return self.replacements[-1][2]
        if self.sent:
            return self.sent[-1][1]
        return ""
