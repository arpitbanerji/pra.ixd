"""Behavioural tests for the conversation runner.

The status sequences here (timeouts, mid-run errors, idle-before-start) cannot be
produced on demand against a real agent server; the real HTTP path is covered
separately in ``test_integration_live.py``.
"""

import asyncio

from support import SHORT_POLL_SETTINGS, RecordingSurface, ScriptedBackend

from ohbot.config import Settings
from ohbot.events import parse_event
from ohbot.store import Store
from ohbot.worker import ConversationRunner, SerialExecutor


def make_settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**SHORT_POLL_SETTINGS, **overrides})


def finish_event(message: str):
    return parse_event(
        {
            "id": "action-finish",
            "kind": "ActionEvent",
            "source": "agent",
            "tool_name": "finish",
            "action": {"message": message, "kind": "FinishAction"},
        }
    )


def agent_message(text: str, event_id: str = "msg-1"):
    return parse_event(
        {
            "id": event_id,
            "kind": "MessageEvent",
            "source": "agent",
            "llm_message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        }
    )


def error_event(detail: str = "provider exploded"):
    return parse_event(
        {
            "id": "err-1",
            "kind": "ConversationErrorEvent",
            "source": "environment",
            "code": "APIError",
            "detail": detail,
        }
    )


def terminal_action(command: str = "pytest -q"):
    return parse_event(
        {
            "id": "action-1",
            "kind": "ActionEvent",
            "source": "agent",
            "tool_name": "terminal",
            "action": {"command": command},
        }
    )


async def build_runner(tmp_path, backend, surface, settings):
    store = await Store(tmp_path / "bot.sqlite3").__aenter__()
    runner = ConversationRunner(backend=backend, store=store, surface=surface, settings=settings)
    return store, runner


async def test_first_message_creates_a_conversation_and_remembers_it(tmp_path):
    backend = ScriptedBackend(statuses=["finished"], final_response="hello from the agent")
    surface = RecordingSurface()
    store, runner = await build_runner(tmp_path, backend, surface, make_settings())
    try:
        await runner.handle_prompt(7, "hi there")

        assert backend.created == ["hi there"]
        assert backend.sent == []  # the create call already carried the prompt
        session = await store.get_session(7)
        assert session is not None and session.conversation_id == "conv-scripted"
        assert "hello from the agent" in surface.last_delivery
    finally:
        await store.close()


async def test_followup_messages_reuse_the_conversation(tmp_path):
    backend = ScriptedBackend(statuses=["finished", "finished"], final_response="ok")
    surface = RecordingSurface()
    store, runner = await build_runner(tmp_path, backend, surface, make_settings())
    try:
        await runner.handle_prompt(7, "first")
        await runner.handle_prompt(7, "second")

        assert backend.created == ["first"]
        assert backend.sent == [("conv-scripted", "second")]
    finally:
        await store.close()


async def test_errors_are_surfaced_to_the_user(tmp_path):
    backend = ScriptedBackend(statuses=["error"], events=[error_event("model rejected the request")])
    surface = RecordingSurface()
    store, runner = await build_runner(tmp_path, backend, surface, make_settings())
    try:
        await runner.handle_prompt(7, "do something")
        assert "error" in surface.last_delivery.lower()
        assert "model rejected the request" in surface.last_delivery
    finally:
        await store.close()


async def test_a_hung_run_times_out_without_blocking_forever(tmp_path):
    backend = ScriptedBackend(statuses=["running"])
    surface = RecordingSurface()
    settings = make_settings(run_timeout_seconds=0.15)
    store, runner = await build_runner(tmp_path, backend, surface, settings)
    try:
        await runner.handle_prompt(7, "run forever")
        assert "still running" in surface.last_delivery.lower()
    finally:
        await store.close()


async def test_idle_after_running_completes_and_falls_back_to_events(tmp_path):
    # A finished run can report "idle" rather than "finished"; with no explicit
    # final response the answer has to come from the event stream.
    backend = ScriptedBackend(statuses=["running", "idle"], events=[agent_message("from events")])
    surface = RecordingSurface()
    store, runner = await build_runner(tmp_path, backend, surface, make_settings())
    try:
        await runner.handle_prompt(7, "hello")
        assert "from events" in surface.last_delivery
    finally:
        await store.close()


async def test_idle_before_a_run_starts_does_not_end_the_turn_early(tmp_path):
    # The server reports "idle" while the run is still spinning up. The answer
    # must still be delivered once the agent actually responds.
    backend = ScriptedBackend(
        statuses=["idle", "idle", "running", "finished"],
        events=[finish_event("late answer")],
        events_reveal_after=2,
    )
    surface = RecordingSurface()
    store, runner = await build_runner(tmp_path, backend, surface, make_settings())
    try:
        await runner.handle_prompt(7, "hello")
        assert "late answer" in surface.last_delivery
    finally:
        await store.close()


async def test_progress_is_streamed_into_the_placeholder(tmp_path):
    backend = ScriptedBackend(
        statuses=["running", "running", "finished"],
        events=[terminal_action("pytest -q")],
        final_response="done",
    )
    surface = RecordingSurface()
    store, runner = await build_runner(tmp_path, backend, surface, make_settings(stream_progress=True))
    try:
        await runner.handle_prompt(7, "run the tests")
        progress = "\n".join(edit[2] for edit in surface.edits)
        assert "Running a command" in progress
        assert "pytest -q" in progress
        assert surface.typing_calls > 0
    finally:
        await store.close()


async def test_confirmations_are_offered_when_the_agent_pauses(tmp_path):
    backend = ScriptedBackend(statuses=["waiting_for_confirmation"])
    surface = RecordingSurface()
    store, runner = await build_runner(tmp_path, backend, surface, make_settings())
    try:
        await runner.handle_prompt(7, "delete the repo")
        _, _, markdown, buttons = surface.edits[-1]
        assert "Approval needed" in markdown
        assert buttons is not None
    finally:
        await store.close()


async def test_executor_serializes_work_for_the_same_chat():
    executor = SerialExecutor()
    running = 0
    peak = 0

    async def job():
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1

    executor.submit(1, job)
    executor.submit(1, job)
    await executor.drain()
    assert peak == 1


async def test_executor_runs_different_chats_concurrently():
    executor = SerialExecutor()
    running = 0
    peak = 0

    async def job():
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1

    executor.submit(1, job)
    executor.submit(2, job)
    await executor.drain()
    assert peak == 2
