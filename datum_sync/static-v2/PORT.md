# Porting app.js onto v2

v1's `datum_sync/static/app.js` is 1987 lines and works. v2 has the stylesheet,
the mockups and two behaviour modules, and no `app.js` at all. This is the plan
for closing that, in chunks that can each be finished, verified and committed
on their own.

Read `STYLE.md` first. This file only covers the port.

## What was measured before planning

Three facts, each checked rather than assumed, and each one shrinks the job:

**The two shells have an identical id set.** `static/index.html` and
`static-v2/index.html` both expose exactly `app, view, nav, nav-toggle, engines,
account, avatar-btn, tb-dropdown, signin, signin-form, signin-name,
signin-password, signin-submit, signin-error, signout` — fifteen ids, no
difference either way. Everything in v1 that talks to the shell by id (sign-in,
the avatar dropdown, the engine counter, `buildNav`, `route`) therefore ports
verbatim. This was the main risk and it is not there.

**Sixty of the sixty-four classes `app.js` emits are already defined in v2's
stylesheet.** The four that are not:

| class | status |
|---|---|
| `listbar` | renamed `action-bar` in v2 |
| `repo-desc` | renamed `desc` in v2 |
| `dash-main` | not in v1's stylesheet either — an unstyled wrapper |
| `dash-rail` | same |

So the port is not a rewrite of the markup. It is two renames, two no-ops, and
twenty *new* v2 classes to start emitting: `action-bar, brand-mark, center,
desc, empty-row, empty-state, es-icon, filter-btn, filters, icon, page-desc,
page-header, pager-per-page, pager-right, queued, ring-chart, sel-count,
sort-arrow, sortable, sorted`.

**`static-v2` is not served.** `ui.py` mounts `/ui/static` only. And
`static-v2/index.html` loads `/v2/app.js` — a path nothing answers — while
carrying no script tag for `nav-collapse.js` or `table.js`; only the mock pages
load those, relatively. Chunk 0 exists because of this.

## The chunks

Sized off the section banners in v1's `app.js`. Each is a commit.

| # | Chunk | v1 lines | ~size | Depends on |
|---|---|---|---|---|
| 0 | Mount `static-v2`, fix script tags | — | small | — |
| 1 | Shell: utils, `api`, auth, nav, routing, helpers | 1–566 | 566 | 0 |
| 2 | Dashboard | 567–666 | 100 | 1 |
| 3 | Repositories + repository detail | 667–733 | 67 | 1 |
| 4 | Workspace form + run submit | 734–887 | 154 | 3 |
| 5 | Jobs list + job detail (SSE) | 888–1082 | 195 | 1 |
| 6 | Schedules list + new/edit | 1083–1424 | 342 | 1 |
| 7 | Automations list + detail | 1425–1589 | 165 | 1 |
| 8 | Connections list + detail + form | 1590–1900 | 311 | 1 |
| 9 | Services | 1901–1942 | 42 | 1 |
| 10 | Admin | 1943–1987 | 45 | 1 |

Chunk 1 is the only one that blocks anything. After it, 2–10 are independent
and can be done in any order, or dropped. Do them in mockup order — 2, 3, 5, 6,
7 all have a mockup to check against; 4, 8, 9, 10 do not, and 8 is the largest
screen in the app.

### Chunk 0 — make v2 loadable

Mount `static-v2` beside `static` and give it its own route, so v1 stays up and
the two can be compared side by side. **This is the only backend change in the
whole plan** — everything since `f6e46a9` has been static-only, and
`git diff --name-only HEAD -- 'datum_sync/*.py'` returning nothing has been the
check. It stops being available after this chunk, so make the diff small enough
to read in one screen.

Then fix `index.html`: `/v2/app.js` has to match wherever the mount lands, and
`nav-collapse.js` and `table.js` need script tags. The mock pages load them
relatively and the real shell loads neither, so **the shell today has none of
the behaviour the mockups demonstrate**.

Done, and one thing it turned up. `nav-collapse.js` works from a script tag —
it binds `#app` and `#nav-toggle`, both of which are in the static markup.
`table.js` does not: it is an IIFE exporting nothing, entered only by
`document.querySelectorAll('.view table')` at load, and in the real shell the
view is empty until `app.js` renders. Verified in a browser against the running
server — `typeof window.initTable` is `undefined`. **So its script tag is inert
until chunk 1 gives it an export and calls it per render**, which is why
`listbar` is where it gets wired.

### Chunk 1 — the shell

