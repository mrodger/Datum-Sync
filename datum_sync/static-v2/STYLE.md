# Datum-Sync v2 — style guide

Locked 2026-08-28. `style.css` is the source of truth; this file explains
*why* the numbers are what they are. If you change one, change both.

The brief: **Flow's geometry, Datum's identity.** Layout, spacing and
component structure are pixel-matched to FME Flow 2026.2 from the 21-screenshot
set in `reference-images/`. Colour, type, icons and the brand mark are Datum's.

`PORT.md` is the companion: the plan for bringing v1's `app.js` onto this
stylesheet, in chunks.

---

## Two zones

Custom properties are redeclared per zone, so the same token means different
things depending on where it is used.

| | Dark zone | Light zone |
|---|---|---|
| Selector | `.signin`, `.brand`, `.sidebar` | `.topbar`, `.view` |
| `--panel` | `#16232F` | `#ffffff` |
| `--text` | `#E8EEF4` | `#0d0d0c` |
| `--muted` | `#8FA3B5` | `#5e5e5e` |
| `--link` | `--amber` | `#1D3A5C` |

**This is the specificity trap.** `--link` is declared on `:root` *and* on
`.topbar, .view`. Overriding it on `:root` alone loses to the zone rule. Any
override has to restate the light-zone selector.

## Colour

Brand: navy `#1D3A5C`, amber `#C89632`.

**Amber is a dark-zone colour.** Measured contrast:

| Pair | Ratio | Verdict |
|---|---|---|
| amber on `#16232F` | 5.98:1 | passes AA |
| amber on white | **2.67:1** | fails AA body (4.5) *and* large (3.0) |
| navy on white | 11.59:1 | passes AAA |

So links carry their own `--link` token, navy in the light zone. `--accent`
stays amber everywhere because it is used for fills, borders and icon accents
— things you look at, not things you read.

Still outstanding: the amber folder icons in table rows sit at 2.67:1 against
white. Arguably exempt as decoration since the adjacent text carries the
meaning, but it has not been decided.

## Type

Sized to Flow, verified by measuring the rendered pages.

| | value | renders |
|---|---|---|
| root | `16px` | — |
| `h1` | `2.5rem` | 40px |
| `h2` | `1.375rem` | 22px |
| `h3` | `1.125rem` | 18px |
| `td` padding | `1.15rem` | row pitch 78px |
| `th` padding | `.7rem 1.15rem` | — |

78px is a **list-table** row: two lines of content plus that padding. The
dashboard's recent-jobs table is a different object — one line per row, no
description under the name — and comes out at **59px on the same padding**.
`check_style.py` locks the two separately (`PITCH`). Collapsing them to one
number would mean either loosening the list tables or padding the dashboard,
and neither is what the design says.

Fonts: DM Sans (body), Space Grotesk (headings), JetBrains Mono (code).

**Raising the root is not enough on its own.** At 16px with the old
`h1: 1.55rem` the heading reached only 24.8px against Flow's 40, and the row
pitch 65 against 78 — the headings and the table density were the binding
constraints. That is why they carry explicit values.

`mock-variants.html` is a live explorer for these two decisions. Its first
radio in each group is the shipped value; everything below it is a rejected
alternative, kept so the comparison can be re-run.

## Layout — do not convert these to rem

```
--nav-width  250px      --nav-item   36px      --gutter   32px
--topbar      56px      --nav-pitch  44px      --control  35px
                                               --search   38px
```

These are **px on purpose.** Keeping them absolute is what makes the type
scale safe to change: raising the root moves type and rem-based padding while
Flow's pinned geometry stays put. Verified — nav stayed 250 and topbar 56
across all four type presets.

`--nav-width-collapsed: 3.5rem` is the lone exception and *is* root-relative,
so the collapsed rail measured 49px at the old 14px root and 56px now.

`--nav-pitch: 44px` was measured by scanning the icon column of
`reference-images/repositories.png` for glyph runs: consecutive centres come
out 44, 44, 44, 44, 43.5, 44.5. An earlier value of 42 was wrong.

## Navigation

The nav **scrolls but shows no scrollbar**, matching Flow:

```css
overflow-y: auto;
scrollbar-width: none;        /* Firefox */
-ms-overflow-style: none;     /* legacy Edge */
.sidebar::-webkit-scrollbar { width: 0; height: 0; }   /* Chrome, Safari */
```

Hiding it also stops a classic 15px scrollbar gutter eating into the 250px
column, which would otherwise shift the labels on short viewports only.

`.sidebar a:last-of-type { margin-bottom: 0 }` — the pitch gap belongs
*between* items. The trailing 8px under the last item was the difference
between fitting a 1080px viewport and not.

