#!/usr/bin/env python3
"""Vendor chosen Phosphor icons into a JS map + build a contact sheet."""
import re, json, pathlib, subprocess, sys

VERSION = '2.1.1'
SRC = pathlib.Path('/tmp/phos/package/assets')
OUT = pathlib.Path(__file__).resolve().parent.parent / 'datum_sync' / 'static-v2'


def ensure_src():
    """A missing source is a SILENT failure, not a loud one: path_of() returns
    None for every icon, the map serialises as nulls, and innerHTML = '' draws
    nothing. Refetch rather than emit an invisible icon set."""
    if SRC.is_dir():
        return
    SRC.parent.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['npm', 'pack', f'@phosphor-icons/core@{VERSION}'],
                   cwd=SRC.parent.parent, check=True,
                   stdout=subprocess.DEVNULL)
    tgz = next(SRC.parent.parent.glob('*.tgz'))
    subprocess.run(['tar', 'xf', tgz.name], cwd=SRC.parent.parent, check=True)
    if not SRC.is_dir():
        sys.exit(f'could not obtain Phosphor {VERSION} assets at {SRC}')

# role -> (primary phosphor name, [alternates])
NAV = [
    ('dashboard',      'Dashboard',               'squares-four',           ['gauge', 'chart-pie-slice']),
    ('repositories',   'Repositories',            'folders',                ['folder', 'archive']),
    ('automations',    'Automations',             'flow-arrow',             ['lightning', 'tree-structure']),
    ('notifications',  'Notifications',           'bell',                   ['bell-ringing', 'megaphone']),
    ('streams',        'Streams',                 'waveform',               ['broadcast', 'wave-sine']),
    ('data-virt',      'API endpoints',           'database',               ['stack', 'hard-drives']),
    ('mcp',            'MCP Servers',             'plugs-connected',        ['plug', 'circuitry']),
    ('apps',           'Apps',                    'app-window',             ['squares-four', 'browsers']),
    ('schedules',      'Schedules',               'calendar-dots',          ['calendar-blank', 'clock-clockwise']),
    ('jobs',           'Jobs',                    'list-checks',            ['queue', 'clipboard-text']),
    ('workspaces',     'Workspaces',              'cube',                   ['stack', 'shapes']),
    ('projects',       'Projects',                'folder-open',            ['kanban', 'briefcase']),
    ('connections',    'Connections',             'plugs',                  ['link-simple', 'share-network']),
    ('resources',      'Resources',               'file-text',              ['hard-drives', 'files']),
    ('analytics',      'Analytics',               'chart-bar',              ['chart-line', 'presentation-chart']),
    ('services',       'Services',                'globe',                  ['globe-hemisphere-west', 'cloud']),
    ('admin',          'Admin',                   'shield-check',           ['gear', 'user-gear']),
    ('auth-services',  'Authentication Services', 'key',                    ['fingerprint', 'lock-key']),
    ('system-config',  'System Configuration',    'gear-six',               ['sliders', 'wrench']),
    ('queue-control',  'Queue Control',           'list-numbers',           ['queue', 'traffic-signal']),
    # floppy-disk, not arrows-clockwise: the chrome "refresh" slot already owns
    # that glyph, and the two are visible together on list screens.
    ('migration',      'Backup & Restore',        'floppy-disk',            ['archive', 'arrows-clockwise']),
]

