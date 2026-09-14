"""Service-authenticated demo MCP providers for Office documents and PostgreSQL."""
from __future__ import annotations

from contextlib import asynccontextmanager
import html
import importlib.util
import json
import os
import secrets
import shlex

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from datum_sync import config, db

PROTOCOL='2025-06-18'
OFFICE_TOOLS=[{
    'name':'officecli',
    'description':'Constrained OfficeCLI-compatible document inspection. Demo commands: list; view NAME text; validate NAME; help.',
    'inputSchema':{'type':'object','properties':{'command':{'oneOf':[{'type':'string'},{'type':'array','items':{'type':'string'}}]}},'required':['command']}
}]
POSTGRES_TOOLS=[
    {'name':'list_schemas','description':'List schemas exposed by the read-only demonstration database.','inputSchema':{'type':'object','properties':{}}},
    {'name':'list_tables','description':'List tables in an exposed schema.','inputSchema':{'type':'object','properties':{'schema':{'type':'string','enum':['mcp_demo']}}}},
    {'name':'describe_table','description':'Describe safe columns for one exposed table.','inputSchema':{'type':'object','properties':{'schema':{'type':'string','enum':['mcp_demo']},'table':{'type':'string','enum':['orders']}},'required':['table']}},
    {'name':'sample_orders','description':'Read a bounded sample of demonstration orders, optionally filtered by region.','inputSchema':{'type':'object','properties':{'region':{'type':'string','maxLength':80},'limit':{'type':'integer','minimum':1,'maximum':25}}}},
    {'name':'orders_summary','description':'Summarize demonstration order counts and totals by region and status.','inputSchema':{'type':'object','properties':{}}},
]
HARNESS_TOOLS=[
    {'name':'list_personas','description':'List the agent personas defined by the Datum Harness source configuration.','inputSchema':{'type':'object','properties':{}}},
    {'name':'render_leaflet_map','description':'Render bounded Leaflet HTML from a centre point and optional markers. No files are written.','inputSchema':{'type':'object','properties':{
        'lat':{'type':'number','minimum':-90,'maximum':90},'lon':{'type':'number','minimum':-180,'maximum':180},
        'zoom':{'type':'integer','minimum':1,'maximum':20},
        'basemap':{'type':'string','enum':['esri_world_imagery','esri_topo','osm','esri_street','carto_dark','carto_light']},
        'markers':{'type':'array','maxItems':100,'items':{'type':'object','properties':{
            'lat':{'type':'number','minimum':-90,'maximum':90},'lon':{'type':'number','minimum':-180,'maximum':180},
            'popup':{'type':'string','maxLength':500}},'required':['lat','lon'],'additionalProperties':False}}},'additionalProperties':False}},
    {'name':'worms_lookup','description':'Resolve up to 20 scientific names through the public World Register of Marine Species API.','inputSchema':{'type':'object','properties':{
        'names':{'type':'array','minItems':1,'maxItems':20,'items':{'type':'string','minLength':1,'maxLength':200}}},
        'required':['names'],'additionalProperties':False}},
]
PERSONAS_PATH=config.REPO_ROOT/'demo'/'personas.py'
WORMS_URL='https://www.marinespecies.org/rest/AphiaRecordsByMatchNames'
MAX_WORMS_RESPONSE=4*1024*1024
DOCUMENTS={
    'quarterly-brief.docx':{'kind':'DOCX','pages':4,'title':'Q3 Spatial Operations Brief','modified':'2026-09-12','text':'Field operations expanded in Auckland and Canterbury. Pending map publication is awaiting review.','valid':True},
    'fleet-overview.xlsx':{'kind':'XLSX','sheets':3,'title':'Agent Fleet Overview','modified':'2026-09-13','text':'Agents, MCP servers, credential grants and recent outcomes.','valid':True},
    'platform-demo.pptx':{'kind':'PPTX','slides':6,'title':'Datum MCP Governance Demo','modified':'2026-09-14','text':'Register. Grant. Observe. Revoke.','valid':True},
}

