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

import re

import httpx
import pytest
import pytest_asyncio

from datum_sync import config, uploads
from datum_sync import db as db_module
from datum_sync.api import app
from datum_sync.ui import STATIC_DIR, STATIC_V2_DIR

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
@pytest.mark.parametrize("path", ["/ui", "/ui/static/app.js", "/ui/static/style.css"])
async def test_the_ui_is_revalidated_rather_than_assumed_fresh(anon, path):
    """A response carrying neither Cache-Control nor Expires may be cached on a
    *guess* -- roughly a tenth of its age -- and served without asking. That is
    what shipped an old stylesheet to a demo audience with no error anywhere:
    not a stale asset served after a check, but one served with no check, which
    nothing the server does can dislodge.

    The shell is in here too. It names the assets, so caching it hides a change
    to any of them.
    """
    r = await anon.get(path)
    assert r.status_code == 200
    assert "no-cache" in r.headers.get("cache-control", "")


@pytest.mark.asyncio
async def test_revalidation_of_an_unchanged_asset_costs_nothing(anon):
    """`no-cache` means "ask", not "send it again" -- the point of paying a
    conditional request is that the answer is usually an empty 304. If this
    starts returning 200 with a body, every page load is re-downloading the UI.
    """
    etag = (await anon.get("/ui/static/style.css")).headers["etag"]
    r = await anon.get("/ui/static/style.css", headers={"If-None-Match": etag})
    assert r.status_code == 304
    assert not r.content


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
# the v2 shell
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_v2_shell_is_served_without_a_credential(anon):
    """v2 is a second shell beside v1, not a replacement, so the two can be
    opened side by side while the port runs. It is public for the same reason
    v1 is: it contains no data and draws a sign-in form off a 401."""
    r = await anon.get("/ui/v2")
    assert r.status_code == 200
    assert "signin-form" in r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/ui/v2", "/v2/style.css"])
async def test_the_v2_ui_is_revalidated_rather_than_assumed_fresh(anon, path):
    r = await anon.get(path)
    assert r.status_code == 200
    assert "no-cache" in r.headers.get("cache-control", "")


@pytest.mark.asyncio
async def test_every_asset_the_v2_shell_names_is_served(anon):
    """The shell is a list of asset URLs, and a wrong one fails silently: a
    404'd stylesheet is an unstyled page, a 404'd script is a page that does
    nothing, and neither says so anywhere but the console. This asserts the
    list rather than a hardcoded copy of it, so adding a tag adds a check.

    The shell's own tags are not the whole list. app.js is an ES module and
    imports icons.js, which appears in no tag and so is invisible to a scan of
    the markup -- while being the harsher failure of the two: a 404 on an
    import aborts the entire module, so the page renders nothing at all rather
    than rendering without icons. The import specifiers are collected too.
    """
    shell = (STATIC_V2_DIR / "index.html").read_text()
    named = re.findall(r'(?:src|href)="(/v2/[^"]+)"', shell)
    assert "/v2/app.js" in named, "the shell no longer loads app.js"

    imported = re.findall(r'''from\s+['"](/v2/[^'"]+)['"]''',
                          (STATIC_V2_DIR / "app.js").read_text())
    assert "/v2/icons.js" in imported, "app.js no longer imports the icon set"

    for url in named + imported:
        r = await anon.get(url)
        assert r.status_code == 200, (url, r.status_code)
        assert r.content


@pytest.mark.asyncio
async def test_the_v2_shell_loads_app_js_as_a_module():
    """app.js imports icons.js, so the tag must say `type="module"`.

    Without it the browser parses the file as a classic script, hits the
    import statement, and throws a SyntaxError before executing a line. The
    page is then blank with both panes hidden -- indistinguishable from the
    pre-chunk-1 state where there was no app.js at all, which is exactly the
    symptom this project has already spent a session chasing once.
    """
    shell = (STATIC_V2_DIR / "index.html").read_text()
    tag = re.search(r'<script[^>]*/v2/app\.js"[^>]*>', shell)
    assert tag, "the shell no longer loads app.js"
    assert 'type="module"' in tag.group(0), tag.group(0)