We carry **21 nav items against Flow's 18**, so there is less headroom: at
1080 it fits, below that it scrolls. Flow's own content runs to y≈1052 in its
1080 screenshot, so it would scroll there too.

### Collapsed rail

The hamburger collapses the nav to a **56px icon rail**
(`--nav-width-collapsed`), as Flow's does. `nav-collapse.js` owns the
behaviour for every mockup and for `app.js`, so there is one definition:
it toggles `.collapsed` on `.app`, flips the button's `aria-expanded` and
`aria-label` (it is the only way back out, so a fixed label is wrong in one
of the two states), and persists to `localStorage['datum-sync.nav-collapsed']`
— Flow remembers the state, and a rail that springs back open on every
navigation is a different and worse feature.

Three things the collapse has to do beyond hiding labels:

- **Re-centre the glyph.** `.sidebar a` padding is `0 .7rem`, sized for a
  label beside the icon; left alone it off-centres the icon in the rail.
- **Keep the ADMIN break.** That heading is the only thing dividing the admin
  group from the working group. Hidden outright, Admin ran straight on from
  Services, so collapsed it becomes a 1px rule instead.
- **Bring the label back on hover**, as a flyout — collapsed, it is the only
  thing naming an icon.

The flyout is **`position: fixed`, not absolute**, and the reason is subtle
enough to be worth stating. `.sidebar` is `overflow-y: auto`, which computes
`overflow-x` to `auto` too, so it clips at the rail edge. `absolute` survives
that *today* only because `.sidebar a` is static — the flyout's containing
block is then the initial one, not the rail. Add `position: relative` to the
nav item for a badge or a status dot and the tooltip silently vanishes behind
the rail. `fixed` is immune, and with `top` left `auto` it still resolves to
the item's static position, so nothing places it in JS.

`check_style.py` asserts this by *stressing* it — it injects
`position: relative` on the item and `pointer-events: auto` on the label,
then requires the flyout's own centre point to hit the flyout. Without the
stress the check passed with the rule regressed to `absolute`; with it, the
regression fails and exits 1. The x-offset and `display` assertions read
identically either way and prove nothing here.

## Wording

Labels carry no vendor's name, and stay generic. Repositories, Workspaces,
Jobs, Connections and engines all name things the API really has
(`/rest/v1/repositories/{repo}/workspaces/{ws}`, `/rest/v1/engines`), so they
stay as they are. The one rename is **Flow Apps → Apps**.

De-branding is a label change only — no id, route or endpoint moves with it.
Worth knowing, though: Apps is an unimplemented stub and it overlaps
**Services**, which is the built `/serve/{name}/` feature already hosting a
job's `service/static`, `service/pwa` and `service/dashboard` output as a
site. Both entries are kept on purpose; merging them is a product decision,
not a naming one.

Mockup data has to be real too. The Jobs table used to list `*.fmw` files; a
Datum-Sync workspace is a **directory** holding `main.py` and `manifest.json`,
and one of the four names had never existed in `repositories/` at all.

## Disabled states

**One rule: a disabled control states its own colours. Never fade a copy.**

```css
background: var(--surface);  color: var(--muted);
border-color: var(--border); opacity: 1;
```

Opacity-only disabling has now caused the same defect three times:

1. `button.secondary` bordered with `--border` made an *enabled* secondary
   identical to a disabled one — an available action reading as unavailable.
2. A disabled `<input>` was **pixel-identical** to an enabled one. The base
   rule sets `background` and `color`, which overrides the UA's disabled
   styling entirely.
3. A disabled `<select>` differed only by the UA's `opacity: 0.7`, so the two
   controls did not even match each other.

Flow separates enabled from disabled by **hue**: Upload and Manage Database
Types carry the accent, Edit and Remove go grey.

When touching any control, check the disabled state by measuring computed
`background`/`color`/`border`/`opacity` on both — not by eye.

## Status badges

`.badge` plus one of: `running`, `complete`, `failed`, `queued`, `cancelled`,
`enabled`, `paused`. These are the only status classes that exist — invented
names such as `ok`, `error` or `warn` fall through to the neutral base and
render as `queued`.

Filled badges are the run lifecycle; `enabled` and `paused` are outline badges
for configuration state.

## Icons

Phosphor Icons 2.1.1, **Regular** weight, vendored to `icons.js` — 51 slots,
no network fetch. `icon(name, size = 17)` emits
`viewBox="0 0 256 256" fill="currentColor"`, so icons inherit `color`.

Sizes in use: 12–15 inline with text, 18 in the nav and topbar, 48 for
empty-state art.