Ports lines 1–566: `el`/`append`/`clear`, `icon` + `GLYPHS`, `ApiError` +
`request` + `api`, sign-in/sign-out, `showApp`, `refreshEngines`, `SECTIONS` +
`buildNav`, `parseHash`/`SCREENS`/`route`/`go`, and the presentation helpers
`when, duration, badge, banner, table, listbar, filterRows, pageTabs, pagerBar`.

Four things in here are load-bearing and must survive the port. Each is
commented in v1 with the failure it prevents:

- **`generation` and the detached view container** (v1 359–370). `route()`
  clears the view synchronously but appends after an `await`, so a slow
  screen's fetch appends into the *next* screen's view. Screens must receive
  their own container, not `#view`.
- **`leaveScreen`** (357, 377, 397). Every screen may return a cleanup. Without
  it, navigating off job detail leaves an `EventSource` open for the life of
  the tab. Note 397: a *stale* screen's cleanup is run immediately rather than
  stored, or the live screen's subscription is the one left running.
- **`view.dataset.ready`** (406), set only past the generation check. This is
  what browser tests wait on. Every flaky wait so far came from inferring
  readiness from content — every screen has an `h1`, and two screens share a
  button label.
- **`HOME`** (318) is written down in three places that must agree, and when
  they disagreed it was `data-ready` that lied.

`listbar()` becomes `action-bar` markup and is where `table.js` gets wired: the
toolbar buttons need `data-needs="one|many"` and the table needs whatever
`table.js` initialises on. Read `table.js` before writing `listbar`, not after.

`table()` must emit the v2 two-line cell (`cell-name` + `desc`) for list tables
and a single-line row for the dashboard — that is the 59-vs-78 pitch split
`check_style.py` locks.

Done, and four things it turned up.

**v2's `app.js` is an ES module and v1's is not.** The icons are the reason:
`icons.js` holds the real vendored Phosphor set as `export const ICONS`, and
importing it is the only way to use it without a second copy that can drift.
That has one consequence worth writing down, because its failure is silent —
the script tag needs `type="module"`, and without it the browser throws on the
import line before executing anything, leaving both panes hidden. That is the
*same blank page* as chunk 0's, where there was no `app.js` at all.

**`icons.js` exports an `icon()` that does `svg.innerHTML = ICONS[name]`, and
`app.js` deliberately does not use it.** It lifts the `d` out of each entry and
builds the node with `createElementNS`. The string is `icons.js`'s own constant
so calling it would not actually be an injection — which is the point. A ban
with one sanctioned exception stops being greppable, and the next call site
borrows the exception rather than the reasoning.

**Three things app.js must NOT do, each invisible rather than broken**, and all
three are now break-tested rather than left as comments:

- Bind `#nav-toggle` (v1 line 217). `nav-collapse.js` has it. Two handlers on
  one click flips the state and flips it back: a nav that does not move, which
  looks exactly like a handler that was never attached.
- Wire the search box, or port `filterRows`. `table.js` owns search and filters
  by rebuilding the tbody; v1 hides rows in place. Both running leaves
  `table.js` re-appending rows v1 hid, so the pager's count disagrees with what
  is on screen.
- Sort, select or page. `table.js` owns the tbody from `mountTable()` onwards.

**The list-chrome helpers are written but unexercised.** `actionBar`, `action`,
`cellName`, `table`, `rowCheck`, `pagerBar`, `mountTable`, `pageTabs`, `crumbs`,
`when`, `duration` and `badge` have no caller until chunk 2, so nothing in the
394-test suite and nothing in the browser run touches them. They are shipped on
the strength of reading, not of evidence. Chunk 2 is the first thing that will
say whether they are right — treat its first render as their test.

### Chunks 2–10 — one screen each

Same shape every time: port the screen function, swap the renamed classes,
emit the new v2 ones, check it against its mockup, extend the smoke.

Two carry live behaviour and are the ones that can leak:

- **Chunk 2 (dashboard)** and **chunk 5 (jobs list)** both
  `setTimeout(route, 4000)` while anything is queued or running, returning a
  `clearTimeout` (v1 662–663, 932–933).
- **Chunk 5 (job detail)** opens an `EventSource` (1033) and returns
  `stream.close` (1080). Its progress bar is hidden until a report arrives —
  progress is announced over SSE and never stored, so it is absent on reload.

The dashboard's ring chart has **no backend**. It is drawn in the mockup as a
stated gap. Chunk 2 either computes it client-side from the counters it already
fetches, or omits it — deciding that is part of the chunk, and quietly shipping
a ring fed by invented numbers is the failure mode to avoid.

### Chunk 2 — the dashboard

