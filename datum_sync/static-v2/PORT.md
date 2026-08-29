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
and can be done in any order, or dropped. Do them in mockup order — 2, 3, 4, 5,
6, 7, 8 all have a mockup to check against (`mock-run-workspace.html` and
`mock-connections.html` are the ones easy to miss: this line used to claim 4 and
8 had none, and was wrong on both); 9 and 10 do not, and 8 is the largest screen
in the app.

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

### Chunk 3 — repositories

Done. Three columns of the mockup's four are gone, and each is gone because the
endpoint cannot fill it — not as a simplification.

**OWNER does not exist.** The repositories table has `name` and `path`; the
handler adds a workspace count and nothing else. v1 renders the literal string
`'admin'` in every row. A column that is the same invented word all the way down
is not a fact about a repository, and porting it faithfully would have carried
the invention into v2 with a fresh coat of paint on it.

**WORKSPACES is real, but it is the name cell's second line rather than its own
column.** The count is the only other field in the response, and the v2 list row
wants a description under the name; with a column as well it would be the same
number printed twice on one row. `path` is withheld on purpose — it is an
absolute server path and this is the screen every reader of every repository
sees.

**Only the screenshot found the fourth.** The mockup's trailing `open` link sits
at the right edge because Owner and Workspaces fill the space between it and the
name. Take those away and the browser splits the row down the middle and strands
a duplicate of the name's own href in the gap. No assertion caught this; the
PNG did, on the first render, which is the STYLE.md habit paying for the second
time.

**All four toolbar buttons are `off`, and `off` is not `needs` with nothing
selected.** Publishing is a CLI operation — there is no POST, PUT or DELETE
anywhere under `/rest/v1/repositories`. The reason the distinction had to become
a new `action()` option is `table.js` 164–167: on every selection change it
assigns `btn.disabled = need === 'one' ? n !== 1 : n < 1` across the whole action
bar, without consulting what the button was built with. So a button built
disabled but carrying `data-needs` is enabled by the first tick, and a
handler-less one then reads as a broken feature rather than an absent one — and
reads that way only to somebody who ticks a row first. The new guard,
`test_no_v2_toolbar_button_is_woken_up_with_nothing_behind_it`, checks the
pairing both ways: `needs` requires a handler, `off` forbids `needs` and forbids
a handler. The browser check deliberately ticks a row before re-reading
`disabled`, because the assertion passes trivially if you never do.

**Select-all is `table.js`'s.** v1 syncs the header checkbox to the rows with its
own listener; from `mountTable()` onwards that belongs to `table.js`, and both
running would toggle every row twice and land back where it started — a
select-all that visibly does nothing.

**The empty state is `.empty-state`, not v1's `.empty`,** which `style.css` marks
as legacy and reserves for inline empties; this is the whole screen. It uses
`h3` rather than the `h2` in `mock-connections.html`, because the sheet sizes
only `.empty-state h3` (1rem) — that mockup's heading falls through to the
generic 1.375rem rule. The stylesheet is the locked artifact, so it wins. The
copy says where publishing actually happens, which is the point of reaching this
branch at all: no repository is publishable from the UI, so a fresh install is
the only thing that lands here.

**`cellName()`'s glyph was exactly the gap chunk 2 wrote down.** A name handed to
`icon()` as somebody else's argument names no `icon()` call site, so every list
row's folder was invisible to the four sources the icon guard read. It now reads
a fifth. Still not total — the dashboard's create tiles pass a glyph through a
table of literals and pass only because each name they use happens to be a
section id too. Three cases added to `break_the_guard.py` (101 now, all proven).

**More of the chunk-1 helpers have evidence.** `actionBar`, `action`, `cellName`,
`rowCheck`, `pagerBar`, `mountTable` and `crumbs` now have a caller and render
correctly. `pageTabs` and `duration` are still shipped on the strength of
reading — chunk 5 is their test.

**The `[data-ready]` selector needs its full route value.** Waiting on a bare
`#view [data-ready]` after navigating matches the screen already on display, so
`wait_for()` returns instantly and the reads straddle the navigation. The first
run of the chunk-3 check printed the `h1` of the old screen next to the `h2` of
the new one. Same family as the same-hash `goto()` trap above: both are checks
that quietly assert against the previous render.

### Chunk 4 — the run form

