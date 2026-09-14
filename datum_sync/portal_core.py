"""Authority transitions for the local portal. All mutating callers own a DB transaction.

Credentials convey use rights only. Durable issuance and approval require an
interactive human session. Resource mutations and their audit records commit
together, so a failed audit insert prevents the mutation.
"""
from __future__ import annotations

import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from datum_sync import auth, config, vault

COOKIE = 'datum_portal_session'
SCOPES = {'mcp': 2, 'mcp:operate': 3}
LEGACY_MCP_TOOLS = ['legacy__list_repositories', 'legacy__list_workspaces', 'legacy__workspace_manifest',
                    'legacy__job_summary', 'legacy__list_jobs', 'legacy__job_status', 'legacy__job_log',
                    'legacy__job_events', 'legacy__job_artifacts']
OFFICE_MCP_TOOLS = ['office__officecli']
POSTGRES_MCP_TOOLS = ['postgres__list_schemas','postgres__list_tables','postgres__describe_table',
                      'postgres__sample_orders','postgres__orders_summary']
HARNESS_MCP_TOOLS = ['harness__list_personas','harness__render_leaflet_map','harness__worms_lookup']
MCP_DEMO_GRANTS = {
    'legacy-local': {'tools': {'allow': LEGACY_MCP_TOOLS, 'deny': []}},
    'officecli-demo': {'tools': {'allow': OFFICE_MCP_TOOLS, 'deny': []}},
    'postgres-demo': {'tools': {'allow': POSTGRES_MCP_TOOLS, 'deny': []}},
    'harness-tools': {'tools': {'allow': HARNESS_MCP_TOOLS, 'deny': []}},
}
DEMO_GRANT = {'repositories':['Testing'], 'read': ['demo/**'], 'write': ['demo/**'], 'publish': ['demo/**'],
              'deny': ['private/**'], 'sessions': 1, 'mcp': MCP_DEMO_GRANTS}
OWNER_GRANT = {'repositories':['*'], 'read': ['**'], 'write': ['**'], 'publish': ['**'], 'deny': [],
               'sessions': 4, 'mcp': MCP_DEMO_GRANTS}


def now():
    return datetime.now(timezone.utc)


def unpack(value):
    return json.loads(value) if isinstance(value, str) else value


def fail(status, code, message=None):
    raise HTTPException(status, {'code': code, 'message': message or code.replace('_', ' ').lower()})


def scope_tier(scope):
    parts = scope.split()
    if not parts or any(p not in SCOPES for p in parts):
        fail(400, 'invalid_scope')
    return max(SCOPES[p] for p in parts)


