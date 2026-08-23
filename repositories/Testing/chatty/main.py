"""Integration fixture: emits log lines continuously, as fast as it can.

Exists to make the SSE replay window testable. The gap an event reader has to
close is between installing its listener and finishing its read of job_log --
two round trips, about a millisecond. A workspace that logs once a second will
almost never land an event in there, so a reader with no de-duplication passes
by luck. This one logs continuously, so the window is certain to contain
events that arrive by both routes.
"""
import asyncio


async def run(params, emit, connections):
    count = int(params["COUNT"])
    for i in range(count):
        await emit("log", {"level": "info", "message": f"line {i}"})
        await asyncio.sleep(0)
    return [{"name": "out", "type": "text/plain", "content": f"{count} lines"}]