Note when adding: only the `regular` weight has bare filenames in the upstream
package. Every other weight suffixes the file (`folder-bold.svg`, not
`bold/folder.svg`).

## Brand mark

`datum-mark.svg`, generated by `tools/gen_brand_mark.py` from the FME roadshow
deck logo. Do not hand-edit it — regenerate.

DATUM with the survey monument as the stylised A. Only the D is new geometry,
built to the source's own construction (stroke 18, butt caps, bowl radius 83 =
half the 166 cap height, matching how the R's bowl and U's base are drawn).
T, U, M and the monument are the deck's own paths unchanged.

The monument is thinned from deck weight — structure ×0.4, theodolite head
×0.75. The split matters: at a uniform ×0.4 the head falls to 0.65 device px
at the 26px render size and turns to mush, while the legs are still a healthy
1.45px. Verified by rendering at true size and upscaling nearest-neighbour,
not by zooming the vector.

The mark is stroke geometry, so it takes `color`, not `fill`, and is inlined
rather than `<img src>`'d so it inherits `currentColor`. Its viewBox is read
from the file by `gen_mockups.py` — never copy it into a second place, since a
stale copy crops or letterboxes silently rather than erroring.

**Open question:** the survey tripod is Stratum's identity device. Using it as
Datum's A may conflate two brands that are meant to be distinct.

## Page archetypes

Derived from the reference set; every screen is one of two.

**List page** — h1, optional tab row, then an action bar of
`search | spacer | primary, secondary…, icon`, then a bordered table card,
then the pager. Repositories and Jobs are the models.

**Form page** — h1, then stacked bordered cards of label/control rows, with
the submit row at the bottom left. Run Workspace is the model.

Two screens are neither. **Dashboard** is a two-column `.dash-grid` of
heterogeneous cards; **Job detail** is a `.split` of a metadata panel beside a
live log. They are one-offs, so their geometry is pinned by the mockups rather
than by an archetype.

### Screen coverage

Eight mockups: dashboard, repositories, jobs, job, schedules, automations,
connections, run-workspace. Still absent: services, admin, workspaces,
projects, resources, analytics.

Every block on these screens maps onto something v1's `app.js` already
fetches — the columns, the trigger strings, the counter grouping and the
paused-schedule em-dash are all read out of `screenSchedules`,
`screenAutomations`, `screenJob` and `COUNTERS`, not copied off Flow. Flow's
schedule prose ("Once a day") and its `.fmw` filenames are deliberately not
reproduced; ours are cron expressions and repository paths because that is
what we store.

Three things to know about them:

- **The ring chart is a stated gap, not a feature.** `style.css` has carried
  `.ring-chart` from the start and Flow draws one, but v1 computes no such
  breakdown. The mockup draws it over the three *settled* states only — a
  queued job has no outcome to colour, and folding it in would make the total
  drift as work starts. The arcs are computed in `gen_mockups.py`, not
  hand-written, so they always close; `check_style.py` asserts closure to
  within half a unit.
- **Automations is populated, where Flow's screenshot is an empty state.**
  Connections already carries the empty-state pattern and a second copy locks
  no new geometry. Automations is the only screen with a `Last error` column,
  which is the only place `--failed` appears as text rather than as a badge —
  an empty table would never show it.
- **The dashboard's card grid is repositories, where Flow's is workspaces.** A
  Datum-Sync workspace is a directory of files, not a file; the repository is
  the unit a user publishes.

Job detail shows a **running** job on purpose. The progress bar exists only
while one is live (`app.js:949` — progress is announced over SSE and never
stored), so a complete job would leave `.progress` unrendered and nothing
would pin its geometry. Cancel-instead-of-Resubmit follows from the same
`showActions()` swap.

`.lvl` holds the **level**, not a timestamp. The log stream sends
`entry.level` and `entry.message` and there is no time field. Timestamps would
look right and lock a column the real screen cannot fill.

## Dynamic behaviour — the gap against Flow

Standing rule: if Flow does something dynamic, we do it too. The mockups draw
every affordance Flow shows, but drawing a sort arrow is not sorting. This is
the honest state of each, so `app.js` has a checklist rather than a vibe.

Confirmed from the reference screenshots (`reference-images/`):