Done. **It has a mockup** — `mock-run-workspace.html`. The chunk table above said
it did not, and said the same of chunk 8, and was wrong about both; that line is
now corrected. Working from "no mockup" would have meant inventing a layout with
the designer's answer sitting in the directory.

**All three of the mockup's selects are gone,** and, like chunk 3's columns, each
for its own reason rather than one blanket judgement.

- *Repository* and *Workspace* are in the URL. The screen only exists at
  `#/repositories/{repo}/{workspace}`, so by the time it renders both are
  chosen. A select offering to change them is either inert or a navigation
  control dressed as a form control, and the crumbs directly above already do
  that job honestly.
- *Service* has nothing behind it. `POST /transformations/submit/{repo}/{ws}`
  takes `params` and an idempotency key — **there is no service argument.**
  `services` on the manifest is the list of interfaces the workspace enables,
  not a per-run choice. Rendering it as a select would let somebody pick one and
  watch it be silently dropped.

So the card keeps its heading and states those facts read-only in a `.kv`,
alongside the rest of the manifest.

**The header's "Workspace Actions ⌄" button was dropped outright, not made
`off`.** That is a deliberate split from chunk 3, where four dead toolbar buttons
are rendered visibly disabled. A toolbar of four where some will work one day
reads as a toolbar with work outstanding. A lone caret that opens no menu does
not read as unfinished — it reads as a broken menu.

**`group` is a display-only field, and v2 uses it.** `grep` finds it exactly once
in the backend, at `manifest.py:55`; nothing reads it. It exists so a UI can lay
the form out the way the workspace author meant, and the mockup's card idiom is
the shape for it, so there is one card per group. **Partial grouping falls back
to a single card.** A manifest where some parameters name a group and some do not
has not decided, and honouring it would file half the parameters under a heading
its author wrote and the rest under one invented here.

**The stacked cards share borders — and so do the mockup's.** `.card` has no
bottom margin and `.view` has no child-spacing rule, so consecutive cards butt
together. That looks like a porting mistake and is not: rendering
`mock-run-workspace.html` shows its own two cards doing exactly the same. Checked
by opening it over `file://`, because **the mockups 401 over HTTP** — chunk 0's
mount serves the shell's own assets and nothing else.

**The HTML `required` attribute was considered and declined.** It would give
native field-anchored validation for free, but it moves the gate into the browser
and leaves `readParams()`' own required check unexercised from this screen — and
chunk 6's schedule forms do not go through a form submit at all. One gate, in the
place every caller shares. v1's banner behaviour is kept; it names the parameter.

**A browser check that submits a real job leaves the pytest suite skipping 20
tests.** No worker runs on the dev instance, so the job sits `queued`, and
`conftest.py` refuses every db test while a live job it does not own is in
flight — correctly, since `claim()` acts on the whole queue. The suite went from
30 passed to `11 passed, 20 skipped`, and each skip names the queue rather than
anything the checker did. Same family as chunk 7's "reverted the source but not
the rows": the acceptance script now cancels its own job before it exits.

**The harness disarmed a guard by leaving stale bytecode behind, and it took a
security test with it.** `break_the_guard.py` reverts the source it breaks, but
CPython decides a `.pyc` is current from the source's mtime *in whole seconds*
and its byte length. The break and the restore land in the same second, and
`    return name.encode()` and `    return b"datum-sync"` are both 24 characters
— so after proving the AAD case, the `.pyc` compiled from the *broken* crypto
stayed valid indefinitely. Every later run imported an `_aad()` returning a
constant: the connection-name binding simply absent from the running program,
sealed secrets portable between rows again, while `git diff` was clean, the file
on disk read correctly, and **`inspect.getsource` printed the good version**,
because it reads the `.py` while the interpreter runs the `.pyc`. It surfaced as
one unrelated-looking failure in `test_connections.py` during this chunk's full
run. `drop_bytecode()` now clears the cache on both writes.

New guard: `test_every_v2_submit_handler_stops_the_browser_submitting`. Without
`preventDefault()` the browser's own submit runs too — a GET on the current URL,
which in an SPA is a full reload that tears down the fetch the handler just
started. Whether the POST lands is a race, so Run either runs the workspace or
does nothing, and both outcomes look like a page that merely refreshed. Checked
once over every submit listener in the file, because chunks 6 and 8 add more
forms and there is one form-level rule. 102 cases now, all proven.

