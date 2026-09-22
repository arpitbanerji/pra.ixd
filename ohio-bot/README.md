# ohbot — chat with OpenHands from Telegram

A Dockerised Telegram bot that turns your Telegram messages into prompts for an
OpenHands agent and streams the answers back to your phone. One Telegram chat
maps to one continuous OpenHands conversation, so context carries across
messages exactly like chatting in the OpenHands web UI.

```
   Phone (Telegram)
          │
          ▼
   Telegram Bot API  ◄──── long polling (works from behind NAT, no public URL)
          │
          ▼
 ┌─────────────────────────────────────────────┐
 │ ohbot container                             │
 │  • allowlist + update de-duplication        │
 │  • one serialised worker per chat           │
 │  • live progress → message edits            │
 │  • SQLite state on a volume                 │
 └─────────────────────────────────────────────┘
          │  HTTP
          ▼
   OpenHands backend
     • local agent server  (X-Session-API-Key)
     • OpenHands Cloud     (Bearer)
```

## How a message flows

1. You send "run the tests" to the bot.
2. The bot maps your chat to an OpenHands conversation, creating one on first
   contact (`POST /api/conversations`) or appending to the existing one
   (`POST /api/conversations/{id}/events`).
3. It polls `execution_status` and the event stream, editing a placeholder
   message to show what the agent is doing ("💻 Running a command: `pytest -q`").
4. When the run finishes it reads `agent_final_response` and replaces the
   placeholder with the rendered answer, split safely across messages.

## Quick start

### 1. Create the bot