| Behaviour | v1 `app.js` | v2 mockup |
|---|---|---|
| Nav collapse to a 56px icon rail | toggle only | **done** — persisted, tooltips, ADMIN rule |
| Active nav item highlight | yes | yes (CSS) |
| Row hover highlight | yes | yes (CSS) |
| Live search filter | yes, client-side | drawn, inert |
| Avatar menu | yes | drawn, inert |
| Auto-refresh while a job runs | yes, `setTimeout(route, 4000)` | n/a |
| Live log + progress over SSE | yes, `EventSource` | n/a |
| Sortable column headers | none at all | **done** — `table.js` |
| Row selection enables Edit / Remove | select-all box only, wired to nothing | **done** — `table.js` |
| Pagination and page size | cosmetic — every button `disabled`, always "1 to N of N" | **done** — `table.js` |
| Column chooser (toolbar's rightmost icon) | none | drawn, inert |

Three known divergences, all deliberate:

- Flow's collapse control is a **floating circular chevron straddling the
  sidebar/content edge**; ours is a hamburger inside the brand bar. Ours keeps
  the control inside the grid instead of overlapping two areas.
- The column chooser's function is **inferred from its icon**, not observed.
  Do not build it off that guess.
- **Cancel enables for any selection**, including a job that already finished.
  Making it state-aware needs per-row status, which belongs with real data
  rather than mock rows.

### table.js

Sorting, selection and paging are one module because they are one problem.
Each is trivially correct alone and wrong in combination. Four decisions, only
the first of which is forced:

1. **A row's identity is its original index**, baked into `data-row-key` once
   at init. Position cannot be the key — that is the thing sorting changes.
2. **Selection survives sorting and paging, and is cleared by the filter.**
   Sort and page still show the same result set, so a selection off screen is
   only out of view, and the `.sel-count` hint keeps saying how many. A filter
   removes rows from the set, and a selection you can no longer reach by
   scrolling is a Remove aimed at something invisible.
3. **Select-all covers the current page**, and goes indeterminate when the page
   is partly selected. A box that silently picks up 400 off-screen rows is the
   same hazard as (2).
4. **Edit needs exactly one row, Remove needs one or more.** Flow's screenshots
   only show both disabled at zero selection; the rest is our decision, not an
   observation of Flow.

Buttons declare their own requirement in the markup (`data-needs="one|many"`)
rather than being matched on their label, so renaming one cannot silently
unwire it. They render `disabled`, which is both Flow's initial state and the
correct state with nothing selected — the page is right before any script runs
and stays right if `table.js` never loads.

The sort arrow is always the `caret-up` glyph; descending rotates it 180°,
which is exactly `caret-down`. One icon covers both, so `table.js` never has to
build SVG and never needs `icons.js` in the browser.

The mock Jobs table carries **twelve rows against a page size of ten** on
purpose: pagination, and a selection surviving a page change, cannot be shown
on a list that never pages. The pre-JS markup lists all twelve and says so —
writing "1 to 10 of 12" there would be a lie about a file that lists twelve.

Client-side search has a limitation already documented at `listbar()` in v1: it
filters what was fetched. That is the whole list today, but would silently
become "the page on screen" the moment paging goes server-side.

## Verification

Mockups are generated: `python3 tools/gen_mockups.py`. Never hand-edit
`mock-*.html`; every one carries a "do not hand-edit" header.

Two lessons worth keeping:

- **Measure at 1920×1080**, the reference screenshots' own size. A scrollbar
  "defect" reported here was really a 1000px test viewport.
- **Headless Chromium paints no scrollbars at all**, so a gutter measurement
  reads 0 whether or not one is hidden. Run scrollbar checks under
  `xvfb-run` with `headless=False`, where a real one measures 15px.
- **Zero console errors and zero overflow is not "it renders".** Both new
  faults on the dashboard and job screens passed every automated check and
  were only found by opening the PNGs. The ring painted as one solid
  near-black arc, because `stroke="currentColor"` with no per-status `color`
  rule inherits `--text` — and that reads as a chart of a single outcome, not
  as a missing stylesheet. The log had a blank line between every entry,
  because `.log` is `white-space: pre-wrap` and the newline plus indent
  between two generated `<div>`s is painted; `app.js` appends elements with no
  text nodes between them, so the generator joins them with nothing. Both are
  now asserted, but the assertions were written *after* looking.

Before trusting any check, confirm it fails on a known-bad input.
`tools/break_style_check.py` does that for `table.js` — it deletes one
guarantee at a time and requires `check_style.py` to exit 1. It paid for
itself on its first run: the row-key break came back MISSED, and the fault was
in the checker. "Selection survived the sort" only counted ticked boxes, so a
row keyed on screen position — where a different job slides under a tick that
never moves — read as a pass. The check now asserts *which* row, by job id.

The ring check was falsified the same way by hand: deleting the three
`.ring-chart` colour rules (asserting the deletion matched exactly once, so a
no-op break could not masquerade as a pass) turned "three distinct colours"
into `got 1 want 3` and exited 1.
