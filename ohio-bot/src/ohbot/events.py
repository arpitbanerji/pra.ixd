"""Translate raw OpenHands conversation events into human-facing chat updates.

The agent server emits a stream of typed events. This module keeps the bot
agnostic of the wire format: everything downstream deals in plain strings.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# Tool -> short status line shown while the agent works.
_TOOL_LABELS: dict[str, str] = {
    "terminal": "💻 Running a command",
    "bash": "💻 Running a command",
    "execute_bash": "💻 Running a command",
    "file_editor": "📝 Editing files",
    "str_replace_editor": "📝 Editing files",
    "task_tracker": "🗂️ Updating the plan",
    "task": "🤖 Delegating to a sub-agent",
    "task_tool_set": "🤖 Delegating to a sub-agent",
    "browser": "🌐 Browsing the web",
    "browser_tool_set": "🌐 Browsing the web",
    "think": "💭 Thinking",
    "ask_oracle": "🔮 Consulting the oracle",
    "finish": "✅ Wrapping up",
}
_FINISH_TOOLS = frozenset({"finish", "finishtool"})
_AGENT_SOURCES = frozenset({"agent", "assistant"})


@dataclass(frozen=True, slots=True)
class AgentEvent:
    id: str
    kind: str
    source: str
    raw: Mapping[str, Any]


def parse_event(raw: Mapping[str, Any]) -> AgentEvent:
    return AgentEvent(
        id=str(raw.get("id") or ""),
        kind=str(raw.get("kind") or ""),
        source=str(raw.get("source") or ""),
        raw=raw,
    )


def _truncate(text: str, limit: int = 120) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def action_progress(event: AgentEvent) -> str | None:
    """A one-line status for an ActionEvent, or None if it isn't one."""
    if event.kind != "ActionEvent":
        return None
    tool = str(event.raw.get("tool_name") or "").lower()
    label = _TOOL_LABELS.get(tool)
    if label is None:
        for prefix, candidate in _TOOL_LABELS.items():
            if tool.startswith(prefix):
                label = candidate
                break
    if label is None:
        label = f"🔧 Using {tool or 'a tool'}"

    action = event.raw.get("action") or {}
    if not isinstance(action, Mapping):
        return label
    detail = action.get("command") or action.get("thought")
    if isinstance(detail, str) and detail.strip():
        return f"{label}\n`{_truncate(detail)}`"
    return label


def assistant_text(event: AgentEvent) -> str | None:
    """Extract the assistant's prose from a MessageEvent, if there is any."""
    if event.kind != "MessageEvent" or event.source not in _AGENT_SOURCES:
        return None
    if event.raw.get("tool_call_id"):
        # Intermediate tool-call messages are status noise, not answers.
        return None
    message = event.raw.get("llm_message") or event.raw.get("message") or {}
    if isinstance(message, str):
        return message
    if not isinstance(message, Mapping):
        return None
    parts: list[str] = []
    content = message.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
    joined = "\n".join(part for part in parts if part.strip())
    return joined or None


def finish_text(event: AgentEvent) -> str | None:
    """Text carried by a finish action, which is how agents often end a turn."""
    if event.kind != "ActionEvent":
        return None
    tool = str(event.raw.get("tool_name") or "").lower()
    if tool not in _FINISH_TOOLS:
        return None
    action = event.raw.get("action") or {}
    if isinstance(action, Mapping):
        for key in ("message", "final_thought", "text"):
            value = action.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None


def error_text(event: AgentEvent) -> str | None:
    if event.kind == "ConversationErrorEvent":
        detail = event.raw.get("detail") or event.raw.get("msg") or "Unknown error"
        code = event.raw.get("code") or "Error"
        return f"{code}: {detail}"
    if event.kind == "ErrorEvent":
        return str(event.raw.get("detail") or event.raw.get("msg") or "Unknown error")
    return None


def latest_answer(events: Iterable[AgentEvent]) -> str | None:
    """Best-effort final answer from a slice of events.

    Prefers an explicit finish message, then the last assistant message.
    """
    answer: str | None = None
    for event in events:
        answer = finish_text(event) or assistant_text(event) or answer
    return answer


def first_error(events: Iterable[AgentEvent]) -> str | None:
    for event in events:
        message = error_text(event)
        if message:
            return message
    return None


TERMINAL_STATUSES = frozenset({"idle", "finished", "error", "stuck"})


def is_terminal_status(status: str | None) -> bool:
    return (status or "") in TERMINAL_STATUSES
