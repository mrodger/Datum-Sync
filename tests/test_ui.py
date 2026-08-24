"""The web UI: its shell, its assets, and the routes only the browser uses.

Two kinds of test live here. The HTTP ones drive the app through an ASGI
transport, as everywhere else. The rest read `static/app.js` as text and assert
properties of the source, which is unusual enough to say why: the UI is vanilla
JavaScript with no build step and no test runner, so there is nothing between
what is written and what a browser executes. A property that must hold of every
line of it -- "this file never assigns markup" -- has no other place to be
checked, and checking it here costs one file read.

What is *not* claimed: none of this proves the UI renders. It proves the shell
is reachable without a credential, that the routes it depends on exist and
behave, and that the one source-level invariant holds. Rendering is a browser's
job and would need a browser to test.
"""
from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from datum_sync import config, uploads
from datum_sync import db as db_module
from datum_sync.api import app
from datum_sync.ui import STATIC_DIR

APP_JS = (STATIC_DIR / "app.js").read_text()
ROOT = STATIC_DIR.parents[1]


@pytest_asyncio.fixture
async def client(db, token):
    await db_module.init_pool()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"authorization": f"Bearer {token}"},
    ) as c:
        yield c
    await db_module.close_pool()


@pytest_asyncio.fixture
async def anon(db):
    """No credential at all. The shell has to be served to this client."""
    await db_module.init_pool()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        yield c
    await db_module.close_pool()


# --------------------------------------------------------------------------
# the shell
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_shell_is_served_without_a_credential(anon):
    """Gating it would only mean serving a 401 page instead of a sign-in page.

    It carries no data: the markup is empty chrome, and the first thing the
    script does is ask /whoami who the viewer is.
    """
    r = await anon.get("/ui")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "signin-form" in r.text


@pytest.mark.asyncio
async def test_the_shell_contains_no_data(anon):
    """The markup names no account, repository or job. If it ever did, the
    public shell would be leaking whatever it named."""
    body = (await anon.get("/ui")).text
    assert "_pytest" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("asset", ["app.js", "style.css"])
async def test_the_static_assets_are_served_without_a_credential(anon, asset):
    """They are fetched by a <script>/<link> on a page nobody has signed in to
    yet, so a 401 here means the sign-in form cannot be styled or submitted."""
    r = await anon.get(f"/ui/static/{asset}")
    assert r.status_code == 200
    assert r.content


@pytest.mark.asyncio
async def test_the_static_mount_does_not_escape_its_directory(anon):
    """The mount is a filesystem path joined with user input, which is the
    shape of every traversal bug, and the consequence here is reading the
    server's own source.

    Two spellings, refused by two different things, which is why the assertion
    admits two codes:

    - `../ui.py` never leaves as a traversal. httpx resolves the dots before
      sending, so what arrives is `/ui/ui.py` -- an ordinary path, not under
      the public prefix, refused by the auth middleware with 401. This proves
      the middleware fails closed; it says nothing about the mount.
    - `..%2Fui.py` is one path segment as far as any URL parser is concerned,
      so it arrives intact at `/ui/static/../ui.py`. This is the one that
      reaches StaticFiles, and 404 is StaticFiles refusing to leave its root.

    Keeping both is deliberate: the first is what a browser sends, the second
    is what an attacker sends.
    """
    for attempt in ("../ui.py", "..%2Fui.py", "%2e%2e%2fui.py",
                    "../../migrations/001_core.sql"):
        r = await anon.get(f"/ui/static/{attempt}")
        assert r.status_code in (401, 404), (attempt, r.status_code)
        assert "STATIC_DIR" not in r.text, attempt


# --------------------------------------------------------------------------
# the one invariant of app.js
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_ui_never_assigns_markup():
    """Every sink that parses a string as HTML, refused in one place.

    The UI builds the whole page out of API data: repository names, workspace
    descriptions, job errors, account names. A runner exists to execute code
    other people published, so "it is only our own data" is false here by
    design, and one interpolation into innerHTML is stored XSS against every
    signed-in user. The HttpOnly cookie means the injected script could not
    read the credential -- but it would not need to, since it runs on the page
    and can drive the admin routes as the viewer.

    A grep is a blunt instrument and would flag a legitimate use. There is no
    legitimate use in this file: el() covers the cases.

    Comment lines are dropped first, because the first run of this test failed
    on app.js's own comment explaining the rule. Stripping them narrows the
    check to what a browser executes -- at the cost that a trailing comment
    naming a sink still fails. That is the right trade: prose about the rule
    belongs in a block comment, and a full JS parser to allow otherwise would
    be more machinery than the property is worth.
    """
    sinks = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")
    code = "\n".join(
        line for line in APP_JS.splitlines()
        if not line.lstrip().startswith(("//", "/*", "*"))
    )
    found = [s for s in sinks if s in code]
    assert found == [], f"app.js reaches for {found}; build nodes with el()"


@pytest.mark.asyncio
async def test_the_ui_talks_only_to_its_own_origin():
    """No CDN, no font host, no analytics. A self-hosted runner on a network
    with no route to the internet must still render its own sign-in page, and
    every third-party origin on this page is one that could serve script into
    a session.

    AUTOMATION_TEMPLATE is cut out first, and that exclusion is the interesting
    part of this test. It contains `https://example.com/hook` -- not an origin
    the browser ever touches, but the starter text for a YAML document the
    *server* will later fetch. A line grep cannot tell those two apart, so it
    flagged it, and the temptation was to spell the URL in pieces to get past
    the check. That would leave a test that says "no third-party origins" and
    means "no third-party origins written in one go".

    Cutting the constant by name is narrower and honest: the property still
    holds over every line the browser executes, and the one place it does not
    apply is named. The index() calls are what stop the exclusion from
    quietly widening -- rename or delete the constant and this fails rather
    than silently skipping nothing, or everything.
    """
    start = APP_JS.index('const AUTOMATION_TEMPLATE')
    end = APP_JS.index('].join(', start)
    code = APP_JS[:start] + APP_JS[end:]

    for marker in ("http://", "https://"):
        for line in code.splitlines():
            if marker in line:
                # The SVG namespace is a identifier, not a fetch.
                assert "w3.org/2000/svg" in line, line


