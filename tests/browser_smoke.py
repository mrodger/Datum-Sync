"""Drive the Schedules and Automations screens in a real browser.

Not a test module -- it needs a running server and a real account, so it will
not work under pytest and does not try to:

    DS_SMOKE_URL=http://127.0.0.1:8000 \\
    DS_SMOKE_USER=someone DS_SMOKE_PASSWORD=... \\
    python tests/browser_smoke.py

`tests/test_ui.py` says in its own docstring that none of it proves the UI
renders, because rendering is a browser's job. This is that job. It does what a
person would do -- sign in, create a schedule, check the row says what was
typed, reopen it, pause it, delete it, then the same for an automation -- and
fails on any console error, page error, failed fetch or unexpected 4xx/5xx.

It earned its keep on its first complete run. The schedule detail screen showed
`Cannot use 'in' operator to search for 'COUNT' in {"COUNT": 7}`: pausing sends
a patch that never mentions params, and `schedules.update` was re-encoding the
JSON *text* asyncpg returns, so params became a string. 251 unit tests did not
see it, because every one of them that patches a schedule patches params too,
which overwrites the corrupted value. Nothing short of clicking Pause and then
looking at the screen would have found it.

The account needs to reach the Testing repository. `chatty` is used because it
publishes exactly one parameter (COUNT), so a form with a missing field is
obvious rather than merely shorter.
"""
from __future__ import annotations

import os
import re
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("DS_SMOKE_URL", "http://127.0.0.1:8000").rstrip("/")
USER = os.environ.get("DS_SMOKE_USER", "admin")
PASSWORD = os.environ.get("DS_SMOKE_PASSWORD", "")

SCHEDULE = "_ui-smoke-schedule"
AUTOMATION = "_ui-smoke-automation"

# Deliberately unrunnable: example.invalid never resolves, and the automation is
# deleted before anything could fire it. What is being tested is that the editor
# hands this document back byte for byte -- the comment and the blank lines
# included -- not that the action works.
AUTOMATION_YAML = """name: _ui-smoke-automation
enabled: true

# a comment, kept verbatim
trigger:
  type: job_complete
  repository: Testing
  workspace: chatty
  status: complete

actions:
  - type: http_request
    method: POST
    url: https://example.invalid/hook
    body: '{"job": "{{job.id}}"}'
"""

problems: list[str] = []

# One 4xx per run is deliberate: the bad-YAML document, which the server has to
# refuse. Consumed rather than allowlisted, so a *second* 400 on the same route
# -- the shape of a real bug -- is still reported.
expected: list[tuple[int, str]] = []


def unexpected(status: int, url: str) -> bool:
    if status < 400 or url.endswith("/whoami"):
        return False        # /whoami 401 is the ordinary not-signed-in probe
    for i, (code, fragment) in enumerate(expected):
        if code == status and fragment in url:
            expected.pop(i)
            return False
    return True


def watch(page) -> None:
    page.on("console", lambda m: m.type == "error"
            and "401" not in m.text and "400" not in m.text
            and problems.append(f"console error: {m.text}"))
    page.on("pageerror", lambda e: problems.append(f"page error: {e}"))
    page.on("requestfailed", lambda r: problems.append(
        f"request failed: {r.url} {r.failure}"))
    page.on("response", lambda r: unexpected(r.status, r.url)
            and problems.append(f"HTTP {r.status}: {r.url}"))


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "ok  " if condition else "FAIL"
    print(f"  {mark} {label}{'  ' + detail if detail else ''}")
    if not condition:
        problems.append(f"{label} {detail}")


def brief(exc: Exception, prefix: str = "") -> str:
    return f"{prefix}{type(exc).__name__}: {exc}".replace("\n", " | ")[:400]


def main() -> int:
    if not PASSWORD:
        print("set DS_SMOKE_PASSWORD (and DS_SMOKE_USER/DS_SMOKE_URL)")
        return 2

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        watch(page)

        page.goto(BASE + "/ui")
        page.fill("#signin-name", USER)
        page.fill("#signin-password", PASSWORD)
        page.click("#signin-submit")
        page.wait_for_selector("#app:not([hidden])")
        print("signed in")

        try:
            schedules(page)
            automations(page)
        except Exception as exc:            # noqa: BLE001 - recorded, not raised
            # Recorded rather than propagated, because cleanup has to run and a
            # `finally` that calls cleanup will throw away this exception the
            # moment cleanup raises its own. That is what the first run did, and
            # the real failure was lost behind a cleanup timeout.
            problems.append(brief(exc))
        finally:
            try:
                cleanup(page)
            except Exception as exc:        # noqa: BLE001
                problems.append(brief(exc, "cleanup: "))
            browser.close()

    print()
    if problems:
        print(f"{len(problems)} PROBLEM(S):")
        for item in problems:
            print("  -", item)
        return 1
    print("PASS: both screens render, create, edit and delete cleanly")
    return 0


