"""Governed, bounded artifacts shared between agents in one Datum Sync fleet."""
from __future__ import annotations
import hashlib, json, re, uuid
from datetime import timedelta
from datum_sync.portal_core import fail, now, record

MAX_BYTES=1024*1024
MEDIA_TYPES={'text/plain','text/markdown','text/html','application/json','application/geo+json','text/csv'}
PATH_RE=re.compile(r'^[a-z0-9][a-z0-9._/-]{0,239}$')

def _id(value):
    try: return uuid.UUID(str(value))
    except (TypeError,ValueError): fail(400,'INVALID_RESOURCE_ID')

def _path(user,value):
    if not isinstance(value,str): fail(400,'INVALID_RESOURCE_PATH')
    value=value.strip().lower(); prefix=f'agents/{user.name}/'
    if not value.startswith(prefix) or not PATH_RE.fullmatch(value) or '..' in value or '//' in value:
        fail(403,'RESOURCE_PATH_DENIED',f'Create resources under {prefix}')
    return value

def descriptor(row,*,access='owner'):
    return {'id':str(row['id']),'uri':f'datum://resources/{row["id"]}','path':row['path'],
        'version':row['version'],'title':row['title'],'media_type':row['media_type'],'bytes':row['bytes'],
        'sha256':row['sha256'],'owner':row['owner_name'],'access':access,'can_share':access=='owner',
        'source':{'trace_id':str(row['source_trace_id']),'connection':row['source_connection'],'tool':row['source_tool']},
        'created_at':row['created_at'],'expires_at':row['expires_at']}

async def _event(conn,user,resource_id,kind,trace,detail=None):
    await conn.execute('''INSERT INTO plane_resource_events(resource_id,trace_id,actor_id,actor_name,kind,detail)
        VALUES($1,$2,$3,$4,$5,$6)''',resource_id,trace,user.id,user.name,kind,json.dumps(detail or {}))
    await record(conn,user,kind,resource_id,trace=trace,detail=detail)

async def create(conn,user,arguments,trace):
    path=_path(user,arguments.get('path')); title=arguments.get('title')
    media_type=arguments.get('media_type','text/markdown'); content=arguments.get('content')
    if not isinstance(title,str) or not title.strip() or len(title)>160: fail(400,'INVALID_RESOURCE_TITLE')
    if media_type not in MEDIA_TYPES: fail(400,'UNSUPPORTED_MEDIA_TYPE')
    if not isinstance(content,str): fail(400,'INVALID_RESOURCE_CONTENT')
    payload=content.encode('utf-8')
    if len(payload)>MAX_BYTES: fail(413,'RESOURCE_TOO_LARGE')
    ttl=arguments.get('ttl_hours',168)
    if type(ttl) is not int or not 1<=ttl<=720: fail(400,'INVALID_RESOURCE_TTL')
    await conn.execute('SELECT pg_advisory_xact_lock(hashtext($1))',path)
    previous=await conn.fetchrow('SELECT id,version FROM plane_resources WHERE path=$1 ORDER BY version DESC LIMIT 1',path)
    version=previous['version']+1 if previous else 1
    row=await conn.fetchrow('''INSERT INTO plane_resources
      (path,version,title,media_type,content,bytes,sha256,owner_id,owner_name,source_trace_id,source_connection,source_tool,parent_id,expires_at)
      VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'datum-sync','resources_create',$11,$12) RETURNING *''',
      path,version,title.strip(),media_type,payload,len(payload),hashlib.sha256(payload).hexdigest(),user.id,user.name,
      trace,previous['id'] if previous else None,now()+timedelta(hours=ttl))
    await _event(conn,user,row['id'],'resource.created',trace,{'path':path,'version':version,'media_type':media_type,'bytes':len(payload)})
    return descriptor(row)

