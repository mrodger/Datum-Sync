"""Render the OfficeCLI DOCX to matching paginated HTML and a fixed-page-count PDF.

The brochure uses the Datum design-system fonts. They are embedded from the copies
the worker vendors (worker/static/branding/fonts), so the PDF renders correctly
without installing fonts on the build host.
"""
from pathlib import Path
import os
import subprocess
from playwright.sync_api import sync_playwright

from build_walkthrough import PAGES_EXPECTED

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
FONTS = ROOT / 'worker' / 'static' / 'branding' / 'fonts'
DOCX = HERE / 'output' / 'Datum-Sync-Walkthrough.docx'
HTML = HERE / 'output' / 'Datum-Sync-Walkthrough.html'
PDF = HERE / 'output' / 'Datum-Sync-Walkthrough.pdf'
env = {**os.environ, 'OFFICECLI_SKIP_UPDATE': '1'}


def font_css() -> str:
    faces = []
    for family, prefix, weights in (('DM Sans', 'dmsans', (400, 500, 600, 700)),
                                    ('Space Grotesk', 'spacegrotesk', (500, 600, 700)),
                                    ('JetBrains Mono', 'jetbrainsmono', (400, 500))):
        file = next(FONTS.glob(prefix + '*.woff2'))
        for weight in weights:
            faces.append(f"@font-face{{font-family:'{family}';font-weight:{weight};font-display:block;"
                         f"src:url('{file.resolve().as_uri()}') format('woff2')}}")
    return '\n'.join(faces)


subprocess.run(['officecli', 'validate', str(DOCX)], env=env, check=True)
subprocess.run(['officecli', 'view', str(DOCX), 'html', '-o', str(HTML)], env=env, check=True)
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={'width': 1100, 'height': 1300})
    page.goto(HTML.resolve().as_uri(), wait_until='load')
    page.add_style_tag(content=font_css())
    page.evaluate('document.fonts.ready')
    page.wait_for_timeout(1000)
    report = page.locator('.page').evaluate_all("""pages=>pages.map((page,i)=>{const body=page.querySelector('.page-body');return {page:i+1,overflowY:body.scrollHeight>body.clientHeight+2,overflowX:body.scrollWidth>body.clientWidth+2}})""")
    if len(report) != PAGES_EXPECTED:
        raise RuntimeError(f'Expected {PAGES_EXPECTED} rendered pages, found {len(report)}')
    bad = [item for item in report if item['overflowY'] or item['overflowX']]
    if bad:
        raise RuntimeError(f'Rendered page overflow: {bad}')
    page.add_style_tag(content='@media print {.page-wrapper {break-after: page; margin: 0 !important;} .page {break-inside: avoid;}}')
    page.pdf(path=str(PDF), format='A4', landscape=True, print_background=True,
             margin={'top': '0', 'right': '0', 'bottom': '0', 'left': '0'}, prefer_css_page_size=False)
    browser.close()
print(f'{len(report)} pages: {PDF}')
