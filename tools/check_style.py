"""Assert the locked style values against the rendered mockups.

Every number here is documented in datum_sync/static-v2/STYLE.md. If a value
changes on purpose, change it in three places: style.css, STYLE.md, and here.

Run against the mockup server (python3 -m http.server 8250 in static-v2/):

    xvfb-run -a python3 tools/check_style.py

MUST run headed under xvfb. Headless Chromium paints no scrollbars at all, so
the gutter check reads 0 whether or not one is hidden -- which is why the
control assertion below requires a real 15px scrollbar. That control is what
makes the sidebar's 0 meaningful; without it the check proves nothing.
"""
import asyncio, sys
from playwright.async_api import async_playwright

PAGES = ['mock-repositories','mock-jobs','mock-connections','mock-run-workspace']
FAIL = []

def chk(label, got, want):
    ok = got == want
    if not ok: FAIL.append(f'{label}: got {got!r} want {want!r}')
    print(f"  {'ok ' if ok else 'FAIL'} {label:26} {got}")

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=False)
        pg = await b.new_page(viewport={'width':1920,'height':1080})
        errs = []
        pg.on('pageerror', lambda e: errs.append(str(e)))
        pg.on('console', lambda m: errs.append(m.text) if m.type=='error' and 'Failed to load resource' not in m.text else None)
        pg.on('response', lambda r: errs.append(f'{r.status} {r.url}') if r.status>=400 and 'favicon' not in r.url else None)

        for n in PAGES:
            r = await pg.goto(f'http://127.0.0.1:8250/{n}.html')
            m = await pg.evaluate("""() => {
              const cs = e => getComputedStyle(e);
              const de = document.documentElement;
              const s  = document.querySelector('.sidebar');
              const a  = [...s.querySelectorAll('a')];
              const c  = a.map(x=>x.getBoundingClientRect().top + x.getBoundingClientRect().height/2);
              const rows = [...document.querySelectorAll('.view tbody tr')];
              const h1 = document.querySelector('.view h1');
              return {
                status: 1,
                root: cs(de).fontSize,
                h1: h1 ? cs(h1).fontSize : null,
                nav: Math.round(s.getBoundingClientRect().width),
                topbar: Math.round(document.querySelector('.topbar').getBoundingClientRect().height),
                pitch: Math.round((c[1]-c[0])*10)/10,
                navGutter: s.offsetWidth - s.clientWidth,
                navOverflow: s.scrollHeight - s.clientHeight,
                pitchRows: rows.length>1
                  ? Math.round(rows[1].getBoundingClientRect().top-rows[0].getBoundingClientRect().top) : null,
                docOver: de.scrollWidth - de.clientWidth,
                mark: (()=>{const g=document.querySelector('.brand-mark');
                        const b=g.getBoundingClientRect(); return [Math.round(b.width),Math.round(b.height)];})(),
              };
            }""")
            print(f'{n}  (HTTP {r.status})')
            chk('root font-size', m['root'], '16px')
            chk('h1', m['h1'], '40px')
            chk('nav width', m['nav'], 250)
            chk('topbar height', m['topbar'], 56)
            chk('nav pitch', m['pitch'], 44)
            chk('nav scrollbar gutter', m['navGutter'], 0)
            chk('nav overflow @1080', m['navOverflow'], 0)
            chk('horizontal overflow', m['docOver'], 0)
            if m['pitchRows'] is not None:
                chk('table row pitch', m['pitchRows'], 78)
        print('\nconsole/page errors:', errs or 'none')
        if errs: FAIL.append('console errors')

        # scrollbar hidden, proven against a control that shows one
        await pg.set_viewport_size({'width':1400,'height':900})
        await pg.goto('http://127.0.0.1:8250/mock-repositories.html')
        sb = await pg.evaluate("""() => {
          const mk = extra => { const c=document.createElement('div');
            c.style.cssText='position:absolute;left:-9999px;width:250px;height:100px;overflow-y:auto;'+extra;
            c.innerHTML='<div style="height:900px"></div>'; document.body.appendChild(c);
            const g=c.offsetWidth-c.clientWidth; c.remove(); return g; };
          const s=document.querySelector('.sidebar');
          return {control: mk(''), sidebar: s.offsetWidth-s.clientWidth,
                  overflow: s.scrollHeight-s.clientHeight};
        }""")
        print('\nscrollbar @900:')
        chk('control shows a scrollbar', sb['control'], 15)   # proves the check discriminates
        chk('sidebar gutter', sb['sidebar'], 0)
        chk('sidebar really overflows', sb['overflow'] > 0, True)

        # Collapsed rail.
        await pg.set_viewport_size({'width': 1400, 'height': 1080})
        await pg.goto('http://127.0.0.1:8250/mock-jobs.html')
        await pg.click('#nav-toggle')
        await pg.hover('.sidebar a:nth-of-type(2)')
        cl = await pg.evaluate("""() => {
          const s = document.querySelector('.sidebar');
          const a = document.querySelectorAll('.sidebar a')[1];
          const t = a.querySelector('.nav-label');
          const g = document.querySelector('.nav-group-label');
          const svg = a.querySelector('svg');
          const R = e => e.getBoundingClientRect();
          return {
            railWidth: Math.round(R(s).width),
            tipShown:  getComputedStyle(t).display,
            tipX:      Math.round(R(t).x),
            tipCentred: Math.round((R(t).y + R(t).height/2) - (R(a).y + R(a).height/2)),
            iconOffCentre: Math.abs(Math.round(R(svg).x - R(a).x)
                                  - Math.round(R(a).right - R(svg).right)),
            dividerBorder: getComputedStyle(g).borderTopWidth,
            stored: localStorage.getItem('datum-sync.nav-collapsed'),
          };
        }""")
        print('\ncollapsed rail:')
        chk('rail width', cl['railWidth'], 56)
        chk('tooltip shown on hover', cl['tipShown'], 'block')
        chk('tooltip escapes the rail', cl['tipX'] > 56, True)
        chk('tooltip centred on item', cl['tipCentred'], 0)
        chk('icon centred in rail', cl['iconOffCentre'], 0)
        chk('ADMIN divider kept', cl['dividerBorder'], '1px')
        chk('collapse persisted', cl['stored'], '1')

        # The tooltip must be position:fixed, and none of the above proves it.
        # .sidebar clips horizontally (overflow-y:auto forces overflow-x:auto),
        # but an absolute flyout escapes anyway while .sidebar a is static --
        # its containing block is then the initial one, not the rail. So the
        # regression only bites once someone positions the nav item, and both
        # x-offset and display read identically either way. This stresses it:
        # position the item, make the flyout hit-testable, and require the
        # flyout's own centre point to hit the flyout. Verified to fail (and
        # exit 1) with the rule regressed to position:absolute.
        await pg.evaluate("""() => {
          const st = document.createElement('style');
          st.textContent = `.app.collapsed .sidebar a { position: relative; }
            .app.collapsed .sidebar a:hover .nav-label { pointer-events: auto; }`;
          document.head.appendChild(st);
        }""")
        await pg.hover('.sidebar a:nth-of-type(3)')
        await pg.hover('.sidebar a:nth-of-type(2)')
        clip = await pg.evaluate("""() => {
          const t = document.querySelectorAll('.sidebar a')[1].querySelector('.nav-label');
          const r = t.getBoundingClientRect();
          const hit = document.elementFromPoint(r.x + r.width/2, r.y + r.height/2);
          return hit === t ? 'flyout' : (hit ? (hit.className || hit.tagName) : 'nothing');
        }""")
        chk('flyout unclipped by the rail', clip, 'flyout')
        await b.close()

asyncio.run(main())
print('\n' + ('ALL CHECKS PASSED' if not FAIL else 'FAILURES:\n  ' + '\n  '.join(FAIL)))
sys.exit(1 if FAIL else 0)
