"""Drive one OpenHands turn per Telegram chat and stream progress back.

Completion detection is the subtle part. The agent server reports ``idle`` both
*before* a run starts and *after* it finishes, so a bare ``status == idle``
check would either cut a run short or hang forever. We therefore treat a turn as
finished when the status is unambiguously terminal, or when ``idle`` coincides
with evidence that work already happened (a observed ``running``, agent-side
events, or a ready answer).
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from ohbot.backends.base import Backend, BackendError
from ohbot.config import Settings
from ohbot.events import (
    AgentEvent,
    action_progress,
    first_error,
    latest_answer,
)
from ohbot.store import Store
from ohbot.surface import ChatSurface

log = logging.getLogger(__name__)

_TYPING_INTERVAL = 5.0
_HARD_TERMINAL = frozenset({"finished", "error", "stuck"})
# How long an "idle" conversation is tolerated before we assume the run never
# started (e.g. the server dropped it) rather than waiting forever.
_IDLE_START_GRACE_SECONDS = 30.0

T = TypeVar("T")


@dataclass(slots=True)
class TurnOutcome:
    text: str = ""
    status: str = "unknown"
    error: str | None = None
    needs_confirmation: bool = False


class SerialExecutor:
    """Runs coroutines one-at-a-time per key (here: per Telegram chat).

    Two prompts arriving for the same chat must not interleave — the agent
    server has a single conversation state per chat.
    """

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._tasks: set[asyncio.Task] = set()

    def submit(self, key: int, factory: Callable[[], Awaitable[None]]) -> asyncio.Task:
        async def runner() -> None:
            async with self._locks[key]:
                await factory()

        task = asyncio.create_task(runner())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


class ConversationRunner:
    def __init__(
        self,
        *,
        backend: Backend,
        store: Store,
        surface: ChatSurface,
        settings: Settings,
    ) -> None:
        self.backend = backend
        self.store = store
        self.surface = surface
        self.settings = settings

    async def handle_prompt(self, chat_id: int, text: str) -> None:
        session = await self.store.get_session(chat_id)
        placeholder = (
            await self.surface.send_markdown(chat_id, "⏳ *Talking to OpenHands…*")
        )[0]

        try:
            baseline = (
                set()
                if session is None
                else {event.id for event in await self.backend.get_events(session.conversation_id)}
            )
            if session is None:
                conversation_id = await self.backend.create_conversation(text)
                await self.store.set_session(chat_id, conversation_id, self.backend.name)
            else:
                conversation_id = session.conversation_id
                await self.backend.send_message(conversation_id, text)
        except BackendError as exc:
            log.error("prompt_dispatch_failed", extra={"error": str(exc)})
            await self.surface.edit_markdown(
                chat_id, placeholder, f"⚠️ Could not reach OpenHands: `{exc}`"
            )
            return

        outcome = await self._poll(chat_id, conversation_id, placeholder, baseline)
        await self._deliver(chat_id, placeholder, outcome)

    async def _poll(
        self,
        chat_id: int,
        conversation_id: str,
        placeholder: int,
        baseline: set[str],
    ) -> TurnOutcome:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.run_timeout_seconds
        seen = set(baseline)
        turn_events: list[AgentEvent] = []
        progress: deque[str] = deque(maxlen=6)
        saw_running = False
        idle_since: float | None = None
        last_edit = 0.0
        last_typing = 0.0
        dirty = False

        while True:
            now = loop.time()
            if now >= deadline:
                return TurnOutcome(
                    text=latest_answer(turn_events) or "",
                    status="timeout",
                    error=f"Timed out after {int(self.settings.run_timeout_seconds)}s.",
                )

            if now - last_typing >= _TYPING_INTERVAL:
                await self.surface.send_typing(chat_id)
                last_typing = now

            try:
                events = await self.backend.get_events(conversation_id)
                status = await self.backend.get_status(conversation_id)
            except BackendError as exc:
                log.warning("poll_failed", extra={"error": str(exc)})
                await asyncio.sleep(self.settings.poll_interval_seconds)
                continue

            for event in events:
                if event.id in seen:
                    continue
                seen.add(event.id)
                turn_events.append(event)
                line = action_progress(event)
                if line:
                    progress.append(line)
                    dirty = True

            if status == "running":
                saw_running = True
                idle_since = None
            elif status == "idle":
                if idle_since is None:
                    idle_since = now
            elif status == "waiting_for_confirmation":
                return TurnOutcome(
                    text="\n".join(progress),
                    status=status,
                    needs_confirmation=True,
                )
            elif status in _HARD_TERMINAL:
                return await self._finish(conversation_id, turn_events, status)

            if self.settings.stream_progress and dirty and now - last_edit >= self.settings.progress_edit_interval_seconds:
                await self.surface.edit_markdown(
                    chat_id, placeholder, "🤖 *Working…*\n\n" + "\n\n".join(progress)
                )
                last_edit = now
                dirty = False

            if status == "idle":
                settled = (
                    saw_running
                    or any(event.source == "agent" for event in turn_events)
                    or (now - idle_since) >= _IDLE_START_GRACE_SECONDS
                )
                if settled:
                    return await self._finish(conversation_id, turn_events, "idle")

            await asyncio.sleep(self.settings.poll_interval_seconds)

    async def _finish(
        self, conversation_id: str, turn_events: list[AgentEvent], status: str
    ) -> TurnOutcome:
        """Prefer the server's explicit final response; fall back to event text."""
        try:
            answer = await self.backend.get_final_response(conversation_id)
        except BackendError as exc:
            log.warning("final_response_failed", extra={"error": str(exc)})
            answer = ""
        return TurnOutcome(
            text=answer or latest_answer(turn_events) or "",
            status=status,
            error=first_error(turn_events),
        )

    async def _deliver(self, chat_id: int, placeholder: int, outcome: TurnOutcome) -> None:
        if outcome.needs_confirmation:
            await self.surface.edit_markdown(
                chat_id,
                placeholder,
                "🛑 *Approval needed*\n\n"
                + (outcome.text or "The agent is waiting for you.")
                + "\n\nApprove this action to continue.",
                buttons=[[("✅ Approve", "confirm:yes"), ("❌ Reject", "confirm:no")]],
            )
            return

        text = outcome.text
        if outcome.status == "timeout":
            note = "⏱️ *Still running.* The agent has not finished yet.\n\n"
            text = note + (text or "No output yet — use /stop to cancel.")
        elif outcome.error:
            text = f"⚠️ *The run ended with an error.*\n\n`{outcome.error}`"
            if outcome.text:
                text += f"\n\n{outcome.text}"
        elif not text.strip():
            text = f"The agent finished (`{outcome.status}`) without a text response."

        await self.surface.replace_with_markdown(chat_id, placeholder, text)

    async def interrupt(self, chat_id: int) -> bool:
        session = await self.store.get_session(chat_id)
        if session is None:
            return False
        await self.backend.interrupt(session.conversation_id)
        return True

    async def confirm(self, chat_id: int, accept: bool) -> bool:
        session = await self.store.get_session(chat_id)
        if session is None:
            return False
        await self.backend.respond_to_confirmation(
            session.conversation_id,
            accept=accept,
            reason="Approved from Telegram." if accept else "Rejected from Telegram.",
        )
        return True

    async def resume_turn(self, chat_id: int) -> None:
        """Keep watching a conversation after a confirmation was answered."""
        session = await self.store.get_session(chat_id)
        if session is None:
            return
        placeholder = (await self.surface.send_markdown(chat_id, "▶️ *Resuming…*"))[0]
        baseline = {event.id for event in await self.backend.get_events(session.conversation_id)}
        outcome = await self._poll(chat_id, session.conversation_id, placeholder, baseline)
        await self._deliver(chat_id, placeholder, outcome)
