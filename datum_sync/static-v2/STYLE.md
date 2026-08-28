# Datum-Sync v2 — style guide

Locked 2026-08-28. `style.css` is the source of truth; this file explains
*why* the numbers are what they are. If you change one, change both.

The brief: **Flow's geometry, Datum's identity.** Layout, spacing and
component structure are pixel-matched to FME Flow 2026.2 from the 21-screenshot
set in `reference-images/`. Colour, type, icons and the brand mark are Datum's.

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
| **Sortable column headers** | **none at all** | arrow drawn, inert |
| **Row selection enables Edit / Remove** | select-all box only, wired to nothing | both drawn disabled — Flow's *initial* state, so it looks right and does nothing |
| **Pagination** | **cosmetic** — every button `disabled`, always "1 to N of N" | drawn, inert |
| **Page-size select** | none | drawn, inert |
| Column chooser (toolbar's rightmost icon) | none | drawn, inert |

Two known divergences, both deliberate for now:

- Flow's collapse control is a **floating circular chevron straddling the
  sidebar/content edge**; ours is a hamburger inside the brand bar. Ours keeps
  the control inside the grid instead of overlapping two areas.
- The column chooser's function is **inferred from its icon**, not observed.
  Do not build it off that guess.

The three in bold are the real work, and they interact: sorting and paging
both reorder rows, and selection state has to survive both or Remove deletes
the wrong row. Client-side search already has this problem documented at
`listbar()` in v1 — it filters what was fetched, which is the whole list today
but would silently become "the page on screen" the moment paging goes server
-side.

## Verification

Mockups are generated: `python3 tools/gen_mockups.py`. Never hand-edit
`mock-*.html`; every one carries a "do not hand-edit" header.

Two lessons worth keeping:

- **Measure at 1920×1080**, the reference screenshots' own size. A scrollbar
  "defect" reported here was really a 1000px test viewport.
- **Headless Chromium paints no scrollbars at all**, so a gutter measurement
  reads 0 whether or not one is hidden. Run scrollbar checks under
  `xvfb-run` with `headless=False`, where a real one measures 15px.

Before trusting any check, confirm it fails on a known-bad input.
