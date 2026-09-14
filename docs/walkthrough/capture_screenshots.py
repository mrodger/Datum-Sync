"""Capture annotated screenshots from the running Datum Sync local prototype."""
from pathlib import Path
import re
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[2]
OUT=Path(__file__).resolve().parent/'assets'
PORTAL='http://127.0.0.1:8210'
WORKER='http://127.0.0.1:8220'

STYLE="""
.__walk_note{position:fixed;z-index:2147483647;display:flex;align-items:flex-start;gap:7px;max-width:280px;padding:6px 9px 6px 6px;border-radius:8px;background:rgba(20,29,35,.94);color:white;border:1px solid rgba(255,255,255,.24);box-shadow:0 6px 18px rgba(0,0,0,.28);font:600 12px/1.35 Arial,sans-serif;pointer-events:none}
.__walk_note b{display:grid;place-items:center;flex:0 0 23px;height:23px;border-radius:50%;background:#d5a844;color:#172027;font-size:13px}
.__walk_target{position:fixed;z-index:2147483646;border:3px solid #d5a844;border-radius:7px;box-shadow:0 0 0 3px rgba(213,168,68,.23);pointer-events:none}
"""

def clear(page):
    page.evaluate("document.querySelectorAll('.__walk_note,.__walk_target,#__walk_style').forEach(e=>e.remove())")

def annotate(page, specs):
    clear(page)
    page.evaluate("css=>{const s=document.createElement('style');s.id='__walk_style';s.textContent=css;document.head.append(s)}",STYLE)
    for number,selector,label,position in specs:
        box=page.locator(selector).first.bounding_box()
        if not box: continue
        page.evaluate("""v=>{const t=document.createElement('div');t.className='__walk_target';Object.assign(t.style,{left:(v.x-3)+'px',top:(v.y-3)+'px',width:v.w+'px',height:v.h+'px'});document.body.append(t);const n=document.createElement('div');n.className='__walk_note';n.innerHTML='<b>'+v.number+'</b><span></span>';n.querySelector('span').textContent=v.label;Object.assign(n.style,{left:v.nx+'px',top:v.ny+'px'});document.body.append(n)}""",
            {'x':box['x'],'y':box['y'],'w':box['width'],'h':box['height'],'number':number,'label':label,'nx':position[0],'ny':position[1]})

def shot(page,name,specs):
    annotate(page,specs); page.screenshot(path=str(OUT/name)); clear(page)


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    password=(ROOT/'.runtime/operator.txt').read_text().split('Password: ')[1].strip()
    with sync_playwright() as pw:
        browser=pw.chromium.launch()
        context=browser.new_context(viewport={'width':1600,'height':1000},device_scale_factor=1,bypass_csp=True)
        page=context.new_page(); errors=[]
        page.on('pageerror',lambda error: errors.append(str(error)))
        page.goto(PORTAL); page.locator('#signin-password').fill(password); page.locator('#signin-submit').click()
        expect(page.locator('#view')).to_have_attribute('data-ready','overview')
        shot(page,'01-portal-overview.png',[(1,'#nav','Operator navigation: governance surfaces stay in Datum Sync.',(264,92)),(2,'.tiles','Start with enrolment, access testing, approvals or proxy management.',(360,270)),(3,'.dash-rail','Live authority and gateway state at a glance.',(1250,620))])

        for target,ready,name,specs in [
          ('Principals','principals','02-principals.png',[(1,'.auth-panel','Each persona is a registered principal with an independent bundle.',(350,165)),(2,'.toolbar','Search and filter the single-user agent fleet.',(1120,160))]),
          ('Secrets & Proxies','integrations','03-secrets-proxies.png',[(1,'.auth-panel','MCP servers, tool states and managed upstream identities.',(340,170)),(2,'.page-head button','Register another governed MCP endpoint here.',(1180,105))]),
          ('MCP Activity','mcpactivity','04-mcp-activity.png',[(1,'.auth-panel','Every endpoint flow receives a deterministic trace.',(340,170)),(2,'tbody tr','Expand a flow to inspect authentication, authorization and dispatch.',(900,330))]),
          ('Resources','resources','05-resources.png',[(1,'.auth-panel','Inventory shows ownership, type, version, size and expiry.',(340,170)),(2,'.auth-panel:nth-of-type(2)','Explicit agent-to-agent grants can be revoked by an operator.',(870,520))]),
        ]:
            page.get_by_role('link',name=target,exact=True).click(); expect(page.locator('#view')).to_have_attribute('data-ready',ready)
            shot(page,name,specs)

        page.goto(WORKER+'/connect')
        expect(page.locator('#dialog')).to_be_visible()
        page.locator('#dialog select').select_option('researcher')
        page.get_by_role('button',name='Approve connection',exact=True).click()
        page.wait_for_url(WORKER+'/')
        expect(page.locator('#input')).to_be_enabled(); expect(page.locator('#artifact-count')).to_have_text('1')
        shot(page,'06-worker-connected.png',[(1,'.worker-context','The selected Datum agent defines the worker identity.',(270,110)),(2,'.worker-nav','Session, MCP, proxy and principal cards remain worker-local views.',(270,390)),(3,'#input-area','Chat is ready immediately after the agent connects.',(520,840)),(4,'#artifact-pane','The inspection pane remains beside the conversation.',(1195,210))])

        page.get_by_role('button',name='Artifacts',exact=False).click(); page.locator('.artifact-item').first.click()
        expect(page.locator('#artifact-frame')).to_be_visible(); expect(page.locator('#artifact-frame').content_frame.locator('h1')).to_have_text('Auckland orders brief')
        shot(page,'07-artifact-split.png',[(1,'.artifact-catalogue','Only resources owned by or shared with this agent appear.',(1040,220)),(2,'.artifact-meta','Owner, MIME type, logical path and access mode stay visible.',(1040,575)),(3,'.artifact-frame','HTML renders in a network-isolated sandbox.',(1060,820))])
        page.locator('#btn-pane-focus').click(); expect(page.locator('#artifact-pane')).to_have_class(re.compile(r'.*focused.*'))
        shot(page,'08-artifact-focused.png',[(1,'.artifact-catalogue','Browse governed artifacts while keeping the active preview.',(80,220)),(2,'.artifact-preview','The same pane object expands for detailed work.',(760,130)),(3,'#btn-pane-focus','Restore the split layout with this control or Escape.',(1210,74))])
        if errors: raise RuntimeError(errors)
        browser.close()
        print('Captured 8 annotated screenshots in',OUT)

if __name__=='__main__': main()