@pytest.mark.asyncio
async def test_every_v2_icon_is_a_single_path():
    """app.js lifts the `d` out of each entry in icons.js rather than assigning
    the markup, which is what keeps the innerHTML ban whole (see
    test_the_v2_ui_never_assigns_markup). That extraction is only faithful
    while every icon really is one <path> carrying one attribute.

    An icon with a second element would have it dropped and still render --
    slightly wrong, never an error, and nowhere near the code that caused it.
    So the shape is asserted over the file rather than trusted at the call.
    """
    src = (STATIC_V2_DIR / "icons.js").read_text()
    entries = re.findall(r'^\s*"([\w-]+)":\s*"(.*)",$', src, re.M)
    assert len(entries) >= 50, f"only found {len(entries)} icons; parser drifted"
    for name, markup in entries:
        assert re.fullmatch(r'<path d=\\"[^"]+\\"/>', markup), name


@pytest.mark.asyncio
async def test_every_v2_icon_app_js_asks_for_exists():
    """A misspelled icon name renders an empty <svg>, not an error.

    icon() looks the name up in ICON_PATHS and appends a <path> only if it
    found one, because the alternative is throwing inside a render. So a typo,
    or an icon renamed in icons.js, produces a correctly sized invisible box in
    the middle of a working page -- a missing glyph that no console, no test
    and no HTTP status reports. The nav in particular takes its glyph from the
    section id, so renaming a section is enough to do it.

    Five sources of a name, each with its own floor so that a regex which stops
    matching fails here rather than passing over an empty set: the icon() call
    sites, the BADGE_GLYPHS table, the pager's arrow() calls, cellName()'s
    leading argument, and the section ids -- the nav builds its glyph from the
    id, so renaming a section is enough to lose its icon with no literal
    anywhere naming the glyph.

    cellName() is here because chunk 2 wrote the gap down and chunk 3 walked
    into it: a glyph that reaches icon() as somebody else's argument names no
    icon() call site, so every list row's folder was invisible to the four
    sources above. Still not total -- the dashboard's create tiles pass a glyph
    through a table of literals, and are covered only because each name they
    use happens to be a section id as well.
    """
    code = (STATIC_V2_DIR / "app.js").read_text()
    icons_src = (STATIC_V2_DIR / "icons.js").read_text()
    available = set(re.findall(r'^\s*"([\w-]+)":\s*"', icons_src, re.M))
    assert len(available) >= 50, f"only found {len(available)} icons; parser drifted"

    calls = set(re.findall(r"""\bicon\(\s*'([\w-]+)'""", code))
    arrows = set(re.findall(r"""\barrow\('[\w ]+',\s*'([\w-]+)'\)""", code))
    cells = set(re.findall(r"""\bcellName\(\s*'([\w-]+)'""", code))
    # Scoped to SECTIONS: elsewhere `id:` is an element attribute, and
    # buildNav's `id: 'nav-' + section.id` would otherwise read as a glyph.
    sections = re.search(r"const SECTIONS = \[(.*?)\n\];", code, re.S)
    assert sections, "SECTIONS is gone or no longer parseable"
    ids = set(re.findall(r"id:\s*'([\w-]+)'", sections.group(1)))
    table = re.search(r"const BADGE_GLYPHS = \{(.*?)\};", code, re.S)
    assert table, "BADGE_GLYPHS is gone; badge() no longer carries a glyph"
    badges = set(re.findall(r":\s*'([\w-]+)'", table.group(1)))

    for label, names, floor in (("icon() calls", calls, 4), ("BADGE_GLYPHS", badges, 4),
                                ("pager arrows", arrows, 4), ("section ids", ids, 20),
                                ("cellName() glyphs", cells, 1)):
        assert len(names) >= floor, f"only found {len(names)} {label}; parser drifted"

    asked = calls | arrows | ids | badges | cells
    assert asked <= available, f"no such icon: {sorted(asked - available)}"


@pytest.mark.asyncio
async def test_every_v2_hash_link_names_a_screen_that_exists():
    """An unrouteable href lands on "Not found", quietly, one click away.

    The dashboard mockup points its first create tile at `#/run`, which is not
    a section -- copying that markup faithfully would have shipped a dead tile
    on the first screen anybody sees, and it would look like a working link
    until pressed. route() cannot help: an unknown section is exactly how a
    mistyped URL arrives, so it has to render "Not found" rather than throw.

    Only the section is checked. What follows it is an id, a `new`, or a query
    the screen parses for itself.
    """
    code = (STATIC_V2_DIR / "app.js").read_text()
    screens = re.search(r"const SCREENS = \{(.*?)\n\};", code, re.S)
    assert screens, "SCREENS is gone or no longer parseable"
    known = set(re.findall(r"^\s*'?([\w-]+)'?:", screens.group(1), re.M))
    assert len(known) >= 20, f"only found {len(known)} screens; parser drifted"

    linked = {h.split("?")[0].split("/")[0]
              for h in re.findall(r"'#/([\w-]+[^']*)'", code)}
    assert len(linked) >= 5, f"only found {len(linked)} hash links; parser drifted"
    assert linked <= known, f"no such screen: {sorted(linked - known)}"