CHROME = [
    ('search',      'Search field',        'magnifying-glass',    []),
    ('columns',     'Column chooser',      'columns',             ['table', 'sliders-horizontal']),
    ('nav-toggle',  'Sidebar toggle',      'list',                ['sidebar-simple']),
    ('avatar',      'Account menu',        'user-circle',         ['user']),
    ('engine',      'Engine indicator',    'cpu',                 ['hard-drives']),
    ('create',      'Create',              'plus',                ['plus-circle']),
    ('upload',      'Upload',              'upload-simple',       ['upload']),
    ('download',    'Download',            'download-simple',     ['download']),
    ('edit',        'Edit',                'pencil-simple',       ['pencil']),
    ('remove',      'Remove',              'trash',               ['trash-simple']),
    ('duplicate',   'Duplicate',           'copy',                ['copy-simple']),
    ('run',         'Run',                 'play',                ['play-circle']),
    ('view',        'View / preview',      'eye',                 []),
    ('link',        'Link',                'link-simple',         ['link']),
    ('more',        'Row overflow menu',   'dots-three-vertical', ['dots-three']),
    ('refresh',     'Refresh',             'arrows-clockwise',    ['arrow-clockwise']),
    ('filter',      'Filter',              'funnel',              ['funnel-simple']),
    ('export',      'Export',              'export',              ['share']),
    ('sort-asc',    'Sort ascending',      'caret-up',            ['arrow-up']),
    ('sort-desc',   'Sort descending',     'caret-down',          ['arrow-down']),
    ('page-first',  'Pager: first',        'caret-double-left',   []),
    ('page-prev',   'Pager: previous',     'caret-left',          []),
    ('page-next',   'Pager: next',         'caret-right',         []),
    ('page-last',   'Pager: last',         'caret-double-right',  []),
    ('dropdown',    'Dropdown caret',      'caret-down',          []),
    ('ok',          'Success',             'check-circle',        ['check']),
    ('fail',        'Failure',             'x-circle',            ['warning-circle']),
    ('warn',        'Warning',             'warning',             ['warning-circle']),
    ('info',        'Info',                'info',                []),
    ('pending',     'Queued / waiting',    'clock',               ['hourglass']),
]

WEIGHTS = ['thin', 'light', 'regular', 'bold']


def path_of(name, weight):
    """Return the inner markup of a Phosphor svg, or None if absent."""
    # Only the "regular" weight is bare; every other weight suffixes the file.
    stem = name if weight == 'regular' else f'{name}-{weight}'
    f = SRC / weight / f'{stem}.svg'
    if not f.exists():
        return None
    s = f.read_text()
    inner = re.sub(r'^<svg[^>]*>|</svg>$', '', s.strip())
    return inner.strip()


def main():
    ensure_src()
    missing = []
    for _, _, prim, alts in NAV + CHROME:
        for n in [prim] + alts:
            for w in WEIGHTS:
                if path_of(n, w) is None:
                    missing.append(f'{w}/{n}')
    if missing:
        print('MISSING:', sorted(set(missing)))

    # ---- vendored map (regular weight) -----------------------------------
    icons = {}
    for role, _, prim, _ in NAV + CHROME:
        icons[role] = path_of(prim, 'regular')
    OUT.joinpath('icons.js').write_text(
        '/* Phosphor Icons (MIT) — @phosphor-icons/core 2.1.1, "regular" weight.\n'
        ' * Vendored, not CDN-loaded: the UI must render on an air-gapped host.\n'
        ' * Every path is a FILLED outline on a 0 0 256 256 viewBox. It is NOT\n'
        ' * stroke geometry — rendering these through the old stroke-based icon()\n'
        ' * (fill:none, stroke-width:2, viewBox 24) draws a clipped fragment, not\n'
        ' * a smaller icon. Generated by tools/gen_icons.py; do not hand-edit. */\n'
        'export const ICONS = ' + json.dumps(icons, indent=4, sort_keys=True) + ';\n\n'
        'export function icon(name, size = 17) {\n'
        '    const svg = document.createElementNS(\n'
        '        \'http://www.w3.org/2000/svg\', \'svg\');\n'
        '    svg.setAttribute(\'width\', size);\n'
        '    svg.setAttribute(\'height\', size);\n'
        '    svg.setAttribute(\'viewBox\', \'0 0 256 256\');\n'
        '    svg.setAttribute(\'fill\', \'currentColor\');\n'
        '    svg.setAttribute(\'aria-hidden\', \'true\');\n'
        '    svg.innerHTML = ICONS[name] || \'\';\n'
        '    return svg;\n'
        '}\n')

    # ---- contact sheet ---------------------------------------------------
    def svg(name, weight, size):
        inner = path_of(name, weight) or ''
        return (f'<svg width="{size}" height="{size}" viewBox="0 0 256 256" '
                f'fill="currentColor" aria-hidden="true">{inner}</svg>')

    rows = []

    def section(title, items):
        rows.append(f'<tr class="sec"><td colspan="7">{title}</td></tr>')
        for role, label, prim, alts in items:
            weights = ''.join(
                f'<span class="w" title="{w}">{svg(prim, w, 20)}</span>'
                for w in WEIGHTS)
            altcells = ''.join(
                f'<span class="alt"><span class="g">{svg(a, "regular", 20)}</span>'
                f'<code>{a}</code></span>' for a in alts) or '<span class="none">—</span>'
            rows.append(
                f'<tr><td class="role"><code>{role}</code></td>'
                f'<td class="lbl">{label}</td>'
                f'<td class="pick"><span class="g">{svg(prim, "regular", 17)}</span>'
                f'<code>{prim}</code></td>'
                f'<td class="weights">{weights}</td>'
                f'<td class="big"><span class="g">{svg(prim, "regular", 32)}</span></td>'
                f'<td class="onnavy"><span class="g">{svg(prim, "regular", 17)}</span></td>'
                f'<td class="alts">{altcells}</td></tr>')

    section('Sidebar sections (21)', NAV)
    section('UI chrome (30)', CHROME)

    html = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Datum-Sync v2 — Phosphor icon map</title>
