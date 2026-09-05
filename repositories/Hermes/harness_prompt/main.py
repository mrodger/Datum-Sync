"""Drive one prompt through a Hermes governance test harness.

The harness (`~/projects/hermes-pwa-harness`) is a locked Hermes agent in a
gVisor container. This workspace makes a turn of it *submittable* -- so a
concurrency test across N harness instances is N jobs with rows, logs and
artifacts, rather than N browser tabs somebody watched.

Two things about the transport are load-bearing and neither is guessable from
the harness README, so both are spelled out here.

**The session token is scraped, never stored.** Hermes generates it at import
time (`hermes_cli/web_server.py`: `_SESSION_TOKEN = secrets.token_urlsafe(32)`,
commented "generated fresh on every server start") and injects it only into the
SPA fallback HTML. It therefore cannot live in the connection store: it would
be stale the first time the container restarted, and the failure would look
like an auth bug rather than a stale credential. `_session_token()` fetches it
per run, which is also what the harness's own terminal page does.

**Only `message.*` is the answer.** The gateway also emits `reasoning.delta`
and `thinking.delta` on the same socket. `thinking.delta` is decorative spinner
text (observed: "reflecting..."), and `reasoning.available` was observed
carrying the *message* text rather than the reasoning. A reader that
accumulates every payload with a `text` key returns the answer three times over
-- measured, and it is what the harness's own probe_ws.py does, which is fine
for a pass/fail probe and wrong for a transcript. So: deltas are collected per
event type, and `message.complete.text` wins over the accumulated deltas
because it is the server's own final answer rather than our reassembly of it.
"""
import asyncio
import json
import re

import httpx
import websockets

_TOKEN_RE = re.compile(r'__HERMES_SESSION_TOKEN__\s*=\s*"([^"]+)"')


class HarnessError(Exception):
    """The harness could not be driven. Fails the job rather than returning half a turn."""


async def _session_token(base_url: str) -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(base_url + "/")
    m = _TOKEN_RE.search(r.text)
    if not m:
        raise HarnessError(
            f"no session token in the index page at {base_url}/ "
            f"(HTTP {r.status_code}, {len(r.text)} bytes) -- is this a Hermes harness?"
        )
    return m.group(1)


