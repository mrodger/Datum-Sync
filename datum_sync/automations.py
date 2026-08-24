"""Automations: YAML-configured triggers and action chains.

    trigger:  job_complete
    actions:  run_workspace | http_request

Three things here are dangerous and are the reason most of this file is
validation rather than execution.

**Loops.** A `run_workspace` action submits a job; that job completes; the
completion is a `job_complete` trigger. An automation that watches the
workspace it also runs is an infinite loop that submits jobs forever, and it
is an easy one to write by accident. Jobs carry `triggered_by`, and a job an
automation caused never re-fires that same automation -- see `_would_loop`.
The static check in `validate` catches the obvious self-reference at authoring
time; the runtime check catches the rest, including a two-automation cycle.

**Templates.** Actions interpolate `{{job.id}}` and friends. The substitution
is a dict lookup over a fixed namespace built here -- there is no eval, no
attribute traversal, and no access to anything not in `_context`. A template
naming something outside it is refused when the automation is saved, so it
fails in front of the person who wrote it.

**Egress.** `http_request` makes the server fetch a URL that came from user
YAML, which is SSRF by construction. `_resolve_public` refuses addresses that
are private, loopback, link-local or otherwise reserved, and re-checks on
every redirect hop, because only the first URL is the one that was validated.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from typing import Any

import asyncpg
import httpx
import yaml

from datum_sync import jobs

TRIGGERS = ("job_complete",)
ACTIONS = ("run_workspace", "http_request")

HTTP_TIMEOUT_SECONDS = 15.0
HTTP_MAX_REDIRECTS = 3
HTTP_MAX_RESPONSE_BYTES = 64 * 1024

# How many finished jobs one tick will consider. A bound so that a long
# outage does not turn the first poll after it into an unbounded burst of
# outbound requests.
BATCH = 50


class AutomationError(Exception):
    """An automation that cannot be stored, or a YAML document that is not one."""


# -- parsing and validation --------------------------------------------------

def parse(text: str) -> dict[str, Any]:
    """Parse and fully validate an automation document.

    `safe_load`, never `load`: PyYAML's default loader constructs arbitrary
    Python objects from tags like `!!python/object/apply`, which in a field
    that accepts user YAML is remote code execution.
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise AutomationError(f"not valid YAML: {e}") from None

    if not isinstance(doc, dict):
        raise AutomationError("an automation must be a YAML mapping")

    unknown = set(doc) - {"name", "enabled", "trigger", "actions"}
    if unknown:
        raise AutomationError(f"unknown key(s): {', '.join(sorted(unknown))}")

    name = doc.get("name")
    if not isinstance(name, str) or not name.strip():
        raise AutomationError("name is required")

    config = {
        "name": name.strip(),
        "enabled": bool(doc.get("enabled", True)),
        "trigger": _trigger(doc.get("trigger")),
        "actions": _actions(doc.get("actions"), name),
    }
    # Inside parse, not beside it. This started as a separate call the two
    # writers each had to remember, which makes "validated" mean whatever the
    # last caller did -- and a caller that forgets stores an automation that
    # submits jobs forever. There is one door now.
    _reject_self_trigger(config)
    return config