@pytest.mark.asyncio
async def test_no_v2_toolbar_button_is_woken_up_with_nothing_behind_it():
    """table.js decides `disabled` for every [data-needs] button, on its own.

    On each selection change it assigns `btn.disabled = need === 'one' ? n !== 1
    : n < 1` over the whole action bar (table.js 164-167). It does not consult
    what the button was built with, so `data-needs` plus a null handler is a
    button that sits correctly greyed until a row is ticked and then lights up
    and does nothing when pressed -- which reads as a broken feature rather
    than an absent one, and reads that way only to somebody who ticks a row
    first. Every repositories button is in this position: publishing is a CLI
    operation and there is no POST, PUT or DELETE under /rest/v1/repositories
    at all.

    So the rule is a pairing, checked both ways round. `needs` means table.js
    will make this clickable, therefore there must be something to click. `off`
    means there is nothing, therefore it must not carry `needs` -- and action()
    drops the handler on that path too, so a later edit that adds one without
    removing `off` fails here instead of silently doing nothing.
    """
    code = (STATIC_V2_DIR / "app.js").read_text()
    calls = re.findall(r"\baction\(\s*'([^']+)',\s*([^,]+?),\s*\{([^}]*)\}\)", code)
    assert len(calls) >= 4, f"only found {len(calls)} action() calls; parser drifted"

    for label, handler, opts in calls:
        handler = handler.strip()
        if "needs:" in opts:
            assert handler != "null", f"{label!r} is selection-gated with no handler"
        if "off:" in opts:
            assert "needs:" not in opts, f"{label!r} is both off and selection-gated"
            assert handler == "null", f"{label!r} is off but was passed a handler"


@pytest.mark.asyncio
async def test_every_v2_submit_handler_stops_the_browser_submitting():
    """A submit handler that does not preventDefault loses the whole request.

    The browser's own submit runs: it serialises the form into a GET on the
    current URL and navigates. In an SPA that is a full reload -- the page
    comes back looking almost right, freshly signed in, on the same screen --
    while the fetch the handler started is torn down mid-flight. Whether the
    POST reached the server at all is a race, so the same button either runs
    the workspace or does nothing, depending on timing, and either way it looks
    like a page that merely refreshed.

    Chunk 4 is the first v2 screen with a form; the schedule and connection
    forms in chunks 6 and 8 are the same shape. There is one form-level rule,
    so it is checked once, over every submit listener in the file.
    """
    code = (STATIC_V2_DIR / "app.js").read_text()
    # Each handler body, from the listener up to the closing `});` at column 0
    # of its own statement -- enough to see whether preventDefault is in it.
    handlers = re.findall(
        r"addEventListener\('submit',[^\n]*\n(.*?)\n\s*\}\);", code, re.S)
    assert len(handlers) >= 2, f"only found {len(handlers)} submit handlers; parser drifted"
    for body in handlers:
        assert "preventDefault()" in body, \
            "a submit handler lets the browser navigate: " + body.strip()[:80]


def _brace_body(src: str, at: int) -> str:
    """The `{ ... }` block starting at or after `at`, brace-matched."""
    start = src.index("{", at)
    depth, i = 0, start
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1
    raise AssertionError("unbalanced braces from offset %d" % at)