### Chunk 5 — jobs, and the log stream

Done. Two screens and a live connection: `mock-jobs.html`, `mock-job.html`, and
the SSE stream behind `#/jobs/{id}`.

**`table.js` gained one line of public surface, and this is the chunk that
needed it.** The mockups never ask a table what is selected — they have no
handlers — so `initTable` kept `selected` private and exported nothing.
A bulk Cancel has to know. **Reading the DOM back is not the substitute it
looks like:** `render()` replaces the tbody on every change, so only the
*current page's* rows are in it, while `selected` deliberately survives paging.
Counting checked boxes would give a toolbar reading "3 selected" over a Cancel
that cancels none of them the moment the reader turns the page — the same class
of bug as acting on rows nobody can see, which is the hazard the module's own
decision (2) was written to avoid. `initTable` now returns
`{ selection() }`, keys not rows: a key is the row's original index, so the
caller maps straight back into the array it built the table from. The mockups
ignore the return value, so they still adopt the file unchanged.

**Remove is `off`; Cancel is live.** Not one judgement applied twice — `grep`
over `api.py` finds no route that deletes a job row at all, while
`DELETE /transformations/jobs/id/{id}` is cancel and exists. A job's history is
the point of the screen.

**A mixed selection is sent to the server as-is.** `jobs.cancel()` reads the row
and returns its status untouched when there is nothing to stop, so a terminal
job in the selection is a no-op. Filtering them out in the client would be a
second copy of the "can this still be cancelled?" rule, one status name away
from disagreeing with the one that decides.

**The mockup's fourth tab is "Dashboards", and v1's "All" is kept instead.**
Dashboards is a Flow feature, not a job status: as a filter it would either 400
— it is not in `JOB_STATUSES` — or silently show everything. Same position,
same shape, and it means something here.

**No column starts sorted, and Duration does not sort at all.** The mockup ships
Job descending, which in Flow is newest-first because Flow's job ids are
integers. Ours are UUIDs, so the same sort is alphabetical over random hex —
a deterministic scramble presented as an order. The list arrives
`ORDER BY submitted_at DESC` and is left that way. Duration is text from
`duration()`, so sorting it would put "2m 5s" before "30s".

**The mockup's Engine row is fiction.** There is no engine column on the `jobs`
table and no engine field in `_job_json`. It is replaced with `Triggered by` and
`Resubmitted from`, which are real, are returned, and were shown nowhere in v1 —
each omitted entirely when absent rather than rendered as an em-dash.

**`min-width: 0` on the Parameters value is not a style.css edit by the back
door.** `.kv dd` already declares `overflow-wrap: break-word` — the designer's
answer to a long value is "wrap it" — but the rule cannot fire: `.kv` is a grid,
a grid item's default min-width is `auto` (= min-content), and `overflow-wrap`
does not shrink min-content. So the track widens to the longest value and the
panel overflows instead of the text wrapping. Measured on the live page: that dd
was **497px inside a 369px panel, and 234px with `min-width:0`**. It is set on
that row alone because it is the only value the user did not write; every other
one is a name, a timestamp or a short id, while `params` is a serialised blob of
unbounded length. v1 rendered the same blob and never saw this — v1's kv is
full-width.

**The SSE stream was proven without a worker.** Nothing runs on the dev
instance, so no log line ever arrives and the obvious check is untestable. But
`jobs.cancel()` notifies `status` on the same channel, so cancelling from the
detail page is a real round trip through the `EventSource`: the badge went
QUEUED → CANCELLED, the button swapped Cancel → Resubmit, Finished filled in,
and the acceptance script asserts **zero frame navigations**, which is what
separates the stream doing the work from a page that reloaded.

**The defect this chunk introduced was found by writing the test, not by reading
the code — and my own acceptance script had it too.** v1's jobs list repolled
straight into `route()` every four seconds and lost nothing by it, because v1's
list had no selection. Chunk 5 gave it tick boxes and a bulk Cancel, and the
same timer now rebuilds the screen, the table, and with it a fresh empty
`selected`. A reader has under four seconds to tick their rows and press the
button. **It does not look like a bug:** the ticks vanish at the same instant the
rows redraw, so it reads as the page refreshing, and the toolbar greys out again
as if nothing had been chosen — nobody reports it, they tick the rows again. And
it lands where it hurts most: a list only repolls when something on it is
unfinished, which means the Queued and Running tabs, the two where Cancel is the
reason you opened the page. The poll now defers while a selection is held.
Waiting is the right resolution and not merely the easy one — a reader with a
selection has stopped watching the queue and started acting on it, and the rows
they ticked are by definition rows they have already seen. Nothing is missed,
only deferred, and it resumes by itself.

