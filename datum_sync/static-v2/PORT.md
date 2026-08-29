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