Talk to [@BotFather](https://t.me/BotFather), run `/newbot`, and copy the token.

### 2. Point it at OpenHands

**Option A — a local agent server (default).** Find the session key the agent
server uses:

```bash
cat ~/.openhands/agent-canvas/api-key.txt      # or your own deployment's key
```

If the agent server runs on the Docker host, the compose file already maps
`host.docker.internal`, and the default `OPENHANDS_BASE_URL` works.

**Option B — OpenHands Cloud.** Set `OPENHANDS_MODE=cloud` and
`OPENHANDS_CLOUD_API_KEY=...`. The bot provisions a conversation through the app
API and then drives its sandbox agent server.

### 3. Configure and run

```bash
cp .env.example .env
$EDITOR .env          # TELEGRAM_BOT_TOKEN and the OpenHands credentials
docker compose up -d --build
docker compose logs -f
```

Leave `TELEGRAM_ALLOWED_USER_IDS` empty for now and send the bot any message. It
will reply with your numeric Telegram user ID and refuse to do anything else.
Put that ID in `TELEGRAM_ALLOWED_USER_IDS` and restart:

```bash
$EDITOR .env          # TELEGRAM_ALLOWED_USER_IDS=123456789
docker compose restart
```

An empty allowlist is a safe starting state, not a broken one: it grants nobody
access, so the bot has to be able to run for you to discover your own ID. Set
`TELEGRAM_ALLOW_ALL=true` only if you genuinely want any Telegram user to drive
your agent.

## Commands

| Command | Purpose |
| --- | --- |
| `/new` | Start a fresh OpenHands conversation |
| `/status` | Show the conversation id, backend and current state |
| `/stop` | Interrupt the running agent |
| `/resume <conversation_id>` | Attach this chat to an existing conversation |
| `/id` | Show your Telegram user id |
| `/help` | Usage summary |

Anything else you send is treated as a prompt.

## Configuration

All settings are environment variables; see `.env.example` for the full list.

| Variable | Default | Notes |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | — | Required, from @BotFather |
| `TELEGRAM_ALLOWED_USER_IDS` | empty | Comma-separated allowlist, e.g. `123456789,987654321`. Empty denies everyone |
| `TELEGRAM_ALLOW_ALL` | `false` | Serve every user — dangerous |
| `TELEGRAM_PROXY` | — | e.g. `socks5://host:1080` where Telegram is blocked |
| `OPENHANDS_MODE` | `local` | `local` or `cloud` |
| `OPENHANDS_BASE_URL` | `http://host.docker.internal:18000` | Agent server, or app server in cloud mode |
| `OPENHANDS_SESSION_API_KEY` | — | Required in local mode |
| `OPENHANDS_CLOUD_API_KEY` | — | Required in cloud mode |
| `OPENHANDS_AGENT_PROFILE_ID` | auto | Agent profile UUID; auto-detected from `/api/settings` |
| `OPENHANDS_AGENT_JSON` | — | Advanced: inline `agent` config pinned in the create body |
| `OPENHANDS_WORKSPACE_DIR` | `/workspace` | Workspace the agent operates in |
| `OPENHANDS_MAX_ITERATIONS` | `200` | Agent loop cap per turn |
| `OPENHANDS_CONFIRMATION_POLICY` | `never` | `always` surfaces Approve/Reject buttons |
| `POLL_INTERVAL_SECONDS` | `2` | Status/event poll cadence |
| `RUN_TIMEOUT_SECONDS` | `1800` | Give up on a turn after this long |
| `STREAM_PROGRESS` | `true` | Show live tool activity |
| `DB_PATH` | `/data/ohbot.sqlite3` | Keep this on the volume |

### Pinning a model without a server profile

`OPENHANDS_AGENT_JSON` is sent as the `agent` field of the create request, which
is useful for a self-hosted or OpenAI-compatible endpoint:

```json
{"kind":"Agent","llm":{"model":"openai/my-model","api_key":"sk-...","base_url":"http://my-endpoint/v1"}}
```

### Running alongside other local services

ohbot only needs outbound HTTPS to `api.telegram.org` and HTTP to one OpenHands
URL. It publishes exactly one port, `127.0.0.1:8080`, for health checks, so it
does not collide with the usual local stacks:

| Port | Typical owner | Conflict with ohbot? |
| --- | --- | --- |
| 3000 | a frontend dev server | no |
| 8000 / 8001 | Agent Canvas UI and backend | no |
| 8080 | **ohbot health** (localhost only) | — |
| 18000 | OpenHands agent server | no — ohbot is a *client* of it |
| 18001, 9xxx | other local services | no |

If your agent server runs in a **different container** from ohbot, the base URL
depends on how that container is reachable. Run the probe to find out rather
than guessing:

```bash
docker compose exec ohbot python scripts/check_backend.py --probe
```

It tests `127.0.0.1`, `localhost`, `host.docker.internal`, and the default bridge
address, then prints the `OPENHANDS_BASE_URL` to use. Two facts make this
necessary:

- `host.docker.internal:18000` works only if the agent server's port is
  **published to the host** (`-p 18000:18000`). On Docker Desktop for Mac that
  hostname resolves automatically; the compose file also maps it for Linux.
- A service bound to `127.0.0.1` *inside its own container* is **not** reachable
  from another container, even with the port published, because Docker forwards
  to the container's interface address, not its loopback. The agent server binds
  `0.0.0.0` by default, so this is usually fine — but it is the first thing to
  check if a published port still refuses connections.

**Do not expose the agent server (18000) or the Agent Canvas backend (8000/8001)
to the internet.** Those endpoints run commands; a tunnel to them is a remote
shell guarded by a single key. Tunnelling a web frontend that *calls* them is a
different thing — see below.

#### Tunnels, and a compile-time gotcha

To reach a web UI from your phone you need a tunnel, but the frontend usually
has its API URL **baked in at build time** (`NEXT_PUBLIC_*` variables are
inlined by Next.js). A build that hardcodes `http://127.0.0.1:8010` will fail
from a phone: `127.0.0.1` resolves to the *phone's* own loopback, which is not
your laptop. Two ways out:

1. **Rebuild** the frontend with the public URL, then tunnel it:
   ```bash
   NEXT_PUBLIC_API_BASE_URL=https://<public-api-url> npm run build
   ```
   Restarting is not enough for a production server — the value is compiled in.
2. **One tunnel plus a reverse proxy** that serves the UI and rewrites `/api/*`
   to the API port. This avoids a rebuild, avoids a second tunnel, and avoids
   cross-origin problems.

ohbot itself needs neither: Telegram long polling works from behind NAT with no
public URL, so no tunnel is required for the bot.

## Security

This bot turns chat messages into commands executed by an agent on your machine.
Treat the token and the allowlist as you would SSH credentials.

- **The allowlist is the security boundary.** With `TELEGRAM_ALLOW_ALL=true`
  anyone who finds the bot can run the agent. Leave it off.
- An empty allowlist grants nobody access. The bot logs a warning and tells
  unauthorised users their own user id, and nothing more.
- `OPENHANDS_CONFIRMATION_POLICY=always` makes the agent pause and ask for
  approval in Telegram before each action.
- Secrets live in `.env` (gitignored) or your orchestrator's secret store —
  never in the image.
- The container runs as a non-root user, and the health port binds to
  `127.0.0.1` only.
- The agent server session key grants full control of that agent server. Keep it
  out of logs and chat.

## Operations

```bash
docker compose ps                      # healthcheck status
docker compose logs -f ohbot           # structured JSON logs
curl -s localhost:8080                 # {"status": "ok", "service": "ohbot"}

# State is one SQLite file on the volume; back it up like any other db
docker run --rm -v ohbot-data:/data -v "$PWD:/backup" busybox \
  cp /data/ohbot.sqlite3 /backup/ohbot-backup.sqlite3
```

On restart, in-flight conversations resume from the SQLite mapping and Telegram
updates that were already handled are ignored, so nothing runs twice.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m pytest                       # unit + behavioural tests
```

Integration tests drive a real agent server, using a local OpenAI-compatible
stub as the model so no external API key is needed:

```bash
OPENHANDS_LIVE_TEST=1 \
OPENHANDS_SESSION_API_KEY=$(cat ~/.openhands/agent-canvas/api-key.txt) \
python -m pytest -m live -v
```

Run the bot locally without Docker:

```bash
PYTHONPATH=src python -m ohbot
```

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `No allowlist configured` warning | Expected on first run — message the bot to learn your id, then set `TELEGRAM_ALLOWED_USER_IDS` |
| `must be comma-separated integers` | `TELEGRAM_ALLOWED_USER_IDS` has a non-numeric entry |
| `cannot create the database directory` | Volume owned by root — `docker compose down -v`, or set `DB_PATH` |
| `credentials rejected (HTTP 401)` | Wrong key. local mode uses `OPENHANDS_SESSION_API_KEY`; cloud uses `OPENHANDS_CLOUD_API_KEY` |
| `cannot reach the agent server` | Run `check_backend.py --probe`; the port may not be published to the host |
| `Could not reach OpenHands` | Wrong `OPENHANDS_BASE_URL` or session key; check the agent server is up |
| `✗ The run ended with an error` | The model provider rejected the request; the detail is in the message |
| Replies stop mid-turn | Raise `RUN_TIMEOUT_SECONDS` for long tasks |
| Telegram unreachable in your region | Set `TELEGRAM_PROXY` |

## Verified in this repository

- 66 hermetic unit/behavioural tests (rendering, chunking, event mapping, store,
  config, wiring, worker polling edge cases).
- 2 live end-to-end tests against a real OpenHands agent server (`-m live`),
  covering a full successful turn and the provider-error path.

Not verified here: `docker build`/`docker run` and a real Telegram round trip —
this development sandbox blocks the container syscalls and has no bot token.
Both need a machine with a working Docker daemon.