<link rel="stylesheet" href="style.css">
<style>
body {{ background:#fff; color:#222; font-family:'DM Sans',system-ui,sans-serif;
       margin:0; padding:2rem 2.5rem; }}
h1 {{ color:#1D3A5C; margin:0 0 .25rem; }}
p.sub {{ color:#666; margin:0 0 1.5rem; max-width:60rem; line-height:1.5; }}
table {{ border-collapse:collapse; width:100%; background:#fff; }}
th, td {{ padding:.5rem .75rem; border-bottom:1px solid #e2e2e2;
          text-align:left; vertical-align:middle; }}
th {{ background:#f9f9f9; font-size:.7rem; letter-spacing:.06em;
      text-transform:uppercase; color:#555; position:sticky; top:0; }}
tr.sec td {{ background:#1D3A5C; color:#fff; font-weight:600;
             letter-spacing:.04em; font-size:.8rem; }}
code {{ font-family:'JetBrains Mono',ui-monospace,monospace; font-size:.75rem;
        color:#555; }}
.g {{ display:inline-flex; align-items:center; justify-content:center;
      color:#C89632; vertical-align:middle; }}
.pick .g, .weights .g {{ margin-right:.4rem; }}
.pick code {{ color:#1D3A5C; font-weight:600; }}
.weights .w {{ display:inline-block; margin-right:.55rem; color:#1D3A5C; }}
td.onnavy {{ background:#16232F; }}
td.onnavy .g {{ color:#E8EEF4; }}
td.big .g {{ color:#1D3A5C; }}
.alt {{ display:inline-flex; align-items:center; gap:.3rem; margin-right:.9rem; }}
.alt .g {{ color:#8a8a8a; }}
.none {{ color:#bbb; }}
.lbl {{ font-size:.85rem; }}
</style></head><body>
<h1>Phosphor icon map — Datum-Sync v2</h1>
<p class="sub">Every icon slot in the UI, with a proposed
<a href="https://phosphoricons.com">Phosphor</a> replacement for the hand-drawn
stubs currently in <code>GLYPHS</code>. The <b>weights</b> column shows the same
icon at thin / light / regular / bold so you can pick a family weight — this is
the single biggest visual decision, and it applies to all of them at once.
The dark cell shows the icon on the sidebar background; the amber column shows it
at the accent colour it will actually carry. Alternates are there to swap in;
tell me which slots to change.</p>
<table>
<thead><tr>
<th>Slot</th><th>Where</th><th>Proposed</th>
<th>thin / light / regular / bold</th><th>32px</th><th>on nav</th><th>Alternates</th>
</tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>
</body></html>'''
    OUT.joinpath('mock-icons.html').write_text(html)
    print(f'wrote icons.js ({len(icons)} icons) and mock-icons.html')


if __name__ == '__main__':
    main()
