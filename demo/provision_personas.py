#!/usr/bin/env python3
"""Clean browser-test data and register the demo personas as governed agents."""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import uuid

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT
PERSONAS_PATH = Path(__file__).resolve().with_name("personas.py")
sys.path.insert(0, str(APP))

from datum_sync.persona_profiles import PERSONA_TOOLS, grant_for, system_prompt  # noqa: E402

SEED_DOCUMENTS = {
    "demo/welcome.md": "Welcome to Datum. This document is readable with a baseline credential. Elevate to edit it.",
    "demo/release-notes.md": "Draft release notes. Publishing requires a separate human approval.",
    "private/operator.md": "This document is excluded from the demonstration agent grant.",
}
RETIRED_PERSONAS = ("opencode", "fme-user")
BUNDLE_VERSION = 2


def personas() -> list[dict]:
    spec = importlib.util.spec_from_file_location("datum_demo_personas", PERSONAS_PATH)
    if not spec or not spec.loader:
        raise RuntimeError("demo personas are unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.get_all()


async def clean_test_data(conn: asyncpg.Connection) -> dict:
    names = await conn.fetchval(
        "SELECT coalesce(array_agg(name ORDER BY name),ARRAY[]::text[]) "
        "FROM service_accounts WHERE name LIKE 'browser-agent-%'"
    )
    ids = await conn.fetchval(
        "SELECT coalesce(array_agg(id),ARRAY[]::integer[]) "
        "FROM service_accounts WHERE name LIKE 'browser-agent-%'"
    )
    job_count = await conn.fetchval(
        "SELECT count(*) FROM jobs WHERE submitted_by=ANY($1::text[]) OR portal_principal_id=ANY($2::integer[])",
        names, ids,
    )
    await conn.execute("DELETE FROM plane_releases")
    await conn.execute("DELETE FROM plane_sessions")
    await conn.execute("DELETE FROM plane_pending_calls")
    await conn.execute("DELETE FROM plane_authorizations")
    await conn.execute("DELETE FROM plane_credentials")
    await conn.execute("DELETE FROM plane_enrolments")
    await conn.execute("DELETE FROM credential_grants")
    await conn.execute("DELETE FROM credential_requests")
    await conn.execute(
        "DELETE FROM jobs WHERE submitted_by=ANY($1::text[]) OR portal_principal_id=ANY($2::integer[])",
        names, ids,
    )
    for path, content in SEED_DOCUMENTS.items():
        await conn.execute(
            "INSERT INTO plane_documents(path,content,updated_by) VALUES($1,$2,NULL) "
            "ON CONFLICT(path) DO UPDATE SET content=excluded.content,updated_by=NULL,updated_at=now()",
            path, content,
        )
    await conn.execute(
        "TRUNCATE mcp_flow_events,mcp_flows,plane_audit,mcp_server_events,"
        "credential_events,mcp_call_log,proxy_log,audit_log RESTART IDENTITY"
    )
    await conn.execute("DELETE FROM service_accounts WHERE id=ANY($1::integer[])", ids)
    await conn.execute("DELETE FROM plane_clients WHERE client_id<>'datum-local'")
    return {"principals_removed": len(ids), "jobs_removed": job_count}


def clean_test_files() -> int:
    removed = 0
    for cache in (ROOT / ".pytest_cache", ROOT / "worker" / ".pytest_cache"):
        if cache.exists():
            shutil.rmtree(cache)
            removed += 1
    for base in (ROOT / "datum_sync", ROOT / "tests", ROOT / "worker", ROOT / "demo"):
        for cache in base.rglob("__pycache__"):
            shutil.rmtree(cache)
            removed += 1
    screenshots = ROOT / "docs" / "review" / "screenshots"
    if screenshots.exists():
        for path in screenshots.glob("*.png"):
            path.unlink()
            removed += 1
    for name in (
        "browser-check.log", "mutations-final.log", "mutations.log", "pytest-final.log",
        "pytest-full.log", "schema-check.log", "source-ui-browser.log",
        "source-ui-geometry.log", "source-ui-tests.log", "terminal-demo.token",
    ):
        path = ROOT / ".runtime" / name
        if path.exists():
            path.unlink()
            removed += 1
    workspace = ROOT / ".runtime" / "worker-workspaces"
    if workspace.exists():
        for path in list(workspace.iterdir()):
            shutil.rmtree(path) if path.is_dir() else path.unlink()
            removed += 1
    return removed


async def remove_retired_personas(conn: asyncpg.Connection) -> list[str]:
    """Delete identities removed or renamed by this managed persona bundle."""
    rows = await conn.fetch(
        "SELECT id,name FROM service_accounts WHERE name=ANY($1::text[]) FOR UPDATE",
        list(RETIRED_PERSONAS),
    )
    if not rows:
        return []
    ids = [row["id"] for row in rows]
    names = [row["name"] for row in rows]

    await conn.execute(
        "DELETE FROM plane_releases WHERE principal_id=ANY($1::integer[]) OR "
        "pending_id IN (SELECT id FROM plane_pending_calls WHERE principal_id=ANY($1::integer[]))",
        ids,
    )
    await conn.execute("DELETE FROM plane_sessions WHERE principal_id=ANY($1::integer[])", ids)
    await conn.execute("DELETE FROM plane_pending_calls WHERE principal_id=ANY($1::integer[])", ids)
    await conn.execute("UPDATE plane_pending_calls SET decided_by=NULL WHERE decided_by=ANY($1::integer[])", ids)
    await conn.execute("DELETE FROM plane_authorizations WHERE principal_id=ANY($1::integer[])", ids)
    await conn.execute("UPDATE plane_authorizations SET decided_by=NULL WHERE decided_by=ANY($1::integer[])", ids)
    await conn.execute(
        "DELETE FROM plane_enrolments WHERE sponsor_id=ANY($1::integer[]) OR principal_id=ANY($1::integer[])",
        ids,
    )
    await conn.execute(
        "DELETE FROM credential_grants WHERE principal_id=ANY($1::integer[]) OR request_id IN "
        "(SELECT id FROM credential_requests WHERE requested_by=ANY($1::integer[]))",
        ids,
    )
    await conn.execute("DELETE FROM credential_requests WHERE requested_by=ANY($1::integer[])", ids)
    await conn.execute("UPDATE credential_requests SET reviewed_by=NULL WHERE reviewed_by=ANY($1::integer[])", ids)
    await conn.execute(
        "UPDATE credential_grants SET granted_by=NULL WHERE granted_by=ANY($1::integer[])", ids
    )
    await conn.execute(
        "UPDATE credential_grants SET revoked_by=NULL WHERE revoked_by=ANY($1::integer[])", ids
    )
    await conn.execute(
        "UPDATE auth_credential_versions SET created_by=NULL WHERE created_by=ANY($1::integer[])", ids
    )
    await conn.execute("UPDATE plane_documents SET updated_by=NULL WHERE updated_by=ANY($1::integer[])", ids)
    await conn.execute(
        "DELETE FROM mcp_flows WHERE principal_id=ANY($1::integer[]) OR principal_name=ANY($2::text[])",
        ids, names,
    )
    await conn.execute(
        "DELETE FROM credential_events WHERE principal_id=ANY($1::integer[]) OR principal_name=ANY($2::text[])",
        ids, names,
    )
    await conn.execute(
        "DELETE FROM jobs WHERE portal_principal_id=ANY($1::integer[]) OR submitted_by=ANY($2::text[])",
        ids, names,
    )
    await conn.execute(
        "DELETE FROM plane_audit WHERE actor_id=ANY($1::integer[]) OR actor_name=ANY($2::text[]) "
        "OR target=ANY($2::text[])",
        ids, names,
    )
    await conn.execute("UPDATE service_accounts SET portal_parent=NULL WHERE portal_parent=ANY($1::integer[])", ids)
    await conn.execute("DELETE FROM service_accounts WHERE id=ANY($1::integer[])", ids)
    return sorted(names)


async def register_personas(conn: asyncpg.Connection) -> tuple[list[dict], list[str]]:
    retired = await remove_retired_personas(conn)
    operator = await conn.fetchrow(
        "SELECT * FROM service_accounts WHERE name='operator' AND portal_kind='human' AND portal_state='active'"
    )
    if not operator:
        raise RuntimeError("active operator is unavailable")
    available = {
        row["name"]: row
        for row in await conn.fetch(
            "SELECT x.name,c.id AS credential_id FROM connections x "
            "JOIN auth_credentials c ON c.connection_name=x.name "
            "WHERE x.portal_state='active' AND c.status='active'"
        )
    }
    required = {connection for tools in PERSONA_TOOLS.values() for connection in tools}
    missing = sorted(required - set(available))
    if missing:
        raise RuntimeError("missing active managed credentials: " + ", ".join(missing))

    created = []
    for persona in personas():
        persona_id = persona["id"]
        existing = await conn.fetchrow("SELECT * FROM service_accounts WHERE name=$1 FOR UPDATE", persona_id)
        if existing and existing["portal_kind"] != "agent":
            raise RuntimeError(f"{persona_id} is already used by a non-agent principal")
        grant = grant_for(persona_id)
        metadata = {
            "purpose": f"{persona['display']} persona · {persona['role']}",
            "persona": {
                "id": persona_id,
                "display": persona["display"],
                "role": persona["role"],
                "system_prompt": system_prompt(persona),
                "definition_source": "demo/personas.py (vendored from Datum Harness)",
                "definition_complete": False,
                "starter_prompts": list(persona["prompts"]),
                "default_pane_mode": persona["default_pane_mode"],
                "colour": persona["colour"],
                "icon": persona["icon"],
                "bundle_version": BUNDLE_VERSION,
            },
        }
        if existing:
            principal_id = existing["id"]
            await conn.execute(
                "UPDATE service_accounts SET max_tier=3,is_admin=false,repo_scope=ARRAY[]::text[],"
                "disabled=false,portal_parent=$2,portal_state='active',portal_grant=$3,portal_metadata=$4 "
                "WHERE id=$1",
                principal_id, operator["id"], json.dumps(grant), json.dumps(metadata),
            )
        else:
            principal_id = await conn.fetchval(
                "INSERT INTO service_accounts(name,description,max_tier,is_admin,repo_scope,disabled,"
                "portal_kind,portal_parent,portal_state,portal_grant,portal_metadata) "
                "VALUES($1,$2,3,false,ARRAY[]::text[],false,'agent',$3,'active',$4,$5) RETURNING id",
                persona_id, persona["role"], operator["id"], json.dumps(grant), json.dumps(metadata),
            )
        await conn.execute("DELETE FROM credential_grants WHERE principal_id=$1", principal_id)
        for connection, tools in PERSONA_TOOLS[persona_id].items():
            credential = available[connection]
            grant_id = await conn.fetchval(
                "INSERT INTO credential_grants(principal_id,principal_name,credential_id,allowed_tools,"
                "source,valid_until,granted_by) VALUES($1,$2,$3,$4,'direct',NULL,$5) RETURNING id",
                principal_id, persona_id, credential["credential_id"], tools, operator["id"],
            )
            await conn.execute(
                "INSERT INTO credential_events(credential_id,credential_label,principal_id,principal_name,"
                "trace_id,kind,detail) SELECT id,label,$2,$3,$4,'credential.access.provisioned',$5 "
                "FROM auth_credentials WHERE id=$1",
                credential["credential_id"], principal_id, persona_id, uuid.uuid4(),
                json.dumps({"grant_id": str(grant_id), "tools": tools, "bundle_version": BUNDLE_VERSION}),
            )
        await conn.execute(
            "INSERT INTO plane_audit(trace_id,actor_id,actor_name,verb,target,outcome,detail) "
            "VALUES($1,$2,$3,'persona.register',$4,'ok',$5)",
            uuid.uuid4(), operator["id"], operator["name"], persona_id,
            json.dumps({"display": persona["display"], "bundle_version": BUNDLE_VERSION,
                        "connections": sorted(PERSONA_TOOLS[persona_id])}),
        )
        created.append({
            "name": persona_id,
            "display": persona["display"],
            "connections": sorted(PERSONA_TOOLS[persona_id]),
            "tools": sum(len(value) for value in PERSONA_TOOLS[persona_id].values()),
            "definition_complete": False,
        })
    return created, retired


async def main(args) -> None:
    config = json.loads((ROOT / ".runtime" / "config.json").read_text())
    conn = await asyncpg.connect(config["DATABASE_URL"])
    try:
        async with conn.transaction():
            cleanup = await clean_test_data(conn) if args.clean_test_artifacts else None
            registered, retired = await register_personas(conn) if args.register else ([], [])
    finally:
        await conn.close()
    if cleanup is not None:
        cleanup["files_removed"] = clean_test_files()
    print(json.dumps({"cleanup": cleanup, "retired": retired, "registered": registered}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean-test-artifacts", action="store_true")
    parser.add_argument("--register", action="store_true")
    options = parser.parse_args()
    if not options.clean_test_artifacts and not options.register:
        parser.error("choose --clean-test-artifacts, --register, or both")
    asyncio.run(main(options))
