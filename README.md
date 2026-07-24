# svg-converter

Two tools:

- **trace.py**: raster logos (PNG/JPG) to SVG with [vtracer](https://github.com/visioncortex/vtracer)
- **render.py**: SVGs back to PNG at any resolution with resvg (like for print-res
  deliverables from a traced master)

## Setup

### With uv (recommended; Ubuntu/Debian or anywhere)

Both scripts declare their own dependencies inline (PEP 723), so with
[uv](https://docs.astral.sh/uv/) there is no setup step at all — no venv, no
pip install. Just run them with `uv run` instead of `python`:

```
uv run trace.py yourlogo.png
uv run render.py logo.svg --scale 4
```

The first run resolves and caches the dependencies (and a Python, if the
system doesn't have one); later runs start instantly. Everywhere the examples
below say `python`, `uv run` works the same.

If you don't have uv yet (it's not in the apt repos):

```
curl -LsSf https://astral.sh/uv/install.sh | sh
```

then restart your shell (or `source ~/.bashrc`).

### Without uv

```
pip install vtracer pillow resvg-py
```

and use `python trace.py` / `python render.py` as in the examples below.

## trace.py usage

```
python trace.py yourlogo.png                     # gradient preset + palette snap, writes ./yourlogo.svg
python trace.py -i yourlogo.png -o out.svg       # explicit input/output paths
python trace.py *.png -o svgs/                   # batch into a directory (created if missing)
python trace.py yourlogo.png -p flat             # flat preset (solid-color logos)
python trace.py yourlogo.png --color-precision 8 --filter-speckle 2   # override any knob
```

Outputs default to the **current working directory** (`<input-name>.svg`), not
the input's folder. `-o` takes either a `.svg` file path (single input) or a
directory (any number of inputs). `~` and `*` wildcards work in any shell;
the scripts expand them, so PowerShell behaves like bash.

## render.py usage

```
python render.py logo.svg --width 4000           # exact pixel width, keeps aspect
python render.py logo.svg --scale 4              # multiple of the SVG's intrinsic size
python render.py *.svg --scale 4 -o pngs/        # batch into a directory
python render.py logo.svg --background white     # flatten (default: transparent)
```

Same `-i`/`-o`/cwd conventions as trace.py. Output files are named
`<input>-<width>w.png` (e.g. `prm-logo-4000w.png`) so different sizes never
collide, and so rendering `logo.svg` next to the `logo.png` it was traced
from can't overwrite the original.

## Presets (`-p`)

- `gradient` (default): flat-looking logos with subtle shading, 3D shadows, or anti-aliasing.
- `flat`: solid colors, sharp edges. Fewest layers.
- `detailed`: real gradients or photo-ish content. Most color layers.

## Palette snapping (the "fuzzy when shrunk" fix, ON by default)

vtracer traces the soft anti-aliased edges of the PNG as thin sliver paths in
slightly-off colors. Fine when big; shrunk, they go sub-pixel and re-blur into a
muddy halo on diagonals. Snapping auto-extracts the logo's true palette from the
source and quantizes the image to it *before* tracing (alpha hardened to 0/255
as well), so vtracer never sees the anti-aliasing ramps: it emits one clean
boundary per color region instead of sliver stacks. Fills are snapped back to
the exact palette hex after tracing too, since vtracer's internal color
quantization can drift them slightly.

- `--no-snap`: turn it off (keep vtracer's raw intermediate colors).
- `--snap-thresh N`: how aggressively source colors merge into one palette entry
  (default 50; RGB distance). Lower keeps more distinct colors; raise it if two
  brand colors are close and one is swallowing the other, lower it if distinct
  shades are being flattened together.

The run prints the final color count, e.g. `-> svgs/prm-logo.svg (97 KB, 5 colors)`.
If that number looks too high, the source has more real colors (or snap-thresh is
too low); too low means brand colors merged, so lower snap-thresh.

## Upscaling (`--upscale`, default 2)

vtracer fits splines to pixel stair-steps, so at typical logo resolutions the
traced curves come out subtly wobbly and outlines heavier than the source.
Supersampling the image (LANCZOS) before tracing gives vtracer finer stairs to
fit, for smoother curves and truer line weights. The SVG keeps the original
intrinsic size (`width`/`height` are the source's pixel dims; the `viewBox`
covers the upscaled coordinates). `--upscale 1` disables; 3 helps for very
small sources. Only worth it combined with snapping; upscaling an anti-aliased
image without quantizing it multiplies the sliver layers instead.

## Notes and gotchas baked into the script

- vtracer emits **no `viewBox`**, only `width`/`height`. Without one, browsers
  can't re-render the vectors at a different display size: Chrome rasterizes
  at intrinsic size and bitmap-scales, which looks exactly like a blurry
  shrunken PNG. The script now always injects a `viewBox`. If an SVG from an
  older run looks blurry on a site, this is why: re-trace it.
- `path_precision` defaults to 2 here (vtracer's own default is 8); the extra
  decimals roughly double file size for sub-1/100-pixel differences.
- Palette-mode ('P') and grayscale PNGs crash vtracer, so they are auto-converted to RGBA first.
- The vtracer 0.6.15 wheel for Python 3.14 segfaults on ANY keyword arg, so the
  script calls it positionally. Don't "clean that up" into kwargs.
