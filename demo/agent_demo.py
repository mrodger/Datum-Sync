#!/usr/bin/env python3
"""A model-free MCP client for the local prototype. Secrets are read from env/files."""
import argparse
import json
import os
from pathlib import Path
import time
import httpx


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['enrol','claim','whoami','read','write','publish','elevate','legacy-repositories','legacy-workspaces','legacy-manifest','job-summary','jobs','job-status','job-log','job-events','job-artifacts','office-list','office-read','office-validate','postgres-schemas','postgres-tables','postgres-sample','postgres-summary','harness-personas','harness-map','harness-worms'])
    parser.add_argument('--url',default='http://127.0.0.1:8210')
    parser.add_argument('--name',default='terminal-agent')
    parser.add_argument('--path',default='demo/welcome.md')
    parser.add_argument('--content',default='Updated by the terminal agent.')
    parser.add_argument('--repository',default='Testing')
    parser.add_argument('--workspace')
    parser.add_argument('--job-id')
    parser.add_argument('--document',default='quarterly-brief.docx')
    parser.add_argument('--region')
    parser.add_argument('--after-id',type=int,default=0)
    parser.add_argument('--limit',type=int,default=100)
    parser.add_argument('--token-file',type=Path)
    parser.add_argument('--save-token',type=Path)
    args=parser.parse_args()
    token=args.token_file.read_text().strip() if args.token_file else os.environ.get('DATUM_AGENT_TOKEN','')
    def save(raw):
        if not args.save_token:
            raise SystemExit('Use --save-token to save the one-time credential to a private file.')
        args.save_token.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(args.save_token,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as out: out.write(raw+'\n')
        print('Credential saved: '+str(args.save_token))
    def checked(result):
        result.raise_for_status()
        return result.json()
    if args.action in ('enrol','claim','elevate') and (not args.save_token or args.save_token.exists()):
        raise SystemExit('Choose a new --save-token file before requesting a credential.')
    with httpx.Client(base_url=args.url,timeout=20,follow_redirects=False,trust_env=False) as client:
        if args.action=='enrol':
            code=os.environ.get('DATUM_ENROL_CODE')
            if not code: raise SystemExit('Set DATUM_ENROL_CODE to an operator-created invitation.')
            result=checked(client.post('/enrol',json={'name':args.name,'code':code,'purpose':'Terminal client demonstration'}))
            # A claim is a credential: store privately instead of logging it.
            save(result.pop('claim_code'))
            print(json.dumps(result,indent=2));return
        if args.action=='claim':
            claim=os.environ.get('DATUM_CLAIM_CODE')
            if not claim: raise SystemExit('Set DATUM_CLAIM_CODE after the operator approves the agent.')
            result=checked(client.post('/enrol/claim',json={'claim_code':claim}))
            save(result['token']);return
        if not token: raise SystemExit('Use --token-file or set DATUM_AGENT_TOKEN.')
        client.headers['Authorization']='Bearer '+token
        if args.action=='elevate':
            device=checked(client.post('/oauth/device',json={'client_id':'datum-local','scope':'mcp:operate','duration_seconds':900}))
            print('Approve code '+device['user_code']+' at '+device['verification_uri'])
            interval=device['interval']
            deadline=time.monotonic()+device['expires_in']
            while time.monotonic()<deadline:
                result=client.post('/oauth/token',data={'grant_type':'urn:ietf:params:oauth:grant-type:device_code','device_code':device['device_code'],'client_id':'datum-local'})
                body=result.json()
                if result.is_success:
                    save(body['access_token'])
                    print('Elevation expires: '+body['authorization_expires_at']);return
                if body.get('error')=='slow_down': interval+=5
                elif body.get('error')!='authorization_pending': raise SystemExit(body)
                time.sleep(interval)
            raise SystemExit('Approval request expired.')
        tool={'whoami':'whoami','read':'documents_read','write':'documents_write','publish':'releases_publish','legacy-repositories':'legacy__list_repositories','legacy-workspaces':'legacy__list_workspaces','legacy-manifest':'legacy__workspace_manifest','job-summary':'legacy__job_summary','jobs':'legacy__list_jobs','job-status':'legacy__job_status','job-log':'legacy__job_log','job-events':'legacy__job_events','job-artifacts':'legacy__job_artifacts','office-list':'office__officecli','office-read':'office__officecli','office-validate':'office__officecli','postgres-schemas':'postgres__list_schemas','postgres-tables':'postgres__list_tables','postgres-sample':'postgres__sample_orders','postgres-summary':'postgres__orders_summary','harness-personas':'harness__list_personas','harness-map':'harness__render_leaflet_map','harness-worms':'harness__worms_lookup'}[args.action]
        if args.action in ('legacy-repositories','postgres-schemas','postgres-summary','harness-personas'): call_args={}
        elif args.action=='office-list': call_args={'command':'list'}
        elif args.action=='office-read': call_args={'command':['view',args.document,'text']}
        elif args.action=='office-validate': call_args={'command':['validate',args.document]}
        elif args.action=='postgres-tables': call_args={'schema':'mcp_demo'}
        elif args.action=='postgres-sample': call_args={'limit':args.limit,**({'region':args.region} if args.region else {})}
        elif args.action=='harness-map': call_args={'lat':-41.29,'lon':174.78,'zoom':12,'basemap':'osm','markers':[{'lat':-41.2865,'lon':174.7762,'popup':'Wellington'}]}
        elif args.action=='harness-worms': call_args={'names':['Crassostrea gigas','Perna canaliculus']}
        elif args.action=='legacy-workspaces': call_args={'repository':args.repository}
        elif args.action=='legacy-manifest':
            if not args.workspace: raise SystemExit('Use --workspace with legacy-manifest.')
            call_args={'repository':args.repository,'workspace':args.workspace}
        elif args.action=='job-summary': call_args={}
        elif args.action=='jobs': call_args={'limit':args.limit}
        elif args.action in ('job-status','job-log','job-events','job-artifacts'):
            if not args.job_id: raise SystemExit('Use --job-id with '+args.action+'.')
            call_args={'job_id':args.job_id}
            if args.action in ('job-log','job-events'): call_args.update({'after_id':args.after_id,'limit':args.limit})
        else: call_args={'path':args.path,'content':args.content}
        print(json.dumps(run_tool(args.url,token,tool,call_args),indent=2))



def run_tool(url,token,name,arguments):
    with httpx.Client(base_url=url,headers={'Authorization':'Bearer '+token},timeout=20,trust_env=False) as client:
        result=client.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','clientInfo':{'name':'datum-terminal-client','version':'0.4.0'},'capabilities':{}}})
        result.raise_for_status()
        if 'error' in result.json(): raise RuntimeError(result.json()['error'])
        client.headers['Mcp-Session-Id']=result.headers['mcp-session-id']
        try:
            client.post('/mcp',json={'jsonrpc':'2.0','method':'notifications/initialized'})
            result=client.post('/mcp',json={'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':name,'arguments':arguments}})
            result.raise_for_status()
            return result.json()
        finally: client.delete('/mcp')


if __name__=='__main__': main()
