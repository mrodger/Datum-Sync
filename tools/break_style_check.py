"""Break each table.js guarantee in turn and require check_style.py to fail.

A check that has never failed proves nothing. This deletes one guarantee at a
time and asserts the checker notices -- exit 1 with a named FAIL line.

It earned its keep immediately: the first run reported MISSED on the row-key
break, and the fault was in check_style.py, not table.js. "Selection survived
the sort" only counted the ticked boxes, so a row keyed on screen position --
where a different job slides under a tick that never moves -- read as a pass.
The checker now asserts WHICH row is selected, by job id.

An anchor matching anything other than exactly once is reported rather than
run: a break that weakens nothing is a pass that means nothing.

    cd ~/projects/datum-sync && python3 tools/break_style_check.py
"""
import pathlib, subprocess, shutil, sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = str(REPO / 'datum_sync/static-v2/table.js')
BAK = '/tmp/table.js.bak'

BREAKS = [
    # Re-key every row by its position on screen at each render. The count of
    # selected rows never changes, so only an assertion naming WHICH row is
    # selected can see it -- a different job slides under the tick.
    ('row key = screen position, not original index',
     "            shown.forEach(function (r) {\n"
     "                var cb = r.querySelector('input[type=checkbox]');",
     "            shown.forEach(function (r, i) { r.dataset.rowKey = String(i); });\n"
     "            shown.forEach(function (r) {\n"
     "                var cb = r.querySelector('input[type=checkbox]');"),

    ('select-all sweeps every row, not the page',
     "Array.prototype.forEach.call(body.rows, function (r) {",
     "all.forEach(function (r) {"),

    ('filter leaves the selection behind',
     "                selected.clear();\n                page = 1;",
     "                page = 1;"),

    ('pager ignores perPage',
     "var shown = rows.slice(from, from + perPage);",
     "var shown = rows.slice(0);"),
]

shutil.copy(SRC, BAK)
good = open(BAK).read()
bad_news = []
try:
    for name, old, new in BREAKS:
        if good.count(old) != 1:
            bad_news.append(f'{name}: anchor matched {good.count(old)}x, not 1 -- '
                            f'this break proves nothing')
            continue
        open(SRC, 'w').write(good.replace(old, new))
        r = subprocess.run(['xvfb-run', '-a', 'python3', 'tools/check_style.py'],
                           cwd=REPO,
                           capture_output=True, text=True)
        fails = [l.strip() for l in r.stdout.splitlines() if l.strip().startswith('FAIL')]
        ok = r.returncode == 1 and fails
        print(f'[{"CAUGHT" if ok else "MISSED"}] {name}')
        for f in fails[:4]:
            print('        ', f)
        if not ok:
            bad_news.append(f'{name}: exit {r.returncode}, no FAIL line')
finally:
    shutil.copy(BAK, SRC)
    print('\nrestored table.js')

if bad_news:
    print('\nNON-DISCRIMINATING:')
    for b in bad_news:
        print(' ', b)
    sys.exit(1)
print('every break was caught')