def grant_valid(grant):
    if not isinstance(grant, dict) or set(grant) - {'repositories','read','write','publish','deny','sessions','mcp'}:
        fail(400, 'INVALID_GRANT')
    for key in ('read','write','publish','deny'):
        values = grant.get(key, [])
        if not isinstance(values, list) or len(values) > 30:
            fail(400, 'INVALID_GRANT')
        for value in values:
            if not isinstance(value, str) or not value or len(value) > 256 or '..' in value or value.startswith('/') or '\\' in value:
                fail(400, 'INVALID_GRANT')
    repositories = grant.get('repositories', [])
    if not isinstance(repositories,list) or len(repositories)>50 or any(
            not isinstance(value,str) or (value!='*' and not re.fullmatch(r'[A-Za-z0-9_.-]{1,200}',value))
            for value in repositories):
        fail(400,'INVALID_GRANT')
    sessions = grant.get('sessions', 1)
    if type(sessions) is not int or not 1 <= sessions <= 4:
        fail(400, 'INVALID_GRANT')
    mcp = grant.get('mcp', {})
    if not isinstance(mcp, dict) or len(mcp) > 20:
        fail(400, 'INVALID_GRANT')
    for connection, block in mcp.items():
        if not isinstance(connection, str) or not connection or len(connection) > 80 or not isinstance(block, dict) or set(block) != {'tools'}:
            fail(400, 'INVALID_GRANT')
        tools = block['tools']
        if not isinstance(tools, dict) or set(tools) != {'allow','deny'}:
            fail(400, 'INVALID_GRANT')
        for key in ('allow','deny'):
            patterns = tools[key]
            if not isinstance(patterns, list) or len(patterns) > 50 or any(not isinstance(v,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',v) for v in patterns):
                fail(400, 'INVALID_GRANT')
    return grant


def narrower(child, parent):
    """Conservative subset for prototype patterns; no inferred regex inclusion."""
    grant_valid(child)
    grant_valid(parent)
    def covered(pattern, allowed):
        return any(p == '**' or p == pattern or (p.endswith('/**') and pattern.startswith(p[:-2])) for p in allowed)
    resource_narrow = (all(covered(p, parent.get(action, [])) for action in ('read','write','publish') for p in child.get(action, []))
            and all(covered(p, child.get('deny', [])) for p in parent.get('deny', []))
            and all(repo in parent.get('repositories',[]) or '*' in parent.get('repositories',[])
                    for repo in child.get('repositories',[]))
            and child.get('sessions',1) <= parent.get('sessions',1))
    for connection, block in child.get('mcp',{}).items():
        parent_block = parent.get('mcp',{}).get(connection)
        if not parent_block:
            return False
        child_tools, parent_tools = block['tools'], parent_block['tools']
        if any(pattern not in parent_tools['allow'] for pattern in child_tools['allow']):
            return False
        if any(pattern not in child_tools['deny'] for pattern in parent_tools['deny']):
            return False
    return resource_narrow


@dataclass
class Identity:
    row: dict
    credential: dict
    grants: list[dict]
    tier: int

    @property
    def id(self): return self.row['id']
    @property
    def name(self): return self.row['name']
    @property
    def kind(self): return self.row['portal_kind']
    @property
    def is_operator(self):
        return self.kind == 'human' and self.row['is_admin'] and self.credential['kind'] == 'session' and self.tier >= 4

    def permits(self, action, path):
        return all(not any(vault.matches(p,path) for p in g.get('deny',[]))
                   and any(vault.matches(p,path) for p in g.get(action,[])) for g in self.grants)

    def repositories(self):
        effective = None
        for grant in self.grants:
            scope = set(grant.get('repositories',[]))
            if '*' in scope:
                continue
            effective = scope if effective is None else effective & scope
        return ['*'] if effective is None else sorted(effective)

    def permits_repository(self, repository):
        scope = self.repositories()
        return '*' in scope or repository in scope

    def permits_mcp(self, connection, tool_name):
        for grant in self.grants:
            block = grant.get('mcp', {}).get(connection)
            if not block:
                return False
            tools = block['tools']
            if tool_name in tools['deny']:
                return False
            if tool_name not in tools['allow']:
                return False
        return True

    def public(self):
        metadata=unpack(self.row.get('portal_metadata') or {})
        persona=metadata.get('persona') if isinstance(metadata,dict) else None
        if isinstance(persona,dict):
            allowed=('id','display','role','system_prompt','definition_source','definition_complete',
                     'starter_prompts','default_pane_mode','colour','icon','bundle_version')
            persona={key:persona[key] for key in allowed if key in persona}
        else:
            persona=None
        return {'id': self.id, 'name': self.name, 'kind': self.kind,
                'state': self.row['portal_state'], 'max_tier': self.row['max_tier'],
                'effective_tier': self.tier, 'credential_kind': self.credential['kind'],
                'credential_id': str(self.credential['id']), 'scope': self.credential['scope'],
                'expires_at': self.credential['expires_at'], 'grant': unpack(self.row['portal_grant']),
                'operator': self.is_operator, 'session_limit': min(g.get('sessions',1) for g in self.grants),
                'persona': persona}


async def record(conn, actor, verb, target='', outcome='ok', detail=None, trace=None):
    await conn.execute('''INSERT INTO plane_audit(trace_id,actor_id,actor_name,verb,target,outcome,detail)
                          VALUES($1,$2,$3,$4,$5,$6,$7)''',
                       trace or uuid.uuid4(), actor.id if actor else None, actor.name if actor else 'anonymous',
                       verb, str(target)[:256], outcome, json.dumps(detail or {}))


async def identity(conn, credential, *, lock=False):
    credential = dict(credential)
    if credential['revoked_at'] or credential['expires_at'] <= now() or credential['authorization_expires_at'] <= now():
        fail(401, 'CREDENTIAL_EXPIRED_OR_REVOKED')
    if credential['kind'] == 'refresh':
        fail(401, 'ACCESS_TOKEN_REQUIRED')
    query = 'SELECT * FROM service_accounts WHERE id=$1' + (' FOR UPDATE' if lock else '')
    row = await conn.fetchrow(query, credential['principal_id'])
    if not row or row['disabled'] or row['portal_kind'] == 'legacy' or row['portal_state'] not in ('active','restricted'):
        fail(401, 'PRINCIPAL_INACTIVE')
    if lock:
        # A revoke may have committed while this request waited for the principal
        # lock. Re-read the credential after acquiring that serialization point.
        latest = await conn.fetchrow('SELECT * FROM plane_credentials WHERE id=$1', credential['id'])
        if not latest or latest['revoked_at'] or latest['expires_at'] <= now() or latest['authorization_expires_at'] <= now():
            fail(401, 'CREDENTIAL_EXPIRED_OR_REVOKED')
        credential = dict(latest)
    tier = min(row['max_tier'], credential['tier'])
    if row['portal_state'] == 'restricted': tier = min(tier,1)
    grants = [unpack(row['portal_grant'])]
    seen = {row['id']}
    parent = row['portal_parent']
    while parent is not None:
        if parent in seen or len(seen) >= 8: fail(401, 'INVALID_ANCESTRY')
        seen.add(parent)
        ancestor = await conn.fetchrow('SELECT * FROM service_accounts WHERE id=$1', parent)
        if not ancestor or ancestor['disabled'] or ancestor['portal_state'] != 'active': fail(401, 'ANCESTOR_INACTIVE')
        tier = min(tier, ancestor['max_tier'])
        grants.append(unpack(ancestor['portal_grant']))
        parent = ancestor['portal_parent']
    return Identity(dict(row), credential, grants, tier)


async def caller(conn, request: Request, *, operator=False, lock=False):
    header = request.headers.get('authorization', '')
    if header:
        if not header.lower().startswith('bearer '): fail(401, 'BEARER_REQUIRED')
        raw = header[7:]
        expected_cookie = False
    else:
        raw = request.cookies.get(COOKIE)
        expected_cookie = True
    if not raw: fail(401, 'SIGN_IN_REQUIRED')
    cred = await conn.fetchrow('SELECT * FROM plane_credentials WHERE token_hash=$1', auth.hash_token(raw))
    if not cred or (cred['kind'] == 'session') != expected_cookie: fail(401, 'INVALID_CREDENTIAL')
    user = await identity(conn, cred, lock=lock)
    if expected_cookie and request.method not in ('GET','HEAD','OPTIONS'):
        expected = auth.hash_token(raw + ':csrf')
        if not secrets.compare_digest(request.headers.get('x-csrf-token',''), expected): fail(403, 'CSRF_REQUIRED')
        origin = request.headers.get('origin')
        if origin and origin != config.PUBLIC_URL: fail(403, 'BAD_ORIGIN')
    if operator and not user.is_operator: fail(403, 'HUMAN_OPERATOR_REQUIRED')
    await conn.execute('UPDATE plane_credentials SET last_used_at=now() WHERE id=$1', cred['id'])
    return user


async def mint(conn, principal_id, kind, label, tier, expires, *, scope='mcp', family=None, client_id=None, authorization_expires=None):
    raw = secrets.token_urlsafe(32)
    deadline = authorization_expires or expires
    row = await conn.fetchrow('''INSERT INTO plane_credentials(principal_id,token_hash,kind,label,tier,scope,family,client_id,expires_at,authorization_expires_at)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id,kind,label,tier,scope,expires_at,family''',
        principal_id, auth.hash_token(raw), kind, label, tier, scope, family or uuid.uuid4(), client_id, min(expires,deadline), deadline)
    return raw, dict(row)


async def token_pair(conn, authorization):
    deadline = authorization['authorized_until']
    if not deadline or deadline <= now(): fail(400,'invalid_grant')
    tier = scope_tier(authorization['scope'])
    raw, access = await mint(conn, authorization['principal_id'], 'access','elevation',tier,
                            min(now()+timedelta(minutes=10), deadline), scope=authorization['scope'],
                            family=authorization['family'],client_id=authorization['client_id'],authorization_expires=deadline)
    refresh, _ = await mint(conn, authorization['principal_id'],'refresh','elevation refresh',tier,deadline,
                           scope=authorization['scope'],family=authorization['family'],client_id=authorization['client_id'],authorization_expires=deadline)
    # A rotating access credential must not tear down an established MCP transport.
    # Rebind live sessions in the family before the refreshed bearer is returned.
    await conn.execute('''UPDATE plane_sessions SET credential_id=$2,expires_at=$3
                          WHERE ended_at IS NULL AND credential_id IN
                          (SELECT id FROM plane_credentials WHERE family=$1)''',
                       authorization['family'],access['id'],deadline)
    return {'access_token':raw,'refresh_token':refresh,'token_type':'Bearer','scope':authorization['scope'],
            'expires_in':max(0,int((access['expires_at']-now()).total_seconds())), 'authorization_expires_at':deadline}


async def resource_call(conn, user, name, arguments, *, approved=False, trace=None):
    if name == 'whoami': return user.public()
    if name.startswith('resources_'):
        from datum_sync import resources
        if user.tier < 2: fail(403,'TIER_REQUIRED')
        trace = trace or uuid.uuid4()
        if name == 'resources_create': return await resources.create(conn,user,arguments,trace)
        if name == 'resources_list': return {'resources':await resources.list_visible(conn,user,arguments.get('limit',50))}
        if name == 'resources_read': return await resources.read(conn,user,arguments,trace)
        if name == 'resources_share': return await resources.share(conn,user,arguments,trace)
        if name == 'resources_revoke': return await resources.revoke(conn,user,arguments,trace)
        fail(404,'UNKNOWN_TOOL')
    if name not in ('documents_read','documents_write','releases_publish'): fail(404,'UNKNOWN_TOOL')
    tier = 1 if name == 'documents_read' else 3
    if user.tier < tier: fail(403,'TIER_REQUIRED','Elevate to mcp:operate for this action.')
    try: path = vault.normalise(arguments.get('path',''))
    except (vault.VaultPathError, TypeError): fail(400,'INVALID_PATH')
    action = {'documents_read':'read','documents_write':'write','releases_publish':'publish'}[name]
    if not user.permits(action,path): fail(403,'RESOURCE_DENIED')
    if name == 'documents_read':
        row = await conn.fetchrow('SELECT * FROM plane_documents WHERE path=$1',path)
        if not row: fail(404,'DOCUMENT_NOT_FOUND')
        await record(conn,user,'document.read',path,trace=trace)
        return dict(row)
    if name == 'documents_write':
        content = arguments.get('content')
        if not isinstance(content,str) or len(content.encode())>65536: fail(400,'INVALID_CONTENT')
        await conn.execute('''INSERT INTO plane_documents(path,content,updated_by) VALUES($1,$2,$3)
            ON CONFLICT(path) DO UPDATE SET content=$2,updated_by=$3,updated_at=now()''',path,content,user.id)
        await record(conn,user,'document.write',path,trace=trace)
        return {'path':path,'saved':True}
    row = await conn.fetchrow('SELECT content FROM plane_documents WHERE path=$1',path)
    if not row: fail(404,'DOCUMENT_NOT_FOUND')
    if approved: fail(400,'USE_APPROVAL_EXECUTOR')
    pending = await conn.fetchval('''INSERT INTO plane_pending_calls(principal_id,credential_id,tool,args,grant_snapshot,expires_at)
        VALUES($1,$2,$3,$4,$5,$6) RETURNING id''',user.id,user.credential['id'],name,
        json.dumps({'path':path,'content':row['content']}),json.dumps({'tier':user.tier,'grants':user.grants}),
        min(user.credential['authorization_expires_at'],now()+timedelta(hours=1)))
    await record(conn,user,'release.request',path,trace=trace)
    return {'status':'pending_approval','pending_id':str(pending)}