Done, and what it turned up.

**The ring needed no third option.** It is computed from `summary.counts` —
`/rest/v1/transformations/jobs/summary` already returns every status, and the
mockup's own `stroke-dasharray` figures turn out to be exactly those counts
scaled to the circumference (2π×70 = 439.82; 385.86/439.82 = 143/163 exactly,
and likewise for the other two arcs). So the mockup was already drawing computed
values and the gap was in the reading, not in the data. It is fed by the *same*
response the counters below it use, which is the part worth keeping: the ring
and the counters cannot disagree, because there is only one number for each. It
returns `null` when nothing has settled — three zero-length arcs render as an
empty grey circle with "0" in it, which reads as a chart that failed to load
rather than as an instance where nothing has finished yet.

**Three places the mockup and the app had to diverge**, each commented at the
call site rather than silently:

- Its first create tile points at `#/run`, which is not a section. An
  unrouteable href lands on "Not found", quietly, one click away — `route()`
  cannot help, because an unknown section is exactly how a mistyped URL arrives.
  The tiles use routes that exist.
- Its right rail has a second reference card promising "Engines, drivers and
  disk" behind `#/resources`. That section is a `stubScreen`. Shipped with one
  card: a card is a claim about content, and the screen behind it would have
  contradicted it.
- Its badges carry a glyph, and there is no mockup badge for `cancelled`. The
  nearest candidates in `icons.js` are `remove` (a trash can) and `warn` (a
  hazard triangle), either of which would say something untrue about a cancelled
  job. `BADGE_GLYPHS` maps only the four states the mockups draw; the rest get
  no glyph. A badge with no icon reads as a badge — a badge with the wrong icon
  reads as a different status.

**Two new guards, both for failures whose whole signature is an absence.**
`icon()` appends a `<path>` only if it found one, so a misspelled name renders
an empty `<svg>`; `route()` renders "Not found" rather than throwing.
`test_every_v2_icon_app_js_asks_for_exists` collects names from four sources
(`icon()` call sites, `BADGE_GLYPHS`, the pager's `arrow()` calls, and the
section ids the nav builds glyphs from) with a floor on each, so a regex that
stops matching fails rather than passes over an empty set. Its gap is worth
knowing: a glyph reaching `icon()` through any *other* table of data is
invisible to it — the create tiles are one, and they pass today only because
every glyph they name is also a section id. Both are in `break_the_guard.py`
(98 cases now, all proven).

**The chunk-1 helpers have their first evidence.** `table`, `when` and `badge`
now have a caller and render correctly. `actionBar`, `action`, `cellName`,
`rowCheck`, `pagerBar`, `mountTable`, `pageTabs`, `crumbs` and `duration` are
still shipped on the strength of reading — chunk 3 is their test.

**A same-hash `goto()` fires no `hashchange`.** So a browser check that
navigates to the hash the page is already on never runs `route()` and asserts
against the *previous* render. The branch test for the ring first ran that way:
it reported the empty-ring result twice and called the second one a failure.
Hop through another screen between stubs.

**Counting requests could not prove the poll cleanup.** The screen you navigate
to fetches nothing, so a timer that outlived its screen fires `route()`,
re-renders *jobs*, and calls summary zero times either way — an assertion that
passes whether or not the bug is present. What a surviving timer does do is
re-enter `route()`, which replaces `#view`'s child; so hold the new screen's
container and ask whether it is still attached. Falsified by deleting the
`clearTimeout` return, which turns it red.

## Verifying a chunk

`tests/browser_smoke.py` already drives v1 through `BASE + "/ui"` and covers
schedules, automations, connections and services. Once chunk 0 gives v2 its own
route, parameterise that base and **the existing smoke becomes the acceptance
test for every chunk** — a ported screen is done when the v1 smoke passes
against v2 unchanged.

`check_style.py` is a different instrument: it measures the *mockups*, which
are static. It does not see `app.js` and will keep passing however badly the
port goes. Do not read a green run as evidence about a chunk.

Two habits that have already paid here, from `STYLE.md`:

- **Open the screenshots.** Both faults on the dashboard and job mockups passed
  every automated check — zero console errors, zero overflow — and were only
  visible in the PNGs.
- **Falsify each new check before trusting it.** `tools/break_style_check.py`
  is the pattern; its first run reported a MISSED break that turned out to be a
  fault in the checker, not the code.

## Not in scope

Six sections have no mockup and no port chunk, because they are `stubScreen` in
v1 too: services detail, admin beyond the single screen, workspaces, projects,
resources, analytics. The nav shows them; they say so on arrival.