@pytest.mark.asyncio
async def test_no_v2_list_repolls_itself_out_from_under_a_selection():
    """A repoll on a screen with a table throws the reader's selection away.

    The two features are innocent apart and hostile together. A live list
    repolls by calling route(), which is a full rebuild -- new container, new
    rows, a new table.js instance whose `selected` Set starts empty. A table
    with tick boxes puts a selection in front of the reader and a toolbar that
    acts on it. Put both on one screen and the reader has one timer period to
    tick their rows and press the button, and beats it or does not depending on
    when in the cycle they arrived.

    What makes it worth a test rather than a comment is that it does not look
    like a bug. The ticks disappear at the same instant the rows redraw, so it
    reads as the page refreshing, and the toolbar greys out again as if nothing
    had been chosen. Nobody reports it; they tick the rows again.

    It also lands where it hurts most. A list only repolls when something on it
    is unfinished, which on the jobs screen means the Queued and Running tabs
    -- the two where Cancel is the reason you opened the page.

    So: in a screen that hands its table to table.js, a timer that re-routes
    must consult the selection first. Screens whose table has no tick boxes are
    not subject to it and are not listed here -- the dashboard's five-row table
    is deliberately never passed to mountTable(), so it has no selection to
    lose and repolls freely.
    """
    code = (STATIC_V2_DIR / "app.js").read_text()

    screens = [(m.group(1), m.start()) for m in
               re.finditer(r"^async function (screen\w+)\(", code, re.M)]
    assert len(screens) >= 6, f"only found {len(screens)} screens; parser drifted"
    bounds = [(name, at, screens[i + 1][1] if i + 1 < len(screens) else len(code))
              for i, (name, at) in enumerate(screens)]

    checked = 0
    for name, at, end in bounds:
        body = code[at:end]
        # A real call, not a mention. screenDashboard's own comment says its
        # table is "Not handed to mountTable()", and a substring test reads
        # that as the opposite of what it says.
        if not re.search(r"^\s*(?:const \w+ = )?mountTable\(", body, re.M):
            continue
        for call in re.finditer(r"setTimeout\(\s*([A-Za-z_$][\w$]*)\s*,", body):
            cb = call.group(1)
            checked += 1
            assert cb != "route", (
                f"{name} repolls straight into route() on a timer, over a table "
                "whose selection that rebuild discards")
            defn = re.search(r"\b(?:const|let|var)\s+" + re.escape(cb) + r"\s*=", body)
            assert defn, f"{name}: cannot find the definition of timer callback {cb!r}"
            assert "selection()" in _brace_body(body, defn.end()), (
                f"{name}: timer callback {cb!r} re-routes without consulting the "
                "selection, so a reader's ticked rows are thrown away mid-click")

    assert checked, "no timer on a selectable list was found; the rule matched nothing"


def test_v2_invents_no_css_classes():
    """Every class name app.js writes has to exist somewhere real.

    A class the stylesheet has never heard of is the worst kind of mistake to
    make here, because it does not look like one. The element renders, the
    page does not break, and the div simply has no styling -- which reads as a
    deliberately plain div. Chunk 6 wrote `.form-actions` for a form footer on
    the strength of it sounding like a class this design system would have.
    It is not one; the idiom is `.action-bar > .actions`. Nothing else in the
    suite would ever have said so.

    Two sources count as real, not one:

      - style.css, which is the locked artifact and the usual answer; and
      - the mockups' own markup, because a few classes are legitimately
        unstyled. `.dash-main` and `.dash-rail` are the two grid CHILDREN of
        `.dash-grid`: the parent's grid-template-columns places them and they
        carry no rules of their own. Requiring a rule per class would have
        failed on those two, and the honest reading is not that they are
        wrong -- it is that the designer wrote them, so they came from
        somewhere.

    Only literals are checked. `class: 'badge ' + status` and
    `class: o.primary ? null : 'secondary'` are expressions, and this does not
    evaluate them; the string half of the latter is still caught, since it is
    matched as a literal wherever one appears.
    """
    app = (STATIC_V2_DIR / "app.js").read_text()

    known = set(re.findall(r"\.([A-Za-z][\w-]*)",
                           (STATIC_V2_DIR / "style.css").read_text()))
    mocks = sorted(STATIC_V2_DIR.glob("mock-*.html"))
    assert mocks, "no mockups found; the second source of truth vanished"
    for mock in mocks:
        for m in re.finditer(r'class="([^"]*)"', mock.read_text()):
            known.update(m.group(1).split())

    used = {}
    for m in re.finditer(r"""class:\s*(['"])([^'"]*)\1""", app):
        line = app.count("\n", 0, m.start()) + 1
        for name in m.group(2).split():
            used.setdefault(name, line)

    assert len(used) > 40, f"only {len(used)} class literals found; parser drifted"
    missing = sorted((n, line) for n, line in used.items() if n not in known)
    assert not missing, "app.js writes class names that exist nowhere: " + ", ".join(
        f"{n!r} (app.js:{line})" for n, line in missing)


