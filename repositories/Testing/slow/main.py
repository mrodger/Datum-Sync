"""Integration fixture: logs, reports progress, and takes its time."""
import asyncio


async def run(params, emit, connections):
    seconds = int(params["SECONDS"])
    steps = max(1, min(seconds, 10))
    for i in range(steps):
        await emit("log", {"level": "info", "message": f"step {i + 1} of {steps}"})
        await emit("progress", {"pct": (i + 1) / steps, "message": f"step {i + 1}"})
        await asyncio.sleep(seconds / steps)
    return [{"name": "out", "type": "text/plain", "content": f"slept {seconds}s"}]