def _trigger(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AutomationError("trigger is required and must be a mapping")

    kind = raw.get("type")
    if kind not in TRIGGERS:
        raise AutomationError(
            f"trigger type must be one of {', '.join(TRIGGERS)} (got {kind!r})"
        )

    unknown = set(raw) - {"type", "repository", "workspace", "status"}
    if unknown:
        raise AutomationError(f"unknown trigger key(s): {', '.join(sorted(unknown))}")

    status = raw.get("status")
    if status is not None and status not in jobs.TERMINAL:
        raise AutomationError(
            f"trigger status must be one of {', '.join(jobs.TERMINAL)}"
        )

    return {
        "type": kind,
        "repository": raw.get("repository"),
        "workspace": raw.get("workspace"),
        # Absent means "any terminal status", which includes failures. Said
        # explicitly because "on complete" reads like success to most people
        # and a delivery firing on a failed run is a bad surprise.
        "status": status,
    }


def _actions(raw: Any, automation_name: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise AutomationError("actions must be a non-empty list")

    out = []
    for i, action in enumerate(raw, start=1):
        if not isinstance(action, dict):
            raise AutomationError(f"action {i} must be a mapping")
        kind = action.get("type")
        if kind not in ACTIONS:
            raise AutomationError(
                f"action {i}: type must be one of {', '.join(ACTIONS)} (got {kind!r})"
            )
        out.append(_run_workspace(action, i) if kind == "run_workspace"
                   else _http_request(action, i))
    return out


def _run_workspace(action: dict, i: int) -> dict[str, Any]:
    unknown = set(action) - {"type", "repository", "workspace", "params"}
    if unknown:
        raise AutomationError(f"action {i}: unknown key(s): "
                              f"{', '.join(sorted(unknown))}")
    for key in ("repository", "workspace"):
        if not isinstance(action.get(key), str) or not action[key]:
            raise AutomationError(f"action {i}: {key} is required")

    params = action.get("params") or {}
    if not isinstance(params, dict):
        raise AutomationError(f"action {i}: params must be a mapping")
    for value in params.values():
        _check_template(str(value), f"action {i}")

    return {
        "type": "run_workspace",
        "repository": action["repository"],
        "workspace": action["workspace"],
        "params": params,
    }


def _http_request(action: dict, i: int) -> dict[str, Any]:
    unknown = set(action) - {"type", "url", "method", "headers", "body"}
    if unknown:
        raise AutomationError(f"action {i}: unknown key(s): "
                              f"{', '.join(sorted(unknown))}")

    url = action.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise AutomationError(f"action {i}: url must be http:// or https://")
    _check_template(url, f"action {i}")

    method = str(action.get("method", "POST")).upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        raise AutomationError(f"action {i}: unsupported method {method}")

    headers = action.get("headers") or {}
    if not isinstance(headers, dict):
        raise AutomationError(f"action {i}: headers must be a mapping")

    body = action.get("body")
    if body is not None:
        if not isinstance(body, str):
            raise AutomationError(f"action {i}: body must be a string")
        _check_template(body, f"action {i}")

    return {"type": "http_request", "url": url, "method": method,
            "headers": {str(k): str(v) for k, v in headers.items()},
            "body": body}


# -- templating --------------------------------------------------------------

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")

# The whole namespace. Anything not derivable from these names cannot be
# reached from a template, which is the point: the alternative -- resolving
# dotted names against live objects -- turns a text field into a way to read
# whatever the process can see.
_JOB_FIELDS = ("id", "repository", "workspace", "status", "error",
               "submitted_by", "submitted_at", "completed_at")


def _check_template(text: str, where: str) -> None:
    """Refuse a placeholder that could never resolve.

    At authoring time, so an automation with a typo fails in front of the
    person who wrote it rather than silently posting the literal string
    `{{job.di}}` to a webhook three weeks later.
    """
    for name in _PLACEHOLDER.findall(text):
        if name.startswith("params."):
            continue
        if name.startswith("job.") and name[4:] in _JOB_FIELDS:
            continue
        raise AutomationError(
            f"{where}: {{{{{name}}}}} is not a value an automation can use; "
            f"use params.<NAME> or job.<{'|'.join(_JOB_FIELDS)}>"
        )


def _context(job: asyncpg.Record) -> dict[str, str]:
    params = job["params"]
    if isinstance(params, str):
        params = json.loads(params)

    values = {}
    for field in _JOB_FIELDS:
        value = job[field]
        values[f"job.{field}"] = "" if value is None else str(value)
    for key, value in (params or {}).items():
        values[f"params.{key}"] = "" if value is None else str(value)
    return values


def render(text: str, context: dict[str, str]) -> str:
    """Substitute placeholders from `context`. A dict lookup, nothing more.

    An unresolved placeholder becomes the empty string rather than raising:
    validation already rejected every name that could not exist, so what is
    left is `params.X` for a parameter this particular job did not carry, and
    an optional parameter being absent is not an error.
    """
    return _PLACEHOLDER.sub(lambda m: context.get(m.group(1).strip(), ""), text)


# -- egress ------------------------------------------------------------------

async def _resolve_public(host: str, port: int) -> None:
    """Refuse a host that resolves anywhere private.

    The URL comes from user YAML, so without this the server is a proxy into
    its own network: cloud metadata endpoints, the Postgres on localhost, every
    admin interface on the LAN. Every resolved address is checked, not just the
    first, because a name with both a public and a private A record would
    otherwise pass and then connect to the private one.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise AutomationError(f"cannot resolve {host}: {e}") from None

    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global or address.is_multicast:
            raise AutomationError(
                f"{host} resolves to {address}, which is not a public address"
            )


async def _fetch(url: str, method: str, headers: dict, body: str | None) -> dict:
    """One outbound request, checking the destination on every hop.

    Redirects are followed by hand rather than by httpx, because httpx would
    follow them itself and only the first URL was ever validated -- a webhook
    that 302s to 169.254.169.254 is the standard way past a check that runs
    once.
    """
    async with httpx.AsyncClient(follow_redirects=False,
                                 timeout=HTTP_TIMEOUT_SECONDS) as client:
        for _ in range(HTTP_MAX_REDIRECTS + 1):
            parsed = httpx.URL(url)
            if parsed.scheme not in ("http", "https"):
                raise AutomationError(f"refusing scheme {parsed.scheme!r}")
            await _resolve_public(parsed.host, parsed.port or
                                  (443 if parsed.scheme == "https" else 80))

            response = await client.request(method, url, headers=headers,
                                            content=body)
            if response.is_redirect and response.headers.get("location"):
                url = str(response.next_request.url)
                method = "GET" if response.status_code in (301, 302, 303) else method
                body = None if method == "GET" else body
                continue

            return {
                "status": response.status_code,
                # Capped: an action's outcome is a status code, and storing a
                # webhook's entire response body in automation_runs would let a
                # remote server decide how much of our database it uses.
                "body": response.text[:HTTP_MAX_RESPONSE_BYTES],
            }

    raise AutomationError(f"more than {HTTP_MAX_REDIRECTS} redirects")


# -- CRUD --------------------------------------------------------------------

_COLUMNS = """
    id, name, yaml, config, enabled, created_by, created_at, updated_at,
    last_fired, last_error
"""


async def create(
    conn: asyncpg.Connection, text: str, created_by: str | None = None
) -> asyncpg.Record:
    config = parse(text)
    return await conn.fetchrow(
        f"""
        INSERT INTO automations (name, yaml, config, enabled, created_by)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING {_COLUMNS}
        """,
        config["name"], text, json.dumps(config), config["enabled"], created_by,
    )


async def get(conn: asyncpg.Connection, automation_id: int) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"SELECT {_COLUMNS} FROM automations WHERE id = $1", automation_id
    )


async def listing(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return await conn.fetch(
        f"SELECT {_COLUMNS} FROM automations ORDER BY enabled DESC, name"
    )


async def replace(
    conn: asyncpg.Connection, automation_id: int, text: str
) -> asyncpg.Record | None:
    """Replace the document. The name in the YAML is authoritative.

    Renaming through an edit is allowed: the YAML is what the author sees, and
    a name in the database that disagrees with the one on screen is worse than
    a rename.
    """
    config = parse(text)
    return await conn.fetchrow(
        f"""
        UPDATE automations
        SET name = $2, yaml = $3, config = $4, enabled = $5, updated_at = now()
        WHERE id = $1
        RETURNING {_COLUMNS}
        """,
        automation_id, config["name"], text, json.dumps(config), config["enabled"],
    )


async def set_enabled(
    conn: asyncpg.Connection, automation_id: int, enabled: bool
) -> asyncpg.Record | None:
    """Toggle without touching the document.

    Separate from `replace` because the UI's on/off switch must not require
    re-parsing and re-writing YAML the user did not edit.
    """
    return await conn.fetchrow(
        f"""
        UPDATE automations SET enabled = $2, updated_at = now()
        WHERE id = $1 RETURNING {_COLUMNS}
        """,
        automation_id, enabled,
    )


async def delete(conn: asyncpg.Connection, automation_id: int) -> bool:
    result = await conn.execute("DELETE FROM automations WHERE id = $1",
                                automation_id)
    return result.endswith(" 1")


async def runs(
    conn: asyncpg.Connection, automation_id: int, limit: int = 20
) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        SELECT id, fired_at, trigger_job, results, ok
        FROM automation_runs WHERE automation_id = $1
        ORDER BY fired_at DESC LIMIT $2
        """,
        automation_id, min(limit, 100),
    )


# -- firing ------------------------------------------------------------------

async def run_pending(conn: asyncpg.Connection) -> list[dict]:
    """Consider every finished job no automation has seen yet.

    Driven by the `automations_at` column rather than by the NOTIFY that
    announced the completion: a notification arriving while the worker is
    restarting is simply lost, and "the automation did not fire and nothing
    says why" is the worst failure this feature can have. The notify makes it
    prompt; this makes it certain.
    """
    pending = await conn.fetch(
        """
        SELECT id FROM jobs
        WHERE automations_at IS NULL AND completed_at IS NOT NULL
        ORDER BY completed_at LIMIT $1
        """,
        BATCH,
    )
    fired = []
    for row in pending:
        fired.extend(await consider(conn, row["id"]))
    return fired


async def consider(conn: asyncpg.Connection, job_id) -> list[dict]:
    """Claim this job for automations, then fire every one that matches.

    The claim is here rather than in `run_pending`'s WHERE clause, and it is
    taken BEFORE the actions run, for two different reasons.

    Here, because a guard a caller has to hold for you is not a guard. Today
    the only caller is `run_pending`, which does filter -- but `consider` is a
    public function that reads as "consider this job", and it is the obvious
    thing to attach the completion NOTIFY to when the prompt path gets wired
    up. That caller would deliver everything a second time and nothing in this
    module would stop it.

    Before, because this is deliberately at-most-once. If the process dies
    mid-fire the job stays claimed and those deliveries are lost; marking
    afterwards instead would re-deliver every action that had already
    succeeded. A webhook that fires twice cannot be un-fired, and a job
    submitted twice is real work done twice.
    """
    job = await conn.fetchrow(
        """
        SELECT id, repository, workspace, status, error, params, submitted_by,
               submitted_at, completed_at, triggered_by
        FROM jobs WHERE id = $1
        """,
        job_id,
    )
    if job is None or job["status"] not in jobs.TERMINAL:
        return []

    claimed = await conn.fetchval(
        "UPDATE jobs SET automations_at = now() "
        "WHERE id = $1 AND automations_at IS NULL RETURNING id",
        job_id,
    )
    if claimed is None:
        return []

    results = []
    # `created_at <= completed_at` is not a tidiness filter, it is the second
    # half of the retroactive-firing guard. The migration's backfill settles
    # history once; this settles it continuously. A job can sit awaiting
    # consideration for as long as no worker is up, so without this, writing an
    # automation is a way to deliver every run that happened during the outage
    # -- webhooks posted and jobs submitted for work finished hours ago, which
    # is a surprise nobody can undo.
    for automation in await conn.fetch(
        "SELECT id, name, config FROM automations "
        "WHERE enabled AND created_at <= $1",
        job["completed_at"],
    ):
        config = json.loads(automation["config"])
        if not _matches(config["trigger"], job):
            continue
        if _would_loop(automation["name"], job):
            continue
        results.append(await _fire(conn, automation, config, job))

    return results


def _matches(trigger: dict, job: asyncpg.Record) -> bool:
    if trigger["type"] != "job_complete":
        return False
    if trigger["repository"] and trigger["repository"] != job["repository"]:
        return False
    if trigger["workspace"] and trigger["workspace"] != job["workspace"]:
        return False
    if trigger["status"] and trigger["status"] != job["status"]:
        return False
    return True


def _would_loop(name: str, job: asyncpg.Record) -> bool:
    """True if this job was itself caused by this automation.

    The runtime half of the loop guard. `_reject_self_trigger` catches an
    automation that names its own trigger workspace, but not two automations
    that run each other's, and not an automation whose trigger has no
    workspace filter at all -- "on any job_complete, run X" watches X too.

    Breaking the chain one link back is enough: A fires B's workspace, B's job
    completes and cannot re-fire A. It does not detect a longer cycle
    (A -> B -> C -> A), which needs the chain recorded rather than one step of
    it, and is deferred with that written down rather than half-done.
    """
    return job["triggered_by"] == f"automation:{name}"


async def _fire(
    conn: asyncpg.Connection, automation: asyncpg.Record, config: dict,
    job: asyncpg.Record,
) -> dict:
    context = _context(job)
    results = []
    ok = True

    for action in config["actions"]:
        try:
            results.append(
                await _perform(conn, action, context, automation["name"], job["id"])
            )
        except Exception as exc:  # noqa: BLE001 - recorded per action
            # One action failing does not cancel the ones after it. A chain is
            # a list of independent deliveries -- a webhook being down is no
            # reason to skip an email -- and an exception here would otherwise
            # take the worker's poll down with it.
            ok = False
            results.append({"type": action["type"], "ok": False,
                            "error": f"{type(exc).__name__}: {exc}"})

    await conn.execute(
        """
        INSERT INTO automation_runs (automation_id, trigger_job, results, ok)
        VALUES ($1, $2, $3, $4)
        """,
        automation["id"], job["id"], json.dumps(results), ok,
    )
    # `ok` is False only because a result carries ok=False, so the generator
    # cannot be empty -- but a bare next() would raise StopIteration if that
    # ever stopped being true, inside a worker poll, as a failure with no
    # relationship to the automation that caused it. The default costs nothing.
    failures = (r.get("error") for r in results if not r.get("ok", True))
    await conn.execute(
        "UPDATE automations SET last_fired = now(), last_error = $2 WHERE id = $1",
        automation["id"],
        None if ok else next(failures, "action failed"),
    )
    return {"automation": automation["name"], "job_id": str(job["id"]),
            "ok": ok, "results": results}


async def _perform(
    conn: asyncpg.Connection, action: dict, context: dict, automation_name: str,
    trigger_job,
) -> dict:
    if action["type"] == "run_workspace":
        params = {k: render(str(v), context) for k, v in action["params"].items()}
        new_job = await jobs.submit(
            conn,
            action["repository"],
            action["workspace"],
            params,
            submitted_by=f"automation:{automation_name}",
            # Both, and they mean different things: parent_job is the job this
            # one came from, so a chain is walkable after the fact; triggered_by
            # is what decided to create it, and is what stops the loop.
            parent_job=trigger_job,
            triggered_by=f"automation:{automation_name}",
        )
        return {"type": "run_workspace", "ok": True, "job_id": str(new_job)}

    response = await _fetch(
        render(action["url"], context),
        action["method"],
        {k: render(v, context) for k, v in action["headers"].items()},
        render(action["body"], context) if action["body"] is not None else None,
    )
    return {"type": "http_request", "ok": 200 <= response["status"] < 400,
            "status": response["status"], "body": response["body"][:500]}


def _reject_self_trigger(config: dict) -> None:
    """Refuse an automation that triggers on a workspace it runs.

    The unmissable case, caught while the author is looking at it. It is not
    the whole loop story -- two automations can point at each other, and that
    is only visible at runtime -- but a self-reference is the one people
    actually write, and catching it here is a better error than discovering it
    from a job list filling up.
    """
    trigger = config["trigger"]
    for action in config["actions"]:
        if action["type"] != "run_workspace":
            continue
        if trigger["workspace"] in (None, action["workspace"]) and \
                trigger["repository"] in (None, action["repository"]):
            raise AutomationError(
                f"this automation runs {action['repository']}/{action['workspace']}"
                f" and also triggers on it, so every run would trigger the next"
            )