def _call_args(src: str, open_paren: int) -> list[str]:
    """Split a JS call's top-level arguments, given the index of its `(`.

    A regex cannot do this: the arguments are nested calls containing commas,
    strings containing brackets and comments containing both. This walks the
    text once, tracking bracket depth and skipping over string literals and
    comments, and splits only on commas at depth 1.
    """
    depth, args, cur, i, n = 0, [], [], open_paren, len(src)
    while i < n:
        c = src[i]
        if src.startswith("//", i):
            i = src.index("\n", i)
        elif src.startswith("/*", i):
            i = src.index("*/", i) + 2
        elif c in "'\"`":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            cur.append(src[i:j + 1])
            i = j + 1
        elif c in "([{":
            depth += 1
            if depth > 1:
                cur.append(c)
            i += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                args.append("".join(cur))
                return args
            cur.append(c)
            i += 1
        elif c == "," and depth == 1:
            args.append("".join(cur))
            cur = []
            i += 1
        else:
            cur.append(c)
            i += 1
    raise AssertionError(f"unbalanced call at offset {open_paren}")


def test_v2_never_appends_a_child_that_can_be_nothing():
    """A conditional child may go to append(), never to node.append().

    They are two different functions and only one of them is ours. app.js's
    append() drops null, undefined and false, which is what lets el() take a
    conditional child -- `glyph ? icon(glyph) : null` -- and render nothing.
    Node.append is the DOM's, and it stringifies: hand it null and the page
    gets the four characters "null".

    Chunk 7 did exactly that. `a.last_error ? el('div', ...) : null` went
    straight into view.append(), and an automation with no error printed a blue
    "null" under its own title. Nothing in the suite could see it, and neither
    could the browser script -- it read the badge, the toggle, the kv pairs and
    the editor contents, all correct. Only the screenshot showed it.

    So the rule is about the receiver, not the value: a dotted `.append(` is the
    DOM's and must be handed nodes and strings only; the bare `append(node, [])`
    is ours and is where a conditional child belongs.
    """
    src = (STATIC_V2_DIR / "app.js").read_text()

    calls, bad = 0, []
    for m in re.finditer(r"\.append\(", src):
        calls += 1
        for arg in _call_args(src, m.end() - 1):
            tail = arg.split()[-1] if arg.split() else ""
            if tail in ("null", "undefined", "false"):
                bad.append((src.count("\n", 0, m.start()) + 1,
                            " ".join(arg.split())[-60:]))

    assert calls > 20, f"only {calls} .append() calls found; the parser drifted"
    assert not bad, (
        "a child that can evaluate to nothing was handed to the DOM's append, "
        "which will render it as text -- use append(node, [...]) instead: "
        + "; ".join(f"app.js:{line} ...{text}" for line, text in bad))


def test_v2_labels_every_automation_action():
    """The list's Actions column is a second vocabulary, and it can drift.

    The server stores `run_workspace` and `http_request`; the mockup writes
    "run workspace" and "webhook", so app.js carries a map between them. Add a
    third action type in automations.py and nothing on this side knows about
    it -- the tests for the automation engine would all pass, and the column
    would be the only thing that was wrong.

    The fallback in app.js keeps that from being a blank cell: an unmapped type
    renders as its own name with the underscores opened up. This test is what
    keeps the fallback from being the answer for a type we could have named
    properly, and it fails in both directions -- a label left behind after a
    type is removed is drift too, pointing at an action that cannot happen.
    """
    from datum_sync import automations

    src = (STATIC_V2_DIR / "app.js").read_text()
    block = re.search(r"const ACTION_LABELS = \{(.*?)\n\};", src, re.S)
    assert block, "ACTION_LABELS is gone from app.js; the Actions column is unmapped"
    labelled = set(re.findall(r"(\w+):", block.group(1)))

    assert labelled, "ACTION_LABELS parsed as empty; the parser drifted"
    assert labelled == set(automations.ACTIONS), (
        "app.js and automations.py disagree about what an automation can do -- "
        f"unlabelled: {sorted(set(automations.ACTIONS) - labelled)}, "
        f"stale: {sorted(labelled - set(automations.ACTIONS))}")