@asynccontextmanager
async def lifespan(_app):
    if min(*(len(os.environ.get(name,'')) for name in ('OFFICE_MCP_TOKEN','POSTGRES_MCP_TOKEN','HARNESS_MCP_TOKEN'))) < 32:
        raise RuntimeError('demo MCP service tokens must be configured')
    await db.init_pool()
    try: yield
    finally: await db.close_pool()

app=FastAPI(title='Datum demonstration MCP providers',version='0.1.0',lifespan=lifespan)

def content(value):
    return {'content':[{'type':'text','text':json.dumps(value,default=str)}],'structuredContent':value,'isError':False}

def denied(message):
    return {'content':[{'type':'text','text':message}],'structuredContent':{'error':message},'isError':True}

def token_ok(request,kind):
    expected=os.environ[{'office':'OFFICE_MCP_TOKEN','postgres':'POSTGRES_MCP_TOKEN','harness':'HARNESS_MCP_TOKEN'}[kind]]
    return secrets.compare_digest(request.headers.get('authorization',''),'Bearer '+expected)

async def principal_ok(principal):
    if not isinstance(principal,dict) or type(principal.get('id')) is not int or not isinstance(principal.get('name'),str):
        return False
    async with db.pool().acquire() as conn:
        return bool(await conn.fetchval("SELECT 1 FROM service_accounts WHERE id=$1 AND name=$2 AND disabled=false AND portal_state IN ('active','restricted')",principal['id'],principal['name']))

def office_call(arguments):
    command=arguments.get('command') if isinstance(arguments,dict) else None
    try: parts=shlex.split(command) if isinstance(command,str) else list(command) if isinstance(command,list) else []
    except ValueError: return denied('invalid OfficeCLI command')
    if not parts or any(not isinstance(part,str) or len(part)>200 for part in parts) or len(parts)>4:
        return denied('command must contain 1-4 bounded arguments')
    if parts==['list']:
        return content({'documents':[{'name':name,**{k:v for k,v in doc.items() if k!='text'}} for name,doc in DOCUMENTS.items()]})
    if parts==['help']:
        return content({'commands':['list','view <name> text','validate <name>'],'boundary':'demo documents only; create, set, add, remove, batch and raw commands are blocked'})
    if len(parts)==3 and parts[0]=='view' and parts[2]=='text' and parts[1] in DOCUMENTS:
        doc=DOCUMENTS[parts[1]]
        return content({'name':parts[1],'title':doc['title'],'text':doc['text']})
    if len(parts)==2 and parts[0]=='validate' and parts[1] in DOCUMENTS:
        return content({'name':parts[1],'valid':DOCUMENTS[parts[1]]['valid'],'issues':[]})
    return denied('command is outside the governed OfficeCLI demo allowlist')

async def postgres_call(name,arguments):
    if not isinstance(arguments,dict): return denied('arguments must be an object')
    async with db.pool().acquire() as conn:
        if name=='list_schemas': return content({'schemas':['mcp_demo']})
        if name=='list_tables':
            if arguments.get('schema','mcp_demo')!='mcp_demo': return denied('schema is not exposed')
            return content({'schema':'mcp_demo','tables':[{'name':'orders','kind':'table'}]})
        if name=='describe_table':
            if arguments.get('schema','mcp_demo')!='mcp_demo' or arguments.get('table')!='orders': return denied('table is not exposed')
            rows=await conn.fetch("""SELECT column_name,data_type,is_nullable FROM information_schema.columns
                WHERE table_schema='mcp_demo' AND table_name='orders' ORDER BY ordinal_position""")
            return content({'schema':'mcp_demo','table':'orders','columns':[dict(row) for row in rows]})
        if name=='sample_orders':
            limit=arguments.get('limit',5); region=arguments.get('region')
            if type(limit) is not int or not 1<=limit<=25 or (region is not None and (not isinstance(region,str) or len(region)>80)):
                return denied('invalid region or limit')
            rows=await conn.fetch("""SELECT id,ordered_on,region,product,status,total_nzd FROM mcp_demo.orders
                WHERE ($1::text IS NULL OR region=$1) ORDER BY id LIMIT $2""",region,limit)
            return content({'orders':[dict(row) for row in rows],'count':len(rows)})
        if name=='orders_summary':
            rows=await conn.fetch("""SELECT region,status,count(*) AS orders,sum(total_nzd) AS total_nzd
                FROM mcp_demo.orders GROUP BY region,status ORDER BY region,status""")
            return content({'summary':[dict(row) for row in rows]})
    return None


