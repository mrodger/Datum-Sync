"""Automations: the YAML door, the loop guards, egress, firing, and the routes.

Most of this file is about things that must NOT happen. An automation runs
later, unattended, with nobody watching -- so every failure here is one that
gets discovered from its consequences rather than from a stack trace.
"""
from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest
import pytest_asyncio

from datum_sync import auth, automations, jobs
from datum_sync import db as db_module
from datum_sync.api import app

UTC = dt.timezone.utc

GOOD = """
name: _pytest-auto
trigger: {type: job_complete, repository: Testing, workspace: chatty}
actions:
  - type: run_workspace
    repository: Testing
    workspace: slow
    params: {SECONDS: 1}
"""


# --------------------------------------------------------------------------
# the YAML door
# --------------------------------------------------------------------------

def test_a_python_object_tag_is_not_constructed():
    """`yaml.load` would call os.system here. `safe_load` refuses the tag.

    This is the single highest-consequence line in the module: the YAML is
    typed by a user into a text box, so the default loader turns that box into
    remote code execution. The test asserts the refusal rather than the absence
    of a side effect, because a side effect that did happen would not fail any
    assertion -- it would just have happened.
    """
    with pytest.raises(automations.AutomationError) as caught:
        automations.parse(
            "name: x\n"
            "trigger: {type: job_complete}\n"
            "actions: !!python/object/apply:os.system ['true']\n"
        )
    assert "not valid YAML" in str(caught.value)


@pytest.mark.parametrize("text,because", [
    ("[]", "not a mapping"),
    ("name: x\ntrigger: {type: job_complete}\nactions: []", "no actions"),
    ("name: x\nactions: [{type: run_workspace}]", "no trigger"),
    ("trigger: {type: job_complete}\nactions: [{type: http_request}]", "no name"),
    ("name: x\ntrigger: {type: on_tuesday}\nactions: [{type: http_request}]",
     "unknown trigger type"),
    ("name: x\ntrigger: {type: job_complete}\nactions: [{type: send_telegram}]",
     "unknown action type"),
    ("name: x\ntrigger: {type: job_complete, when: soon}\n"
     "actions: [{type: http_request, url: 'https://e.com'}]",
     "unknown trigger key"),
    ("name: x\ntrigger: {type: job_complete}\n"
     "actions: [{type: http_request, url: 'ftp://e.com'}]", "not http"),
    ("name: x\ntrigger: {type: job_complete}\n"
     "actions: [{type: http_request, url: 'https://e.com', method: TRACE}]",
     "unsupported method"),
    ("name: x\ntrigger: {type: job_complete, status: nearly}\n"
     "actions: [{type: http_request, url: 'https://e.com'}]", "not a terminal status"),
])
def test_a_document_that_is_not_an_automation_is_refused(text, because):
    with pytest.raises(automations.AutomationError):
        automations.parse(text)


def test_a_placeholder_naming_nothing_is_refused_where_it_was_typed():
    """`{{job.di}}` resolves to the empty string, forever, silently.

    Caught at authoring time because the alternative is a webhook that has been
    posting an empty field for three weeks and a body nobody reads closely.
    """
    with pytest.raises(automations.AutomationError) as caught:
        automations.parse(
            "name: x\ntrigger: {type: job_complete}\n"
            "actions: [{type: http_request, url: 'https://e.com/{{job.di}}'}]"
        )
    assert "job.di" in str(caught.value)


def test_a_template_cannot_walk_out_of_its_namespace():
    """Asserted at the door that holds it, not at `render`.

    `render` is a dict lookup, so it returns "" for anything unknown -- which
    means a test asserting that would pass against a render that DID evaluate
    the traversal and got nothing back. The refusal has to be shown where the
    refusal is.
    """
    with pytest.raises(automations.AutomationError):
        automations.parse(
            "name: x\ntrigger: {type: job_complete}\n"
            "actions: [{type: http_request, url: 'https://e.com/"
            "{{job.id.__class__}}'}]"
        )


