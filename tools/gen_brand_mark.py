#!/usr/bin/env python3
"""Derive the Datum-Sync brand mark from the Stratum deck logo.

Source: datum-ui/static/deck-assets/stratum-rev.svg (the reversed/white
variant -- the navy one is invisible on the dark zone's --panel #16232F).

The source reads STRATUM, drawn as six stroked letter paths with a survey
monument standing in for the "A". Three changes, none of them a redraw of
anything that survives:

1. WORDMARK. Drop the S, T and R and draw a D in their place, keeping the
   monument and the T, U and M paths byte-for-byte from the source. The
   result reads DATUM in the same letterform system, with the same stylised
   "A". The D is the only new geometry in the file.

   The D is built to the system's own rules: stroke 18, butt caps, miter
   joins, cap height 166 (y -46 to 120), and a bowl that is an exact
   semicircle (r = 83 = half the cap height), which is how the R's bowl and
   the U's base are constructed. At 120 units wide it sits between the U
   (109.6) and the M (142.7).

   Its position was fitted, not guessed: the gap to the monument was swept
   from 132 to 170 and read at the 26px render size. Below ~145 the D-A gap
   is visibly wider than the A-T gap; above ~170 the bowl crowds the near
   tripod leg. 158 makes the two gaps match.

2. CROP. The deck lockup is 504 units tall because the monument hangs ~250
   units below the letter baseline. At any height that fits a 56px brand bar
   the whole lockup is ~90px wide and the word is unreadable. Cropping the
   viewBox to the letter band lets the mark run ~118x26px, where the tripod
   reads as the "A" with its feet on the baseline.

3. STROKE WEIGHT. In the deck the monument is drawn 1.5x heavier than the
   letters (legs 27 vs 18) because it is the hero mark on a projected slide.
   At 26px that is 3.6px of stroke against 2.4px of letter -- the "A" reads
   as a blob. Structure is scaled to 0.4 and the instrument head to 0.75.

   The head is scaled separately on purpose: its strokes sit on 6-7 unit
   rects, so the stroke is wider than the shape it outlines. Scaling it with
   the legs turns the finial into grey mush at 26px (0.65 device px) long
   before the legs are too thin.

   Measured in device pixels at the 26px render size, over the 194-unit
   cropped viewBox:
       letters   18u   = 2.41px   (untouched)
       legs      10.8u = 1.45px
       vertical   9.0u = 1.21px
       head       9.1u = 1.22px   (6.07 x the head group's own scale(1.5))

   Below roughly 0.3 the legs cross under 1 device px and antialias to an
   inconsistent grey. That is the floor, not a taste boundary.
"""
import re
import pathlib
import sys

SRC = pathlib.Path('/home/marcus/projects/datum-ui/static/deck-assets/stratum-rev.svg')
OUT = (pathlib.Path(__file__).resolve().parent.parent
       / 'datum_sync' / 'static-v2' / 'datum-mark.svg')

CAP_TOP, BASELINE = -46.0, 120.0
D_RIGHT = 158.0      # in the first letter group's translate(72,0) frame
D_WIDTH = 120.0
STRUCT, HEAD = 0.4, 0.75
PAD = 46.0           # side bearing, matching the source lockup's own padding
CROP_TOP, CROP_BOTTOM = -70.0, 124.0   # feet land on the letters' baseline


def scale_strokes(markup, factor):
    return re.sub(r'stroke-width="([\d.]+)"',
                  lambda m: f'stroke-width="{round(float(m.group(1)) * factor, 2)}"',
                  markup)


def draw_d():
    """A D in the source's construction: stem, flat top, semicircular bowl."""
    r = (BASELINE - CAP_TOP) / 2
    stem = D_RIGHT - D_WIDTH
    arc = D_RIGHT - r          # where the flat top hands over to the bowl
    return (f'M {stem} {BASELINE} L {stem} {CAP_TOP} L {arc} {CAP_TOP} '
            f'A {r} {r} 0 0 1 {arc} {BASELINE} Z')


def main():
    if not SRC.exists():
        sys.exit(f'source logo missing: {SRC}')

    inner = re.sub(r'^.*?<svg[^>]*>|</svg>\s*$', '', SRC.read_text().strip(),
                   flags=re.S).strip().replace('#FFFFFF', 'currentColor')

    i = inner.index('<g id="m"')
    letters, mono = inner[:i], inner[i:]

    # S T R | T U M, in source order. Only the second three are kept.
    paths = re.findall(r'<path d="[^"]*"\s*/>', letters)
    if len(paths) != 6:
        sys.exit(f'expected 6 letter paths in the source, found {len(paths)}')
    tum = ''.join(f'    {p}\n' for p in paths[3:])

    # Letters keep the weight the typeface was drawn at; only the monument
    # is lightened, and its head separately from its structure.
    j = mono.index('<g transform="translate(340, -68)')
    k = mono.index('</g>', mono.index('M329 -52'))
    mono = (scale_strokes(mono[:j], STRUCT)
            + scale_strokes(mono[j:k], HEAD)
            + scale_strokes(mono[k:], STRUCT))

    letters = (
        '  <g id="letters" fill="none" stroke="currentColor" stroke-width="18"'
        ' stroke-linecap="butt" stroke-linejoin="miter">\n'
        '   <g transform="translate(72,0)">\n'
        f'    <path d="{draw_d()}"/>\n'
        '   </g>\n'
        '   <g transform="translate(-82,0)">\n'
        f'{tum}'
        '   </g>\n'
        '  </g>\n')

    # Ink runs from the D's stem to the M's right edge; pad both sides equally.
    x0 = D_RIGHT - D_WIDTH + 72 - PAD
    x1 = 977.9 - 82 + PAD
    viewbox = f'{x0:g} {CROP_TOP:g} {x1 - x0:g} {CROP_BOTTOM - CROP_TOP:g}'

    OUT.write_text(
        f'<!-- GENERATED by tools/gen_brand_mark.py from\n'
        f'     {SRC}\n'
        f'     Reads DATUM: the source S/T/R are replaced by one drawn D, while\n'
        f'     the stylised "A" (survey monument) and the T/U/M paths come from\n'
        f'     the source unchanged. Cropped to the letter band and lightened\n'
        f'     for UI chrome -- monument structure x{STRUCT}, head x{HEAD}, letters\n'
        f'     untouched. See the generator for the measurements. Colour comes\n'
        f'     from currentColor: this is the reversed variant, dark zone only.\n'
        f'     Do not hand-edit: regenerate. -->\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{viewbox}" '
        f'fill="none" role="img" aria-label="Datum">\n'
        f'<title>Datum</title>\n{letters}{mono}\n</svg>\n')

    # A silently-empty mark is the failure mode worth guarding: the string
    # surgery above would happily emit a well-formed <svg> with nothing in it.
    body = OUT.read_text()
    counts = {t: body.count('<' + t) for t in ('path', 'line', 'rect', 'circle')}
    want = {'path': 7, 'line': 6, 'rect': 6, 'circle': 1}   # D + TUM + 3 monument
    if counts != want:
        sys.exit(f'unexpected geometry: {counts} != {want}')
    print(f'wrote {OUT.name} ({OUT.stat().st_size} bytes) viewBox="{viewbox}" {counts}')


if __name__ == '__main__':
    main()
