"""Verify that the deployed auth demo uses the supplied v2 assets and geometry."""
from pathlib import Path
import argparse
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT/'datum_sync/static-v2'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--break-geometry', action='store_true', help='Intentionally inject a wrong sidebar width; the check must fail.')
    args = parser.parse_args()
    password = (ROOT/'.runtime/operator.txt').read_text().split('Password: ')[1].strip()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={'width': 1920, 'height': 1080})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://127.0.0.1:8210')
        for asset in ('style.css', 'nav-collapse.js', 'icons.js'):
            response = page.request.get('http://127.0.0.1:8210/v2/'+asset)
            assert response.ok and response.body() == (SOURCE/asset).read_bytes(), asset+' differs from source'
        assert page.locator('link[href="/v2/style.css"]').count() == 1
        assert page.locator('link[href="/assets/style.css"]').count() == 0
        page.locator('#signin-password').fill(password)
        page.locator('#signin-submit').click()
        expect(page.locator('#view')).to_have_attribute('data-ready', 'overview')
        def measure(selector, key):
            return page.locator(selector).first.bounding_box()[key]
        def close(label, actual, expected):
            assert abs(actual-expected) <= .5, f'{label}: {actual} != {expected}'
        if args.break_geometry:
            page.locator('#app').evaluate("e => e.style.gridTemplateColumns = '270px 1fr'")
        close('sidebar width', measure('.sidebar', 'width'), 250)
        close('topbar height', measure('.topbar', 'height'), 56)
        close('nav item height', measure('.sidebar a', 'height'), 36)
        close('nav item width', measure('.sidebar a', 'width'), 234)
        items = page.locator('.sidebar a')
        close('nav pitch within group', items.nth(2).bounding_box()['y']-items.nth(1).bounding_box()['y'], 44)
        close('heading left gutter', measure('#view h1', 'x')-measure('#view', 'x'), 32)
        assert page.locator('#view h1').evaluate('e=>getComputedStyle(e).fontSize') == '40px'
        assert page.locator('.brand-mark').get_attribute('viewBox') == '64 -70 877.9 194'
        page.screenshot(path=str(ROOT/'docs/review/screenshots/source-ui-desktop.png'), full_page=True)
        page.locator('#nav-toggle').click()
        close('collapsed rail', measure('.sidebar', 'width'), 56)
        page.reload()
        expect(page.locator('#view')).to_have_attribute('data-ready', 'overview')
        expect(page.locator('#nav-toggle')).to_have_attribute('aria-expanded', 'false')
        close('persisted collapsed rail', measure('.sidebar', 'width'), 56)
        page.locator('#nav-toggle').click()
        page.set_viewport_size({'width': 390, 'height': 844})
        expect(page.locator('#nav-toggle')).to_have_attribute('aria-expanded', 'false')
        for label in ('Dashboard', 'Principals', 'Approvals', 'Access lab', 'Secrets & Proxies', 'MCP Activity', 'Credentials', 'Activity', 'Workspace settings'):
            page.locator('#nav').get_by_role('link', name=label, exact=True).click()
            expect(page.locator('#view h1')).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), label+' overflows viewport'
        page.locator('#avatar-btn').click()
        page.get_by_role('button', name='Sign out', exact=True).click()
        expect(page.locator('#signin')).to_be_visible()
        assert not errors, errors
        print('PASS: deployed CSS, icons and navigation exactly match the source v2 assets.')
        print('PASS: source desktop geometry, Datum mark, persisted collapsed rail, mobile screens and account-menu sign-out.')
        browser.close()


if __name__ == '__main__':
    main()
