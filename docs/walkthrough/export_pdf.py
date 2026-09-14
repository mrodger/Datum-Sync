"""Render the OfficeCLI DOCX to matching paginated HTML and a five-page PDF."""
from pathlib import Path
import os
import subprocess
from playwright.sync_api import sync_playwright

HERE=Path(__file__).resolve().parent
DOCX=HERE/'output'/'Datum-Sync-Walkthrough.docx'
HTML=HERE/'output'/'Datum-Sync-Walkthrough.html'
PDF=HERE/'output'/'Datum-Sync-Walkthrough.pdf'
env={**os.environ,'OFFICECLI_SKIP_UPDATE':'1'}
subprocess.run(['officecli','validate',str(DOCX)],env=env,check=True)
subprocess.run(['officecli','view',str(DOCX),'html','-o',str(HTML)],env=env,check=True)
with sync_playwright() as pw:
    browser=pw.chromium.launch()
    page=browser.new_page(viewport={'width':1100,'height':1300})
    page.goto(HTML.resolve().as_uri(),wait_until='load')
    page.wait_for_timeout(1000)
    report=page.locator('.page').evaluate_all("""pages=>pages.map((page,i)=>{const body=page.querySelector('.page-body');return {page:i+1,overflowY:body.scrollHeight>body.clientHeight+2,overflowX:body.scrollWidth>body.clientWidth+2}})""")
    if len(report) != 5: raise RuntimeError(f'Expected 5 rendered pages, found {len(report)}')
    bad=[item for item in report if item['overflowY'] or item['overflowX']]
    if bad: raise RuntimeError(f'Rendered page overflow: {bad}')
    page.add_style_tag(content='@media print {.page-wrapper {break-after: page; margin: 0 !important;} .page {break-inside: avoid;}}')
    page.pdf(path=str(PDF),format='A4',landscape=True,print_background=True,
             margin={'top':'0','right':'0','bottom':'0','left':'0'},prefer_css_page_size=False)
    browser.close()
print(f'{len(report)} pages: {PDF}')
