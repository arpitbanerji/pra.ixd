"""Tests for translating OpenHands events into chat updates."""

from ohbot.events import (
    action_progress,
    assistant_text,
    error_text,
    finish_text,
    first_error,
    is_terminal_status,
    latest_answer,
    parse_event,
)

# Shapes below mirror real events captured from an OpenHands agent server.


def message(source, text, **extra):
    return parse_event(
        {
            "id": "m1",
            "kind": "MessageEvent",
            "source": source,
            "llm_message": {"role": source, "content": [{"type": "text", "text": text}]},
            **extra,
        }
    )


def test_action_progress_labels_known_tools():
    event = parse_event(
        {
            "id": "a1",
            "kind": "ActionEvent",
            "source": "agent",
            "tool_name": "terminal",
            "action": {"command": "pytest -q"},
        }
    )
    progress = action_progress(event)
    assert progress is not None
    assert "Running a command" in progress
    assert "pytest -q" in progress


def test_action_progress_handles_unknown_tool_and_missing_action():
    event = parse_event({"id": "a2", "kind": "ActionEvent", "source": "agent", "tool_name": "mystery"})
    assert action_progress(event) == "🔧 Using mystery"


def test_action_progress_returns_none_for_non_actions():
    assert action_progress(message("agent", "hi")) is None


def test_assistant_text_reads_agent_messages_only():
    assert assistant_text(message("agent", "hello there")) == "hello there"
    assert assistant_text(message("user", "hello there")) is None


def test_assistant_text_skips_tool_call_messages():
    event = message("agent", "calling a tool", tool_call_id="t1")
    assert assistant_text(event) is None


def test_finish_text_is_extracted_from_finish_action():
    event = parse_event(
        {
            "id": "a3",
            "kind": "ActionEvent",
            "source": "agent",
            "tool_name": "finish",
            "action": {"message": "All done."},
        }
    )
    assert finish_text(event) == "All done."


def test_latest_answer_prefers_finish_over_earlier_chatter():
    events = [
        message("agent", "working on it"),
        parse_event(
            {
                "id": "a4",
                "kind": "ActionEvent",
                "source": "agent",
                "tool_name": "finish",
                "action": {"message": "Final answer"},
            }
        ),
    ]
    assert latest_answer(events) == "Final answer"


def test_latest_answer_falls_back_to_last_assistant_message():
    events = [message("agent", "first"), message("agent", "second")]
    assert latest_answer(events) == "second"


def test_error_text_and_first_error():
    event = parse_event(
        {
            "id": "e1",
            "kind": "ConversationErrorEvent",
            "source": "environment",
            "code": "APIError",
            "detail": "provider rejected the request",
        }
    )
    assert "APIError" in error_text(event)
    assert "provider rejected" in first_error([message("agent", "hi"), event])


def test_is_terminal_status():
    assert is_terminal_status("finished")
    assert is_terminal_status("error")
    assert not is_terminal_status("running")