async def list_visible(conn,user,limit=50):
    if type(limit) is not int or not 1<=limit<=100: fail(400,'INVALID_PARAMETER')
    rows=await conn.fetch('''SELECT r.*,CASE WHEN r.owner_id=$1 THEN 'owner' ELSE 'shared' END AS access
      FROM plane_resources r WHERE r.state='active' AND (r.expires_at IS NULL OR r.expires_at>now()) AND
      (r.owner_id=$1 OR EXISTS(SELECT 1 FROM plane_resource_grants g WHERE g.resource_id=r.id AND g.principal_id=$1
       AND g.revoked_at IS NULL AND (g.expires_at IS NULL OR g.expires_at>now()))) ORDER BY r.created_at DESC LIMIT $2''',user.id,limit)
    return [descriptor(row,access=row['access']) for row in rows]

async def _readable(conn,user,resource_id):
    row=await conn.fetchrow('''SELECT r.*,CASE WHEN r.owner_id=$2 THEN 'owner' ELSE 'shared' END AS access
      FROM plane_resources r WHERE r.id=$1 AND r.state='active' AND (r.expires_at IS NULL OR r.expires_at>now()) AND
      (r.owner_id=$2 OR EXISTS(SELECT 1 FROM plane_resource_grants g WHERE g.resource_id=r.id AND g.principal_id=$2
       AND g.revoked_at IS NULL AND (g.expires_at IS NULL OR g.expires_at>now())))''',_id(resource_id),user.id)
    if not row: fail(404,'RESOURCE_NOT_FOUND')
    return row

async def read(conn,user,arguments,trace,*,include_content=True):
    row=await _readable(conn,user,arguments.get('id'))
    await _event(conn,user,row['id'],'resource.read',trace,{'path':row['path'],'version':row['version']})
    value=descriptor(row,access=row['access'])
    if include_content: value['content']=bytes(row['content']).decode('utf-8')
    return value

async def share(conn,user,arguments,trace):
    resource_id=_id(arguments.get('id')); target_name=arguments.get('target')
    if not isinstance(target_name,str): fail(400,'INVALID_TARGET')
    row=await conn.fetchrow("SELECT * FROM plane_resources WHERE id=$1 AND owner_id=$2 AND state='active'",resource_id,user.id)
    if not row: fail(404,'RESOURCE_NOT_FOUND')
    target=await conn.fetchrow("SELECT id,name,portal_parent FROM service_accounts WHERE name=$1 AND portal_kind='agent' AND portal_state='active' AND disabled=false",target_name)
    if not target or target['id']==user.id or target['portal_parent']!=user.row['portal_parent']: fail(403,'SHARE_TARGET_DENIED')
    ttl=arguments.get('ttl_hours',24)
    if type(ttl) is not int or not 1<=ttl<=720: fail(400,'INVALID_RESOURCE_TTL')
    expires=min(row['expires_at'],now()+timedelta(hours=ttl)) if row['expires_at'] else now()+timedelta(hours=ttl)
    await conn.execute('''INSERT INTO plane_resource_grants(resource_id,principal_id,principal_name,granted_by,granted_by_name,expires_at)
      VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(resource_id,principal_id) DO UPDATE SET granted_by=$4,granted_by_name=$5,
      granted_at=now(),expires_at=$6,revoked_at=NULL,revoked_by=NULL''',resource_id,target['id'],target['name'],user.id,user.name,expires)
    await _event(conn,user,resource_id,'resource.shared',trace,{'target':target['name'],'expires_at':expires.isoformat()})
    return {'resource':descriptor(row),'shared_with':target['name'],'expires_at':expires}

async def revoke(conn,user,arguments,trace):
    resource_id=_id(arguments.get('id')); target=arguments.get('target')
    if not isinstance(target,str): fail(400,'INVALID_TARGET')
    resource=await conn.fetchrow('SELECT id FROM plane_resources WHERE id=$1 AND owner_id=$2',resource_id,user.id)
    if not resource: fail(404,'RESOURCE_NOT_FOUND')
    changed=await conn.fetchval('''UPDATE plane_resource_grants SET revoked_at=now(),revoked_by=$3
      WHERE resource_id=$1 AND principal_name=$2 AND revoked_at IS NULL RETURNING id''',resource_id,target,user.id)
    if not changed: fail(404,'RESOURCE_GRANT_NOT_FOUND')
    await _event(conn,user,resource_id,'resource.revoked',trace,{'target':target})
    return {'id':str(resource_id),'revoked_for':target}
