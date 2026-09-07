"""Render the six dashboard screens that arrived with migrations 014/015.

Not a test module -- it needs a running server and an admin password, so it will
not work under pytest and does not try to:

    DS_SMOKE_URL=http://127.0.0.1:8201 \\
    DS_SMOKE_USER=someone DS_SMOKE_PASSWORD=... \\
    .venv/bin/python tests/browser_smoke_dashboard.py

Companion to `browser_smoke.py`, which drives the screens that write. These six
only read, so there is nothing to create and check afterwards -- the failure
mode is a screen that throws while turning a valid answer into a table, and
`tests/test_dashboard_routes.py` cannot see that. Every one of its 30 assertions
passes against a route whose screen renders nothing at all.

It earned its keep on its first run. `/auth/clients` was written with no LIMIT
against an empty `oauth_clients`; dynamic client registration had since put 3056
rows in it, and the screen rendered 390 KB into a single table. The route
answered 200, so nothing in the suite objected and nothing could have -- the
size only exists once something draws it. Bounding it took the same screen to
12.8 KB. DASH-012 now holds the bound from pytest, which is the right place for
it, but it is not where it was found.

Checks, in the order they matter:

  - any console error, page error, or 4xx/5xx fetch;
  - the view reaching `data-ready`, so a screen that hangs is not read as blank;
  - a rendered length, because a screen that draws nothing raises nothing. This
    is the one that caught the stub: an unknown hash renders "Not found" in 25
    characters and is otherwise indistinguishable from a clean run.

The 401 on `/rest/v1/whoami` before sign-in is expected and filtered: the app
probes for an existing session on load, and not having one is the normal case.
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("DS_SMOKE_URL", "http://127.0.0.1:8201").rstrip("/")
USER = os.environ.get("DS_SMOKE_USER", "")
PASSWORD = os.environ.get("DS_SMOKE_PASSWORD", "")

# The v2 app is mounted at /v2 as a directory, not a route, so the index has to
# be named. `/v2/` alone is a 404.
APP = BASE + "/v2/index.html"

SCREENS = [
    "notifications",
    "analytics",
    "mcp",
    "auth-services",
    "system-config",
    "queue-control",
]

# Under this, treat the screen as not rendered. "Not found" is 25 characters and
# a real screen with nothing to show still draws its heading and empty state.
MIN_CHARS = 40


def main() -> int:
    if not (USER and PASSWORD):
        print("set DS_SMOKE_USER and DS_SMOKE_PASSWORD (admin account)")
        return 2

    problems: list[str] = []
    signed_in = False

    def on_response(r):
        # Everything before sign-in is the app looking for a session it does not
        # have. Only failures after the nav appears are this script's business.
        if signed_in and r.status >= 400:
            problems.append(f"HTTP {r.status} {r.request.method} {r.url}")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("console", lambda m: (
            signed_in and m.type == "error" and problems.append(f"console: {m.text}")))
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        page.on("response", on_response)

        page.goto(APP, wait_until="networkidle")
        page.fill("#signin-name", USER)
        page.fill("#signin-password", PASSWORD)
        page.click("#signin-submit")
        page.wait_for_selector("nav", timeout=10_000)
        signed_in = True
        print(f"signed in as {USER}")

        for section in SCREENS:
            before = len(problems)
            page.goto(f"{APP}#{section}", wait_until="networkidle")
            try:
                page.wait_for_selector("#view [data-ready], #view h1", timeout=10_000)
            except Exception as exc:
                problems.append(f"{section}: never became ready ({type(exc).__name__})")
            page.wait_for_timeout(400)

            text = page.inner_text("#view").strip()
            h1 = page.query_selector("#view h1")
            heading = h1.inner_text() if h1 else "(no h1)"
            if len(text) < MIN_CHARS:
                problems.append(f"{section}: view rendered {len(text)} chars")

            mark = "ok " if len(problems) == before else "BAD"
            print(f"  {mark} {section:15} h1={heading!r} {len(text)} chars")

        browser.close()

    print()
    if problems:
        print(f"{len(problems)} problem(s):")
        for problem in problems:
            print("  -", problem)
        return 1
    print(f"all {len(SCREENS)} screens rendered clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
