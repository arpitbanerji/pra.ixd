"""Check that the configured OpenHands backend is reachable and usable.

Run inside the container to diagnose "Could not reach OpenHands":

    docker compose exec ohbot python scripts/check_backend.py
    docker compose exec ohbot python scripts/check_backend.py --run "say hi"
    docker compose exec ohbot python scripts/check_backend.py --probe

Exits non-zero on the first problem found.
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx

from ohbot.config import BackendMode, Settings

# Candidates tried by --probe, covering the usual topologies when the agent
# server is not in this container: the Docker host, or the machine running it.
PROBE_CANDIDATES = (
    "http://127.0.0.1:18000",
    "http://localhost:18000",
    "http://host.docker.internal:18000",
    "http://172.17.0.2:18000",
)


def auth_headers(settings: Settings) -> dict[str, str]:
    if settings.openhands_mode is BackendMode.CLOUD:
        return {"Authorization": f"Bearer {settings.openhands_cloud_api_key}"}
    return {"X-Session-API-Key": settings.openhands_session_api_key or ""}


def check(url: str, headers: dict[str, str], timeout: float = 6.0) -> tuple[bool, bool, str]:
    """Return ``(reachable, authenticated, detail)`` for one base URL.

    ``/server_info`` answers anonymously by design, so it proves only that the
    server is up -- it says nothing about the API key. Authentication is proven
    against ``/api/settings``, which does enforce it. httpx does not raise on
    4xx, so every status code here is checked explicitly; otherwise a rejected
    key parses as JSON and looks like success.
    """
    try:
        with httpx.Client(base_url=url, headers=headers, timeout=timeout) as client:
            info = client.get("/server_info")
            if info.status_code >= 400:
                return False, False, f"HTTP {info.status_code}"
            version = info.json().get("version", "?")

            resp = client.get("/api/settings")
            if resp.status_code in (401, 403):
                return True, False, f"v{version}; credentials rejected (HTTP {resp.status_code})"
            if resp.status_code >= 400:
                return True, False, f"v{version}; /api/settings returned HTTP {resp.status_code}"
            return True, True, f"v{version}"
    except (httpx.HTTPError, ValueError) as exc:
        return False, False, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        metavar="PROMPT",
        help="also start a conversation with this prompt and wait for the answer",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="try the common agent-server URLs and report which one works",
    )
    args = parser.parse_args()

    settings = Settings()
    base = settings.openhands_base_url
    print(f"mode      : {settings.openhands_mode.value}")

    headers = auth_headers(settings)

    if args.probe:
        print("probing candidate agent-server URLs ...\n")
        print(f"{'URL':<40} RESULT")
        winner = None
        for url in PROBE_CANDIDATES:
            reachable, authed, detail = check(url, headers)
            if authed:
                state = f"OK — reachable and authenticated ({detail})"
                winner = winner or url
            elif reachable:
                state = f"reachable, AUTH FAILED ({detail})"
            else:
                state = "unreachable"
            print(f"{url:<40} {state}")
        if winner:
            print(f"\nset OPENHANDS_BASE_URL={winner}")
            return 0
        print(
            "\nFAIL: no candidate was reachable and authenticated.\n"
            "      If the agent server runs in another container, publishing its\n"
            "      port to the host (`-p 18000:18000`) makes host.docker.internal\n"
            "      work. Check the key too: local mode uses OPENHANDS_SESSION_API_KEY.",
            file=sys.stderr,
        )
        return 1

    print(f"base url  : {base}")
    print(f"workspace : {settings.openhands_workspace_dir}")
    reachable, authed, detail = check(base, headers)
    if not reachable:
        print(f"FAIL: cannot reach the agent server at {base}: {detail}", file=sys.stderr)
        print("      Run with --probe to find a URL that works from here.", file=sys.stderr)
        return 1
    print(f"server    : OpenHands Agent Server {detail}")
    if not authed:
        print(
            f"FAIL: {detail}.\n"
            "      local mode -> OPENHANDS_SESSION_API_KEY (sent as X-Session-API-Key)\n"
            "      cloud mode -> OPENHANDS_CLOUD_API_KEY (sent as Bearer)",
            file=sys.stderr,
        )
        return 1

    with httpx.Client(base_url=base, headers=headers, timeout=15.0) as client:
        settings_payload = client.get("/api/settings").json()
        profile = settings.openhands_agent_profile_id or settings_payload.get(
            "active_agent_profile_id"
        )
        if not profile:
            print("WARN: no agent profile found; set OPENHANDS_AGENT_PROFILE_ID", file=sys.stderr)
        else:
            print(f"profile   : {profile}")
        print("OK: backend reachable and authenticated")

        if not args.run:
            return 0

        print(f"\nstarting a conversation: {args.run!r}")
        body: dict[str, object] = {
            "workspace": {
                "kind": "LocalWorkspace",
                "working_dir": settings.openhands_workspace_dir,
            },
            "max_iterations": settings.openhands_max_iterations,
            "initial_message": {"content": [{"type": "text", "text": args.run}], "run": True},
        }
        override = settings.agent_override()
        if override:
            body["agent"] = override
        elif profile:
            body["agent_profile_id"] = profile

        conversation = client.post("/api/conversations", json=body).json()
        conversation_id = conversation.get("id")
        if not conversation_id:
            print(f"FAIL: could not create a conversation: {conversation}", file=sys.stderr)
            return 1
        print(f"conversation: {conversation_id}")

        deadline = time.monotonic() + settings.run_timeout_seconds
        status = "unknown"
        while time.monotonic() < deadline:
            time.sleep(settings.poll_interval_seconds)
            status = client.get(f"/api/conversations/{conversation_id}").json().get(
                "execution_status", "unknown"
            )
            if status in {"finished", "error", "stuck"}:
                break

        answer = client.get(f"/api/conversations/{conversation_id}/agent_final_response").json()
        print(f"status    : {status}")
        print(f"answer    : {answer.get('response')!r}")
        if status != "finished":
            events = client.get(
                f"/api/conversations/{conversation_id}/events/search", params={"limit": 50}
            ).json()
            for event in events.get("items", []):
                if event.get("kind") == "ConversationErrorEvent":
                    print(
                        f"error     : {event.get('code')}: {event.get('detail')}",
                        file=sys.stderr,
                    )
            return 1
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