New guard: `test_no_v2_list_repolls_itself_out_from_under_a_selection`. In a
screen that hands its table to `table.js`, a timer that re-routes must consult
the selection first. **Its first version failed on `screenDashboard`, whose own
comment says its table is "Not handed to `mountTable()`"** — a substring test
read that sentence as the opposite of what it says, so the rule now matches a
call, not a mention. 103 cases now, all proven.

### Chunk 6 — schedules

Three screens: the list, the create form and the edit form. `SCREENS.schedules`
is `[screenSchedules, screenSchedule]`, and `screenSchedule` splits on the id —
`#/schedules/new` reaches `newSchedule`, anything else `editSchedule`. That is a
two-entry table because the router indexes by segment count, not by matching, so
`new` and an id are the same shape of route and have to be told apart in the
screen.

**There is no mockup for the two forms.** `mock-schedules.html` is the list only,
so the forms are chunk 4's run-workspace idiom applied again: `.card` per
section, `.field` per control, `.action-bar > .actions` for the footer. That last
one is the thing this chunk got wrong first, and the new guard exists because of
it — see below.

**The list follows the mockup, with one deliberate exception.** The mockup drops
the timezone from the Trigger column; v2 keeps it. `0 2 * * *` is not a time
until you know the zone it is read in, the zone is a stored and editable field,
and a column whose entire job is to say when this runs should not state two
thirds of the answer. An interval carries no zone, because it is not evaluated in
one. Otherwise the mockup wins: both forms of trigger are set in `<code>` so the
column reads as one kind of thing, Name is the sorted column, and v1's separate
Workspace column, Last run column and trailing "open" link are all gone — the
workspace now rides under the name in `cellName`, the way every other v2 list
writes it.

**Next run is relative, and the absolute time is in the `title`.** "in 6 hours"
is what the mockup asks for, and it is computed exactly once: this list has no
repoll to correct it, so a tab left open overnight would sit there claiming a run
is six hours away that happened at 2am. Hovering gives the timestamp that is
still true. `Intl.RelativeTimeFormat` does the wording, so it is the browser's
locale rather than a table of English plurals maintained here.

**A paused schedule shows no next run at all.** Pausing does not clear
`next_run` server-side, so a paused row still carries whatever time it was paused
at. Rendered, that reads as permanently overdue, which is precisely wrong — it is
not late, it is off.

**Bulk Pause sets `enabled: false`; it does not toggle.** Over a mixed selection
a toggle would resume the paused rows, so a reader who pressed a button labelled
Pause would have started something. Setting the state named on the button makes
the already-paused rows a no-op, which is what pressing Pause is understood to
mean. Resuming is therefore only on the detail screen, where there is exactly one
schedule, its state is known, and the button can say which direction it goes —
which is why that button reads Pause or Resume rather than Toggle.

**Delete is the only `confirm()` in the UI, and deliberately the only one.**
Cancelling a job is reversible by resubmitting it and pausing is reversible by
definition, so neither earns a dialog. A schedule is a row nothing else stores:
delete it and its cron expression, its timezone and the parameter set it has been
running with are gone, with no undo anywhere in the API.

**Repository and workspace are not editable, and the API agrees** — `PATCH
/schedules/{id}` does not accept them. That is not an omission to route around. A
schedule that could be repointed is a permission check made once, on a row that
no longer says what it said. The edit form states this rather than silently
omitting the fields.

**The edit form degrades instead of failing when the workspace is gone.**
Unpublishing does not delete schedules, and a schedule pointing at a workspace
that no longer exists is exactly the one someone comes to look at. A 404 on the
manifest gives a read-only dump of the stored params and a banner, not a dead
screen. In that state `params` is omitted from the PATCH rather than sent empty,
because an empty object would erase what the schedule runs with.

`paramControls()` also holds a FILE parameter's stored upload id. A file input
cannot be given a value, so an untouched FILE field reads as null, and on an edit
form that would quietly drop the upload the schedule has been running with for
weeks — the next run failing on a missing required parameter, hours later, with
nothing pointing back at the edit. `readParams()` was written in chunk 4 to key
its upload branch off `instanceof File` precisely so an id handed back this way
passes through untouched.

