"""Measure the rendered UI against FME Flow's own pixel geometry.

Not a test module -- like browser_smoke.py it needs a running server and a real
account, so it does not run under pytest:

    DS_SMOKE_URL=http://127.0.0.1:8251 \\
    DS_SMOKE_USER=someone DS_SMOKE_PASSWORD=... \\
    python tests/flow_geometry.py

The numbers on the right-hand side are not preferences. They are measured from
FME Flow 2026.2 by an automated scan of the running server, recorded in
~/vault/fme/knowledge/flow_ui_map.md, and every one of them appears there
identically on all nine pages that were scanned:

    nav item      x=8  w=234  h=36, one every 44px, first at y=82
    content       starts at x=290
    action button h=35        search field h=40        tab h=36

The point of asserting them is that layout drift is invisible to every other
check we have. `browser_smoke.py` drives the whole application and passes with
the sidebar at any width at all; the pytest suite never renders anything. A
side-by-side demo is exactly where a 20px discrepancy shows up, and by then it
is being shown to somebody.

Only the chrome is asserted, because only the chrome is what Flow's scan
measured on every page. Screen-specific content is not claimed to match.
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("DS_SMOKE_URL", "http://127.0.0.1:8000").rstrip("/")
USER = os.environ.get("DS_SMOKE_USER", "admin")
PASSWORD = os.environ.get("DS_SMOKE_PASSWORD", "")

# Flow's viewport when it was scanned. Measuring at a different width would
# move the content edge and prove nothing about the numbers below.
VIEWPORT = {"width": 1920, "height": 1080}

problems: list[str] = []


def check(label: str, got, want, tol: float = 0.5) -> None:
    ok = got is not None and abs(got - want) <= tol
    got_s = "missing" if got is None else f"{got:g}"
    print(f"  {'ok  ' if ok else 'FAIL'} {label:<34} want {want:<5g} got {got_s}")
    if not ok:
        problems.append(f"{label}: want {want}, got {got_s}")


def box(page, selector: str, prop: str):
    """One geometric property of the first match, or None if it is not there."""
    b = page.locator(selector).first
    if b.count() == 0:
        return None
    rect = b.bounding_box()
    return None if rect is None else rect[prop]


def main() -> int:
    if not PASSWORD:
        print("DS_SMOKE_PASSWORD is not set", file=sys.stderr)
        return 2

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_context(viewport=VIEWPORT).new_page()

        page.goto(f"{BASE}/ui")
        page.wait_for_selector("#signin-form")
        page.fill("#signin-name", USER)
        page.fill("#signin-password", PASSWORD)
        page.click("#signin-submit")
        page.wait_for_selector("#view > [data-ready]")

        print("\nrail")
        check("sidebar width", box(page, ".sidebar", "width"), 250)
        check("nav item x", box(page, ".sidebar a", "x"), 8)
        check("nav item width", box(page, ".sidebar a", "width"), 234)
        check("nav item height", box(page, ".sidebar a", "height"), 36)
        check("first nav item y", box(page, ".sidebar a", "y"), 82)

        # The pitch is what makes the rail scan like Flow's; asserted between
        # the first two items rather than assumed from height plus margin.
        items = page.locator(".sidebar a")
        pitch = None
        if items.count() >= 2:
            a, b = items.nth(0).bounding_box(), items.nth(1).bounding_box()
            pitch = b["y"] - a["y"]
        check("nav pitch", pitch, 44)

        print("\ncontent")
        # The gutter, not the view box: `.view` starts at the rail's edge and
        # its padding is what puts content at Flow's x=290.
        check("content left edge", box(page, "#view h1", "x"), 290)

        print("\ncontrols")
        page.goto(f"{BASE}/ui#/schedules")
        page.wait_for_selector("#view > [data-ready='schedules']")
        check("action button height", box(page, ".listbar .actions .button", "height"), 35)
        check("search field height", box(page, ".listbar .search input", "height"), 40)

        browser.close()

    if problems:
        print(f"\n{len(problems)} PROBLEM(S):")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("\nPASS: chrome matches the FME Flow 2026.2 scan")
    return 0


if __name__ == "__main__":
    sys.exit(main())