def test_an_automation_that_triggers_on_what_it_runs_is_refused():
    with pytest.raises(automations.AutomationError) as caught:
        automations.parse(
            "name: x\n"
            "trigger: {type: job_complete, repository: Testing, workspace: slow}\n"
            "actions: [{type: run_workspace, repository: Testing, workspace: slow}]"
        )
    assert "trigger" in str(caught.value)


def test_an_unfiltered_trigger_counts_as_triggering_on_everything():
    """"On any job_complete, run X" watches X too -- and is the subtler loop.

    The self-reference above is visible on the page. This one is not: nothing
    in the document names the workspace twice.
    """
    with pytest.raises(automations.AutomationError):
        automations.parse(
            "name: x\ntrigger: {type: job_complete}\n"
            "actions: [{type: run_workspace, repository: Testing, workspace: slow}]"
        )


def test_validation_is_inside_parse_so_there_is_one_door():
    """Every writer goes through `parse`; none has its own extra step.

    A guard that callers must remember to call separately is a guard that means
    whatever the last caller did.
    """
    source = (automations.create.__code__, automations.replace.__code__)
    for code in source:
        assert "parse" in code.co_names
        assert "_reject_self_trigger" not in code.co_names


# --------------------------------------------------------------------------
# templating
# --------------------------------------------------------------------------

def test_a_placeholder_is_substituted_from_the_fixed_namespace():
    context = {"job.id": "abc", "params.WHO": "world"}
    assert automations.render("{{job.id}}/{{ params.WHO }}", context) == "abc/world"


def test_a_parameter_this_job_did_not_carry_becomes_empty_not_an_error():
    """Validation already refused every name that could never exist.

    What is left is an optional parameter being absent, which is not a failure
    and must not take the action down.
    """
    assert automations.render("x={{params.MAYBE}}", {"job.id": "abc"}) == "x="


# --------------------------------------------------------------------------
# egress
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("host", [
    "localhost",         # the API itself
    "127.0.0.1",         # ditto, spelled around a name check
    "169.254.169.254",   # cloud metadata, the reason this guard exists
    "192.168.88.102",    # the LAN this actually runs on
    "10.0.0.1",
    "0.0.0.0",
    "::1",
])
async def test_the_server_refuses_to_fetch_its_own_network(host):
    with pytest.raises(automations.AutomationError):
        await automations._resolve_public(host, 80)


@pytest.mark.asyncio
async def test_a_public_address_is_allowed():
    """Otherwise the guard could be "refuse everything" and pass every test."""
    await automations._resolve_public("example.com", 443)