@pytest.mark.asyncio
async def test_the_progress_bar_reads_pct_as_a_fraction():
    """`pct` is 0.0-1.0 despite its name -- spec/workspace-contract.md says so
    and every workspace emits it that way.

    This is a source check, which proves only that the scaling is written, not
    that the bar moves; a browser proved that once by hand. It is here because
    the units are a published contract on one side and a number in a stylesheet
    on the other, with nothing in between to notice they disagree. The first
    draft of the handler rendered a completed job at 1%.
    """
    for repo_main in (ROOT / "repositories").glob("*/*/main.py"):
        for line in repo_main.read_text().splitlines():
            if '"pct"' not in line:
                continue
            value = line.split('"pct"')[1].split(",")[0].lstrip(": ")
            if _is_number(value):
                assert float(value) <= 1.0, f"{repo_main}: {line.strip()}"

    handler = APP_JS.split("addEventListener('progress'")[1].split("});")[0]
    assert "* 100" in handler, "progress handler no longer scales the fraction"


def _is_number(text):
    try:
        float(text)
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------------
# uploads: the id a FILE parameter carries
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_upload_returns_an_id_that_resolves_to_the_bytes(client):
    r = await client.post(
        "/rest/v1/uploads", files={"file": ("sample.txt", b"hello", "text/plain")}
    )
    assert r.status_code == 201
    body = r.json()
    assert body["filename"] == "sample.txt"
    assert body["bytes"] == 5

    stored = uploads.resolve(body["id"])
    try:
        assert stored.read_bytes() == b"hello"
    finally:
        for f in stored.parent.iterdir():
            f.unlink()
        stored.parent.rmdir()


@pytest.mark.asyncio
async def test_an_upload_id_is_not_a_path(client):
    """The whole reason FILE parameters carry an id: what comes back must not
    be somewhere the caller could have chosen."""
    body = (await client.post(
        "/rest/v1/uploads", files={"file": ("../../etc/passwd", b"x", "text/plain")}
    )).json()
    stored = uploads.resolve(body["id"])
    try:
        assert stored.parent.parent == config.DATA_PATH / "uploads"
        assert "/" not in body["filename"]
        assert ".." not in body["filename"]
    finally:
        for f in stored.parent.iterdir():
            f.unlink()
        stored.parent.rmdir()


@pytest.mark.asyncio
async def test_an_upload_needs_exactly_one_file(client):
    """Zero and two are both refused. Two is the interesting one: silently
    keeping the first would store a file the caller believed they had sent
    under a different parameter."""
    empty = await client.post("/rest/v1/uploads", data={"not": "a file"})
    assert empty.status_code == 400
    assert empty.json()["code"] == "INVALID_PARAMETER"

    two = await client.post("/rest/v1/uploads", files=[
        ("a", ("one.txt", b"1", "text/plain")),
        ("b", ("two.txt", b"2", "text/plain")),
    ])
    assert two.status_code == 400


@pytest.mark.asyncio
async def test_uploading_requires_a_credential(anon):
    r = await anon.post(
        "/rest/v1/uploads", files={"file": ("x.txt", b"x", "text/plain")}
    )
    assert r.status_code == 401


# --------------------------------------------------------------------------
# the job listing filters the workspace screen depends on
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_job_listing_filters_by_workspace(client, db, workspace):
    """The workspace screen's "recent jobs" asks for this. Filtering the
    result of a limited query instead would show nothing whenever ten other
    jobs ran more recently -- the same mistake as the scope filter, with a
    milder consequence.
    """
    repo, ws = workspace
    mine = (await client.post(
        f"/rest/v1/transformations/submit/{repo}/{ws}", json={"params": {"WHO": "x"}}
    )).json()["id"]

    # Newer, and in another repository: with the filter applied after LIMIT
    # this row would take the only slot.
    await db.execute(
        "INSERT INTO jobs (repository, workspace, submitted_by) VALUES ($1, $2, $3)",
        "other-repo", "elsewhere", "someone",
    )
    try:
        r = await client.get(
            f"/rest/v1/transformations/jobs?limit=1&repository={repo}&workspace={ws}"
        )
        items = r.json()["items"]
        assert [j["id"] for j in items] == [mine]
    finally:
        await db.execute("DELETE FROM jobs WHERE repository = $1", "other-repo")


@pytest.mark.asyncio
async def test_the_workspace_filter_cannot_widen_scope(client, db, workspace):
    """Naming a repository the caller is not scoped to returns nothing, rather
    than reaching past the scope filter. Both clauses are ANDed in SQL, so this
    is really a test that neither replaced the other."""
    repo, ws = workspace
    await db.execute(
        "INSERT INTO jobs (repository, workspace, submitted_by) VALUES ($1, $2, $3)",
        "other-repo", "elsewhere", "someone",
    )
    try:
        await db.execute(
            "UPDATE service_accounts SET repo_scope = $1 WHERE name = '_pytest'",
            [repo],
        )
        r = await client.get("/rest/v1/transformations/jobs?repository=other-repo")
        assert r.status_code == 200
        assert r.json()["items"] == []
    finally:
        await db.execute("DELETE FROM jobs WHERE repository = $1", "other-repo")
