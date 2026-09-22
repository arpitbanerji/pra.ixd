# AGENTS.md

Repository-specific notes for agents working on **ohbot** (Telegram ⇄ OpenHands
bridge). See `README.md` for user-facing docs.

## Layout

```
src/ohbot/
  config.py         pydantic-settings; fails fast on unsafe/missing config
  render.py         Markdown -> Telegram HTML + fence-safe chunking
  events.py         raw agent-server events -> human status/answer strings
  store.py          SQLite: chat->conversation map, update de-dupe, kv
  surface.py        ChatSurface protocol + TelegramSurface implementation
  worker.py         ConversationRunner (per-chat turn), SerialExecutor
  app.py            PTB handlers, allowlist, health server, lifecycle
  __main__.py       entry point (`python -m ohbot`)
  backends/
    base.py         Backend interface + HttpBackend (pooled client, retries)
    agent_server.py self-hosted agent server (X-Session-API-Key)
    cloud.py        OpenHands Cloud (Bearer) -> provisions, then delegates
tests/              pytest; asyncio_mode=auto
```

## Commands

```bash
python -m pytest                                   # 58 tests, no network
python -m pytest -m live -v                        # needs a real agent server
PYTHONPATH=src python -m ohbot                     # run locally
PYTHONPATH=src python scripts/check_backend.py     # diagnose connectivity
```

Live tests:

```bash
OPENHANDS_LIVE_TEST=1 \
OPENHANDS_SESSION_API_KEY=$(cat ~/.openhands/agent-canvas/api-key.txt) \
OPENHANDS_BASE_URL=http://127.0.0.1:18000 \
python -m pytest -m live -v
```

## Agent-server API facts (verified against SDK 1.49.1)

These were established by probing a live server; do not "simplify" them away.

- `POST /api/conversations` **requires exactly one of** `agent`,
  `agent_settings`, or `agent_profile_id`. Omitting all three returns
  HTTP 422. `agent_profile_id` must be a **UUID** — a profile *name* is rejected
  with a uuid_parsing error. The server's active profile UUID is at
  `GET /api/settings` → `active_agent_profile_id`, and the bot auto-detects it.
- Send a follow-up turn with `POST /api/conversations/{id}/events`, body
  `{"role":"user","content":[{"type":"text","text":...}],"run":true}`.
- `execution_status` of `idle` is **ambiguous**: it appears both before a run
  starts and after it finishes. Completion logic must not treat bare `idle` as
  done — see the comment block at the top of `worker.py`. Reliable terminal
  states are `finished`, `error`, `stuck`; `waiting_for_confirmation` means the
  agent paused.
- The authoritative answer is `GET /api/conversations/{id}/agent_final_response`
  → `{"response": "..."}` (empty until there is one). The event stream is only a
  fallback, and only events *after* the user's prompt are valid.
- A finished turn emits an `ActionEvent` with `tool_name="finish"` and
  `action={"message": ..., "kind": "FinishAction"}`.
- Errors arrive as `ConversationErrorEvent` with `code` and `detail`
  (e.g. `APIError`, a litellm/provider message).
- Events: `GET /api/conversations/{id}/events/search?limit=N&sort_order=TIMESTAMP_DESC`.
- Confirmation policies are discriminated by `kind`: `NeverConfirm`,
  `AlwaysConfirm`, `ConfirmRisky`. Reply with
  `POST /api/conversations/{id}/events/respond_to_confirmation`
  `{"accept": bool, "reason": str}`.
- Interrupt with `POST /api/conversations/{id}/interrupt` (no body).

## Testing approach

- `tests/support.py` has `StubLLM` — a **real** HTTP server speaking the OpenAI
  chat-completions API that replies by calling the `finish` tool. Live tests
  point the agent server at it, so the whole agent loop runs without a paid
  model. This is the preferred way to get a real model turn in tests.
- `ScriptedBackend` / `RecordingSurface` are doubles used only for status
  sequences that cannot be produced on demand (timeouts, mid-run errors,
  idle-before-start). The real HTTP path is covered by the live tests.
- Keep `tests/test_config.py` hermetic — it has an autouse fixture that strips
  ambient `OPENHANDS_*`/`TELEGRAM_*` vars, because pydantic-settings reads
  `os.environ` even with `_env_file=None`.

## Environment gotchas

- This dev sandbox **cannot build or run Docker images**: the daemon lacks
  `NET_ADMIN` and the container runtime blocks `mount`/`unshare`
  (`operation not permitted`). Start `dockerd` with
  `--iptables=false --bridge=none --storage-driver=vfs` for API-level work only;
  a real Docker host is required to build the image.
- Image builds install from `requirements.txt` (wheel pre-build stage) and run
  with `PYTHONPATH=/app/src`, so `src/` must stay importable as a plain package.

## Style

- Minimal comments; explain *why*, not *what*. Keep functions small.
- The bot must never execute anything for a non-allowlisted Telegram user.
- No secrets in the image, in logs, or in prompts.