def schedules(page) -> None:
    print("\nschedules")
    page.goto(BASE + "/ui#/schedules")
    # Scoped to #view throughout. The sign-in <h1> stays in the DOM behind
    # [hidden], so a bare h1 locator matches two elements and waits forever for
    # the wrong one.
    #
    # And waited on by the New button rather than by `#view h1`, because every
    # screen has an h1: arriving from another one, the selector matches the
    # heading still on display and the assertion below reads the old screen.
    # That is a flaky pass, not a failure, which is the worse kind.
    page.wait_for_selector("#view >> text=New schedule")
    check("list renders", page.locator("#view h1").first.inner_text() == "Schedules")

    page.click("#view >> text=New schedule")
    page.wait_for_selector("form.panel")
    check("form renders", page.locator("form.panel").count() == 1)

    texts = page.locator("form.panel input[type=text]")
    selects = page.locator("form.panel select")
    repository, workspace = selects.nth(0), selects.nth(1)

    texts.nth(0).fill(SCHEDULE)
    repository.select_option("Testing")
    # The workspace select is populated by a fetch the repository choice starts,
    # so selecting into it immediately picks from an empty list.
    page.wait_for_function(
        "() => document.querySelectorAll('form.panel select')[1].options.length > 1")
    workspace.select_option("chatty")
    page.wait_for_selector("#param-COUNT")
    check("workspace parameters loaded", page.locator("#param-COUNT").count() == 1)

    page.fill("#param-COUNT", "7")
    # The trigger select defaults to cron; the cron box is the second text input.
    texts.nth(1).fill("0 7 * * 1-5")
    page.click("form.panel button[type=submit]")

    page.wait_for_selector(f"tr:has-text('{SCHEDULE}')")
    text = page.locator(f"tr:has-text('{SCHEDULE}')").inner_text()
    check("row shows the cron", "0 7 * * 1-5" in text, repr(text))
    check("row shows enabled", "ENABLED" in text.upper())
    check("next run is a real date", bool(re.search(r"\d{1,4}[/-]\d", text)), repr(text))

    page.click(f"#view >> tr:has-text('{SCHEDULE}') >> text={SCHEDULE}")
    page.wait_for_selector("#view >> text=Pause")
    check("detail shows the workspace",
          "Testing/chatty" in page.locator(".panel").nth(1).inner_text())
    # The check that found the params bug: what was typed has to survive a round
    # trip through the database and come back into the form.
    check("stored parameter is seeded back",
          page.locator("#param-COUNT").input_value() == "7")

    # Pausing redraws the screen the hash names, which is still the detail one.
    # The first version of this waited for a row in a table that was not on
    # screen, and timed out long after the click had worked.
    page.click("#view >> text=Pause")
    page.wait_for_selector("#view >> text=Resume")
    check("pausing flips the badge in place",
          page.locator("#view .badge").first.inner_text().upper() == "PAUSED")

    page.goto(BASE + "/ui#/schedules")
    page.wait_for_selector(f"tr:has-text('{SCHEDULE}') .badge.paused")
    check("a paused schedule shows no next run",
          "\u2014" in page.locator(f"tr:has-text('{SCHEDULE}')").inner_text())


def automations(page) -> None:
    print("\nautomations")
    page.goto(BASE + "/ui#/automations")
    page.wait_for_selector("#view >> text=New automation")
    check("list renders", page.locator("#view h1").first.inner_text() == "Automations")

    page.click("#view >> text=New automation")
    page.wait_for_selector("textarea.yaml")
    check("template is offered",
          "job_complete" in page.locator("textarea.yaml").input_value())

    page.fill("textarea.yaml", "name: broken\ntrigger: nope\n")
    expected.append((400, "/rest/v1/automations"))
    page.click("form.panel button[type=submit]")
    page.wait_for_selector(".banner")
    said = page.locator(".banner").inner_text()
    # The server's own message, on the screen the document was typed on. A
    # rejection the editor swallows is worse than no validation at all.
    check("a bad document is refused in place, not swallowed",
          "trigger" in said.lower(), repr(said))

    page.fill("textarea.yaml", AUTOMATION_YAML)
    page.click("form.panel button[type=submit]")
    page.wait_for_selector(f"tr:has-text('{AUTOMATION}')")
    row = page.locator(f"tr:has-text('{AUTOMATION}')").inner_text()
    check("row shows the trigger", "Testing/chatty" in row, repr(row))
    check("row shows the action", "http_request" in row, repr(row))

    page.click(f"#view >> tr:has-text('{AUTOMATION}') >> text={AUTOMATION}")
    page.wait_for_selector("textarea.yaml")
    stored = page.locator("textarea.yaml").input_value()
    check("the editor shows the document verbatim, comment included",
          stored == AUTOMATION_YAML,
          "" if stored == AUTOMATION_YAML else repr(stored[:120]))
    check("runs table says it has not fired",
          "not fired" in page.locator(".empty").inner_text())


def cleanup(page) -> None:
    print("\ncleanup")
    for section, name in (("schedules", SCHEDULE), ("automations", AUTOMATION)):
        page.goto(f"{BASE}/ui#/{section}")
        # Not `#view h1`: arriving from a detail screen that also has one, it
        # matches the screen still on display and the count below is read before
        # the list has fetched. The New button exists only on the list.
        page.wait_for_selector("#view >> text=New ")
        if page.locator(f"tr:has-text('{name}')").count() == 0:
            print(f"  no {name} to remove")
            continue
        page.click(f"#view >> tr:has-text('{name}') >> text={name}")
        page.wait_for_selector("#view >> text=Delete")
        page.click("#view >> text=Delete")
        page.wait_for_selector("#view h1")
        check(f"{name} deleted", page.locator(f"tr:has-text('{name}')").count() == 0)


if __name__ == "__main__":
    sys.exit(main())