One latent bug fixed in passing: `actionBar`'s `subtitle` option emitted v1's
`.subtitle` class. Both classes are styled, so either would have looked
deliberate; the mockups settle it — schedules, automations and connections all
write the sentence under a list title as `.page-desc`, and no mockup uses
`.subtitle` at all. The option had no callers until this chunk, so the wrong
class had never reached a screen. Renamed to `desc`.

New guard: `test_v2_invents_no_css_classes`. Every class name `app.js` writes as
a literal must exist either as a rule in `style.css` or in a mockup's own markup.
**This is the mistake chunk 6 actually made:** the schedule forms' footer was
first written as `.form-actions`, a class that sounds exactly like one this
design system would own and that does not exist. Nothing would have caught it —
the element renders, the page does not break, and the footer is simply unstyled,
which reads as a deliberately plain div. The idiom is `.action-bar > .actions`.
The mockups have to count as a second source of truth, not just the stylesheet:
`.dash-main` and `.dash-rail` are the two grid *children* of `.dash-grid`, placed
entirely by the parent's `grid-template-columns` and carrying no rules of their
own. Requiring a rule per class would fail on those, and the honest reading is
not that they are wrong — it is that the designer wrote them.

**This chunk also silently disarmed a guard that had nothing to do with it.**
`break_the_guard`'s "a v2 submit handler that lets the browser navigate" anchored
on a bare `event.preventDefault();` at eight spaces, which was unique when chunk
4 wrote it. Adding two more forms made it match three times, and the harness
skipped the case — a guard from a finished chunk, quietly no longer proving
anything, with no test failing to say so. The anchor now includes the following
line, which names the submit button and so differs per form. **Run the harness
after ordinary feature work, not only after writing a guard.** 104 cases, all
proven.

### Chunk 7 — automations

`SCREENS.automations` is two entries: the list, and one screen that serves both
`#/automations/new` and `#/automations/<id>` because the router indexes by
segment count and cannot tell them apart.

**The mockup has one column fewer than v1.** v1 gave the trigger its own column;
`mock-automations.html` folds it into the name cell's `.desc` line ("job complete
&middot; SCIMAC/site_plan") and spends the column on Status instead. There is no
mockup for the detail screen, so it follows the schedule editor's shape.

`triggerDesc()` spells out a statusless trigger as "any finished job" rather
than leaving it blank. The fixture automation has `status: null`, and blank is
indistinguishable from a rendering failure.

`ACTION_LABELS` is a second vocabulary — the list writes "webhook" where the
server says `http_request`. It falls back to `replace(/_/g, ' ')` so a type
nobody has labelled still reads as something, and a guard asserts its key set
equals `automations.ACTIONS` in both directions.

**The editor holds the stored YAML verbatim, never a re-serialisation of the
parsed config.** Round-tripping through the parser would rewrite comments, key
order and quoting on a save that changed nothing. The server is the only YAML
parser: a document it refuses comes back as a banner on the form, not a
navigation. PUT takes `{yaml}`; PATCH on the same resource takes only
`{enabled}`.

Chunk 6's `untilNode` is now `relativeNode`. The function already handled both
directions — the difference is signed — but Last fired is a past time, and a
function called `untilNode` returning "2 minutes ago" is a name that lies.

**New guard: `test_v2_never_appends_a_child_that_can_be_nothing`. This is the
mistake chunk 7 actually shipped.** `append(node, children)` is ours and skips
`null`, `undefined` and `false`, which is why `el()` can take a conditional
child. `view.append(...)` is the DOM's `Node.append`, which stringifies whatever
it is handed. A conditional `last_error` banner passed to the wrong one printed
a blue **null** under the automation's title. The full suite passed. The browser
script's twelve assertions about that exact screen passed. `node --check` passes
on the broken form — it is valid JavaScript. **Only reading the screenshot found
it.** The rule the guard enforces: a dotted `.append(` is the DOM's and takes
nodes and strings only; the bare `append(node, [...])` is where a child that can
be nothing belongs. A blanket ban on `view.append(` was rejected — 34 call sites
across finished chunks, plus a FormData `body.append('file', file)` it would
have caught wrongly.

106 cases, all proven.

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