async def run(params, emit, connections):
    conn = connections["hermes-researcher"]
    base_url = conn["base_url"].rstrip("/")
    prompt = params["PROMPT"]
    timeout = params["TIMEOUT_SECONDS"]
    resume = (params.get("RESUME_SESSION") or "").strip()

    await emit("progress", {"pct": 0.0, "message": f"Reaching {base_url}"})
    token = await _session_token(base_url)

    ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = f"{ws_url}/api/ws?token={token}"

    # `seen` and `deltas` are separate because not every event carries text --
    # gateway.ready carries none at all, so waiting on it via the text map would
    # be a condition that can never fire and a pump that only ever times out.
    seen: set[str] = set()
    deltas: dict[str, list[str]] = {}
    final: dict | None = None
    rpc_results: dict[str, dict] = {}

    # 16MB: the gateway can send a whole conversation in session.info on resume.
    async with websockets.connect(ws_url, max_size=16 * 1024 * 1024) as ws:
        await emit("progress", {"pct": 0.1, "message": "Connected to /api/ws"})

        async def rpc(method, rpc_params, rid):
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": rid, "method": method, "params": rpc_params,
            }))

        async def pump(seconds, until=None):
            """Read frames for `seconds`, or until `until(...)` says stop.

            Returns True if the turn completed or `until` fired, False if the
            clock ran out. The caller distinguishes the two, because a complete
            transcript and a truncated one must not be reported alike.
            """
            nonlocal final
            try:
                async with asyncio.timeout(seconds):
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("method") == "event":
                            p = msg.get("params") or {}
                            etype = p.get("type", "?")
                            seen.add(etype)
                            text = (p.get("payload") or {}).get("text")
                            if etype == "message.complete":
                                final = p.get("payload") or {}
                                return True
                            if isinstance(text, str) and text:
                                deltas.setdefault(etype, []).append(text)
                        elif "id" in msg:
                            rpc_results[msg["id"]] = msg
                        if until and until():
                            return True
            except (TimeoutError, asyncio.TimeoutError):
                pass
            return False

        await pump(5, until=lambda: "gateway.ready" in seen)

        if resume:
            await rpc("session.resume", {"session_id": resume}, "s")
        else:
            await rpc("session.create", {}, "s")
        await pump(25, until=lambda: "s" in rpc_results)

        reply = rpc_results.get("s")
        if reply is None:
            raise HarnessError("the gateway never answered session.create/resume")
        if reply.get("error"):
            raise HarnessError(f"session setup refused: {reply['error']}")
        session_id = (reply.get("result") or {}).get("session_id")
        if not session_id:
            raise HarnessError(
                f"no session_id in the reply: keys={sorted((reply.get('result') or {}))}"
            )

        await emit("progress", {"pct": 0.2, "message": f"Session {session_id}"})
        await emit("log", {"level": "info", "message": f"prompt: {prompt[:200]}"})

        await rpc("prompt.submit", {"session_id": session_id, "text": prompt}, "p")
        completed = await pump(timeout)

    if not completed:
        raise HarnessError(
            f"no message.complete within {timeout}s "
            f"({sum(len(v) for v in deltas.values())} partial chunks received)"
        )

    # The server's own final text, not our reassembly of the deltas. They agree
    # in the ordinary case; when they do not, the server is right.
    answer = final.get("text") or "".join(deltas.get("message.delta", []))
    reasoning = final.get("reasoning") or "".join(deltas.get("reasoning.delta", []))
    usage = final.get("usage") or {}

    await emit("log", {"level": "info", "message": f"answer: {answer[:400]}"})
    await emit("progress", {"pct": 1.0, "message": (
        f"{usage.get('total', '?')} tokens, "
        f"${usage.get('cost_usd', 0):.6f} ({usage.get('cost_status', 'unknown')})"
    )})

    transcript = (
        f"prompt:\n{prompt}\n\n"
        f"answer:\n{answer}\n"
        + (f"\nreasoning:\n{reasoning}\n" if reasoning else "")
    )

    return [
        {"name": "transcript", "type": "text/plain", "content": transcript},
        {
            "name": "turn",
            "type": "application/json",
            "content": json.dumps({
                "base_url": base_url,
                "session_id": session_id,
                "prompt": prompt,
                "answer": answer,
                "reasoning": reasoning,
                "status": final.get("status"),
                "usage": usage,
                "events_seen": sorted(seen),
                "delta_counts": {k: len(v) for k, v in sorted(deltas.items())},
            }, indent=2),
        },
    ]


# A verbatim copy of the bytes Hermes actually serves, token replaced. The whole
# block is kept, not just the token assignment, and the trailing
# `__HERMES_BASE_PATH__=""` is the reason: it is the only other quoted value on
# the line, so it is the only thing that can catch a regex that captures
# greedily. A sample cut off before it passes just as happily with `(.+)` as
# with `([^"]+)` -- measured, by breaking it.
_SAMPLE = (
    '<script>window.__HERMES_SESSION_TOKEN__="tok-EXAMPLE_123";'
    'window.__HERMES_DASHBOARD_EMBEDDED_CHAT__=true;'
    'window.__HERMES_BASE_PATH__="";</script></head>'
)


def _smoke() -> None:
    """Publish-gate check: is this workspace structurally able to run.

    Deliberately offline. It does NOT prove a harness is reachable -- that is
    what `connections.test()` on `hermes-researcher` is for, and wiring it in
    here would either block publishing whenever the container is down, or
    require hardcoding a `base_url` that already lives in the connection store.
    Two copies of an address is a bug waiting for someone to change one of them.
    """
    import inspect

    assert _TOKEN_RE.search(_SAMPLE).group(1) == "tok-EXAMPLE_123"
    assert inspect.iscoroutinefunction(run)
    assert list(inspect.signature(run).parameters) == ["params", "emit", "connections"]
    print("ok: token regex matches the served format; run() has the job-engine signature")


if __name__ == "__main__":
    import sys

    if "--smoke" in sys.argv:
        _smoke()