def _harness_personas():
    # Load the vendored persona definitions without putting demo/ on sys.path.
    spec=importlib.util.spec_from_file_location('datum_demo_personas',PERSONAS_PATH)
    if not spec or not spec.loader: raise RuntimeError('demo personas are unavailable')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module.get_all()


def _finite_number(value,minimum,maximum):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and minimum<=value<=maximum


def render_leaflet_map(arguments):
    if not isinstance(arguments,dict) or set(arguments)-{'lat','lon','zoom','basemap','markers'}:
        return denied('invalid map arguments')
    lat=arguments.get('lat',-36.85); lon=arguments.get('lon',174.76); zoom=arguments.get('zoom',13)
    basemap=arguments.get('basemap','esri_world_imagery'); markers=arguments.get('markers',[])
    basemaps={
        'esri_world_imagery':('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',19,'Tiles &copy; Esri'),
        'esri_topo':('https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}',19,'Tiles &copy; Esri'),
        'esri_street':('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}',19,'Tiles &copy; Esri'),
        'osm':('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',19,'&copy; OpenStreetMap contributors'),
        'carto_dark':('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',19,'&copy; OpenStreetMap contributors &copy; CARTO'),
        'carto_light':('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',19,'&copy; OpenStreetMap contributors &copy; CARTO'),
    }
    if not _finite_number(lat,-90,90) or not _finite_number(lon,-180,180) or type(zoom) is not int or not 1<=zoom<=20 or basemap not in basemaps:
        return denied('invalid map centre, zoom or basemap')
    if not isinstance(markers,list) or len(markers)>100: return denied('markers must be an array of at most 100 items')
    clean=[]
    for marker in markers:
        if (not isinstance(marker,dict) or set(marker)-{'lat','lon','popup'} or
                not _finite_number(marker.get('lat'),-90,90) or not _finite_number(marker.get('lon'),-180,180) or
                not isinstance(marker.get('popup',''),str) or len(marker.get('popup',''))>500):
            return denied('invalid marker')
        clean.append({'lat':marker['lat'],'lon':marker['lon'],'popup':marker.get('popup','')})
    tile,max_zoom,attribution=basemaps[basemap]
    marker_js='\n'.join('L.marker('+json.dumps([m['lat'],m['lon']])+').addTo(map).bindPopup('+json.dumps(html.escape(m['popup']))+');' for m in clean)
    document=f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Datum Harness map</title><link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"></head>