def test_every_v2_form_label_names_its_control():
    """A `<label>` with no `for` is decoration, and it looks identical.

    The text sits where a label sits and is styled like one, so a screenshot
    cannot tell the two apart. What is missing is only visible by trying it:
    clicking the word does not focus the field, and a screen reader announces
    the control with no name at all -- on a form whose fields are called
    "Scope targets" and "Secret", where the name is the only thing saying what
    to type.

    v1's connection form is nine bare labels, and chunk 8 is the largest form
    in the app -- porting it faithfully would have tripled the count of
    unlabelled controls in one commit. Chunks 4 and 6 already do it properly,
    so this is a rule the file follows and nothing was enforcing.

    Checked both halves, because the second is the one a rename breaks. A
    `for` naming an id that no control carries is worth less than no `for` at
    all: it looks correct in the source and fails in exactly the same way on
    the page. Only the control tags are read for ids -- `id:` also appears in
    SECTIONS and in the two tab tables, where it names a route rather than an
    element, and letting those count would let a label point at one.
    """
    src = (STATIC_V2_DIR / "app.js").read_text()

    labels = re.findall(r"el\('label',\s*\{([^}]*)\}", src)
    assert len(labels) >= 3, f"only found {len(labels)} labels; the parser drifted"
    for attrs in labels:
        assert "for:" in attrs, \
            "a <label> names no control: el('label', {" + attrs + "}"

    ids = set(re.findall(
        r"el\('(?:input|select|textarea)',\s*\{[^}]*?\bid:\s*'([\w-]+)'", src))
    assert len(ids) >= 8, f"only found {len(ids)} control ids; the parser drifted"

    # Every label in the file goes through one of the local fieldOf() helpers,
    # which take the id as their first argument and write it into both the
    # label's `for` and nothing else -- so the call sites are where a
    # mismatch is visible.
    targets = re.findall(r"\bfieldOf\('([\w-]+)'", src)
    assert len(targets) >= 10, f"only found {len(targets)} fields; the parser drifted"
    missing = sorted(set(targets) - ids)
    assert not missing, (
        "a form label points at an id no input, select or textarea carries, so "
        "the label is inert and the control is unnamed: " + ", ".join(missing))


@pytest.mark.asyncio
async def test_the_v2_mount_does_not_escape_its_directory(anon):
    """Same property as the v1 mount, and it has to be asserted separately:
    the two are separate StaticFiles instances under separate prefixes, and
    `/v2/` is a public prefix, so nothing else refuses these.
    """
    for attempt in ("../ui.py", "..%2Fui.py", "%2e%2e%2fui.py",
                    "../../migrations/001_core.sql"):
        r = await anon.get(f"/v2/{attempt}")
        assert r.status_code in (401, 404), (attempt, r.status_code)
        assert "STATIC_V2_DIR" not in r.text, attempt


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
async def test_the_v2_ui_never_assigns_markup():
    """The same invariant over the v2 shell, asserted separately because it is
    a separate file that the test above cannot see.

    It is not a copy of a passing test. v2 has a sink v1 never had: icons.js
    ships an `icon()` that does `svg.innerHTML = ICONS[name]`, and importing it
    is a one-line change that would read as the obvious thing to do. app.js
    lifts the path data out instead. The string is icons.js's own constant, so
    using it would not actually be an injection -- which is the point. A ban
    with one sanctioned exception in it stops being greppable, and the next
    call site borrows the exception rather than the reasoning.

    icons.js itself is not scanned: it is vendored, generated, and never sees
    API data. This asserts the property over the file that renders it.
    """
    sinks = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")
    code = "\n".join(
        line for line in (STATIC_V2_DIR / "app.js").read_text().splitlines()
        if not line.lstrip().startswith(("//", "/*", "*"))
    )
    found = [s for s in sinks if s in code]
    assert found == [], f"v2 app.js reaches for {found}; build nodes with el()"


@pytest.mark.asyncio
async def test_the_v2_ui_leaves_the_nav_toggle_to_nav_collapse_js():
    """Two click handlers on one button is a toggle that flips twice.

    v1's app.js binds #nav-toggle itself (its line 217). v2 has
    nav-collapse.js, which binds the same button and adds the persistence v1
    never had, and both scripts load into the same page. Porting v1's line as
    well gives a button that reads the collapsed state, flips it, and flips it
    straight back -- so the nav does not move, no error is raised, and the
    symptom is indistinguishable from a handler that was never attached.
    """
    code = (STATIC_V2_DIR / "app.js").read_text()
    assert "nav-toggle" not in code.replace("// No handler for #nav-toggle", "")


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
