"""Exercise the visible local portal. Leaves a labelled demonstration agent and audit history."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import asyncio
import json
import time

import asyncpg
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
OUTPUT=ROOT/'review/screenshots'


def seed_job(submitted_by):
    async def create():
        config=json.loads((ROOT/'.runtime/config.json').read_text())
        conn=await asyncpg.connect(config['DATABASE_URL'])
        try:
            principal_id=await conn.fetchval("SELECT id FROM service_accounts WHERE name=$1",submitted_by)
            artifacts=json.dumps([{'name':'browser-report.txt','type':'text/plain','primary':True,
                                   'dir':False,'size':24,'file':'browser-report.txt'}])
            job_id=await conn.fetchval("""INSERT INTO jobs(repository,workspace,status,submitted_by,portal_principal_id,
                artifacts,started_at,completed_at) VALUES('Testing','Browser demo','complete',$1,$2,$3,now(),now()) RETURNING id""",
                submitted_by,principal_id,artifacts)
            log_id=await conn.fetchval("INSERT INTO job_log(job_id,level,message) VALUES($1,'info','browser demo completed') RETURNING id",job_id)
            await conn.execute("INSERT INTO job_events(job_id,kind,payload) VALUES($1,'log',$2)",job_id,
                               json.dumps({'event':'log','id':log_id,'level':'info','message':'browser demo completed'}))
            return str(job_id)
        finally:
            await conn.close()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(create())).result()


def main():
    password=(ROOT/'.runtime/operator.txt').read_text().split('Password: ')[1].strip()
    name='browser-agent-'+str(int(time.time()))[-6:]
    OUTPUT.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch()
        page=browser.new_page(viewport={'width':1440,'height':1000})
        errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto('http://127.0.0.1:8210')
        expect(page.locator('#signin')).to_be_visible()
        page.locator('input[name=password]').fill(password)
        page.get_by_role('button',name='Sign in').click()
        expect(page.locator('#view')).to_have_attribute('data-ready','overview')
        page.locator('#nav').get_by_role('link',name='Secrets & Proxies',exact=True).click()
        expect(page.locator('#view')).to_have_attribute('data-ready','integrations')
        for server,add_label in (('officecli-demo','Add OfficeCLI document demo'),('postgres-demo','Add PostgreSQL analytics demo'),('harness-tools','Add Datum Harness tools')):
            if page.get_by_role('row').filter(has_text=server).count()==0:
                page.get_by_role('button',name='+ Add MCP server',exact=True).first.click()
                page.get_by_role('button',name=add_label,exact=True).click()
                expect(page.get_by_text(server,exact=True).first).to_be_visible()
        page.locator('#nav').get_by_role('link',name='Dashboard',exact=True).click()
        page.get_by_role('button',name='+ Enrol agent',exact=True).click()
        page.locator('input[name=agent-name]').fill(name)
        page.locator('input[name=purpose]').fill('Browser-verified prototype demonstration')
        page.get_by_role('button',name='Submit enrolment').click()
        expect(page.locator('#view')).to_have_attribute('data-ready','approvals')
        page.locator('.approval-card').filter(has_text=name).get_by_role('button',name='Approve',exact=True).click()
        expect(page.locator('.approval-card').filter(has_text=name)).to_have_count(0)
        job_id=seed_job(name)
        page.locator('#nav').get_by_role('link',name='Principals',exact=True).click()
        expect(page.locator('#view')).to_have_attribute('data-ready','principals')
        page.get_by_role('row').filter(has_text=name).get_by_role('button',name='Issue token').click()
        expect(page.get_by_role('heading',name='Baseline credential issued')).to_be_visible()
        page.get_by_role('button',name='Open access lab →').click()
        expect(page.locator('#view')).to_have_attribute('data-ready','access')
        page.get_by_role('button',name='Read',exact=True).click()
        expect(page.locator('.result')).to_contain_text('content')
        for credential in ('Legacy MCP service','OfficeCLI demo service','PostgreSQL demo service','Datum Harness tool service'):
            page.locator('.notice').filter(has_text=credential).get_by_role('button',name='Request access',exact=True).click()
            page.get_by_label('Credential request purpose').fill('Browser-verified governed MCP demonstration')
            page.get_by_role('button',name='Submit request',exact=True).click()
        page.locator('#nav').get_by_role('link',name='Approvals',exact=True).click()
        cards=page.locator('.approval-card').filter(has_text=name+' · Credential access')
        while cards.count():
            before=cards.count()
            cards.first.get_by_role('button',name='Approve',exact=True).click()
            expect(cards).to_have_count(before-1)
        page.locator('#nav').get_by_role('link',name='Access lab',exact=True).click()
        page.get_by_role('button',name='List legacy repositories',exact=True).click()
        expect(page.locator('.result')).to_contain_text('Testing')
        expect(page.locator('.result')).not_to_contain_text('Hermes')
        page.get_by_role('button',name='Read quarterly brief',exact=True).click()
        expect(page.locator('.result')).to_contain_text('Q3 Spatial Operations Brief')
        page.get_by_role('button',name='Sample Auckland orders',exact=True).click()
        expect(page.locator('.result')).to_contain_text('Field Survey')
        page.get_by_role('button',name='Summarize orders',exact=True).click()
        expect(page.locator('.result')).to_contain_text('Wellington')
        page.get_by_role('button',name='List harness personas',exact=True).click()
        expect(page.locator('.result')).to_contain_text('gis-analyst')
        page.get_by_role('button',name='Render Wellington map',exact=True).click()
        expect(page.locator('.result')).to_contain_text('marker_count')
        page.get_by_role('button',name='List my jobs',exact=True).click()
        expect(page.locator('.result')).to_contain_text(job_id)
        page.get_by_label('Job ID',exact=True).fill(job_id)
        page.get_by_role('button',name='Read job status',exact=True).click()
        expect(page.locator('.result')).not_to_contain_text('"jobs"')
        expect(page.locator('.result')).to_contain_text('complete')
        page.get_by_role('button',name='Read job log',exact=True).click()
        expect(page.locator('.result')).to_contain_text('browser demo completed')
        page.get_by_role('button',name='Read job events',exact=True).click()
        expect(page.locator('.result')).to_contain_text('"events"')
        expect(page.locator('.result')).to_contain_text('browser demo completed')
        page.get_by_role('button',name='List artifacts',exact=True).click()
        expect(page.locator('.result')).to_contain_text('browser-report.txt')
        page.get_by_role('button',name='Write',exact=True).click()
        expect(page.locator('.result')).to_contain_text('TIER_REQUIRED')
        page.screenshot(path=str(OUTPUT/'baseline-denied.png'),full_page=True)
        page.get_by_role('button',name='Request elevation',exact=True).click()
        expect(page.locator('#view')).to_contain_text('Approval code:')
        page.locator('#nav').get_by_role('link',name='Approvals').click()
        expect(page.locator('#view')).to_have_attribute('data-ready','approvals')
        page.screenshot(path=str(OUTPUT/'elevation-approval.png'),full_page=True)
        page.locator('.approval-card').filter(has_text=name).get_by_role('button',name='Approve',exact=True).click()
        expect(page.locator('.approval-card').filter(has_text=name)).to_have_count(0)
        page.locator('#nav').get_by_role('link',name='Access lab').click()
        page.get_by_role('button',name='Claim approved elevation').click()
        expect(page.locator('#view')).to_contain_text('effective tier 3')
        page.get_by_label('Document content',exact=True).fill('Published through a human-approved agent authorization.')
        page.get_by_role('button',name='Write',exact=True).click()
        expect(page.locator('.result')).to_contain_text('"saved": true')
        page.get_by_label('Resource path',exact=True).fill('private/operator.md')
        page.get_by_role('button',name='Read',exact=True).click()
        expect(page.locator('.result')).to_contain_text('RESOURCE_DENIED')
        page.get_by_label('Resource path',exact=True).fill('demo/welcome.md')
        page.get_by_role('button',name='Request publication').click()
        expect(page.locator('.result')).to_contain_text('pending_approval')
        page.locator('#nav').get_by_role('link',name='Approvals').click()
        page.locator('.approval-card').filter(has_text=name).get_by_role('button',name='Approve',exact=True).click()
        expect(page.locator('.approval-card').filter(has_text=name)).to_have_count(0)
        page.locator('#nav').get_by_role('link',name='Principals',exact=True).click()
        row=page.get_by_role('row').filter(has_text=name)
        row.get_by_role('button',name='MCP access',exact=True).click()
        expect(page.get_by_role('dialog')).to_contain_text('officecli-demo')
        expect(page.get_by_role('dialog')).to_contain_text('postgres-demo')
        expect(page.get_by_role('dialog')).to_contain_text('harness-tools')
        page.get_by_role('button',name='×',exact=True).click()
        row.get_by_role('button',name='Restrict',exact=True).click()
        page.get_by_role('button',name='Confirm restrict').click()
        expect(row).to_contain_text('restricted')
        row.get_by_role('button',name='Restore',exact=True).click()
        page.get_by_role('button',name='Confirm restore').click()
        expect(row).to_contain_text('active')
        page.locator('#nav').get_by_role('link',name='Access lab').click()
        page.get_by_role('button',name='Read',exact=True).click()
        expect(page.locator('.result')).to_contain_text('credential expired or revoked')
        page.locator('#nav').get_by_role('link',name='Secrets & Proxies',exact=True).click()
        expect(page.locator('#view')).to_contain_text('Legacy MCP service')
        expect(page.locator('#view')).to_contain_text('OfficeCLI document demo')
        expect(page.locator('#view')).to_contain_text('PostgreSQL analytics demo')
        expect(page.locator('#view')).to_contain_text('Datum Harness tools')
        tool_row=page.get_by_role('row').filter(has_text='postgres__sample_orders').first
        tool_row.get_by_role('button',name='Disable tool',exact=True).click()
        expect(tool_row).to_contain_text('disabled')
        tool_row.get_by_role('button',name='Enable tool',exact=True).click()
        office_row=page.get_by_role('row').filter(has_text='officecli-demo').filter(has=page.get_by_role('button',name='Disable server',exact=True))
        office_row.get_by_role('button',name='Disable server',exact=True).click()
        expect(page.get_by_role('row').filter(has_text='officecli-demo').filter(has=page.get_by_role('button',name='Enable server',exact=True))).to_contain_text('disabled')
        page.get_by_role('row').filter(has_text='officecli-demo').filter(has=page.get_by_role('button',name='Enable server',exact=True)).get_by_role('button',name='Enable server',exact=True).click()
        access_row=page.get_by_role('row').filter(has_text=name).filter(has_text='PostgreSQL demo service').filter(has=page.get_by_role('button',name='Revoke server access',exact=True))
        expect(access_row).to_contain_text('active')
        page.screenshot(path=str(OUTPUT/'secrets-and-proxies.png'),full_page=True)
        access_row.get_by_role('button',name='Revoke server access',exact=True).click()
        expect(page.get_by_role('row').filter(has_text=name).filter(has_text='PostgreSQL demo service').first).to_contain_text('revoked')
        page.locator('#nav').get_by_role('link',name='MCP Activity',exact=True).click()
        expect(page.locator('#view')).to_contain_text('legacy__job_artifacts')
        expect(page.locator('#view')).to_contain_text('tools/call')
        page.get_by_role('row').filter(has_text='legacy__job_artifacts').get_by_role('button',name='Timeline',exact=True).first.click()
        expect(page.get_by_role('dialog')).to_contain_text('request.completed')
        page.screenshot(path=str(OUTPUT/'mcp-activity.png'),full_page=True)
        page.get_by_role('button',name='×',exact=True).click()
        for name_ in ['Credentials','Activity','Workspace settings','Dashboard']:
            page.locator('#nav').get_by_role('link',name=name_,exact=True).click()
            expect(page.locator('#view h1')).to_be_visible()
        page.screenshot(path=str(OUTPUT/'overview.png'),full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        page.screenshot(path=str(OUTPUT/'overview-mobile.png'),full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'mobile layout overflows'
        assert not errors,errors
        print('PASS: sign-in, enrol, approve, issue, legacy metadata, OfficeCLI, PostgreSQL and Datum Harness demos, agent governance, and job status/log/event/artifact reads, baseline denial, elevate, scoped write, path denial, publication approval, restrict/restore and revocation.')
        print('PASS: all navigation and Secrets & Proxies screens and mobile layout; no browser JavaScript errors.')
        print('Demonstration agent: '+name)
        browser.close()


if __name__=='__main__': main()
