"""Drive the Schedules, Automations, Connections, Services and Workspaces
screens in a browser.

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
CONNECTION = "_ui-smoke-connection"

# Not created by this script: it is whatever `Testing/site` publishes, because a
# service name belongs to the workspace that declares it. Running the smoke
# leaves it registered, which is correct -- there is no route that unpublishes
# one, and re-running the workspace is how a hosted site is meant to be updated.
SERVICE = "_fixture-site"
SERVICE_HEADING = "_ui-smoke-hosted-page"

# Distinctive enough to search the whole rendered page for. The point of the
# connections checks is that this string reaches the database and never comes
# back, and "never comes back" is only demonstrable by looking everywhere.
CONNECTION_SECRET = "_ui-smoke-must-not-appear"

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
            connections(page)
            services(page)
            workspaces(page)
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
    print("PASS: every screen renders, creates, edits and deletes cleanly")
    return 0


def schedules(page) -> None:
    print("\nschedules")
    page.goto(BASE + "/ui#/schedules")
    # Scoped to #view throughout. The sign-in <h1> stays in the DOM behind
    # [hidden], so a bare h1 locator matches two elements and waits forever for
    # the wrong one.
    #
    # And waited on by `data-ready` rather than by `#view h1`, because every
    # screen has an h1: arriving from another one, the selector matches the
    # heading still on display and the assertion below reads the old screen.
    # That is a flaky pass, not a failure, which is the worse kind.
    page.wait_for_selector("#view > [data-ready='schedules']")
    # The count is asserted, not just `.first`. This check found a route race --
    # the landing screen's fetch resolving after this screen had rendered, and
    # appending its heading into the same view -- and it found it only half the
    # time, because which of the two headings lands first is a coin toss. One
    # h1 is the claim; `.first` was a weaker one that agreed with the bug.
    heads = page.locator("#view h1")
    found = [heads.nth(i).inner_text() for i in range(heads.count())]
    check("list renders, and nothing else rendered into it",
          found == ["Schedules"], repr(found))

    page.click("#view #create")
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
    page.wait_for_selector("#view > [data-ready='automations']")
    check("list renders", page.locator("#view h1").first.inner_text() == "Automations")

    page.click("#view #create")
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


def connections(page) -> None:
    print("\nconnections")
    page.goto(BASE + "/ui#/connections")
    page.wait_for_selector("#view > [data-ready='connections']")
    check("list renders", page.locator("#view h1").first.inner_text() == "Connections")

    page.click("#view #create")
    page.wait_for_selector("form.panel")

    texts = page.locator("form.panel input[type=text]")
    selects = page.locator("form.panel select")
    kind, scope, targets = selects.nth(0), selects.nth(2), texts.nth(1)

    texts.nth(0).fill(CONNECTION)
    kind.select_option("file")
    # The database refuses a global connection carrying targets, so the form has
    # to refuse it too -- otherwise the only way to learn is a 400 on save.
    check("a global connection cannot carry scope targets", targets.is_disabled())
    scope.select_option("repository")
    check("choosing a narrower scope enables the targets", targets.is_enabled())
    targets.fill("Testing")

    boxes = page.locator("form.panel textarea")
    boxes.nth(0).fill('{"root": "/tmp"}')
    boxes.nth(1).fill('{"password": "%s"}' % CONNECTION_SECRET)
    page.click("form.panel button[type=submit]")

    page.wait_for_selector(f"tr:has-text('{CONNECTION}')")
    row = page.locator(f"tr:has-text('{CONNECTION}')").inner_text()
    check("row shows the scope", "Testing" in row, repr(row))
    check("row says a secret is stored", "stored" in row, repr(row))
    check("the secret is not on the list screen",
          CONNECTION_SECRET not in page.content())

    page.click(f"#view >> tr:has-text('{CONNECTION}') >> text={CONNECTION}")
    page.wait_for_selector("#view >> text=Clear secret")
    boxes = page.locator("form.panel textarea")
    check("config survives the round trip", "/tmp" in boxes.nth(0).input_value())
    # The guarantee the whole store is built around, seen from the screen rather
    # than argued from the schema: the box reopens empty because there is no
    # route that could fill it, and the value is nowhere in the document.
    check("the secret box reopens empty", boxes.nth(1).input_value() == "")
    check("the secret is not on the detail screen",
          CONNECTION_SECRET not in page.content())

    # A real open() of /tmp, not a stub -- the outcome is written to the row and
    # read back, so a Test button that only looked like it worked would show
    # "never tested" here.
    page.click("#view >> button:has-text('Test')")
    page.wait_for_selector("#view .badge.complete")
    check("a real test is recorded and read back",
          "never tested" not in page.locator(".panel").last.inner_text())


def services(page) -> None:
    print("\nservices")

    # The one check here that cannot manufacture its own subject. A hosted
    # service exists only because a job produced a directory, so without a
    # worker there is nothing to publish one -- and a run with no worker waits
    # in the queue until the poll below gives up, which reads as a broken screen
    # rather than a missing process. Say which it is.
    health = page.request.get(BASE + "/health").json()
    if health.get("worker") != "running":
        print("  skip  no worker running -- nothing can publish a service")
        return

    # page.request carries the browser context's cookies, so this is the signed
    # in session submitting a job, not a bearer token borrowed for the occasion.
    r = page.request.post(BASE + "/rest/v1/transformations/submit/Testing/site",
                          data={"params": {"HEADING": SERVICE_HEADING}})
    check("the session can submit a job", r.status == 202, str(r.status))
    if r.status != 202:
        return
    job = r.json()["id"]

    state = {}
    for _ in range(60):
        state = page.request.get(
            f"{BASE}/rest/v1/transformations/jobs/id/{job}").json()
        if state["status"] in ("complete", "failed", "cancelled"):
            break
        page.wait_for_timeout(500)
    check("the job completed", state.get("status") == "complete",
          repr(state.get("status")))

    page.goto(BASE + "/ui#/services")
    page.wait_for_selector(f"#view >> tr:has-text('{SERVICE}')")
    check("list renders", page.locator("#view h1").first.inner_text() == "Services")
    row = page.locator(f"tr:has-text('{SERVICE}')").inner_text()
    check("row shows the type", "static" in row, repr(row))
    check("row credits the workspace", "Testing/site" in row, repr(row))
    # The re-run case, seen from the screen: the row has to name the job that
    # just ran, not the first one that ever published this name.
    check("row names the job that published it", job[:8] in row, repr(row))

    # The part only a browser can show. /serve/ is in COOKIE_PATHS -- unlike
    # /stream/ and /download/, which execute a workspace and so refuse cookies
    # -- because a hosted dashboard that the signed-in person it was built for
    # cannot open is not hosted. A top-level navigation is exactly the request
    # that distinguishes the two, and this is one.
    page.goto(f"{BASE}/serve/{SERVICE}/")
    check("the signed-in browser is served the built page",
          page.locator("h1").inner_text() == SERVICE_HEADING)
    # The stylesheet is not asserted here on purpose: the browser fetches it
    # because the page links it, and a 404 on a subpath is reported by the
    # response watcher. Asserting it as well would only prove httpx works.


def workspaces(page) -> None:
    """The flat catalogue.

    Runs last, after services() has submitted a job against Testing/site, so a
    non-zero Jobs count and a real Last run are present to assert on rather
    than two never-run nulls. It is placement, not a guarantee: services()
    skips itself when no worker is running, and then the count being asserted
    is whatever history the database already held. That is why the assertion
    is "some positive integer" and not a number -- a fixed count would pass or
    fail on whether a worker happened to be up.

    Driven against /ui/v2, not /ui, and that is the one thing here worth
    stopping on. There are two front ends in this repo -- `static/` served at
    /ui and `static-v2/` served at /ui/v2 -- and the Workspaces screen exists
    only in the second. Every other pass above runs against /ui because every
    other screen exists in both. Which of the two ships is an open question;
    until it is answered, a check written against /ui would fail on a stub and
    a check that silently used /ui/v2 for everything would stop testing the UI
    people actually open.
    """
    print("\nworkspaces (v2 only)")
    page.goto(BASE + "/ui/v2#/workspaces")
    page.wait_for_selector("#view > [data-ready='workspaces']")
    check("list renders", page.locator("#view h1").first.inner_text() == "Workspaces")

    # Located by the repository cell's href rather than by text. Every row
    # carries the word "Testing" in that cell, and several carry it in the
    # workspace name as well, so a text locator cannot say which column it
    # matched -- and the column is the point: this screen exists to show the
    # repository beside a workspace the repository tree only shows underneath.
    rows = page.locator("#view tr", has=page.locator("a[href='#/repositories/Testing']"))
    check("Testing workspaces are listed with their repository", rows.count() > 0,
          str(rows.count()))

    site = page.locator(
        "#view tr", has=page.locator("a[href='#/repositories/Testing/site']")
    ).first.inner_text()
    check("the row shows a job count", re.search(r"\b[1-9]\d*\b", site) is not None,
          repr(site))
    # Never is the string the screen prints for a null last_run. Seeing it on
    # the row for a workspace services() just ran would mean the aggregate is
    # joining on something that does not match.
    check("a workspace that has run does not say Never", "Never" not in site,
          repr(site))


def cleanup(page) -> None:
    print("\ncleanup")
    for section, name in (("schedules", SCHEDULE), ("automations", AUTOMATION),
                          ("connections", CONNECTION)):
        page.goto(f"{BASE}/ui#/{section}")
        # `data-ready` is set by route() only after the screen's fetch has
        # resolved and only past its generation check, so it means this exact
        # screen, live, finished -- which is the thing being waited for.
        #
        # It replaces two guesses that each shipped a bug. `#view h1` matched
        # the detail screen still on display, so the count below ran before the
        # list had fetched. Naming the New button dodged that but not the
        # neighbouring list: "New " matched the *previous* list's button, so
        # arriving at Connections straight from Automations the count ran
        # against the old screen and cleanup reported nothing to remove while
        # leaving the row in the database -- a leak that announces itself as
        # success. Both were inferences about rendering; this is the fact.
        page.wait_for_selector(f"#view > [data-ready='{section}']")
        if page.locator(f"tr:has-text('{name}')").count() == 0:
            print(f"  no {name} to remove")
            continue
        page.click(f"#view >> tr:has-text('{name}') >> text={name}")
        page.wait_for_selector("#view >> text=Delete")
        page.click("#view >> text=Delete")
        # The same fact again, and for the same reason. Deleting navigates back
        # to the list, but `#view h1` matched the *detail* screen still on
        # display, so this returned before that navigation had run. The next
        # iteration then issued its own navigation into the gap, the late one
        # landed second and won, and the loop sat on the wrong screen until it
        # timed out -- the connections pass failed having never been shown a
        # connections screen. `data-ready` tells the two apart: the detail
        # screen carries 'automations/367', only the list carries 'automations'.
        page.wait_for_selector(f"#view > [data-ready='{section}']")
        check(f"{name} deleted", page.locator(f"tr:has-text('{name}')").count() == 0)


if __name__ == "__main__":
    sys.exit(main())
