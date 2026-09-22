"""Tests for configuration parsing.

The important cases here go through pydantic-settings' *environment* source
rather than constructor kwargs, because that source pre-processes complex-typed
fields before validators run. Passing values as kwargs skips that path entirely
and once let a startup crash (`TELEGRAM_ALLOWED_USER_IDS=`) ship undetected.
"""

import os
import subprocess
import sys

import pytest
from pydantic import ValidationError

from ohbot.config import BackendMode, ConfirmationPolicy, Settings

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch):
    """Keep these tests hermetic.

    pydantic-settings reads os.environ even when ``_env_file=None``, so an
    ambient OPENHANDS_* variable (e.g. when running the live suite) would
    otherwise satisfy the very settings these tests assert are required.
    """
    for name in list(os.environ):
        if name.startswith(("OPENHANDS_", "TELEGRAM_")):
            monkeypatch.delenv(name, raising=False)


def build(**overrides) -> Settings:
    base = {
        "telegram_bot_token": "123:abc",
        "telegram_allowed_user_ids": [1],
        "openhands_session_api_key": "key",
    }
    return Settings(_env_file=None, **{**base, **overrides})


def from_env(monkeypatch, value: str) -> Settings:
    """Load settings the way the container does: purely from the environment."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("OPENHANDS_SESSION_API_KEY", "key")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", value)
    return Settings(_env_file=None)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("123456789", [123456789]),
        ("111,222,333", [111, 222, 333]),
        ("111, 222 ,333", [111, 222, 333]),
        ("[111, 222]", [111, 222]),
        ("[111,222]", [111, 222]),
        ("1,,2", [1, 2]),
        (" 42 ", [42]),
        ("", []),
    ],
)
def test_allowlist_parses_from_every_env_format(monkeypatch, raw, expected):
    assert from_env(monkeypatch, raw).telegram_allowed_user_ids == expected


def test_empty_allowlist_env_value_does_not_crash_startup(monkeypatch):
    # Regression: pydantic-settings called json.loads("") on this and the
    # container crash-looped with SettingsError before reaching validation.
    settings = from_env(monkeypatch, "")
    assert settings.telegram_allowed_user_ids == []
    assert not settings.allowlist_configured


def test_comma_separated_allowlist_does_not_crash_startup(monkeypatch):
    # Regression: json.loads("1,2") raises "Extra data", so the documented
    # format used to fail even though it looks obviously correct.
    settings = from_env(monkeypatch, "111,222")
    assert settings.telegram_allowed_user_ids == [111, 222]


def test_malformed_allowlist_reports_an_actionable_error(monkeypatch):
    with pytest.raises(ValidationError, match="comma-separated integers"):
        from_env(monkeypatch, "not-a-number")


def test_shipped_env_example_loads_without_crashing(monkeypatch):
    """The file we tell users to copy must not crash the process.

    Secrets are supplied via the environment, as a user would; the point under
    test is that the example's empty ``TELEGRAM_ALLOWED_USER_IDS`` parses cleanly
    instead of raising SettingsError.
    """
    for name in list(os.environ):
        if name.startswith(("OPENHANDS_", "TELEGRAM_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "placeholder")
    monkeypatch.setenv("OPENHANDS_SESSION_API_KEY", "placeholder")

    settings = Settings(_env_file=os.path.join(REPO_ROOT, ".env.example"))
    assert settings.telegram_allowed_user_ids == []
    assert not settings.allowlist_configured


def test_settings_import_and_startup_succeeds_in_a_fresh_process():
    """End-to-end check of the real startup path, as Docker runs it."""
    env = {
        **os.environ,
        "PYTHONPATH": os.path.join(REPO_ROOT, "src"),
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "TELEGRAM_ALLOWED_USER_IDS": "",
        "OPENHANDS_SESSION_API_KEY": "key",
    }
    result = subprocess.run(
        [sys.executable, "-c", "from ohbot.config import Settings; Settings(_env_file=None)"],
        env=env,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr


def test_authorization_checks_allowlist_membership():
    settings = build(telegram_allowed_user_ids=[7])
    assert settings.is_authorized(7)
    assert not settings.is_authorized(8)


def test_allow_all_is_an_explicit_override():
    settings = build(telegram_allowed_user_ids=[], telegram_allow_all=True)
    assert settings.is_authorized(999)


def test_empty_allowlist_still_denies_everyone():
    # Empty is a valid "not bootstrapped yet" state, not a fatal error: the bot
    # must be able to run so a user can ask it for their own id. It still grants
    # nobody access, so this stays fail-closed.
    settings = build(telegram_allowed_user_ids=[])
    assert not settings.allowlist_configured
    assert not settings.is_authorized(1)
    assert not settings.is_authorized(999999)


def test_local_mode_requires_a_session_key():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            telegram_bot_token="123:abc",
            telegram_allowed_user_ids=[1],
            openhands_mode=BackendMode.LOCAL,
        )


def test_cloud_mode_requires_a_cloud_key():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            telegram_bot_token="123:abc",
            telegram_allowed_user_ids=[1],
            openhands_mode=BackendMode.CLOUD,
        )


def test_base_url_trailing_slash_is_normalised():
    assert build(openhands_base_url="http://host:18000/").openhands_base_url == "http://host:18000"


def test_confirmation_policy_parses_from_env_style_value():
    assert build(openhands_confirmation_policy="always").openhands_confirmation_policy is (
        ConfirmationPolicy.ALWAYS
    )


def test_agent_override_is_parsed_from_json():
    settings = build(openhands_agent_json='{"kind": "Agent", "llm": {"model": "openai/x"}}')
    assert settings.agent_override() == {"kind": "Agent", "llm": {"model": "openai/x"}}


def test_agent_override_absent_is_none():
    assert build().agent_override() is None


def test_agent_override_rejects_invalid_json():
    settings = build(openhands_agent_json="{not json")
    with pytest.raises(ValueError):
        settings.agent_override()


def test_agent_override_rejects_non_objects():
    settings = build(openhands_agent_json='["nope"]')
    with pytest.raises(ValueError):
        settings.agent_override()