@pytest.mark.asyncio
async def test_every_redirect_hop_is_checked_not_just_the_first(monkeypatch):
    """A webhook that 302s to the metadata endpoint is the standard bypass.

    The probe allows loopback deliberately: with it refused, hop one fails and
    the redirect is never followed, so the test would pass just as well against
    a `_fetch` that checks nothing after the first URL. Allowing hop one is
    what makes hop two the thing under test.
    """
    asked: list[str] = []
    real = automations._resolve_public

    async def watching(host, port):
        asked.append(host)
        if host in ("127.0.0.1", "localhost"):
            return
        await real(host, port)

    monkeypatch.setattr(automations, "_resolve_public", watching)

    async def handler(request):
        return httpx.Response(302, headers={"location": "http://169.254.169.254/"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(automations.httpx, "AsyncClient", client)

    with pytest.raises(automations.AutomationError) as caught:
        await automations._fetch("http://127.0.0.1/hook", "GET", {}, None)

    assert asked == ["127.0.0.1", "169.254.169.254"]
    assert "not a public address" in str(caught.value)


# --------------------------------------------------------------------------
# firing
# --------------------------------------------------------------------------

@pytest_asyncio.fixture
async def only_test_automations(db):
    """No automation but this module's is enabled while these tests run.

    `automations.consider()` reads the whole table -- that is its job -- so any
    automation a human left in this database fires for real here. The damage is
    not only a miscount in the assertion: the other automation submits a
    genuine job, which then trips the "live jobs in the queue, refusing to
    disturb them" guard and silently SKIPS 58 unrelated UI tests. One demo row
    seeded for the web UI cost 1 failure and 58 skips, and only the failure
    named anything real.

    So the precondition is established rather than assumed. Restored after,
    including when the test body fails -- fixture finalisation still runs.
    """
    others = [
        r["id"] for r in await db.fetch(
            "UPDATE automations SET enabled = false WHERE enabled RETURNING id"
        )
    ]
    yield
    if others:
        await db.execute(
            "UPDATE automations SET enabled = true WHERE id = ANY($1::int[])", others
        )


@pytest_asyncio.fixture
async def automation(db, only_test_automations):
    await db.execute("DELETE FROM automations WHERE name = '_pytest-auto'")
    row = await automations.create(db, GOOD, created_by="_pytest")
    yield row
    await db.execute("DELETE FROM jobs WHERE triggered_by = 'automation:_pytest-auto'")
    await db.execute("DELETE FROM automations WHERE id = $1", row["id"])


async def _finished(db, *, workspace="chatty", status="complete", triggered_by=None,
                    completed="now()"):
    """A job row in a terminal state, without running anything."""
    return await db.fetchval(
        f"""
        INSERT INTO jobs (repository, workspace, params, status, submitted_by,
                          triggered_by, submitted_at, completed_at)
        VALUES ('Testing', $1, '{{"COUNT": 1}}'::jsonb, $2, '_pytest', $3,
                now(), {completed})
        RETURNING id
        """,
        workspace, status, triggered_by,
    )


@pytest.mark.asyncio
async def test_a_matching_job_fires_and_the_new_job_says_what_caused_it(
    db, automation
):
    job_id = await _finished(db)

    fired = await automations.consider(db, job_id)

    assert len(fired) == 1 and fired[0]["ok"]
    caused = await db.fetchrow(
        "SELECT workspace, triggered_by, parent_job FROM jobs "
        "WHERE triggered_by = 'automation:_pytest-auto'")
    assert caused["workspace"] == "slow"
    # parent_job is the chain; triggered_by is what stops the loop. Both, and
    # they are not the same fact.
    assert caused["parent_job"] == job_id
    await db.execute("DELETE FROM jobs WHERE id = $1", job_id)


@pytest.mark.asyncio
async def test_a_job_from_another_workspace_does_not_fire_it(db, automation):
    job_id = await _finished(db, workspace="slow")
    assert await automations.consider(db, job_id) == []
    await db.execute("DELETE FROM jobs WHERE id = $1", job_id)


@pytest.mark.asyncio
async def test_considering_is_recorded_so_the_next_poll_does_not_redeliver(
    db, automation
):
    """Asserted against `consider` directly, not against `run_pending`.

    The claim has to be held by the function that writes it. Testing this
    through `run_pending` would pass just as well with the claim moved out into
    that caller's WHERE clause -- which is where it started, and which leaves
    any future second caller free to deliver everything twice.
    """
    job_id = await _finished(db)

    assert len(await automations.consider(db, job_id)) == 1
    assert await automations.consider(db, job_id) == []
    assert await db.fetchval(
        "SELECT count(*) FROM jobs WHERE triggered_by = 'automation:_pytest-auto'"
    ) == 1
    assert await db.fetchval(
        "SELECT automations_at FROM jobs WHERE id = $1", job_id) is not None
    await db.execute("DELETE FROM jobs WHERE id = $1", job_id)


@pytest.mark.asyncio
async def test_a_job_this_automation_caused_does_not_re_fire_it(db, automation):
    """The runtime half of the loop guard, one link back.

    Without it, an automation whose trigger has no workspace filter runs
    forever: its own job completes, matches, and submits the next one.
    """
    job_id = await _finished(db, triggered_by="automation:_pytest-auto")
    assert await automations.consider(db, job_id) == []
    await db.execute("DELETE FROM jobs WHERE id = $1", job_id)


@pytest.mark.asyncio
async def test_a_job_that_finished_before_the_automation_existed_is_not_delivered(
    db, automation
):
    """Timestamps written explicitly, because `now()` is frozen in a transaction.

    Comparing two now()-derived values inside one transaction compares two
    identical values, so `created_at <= completed_at` is trivially true and the
    test proves nothing -- which is exactly what my first version of it did.
    """
    job_id = await _finished(db, completed="now() - interval '1 hour'")
    assert await automations.consider(db, job_id) == []
    await db.execute("DELETE FROM jobs WHERE id = $1", job_id)


@pytest.mark.asyncio
async def test_a_disabled_automation_does_not_fire(db, automation):
    await automations.set_enabled(db, automation["id"], False)
    job_id = await _finished(db)
    assert await automations.consider(db, job_id) == []
    await db.execute("DELETE FROM jobs WHERE id = $1", job_id)


@pytest.mark.asyncio
async def test_a_job_still_running_is_not_considered_finished(db, automation):
    job_id = await db.fetchval(
        """
        INSERT INTO jobs (repository, workspace, params, status, submitted_by)
        VALUES ('Testing', 'chatty', '{}'::jsonb, 'running', '_pytest')
        RETURNING id
        """
    )
    assert await automations.consider(db, job_id) == []
    await db.execute("DELETE FROM jobs WHERE id = $1", job_id)


@pytest_asyncio.fixture
async def broken_automation(db, only_test_automations):
    """Teardown in a fixture, not at the end of the test body.

    Cleaning up on the happy path only means a failing test leaves an enabled
    automation behind that watches the same workspace every other test here
    uses -- so one failure becomes four, and none of the four is about the
    thing it names. That is not hypothetical, it is what the first run did.
    """
    await db.execute("DELETE FROM automations WHERE name = '_pytest-bad-action'")
    row = await automations.create(db, """
name: _pytest-bad-action
trigger: {type: job_complete, workspace: chatty}
actions:
  - type: run_workspace
    repository: Testing
    workspace: does-not-exist
""")
    yield row
    await db.execute("DELETE FROM automations WHERE id = $1", row["id"])


@pytest.mark.asyncio
async def test_a_failing_action_is_recorded_and_does_not_raise(db, broken_automation):
    """A broken action is an operational fact written to a table.

    Raising here would take down the worker poll that happened to be carrying
    it, turning one bad automation into a stopped queue.
    """
    auto = broken_automation
    job_id = await _finished(db)

    fired = await automations.consider(db, job_id)

    assert len(fired) == 1 and fired[0]["ok"] is False
    assert "WorkspaceNotFound" in fired[0]["results"][0]["error"]
    assert (await db.fetchrow(
        "SELECT last_error FROM automations WHERE id = $1", auto["id"]
    ))["last_error"] is not None
    runs = await automations.runs(db, auto["id"])
    assert len(runs) == 1 and runs[0]["ok"] is False

    await db.execute("DELETE FROM jobs WHERE id = $1", job_id)


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

@pytest_asyncio.fixture
async def client(db, token):
    await db_module.init_pool()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver",
        headers={"authorization": f"Bearer {token}"},
    ) as c:
        yield c
    await db_module.close_pool()


@pytest_asyncio.fixture
async def scoped_token(db):
    raw = auth.new_token()
    await db.execute("DELETE FROM service_accounts WHERE name = '_pytest_scoped'")
    await db.execute(
        """
        INSERT INTO service_accounts (name, token_hash, max_tier, repo_scope)
        VALUES ('_pytest_scoped', $1, 4, ARRAY['Elsewhere/*'])
        """,
        auth.hash_token(raw),
    )
    yield raw
    await db.execute("DELETE FROM service_accounts WHERE name = '_pytest_scoped'")


@pytest.mark.asyncio
async def test_the_yaml_comes_back_verbatim(client, db):
    """Comments and all: what the editor shows must be what was submitted.

    A round trip through a YAML dumper is not that, which is why the table
    stores the text alongside the parse rather than re-emitting it.
    """
    await db.execute("DELETE FROM automations WHERE name = '_pytest-auto'")
    text = "# why this exists\n" + GOOD
    r = await client.post("/rest/v1/automations", json={"yaml": text})
    assert r.status_code == 201, r.text
    assert r.json()["yaml"] == text
    assert r.json()["config"]["trigger"]["workspace"] == "chatty"
    await client.delete(f"/rest/v1/automations/{r.json()['id']}")


@pytest.mark.asyncio
async def test_a_bad_document_is_a_400_on_the_request_that_wrote_it(client):
    r = await client.post("/rest/v1/automations", json={"yaml": "name: x"})
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_PARAMETER"


@pytest.mark.asyncio
async def test_a_scoped_caller_cannot_automate_another_repository(
    client, db, scoped_token
):
    """The escalation this whole scope layer exists for.

    A job an automation submits runs as `automation:<name>`, and `jobs.submit`
    performs no scope check -- so without a check at write time, this is a way
    to have the server run a workspace the caller cannot reach.

    The delete establishes the precondition rather than assuming it:
    `break_the_guard.py` runs this test with the check deleted, and that run
    really does store the automation.
    """
    await db.execute("DELETE FROM automations WHERE name = '_pytest-auto'")
    r = await client.post(
        "/rest/v1/automations", json={"yaml": GOOD},
        headers={"authorization": f"Bearer {scoped_token}"})
    assert r.status_code == 403, r.text
    assert await db.fetchval(
        "SELECT count(*) FROM automations WHERE name = '_pytest-auto'") == 0


@pytest.mark.asyncio
async def test_a_scoped_caller_cannot_watch_every_repository(
    client, db, scoped_token
):
    """An unfiltered trigger reads every job in the system, including its params.

    Naming no repository is not "no repository", it is all of them.

    Bracketed by deletes for the reason its sibling above is, and this is the
    run that proved that reason real: `break_the_guard.py` reverts the source it
    edits but not the rows that source wrote, so breaking this very guard stored
    an enabled `_pytest-watch` watching every workspace. It outlived the run and
    then fired alongside six later tests, each of which failed naming something
    other than the cause.

    Asserting nothing was written is also a stronger claim than the 403 alone:
    a refusal that stores the row anyway is a bug the status code cannot see.
    """
    await db.execute("DELETE FROM automations WHERE name = '_pytest-watch'")
    try:
        r = await client.post(
            "/rest/v1/automations",
            json={"yaml": "name: _pytest-watch\ntrigger: {type: job_complete}\n"
                          "actions: [{type: http_request, url: 'https://e.com'}]"},
            headers={"authorization": f"Bearer {scoped_token}"})
        assert r.status_code == 403, r.text
        assert await db.fetchval(
            "SELECT count(*) FROM automations WHERE name = '_pytest-watch'") == 0
    finally:
        # In the finally, not after the asserts: the assert is what fails when
        # the guard is gone, so a trailing delete is exactly the one that never
        # runs in the only case where there is something to delete.
        await db.execute("DELETE FROM automations WHERE name = '_pytest-watch'")


@pytest.mark.asyncio
async def test_scope_is_checked_against_the_new_document_not_only_the_old(
    client, db, scoped_token
):
    """Otherwise the check is bypassed by writing something harmless and editing it.

    The automation created here is within the scoped caller's reach; the edit
    is not, and the edit is where the privilege would be gained.
    """
    await db.execute("DELETE FROM automations WHERE name = '_pytest-elsewhere'")
    mine = await automations.create(db, """
name: _pytest-elsewhere
trigger: {type: job_complete, repository: Elsewhere, workspace: a}
actions: [{type: run_workspace, repository: Elsewhere, workspace: b}]
""")
    scoped = {"authorization": f"Bearer {scoped_token}"}

    assert (await client.get(f"/rest/v1/automations/{mine['id']}",
                             headers=scoped)).status_code == 200

    r = await client.put(f"/rest/v1/automations/{mine['id']}",
                         json={"yaml": GOOD}, headers=scoped)
    assert r.status_code == 403, r.text
    assert (await db.fetchrow(
        "SELECT yaml FROM automations WHERE id = $1", mine["id"]
    ))["yaml"].strip().startswith("name: _pytest-elsewhere")

    await db.execute("DELETE FROM automations WHERE id = $1", mine["id"])


@pytest.mark.asyncio
async def test_toggling_does_not_rewrite_the_document(client, automation):
    """The UI's on/off switch must not re-parse and re-emit YAML nobody edited."""
    r = await client.patch(f"/rest/v1/automations/{automation['id']}",
                           json={"enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False
    assert r.json()["yaml"] == automation["yaml"]