<body style="margin:0"><div id="map" style="height:600px"></div><script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>
const map=L.map("map").setView({json.dumps([lat,lon])},{zoom});L.tileLayer({json.dumps(tile)},{{maxZoom:{max_zoom},attribution:{json.dumps(attribution)}}}).addTo(map);
{marker_js}</script></body></html>"""
    return content({'html':document,'basemap':basemap,'centre':[lat,lon],'zoom':zoom,'marker_count':len(clean)})


async def _fetch_worms(cleaned):
    params=[('scientificnames[]',name) for name in cleaned]+[('marine_only','false')]
    async with httpx.AsyncClient(timeout=15,follow_redirects=False,trust_env=False) as client:
        async with client.stream('GET',WORMS_URL,params=params,headers={'Accept':'application/json','User-Agent':'Datum-Sync-local-prototype/0.9'}) as response:
            response.raise_for_status()
            body=bytearray()
            async for chunk in response.aiter_bytes():
                if len(body)+len(chunk)>MAX_WORMS_RESPONSE: raise ValueError('WoRMS response exceeds limit')
                body.extend(chunk)
    return json.loads(body)


async def worms_lookup(arguments):
    names=arguments.get('names') if isinstance(arguments,dict) and set(arguments)<= {'names'} else None
    if (not isinstance(names,list) or not 1<=len(names)<=20 or
            any(not isinstance(name,str) or not 1<=len(name.strip())<=200 for name in names)):
        return denied('names must contain 1-20 bounded scientific names')
    cleaned=[name.strip() for name in names]
    try:
        raw=await _fetch_worms(cleaned)
    except (httpx.HTTPError,ValueError):
        return denied('WoRMS lookup is temporarily unavailable')
    if not isinstance(raw,list) or len(raw)!=len(cleaned): return denied('WoRMS returned an invalid response')
    results=[]
    for query,candidates in zip(cleaned,raw):
        choices=[row for row in (candidates or []) if isinstance(row,dict)] if isinstance(candidates,list) else []
        exact=[row for row in choices if str(row.get('match_type','')).lower()=='exact']
        accepted=[row for row in exact if row.get('status')=='accepted']
        record=(accepted or exact or choices or [None])[0]
        if record is None:
            results.append({'query':query,'matched':False}); continue
        environments=[label for label,key in (('marine','isMarine'),('brackish','isBrackish'),('freshwater','isFreshwater'),('terrestrial','isTerrestrial')) if record.get(key)]
        results.append({'query':query,'matched':True,'scientific_name':record.get('scientificname'),
            'authority':record.get('authority'),'aphia_id':record.get('AphiaID'),'status':record.get('status'),
            'valid_name':record.get('valid_name'),'valid_aphia_id':record.get('valid_AphiaID'),
            'rank':record.get('rank'),'kingdom':record.get('kingdom'),'phylum':record.get('phylum'),
            'class':record.get('class'),'order':record.get('order'),'family':record.get('family'),
            'environments':environments})
    return content({'source':'World Register of Marine Species','results':results})


async def harness_call(name,arguments):
    if not isinstance(arguments,dict): return denied('arguments must be an object')
    if name=='list_personas':
        if arguments: return denied('list_personas does not accept arguments')
        personas=[{key:item[key] for key in ('id','display','role','icon','colour','default_pane_mode','prompts')} for item in _harness_personas()]
        return content({'source':'demo/personas.py','personas':personas,'count':len(personas)})
    if name=='render_leaflet_map': return render_leaflet_map(arguments)
    if name=='worms_lookup': return await worms_lookup(arguments)
    return None

@app.get('/health')
async def health():
    async with db.pool().acquire() as conn: await conn.fetchval('SELECT 1')
    return {'status':'ok','providers':['officecli-demo','postgres-demo','harness-tools']}

@app.post('/{kind}/mcp')
async def mcp(kind:str,request:Request):
    if kind not in ('office','postgres','harness') or not token_ok(request,kind):
        return JSONResponse({'detail':'unauthorized'},status_code=401)
    try: body=await request.json()
    except ValueError: return JSONResponse({'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'Parse error'}})
    rid,method=body.get('id'),body.get('method')
    tools={'office':OFFICE_TOOLS,'postgres':POSTGRES_TOOLS,'harness':HARNESS_TOOLS}[kind]
    if method=='initialize': result={'protocolVersion':PROTOCOL,'capabilities':{'tools':{'listChanged':False}},'serverInfo':{'name':'datum-'+kind+'-demo','version':'0.1.0'}}
    elif method=='tools/list': result={'tools':tools}
    elif method=='ping': result={}
    elif method=='notifications/initialized': return JSONResponse({},status_code=202)
    elif method=='tools/call':
        params=body.get('params') or {}; meta=params.get('_meta') if isinstance(params.get('_meta'),dict) else {}
        if not await principal_ok(meta.get('io.datum.principal')): result=denied('principal context is missing or inactive')
        elif kind=='office' and params.get('name')=='officecli': result=office_call(params.get('arguments') or {})
        elif kind=='postgres': result=await postgres_call(params.get('name'),params.get('arguments') or {})
        elif kind=='harness': result=await harness_call(params.get('name'),params.get('arguments') or {})
        else: result=None
        if result is None: return JSONResponse({'jsonrpc':'2.0','id':rid,'error':{'code':-32602,'message':'Unknown tool'}})
    else: return JSONResponse({'jsonrpc':'2.0','id':rid,'error':{'code':-32601,'message':'Method not found'}})
    return {'jsonrpc':'2.0','id':rid,'result':result}
