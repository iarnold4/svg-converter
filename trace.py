"""
trace.py - Convert raster logos (PNG/JPG) to SVG with vtracer.

Usage:
    python trace.py input.png                 # 'gradient' preset, writes ./input.svg (cwd)
    python trace.py -i input.png -o out.svg   # explicit input/output paths
    python trace.py *.png -o svgs/            # batch into a directory (created if missing)
    python trace.py input.png -p flat         # pick a preset
    python trace.py input.png -p flat --color-precision 8   # override any single param

Presets:
    flat      - solid-color logos, sharp edges. Fewest colors, cleanest paths.
    gradient  - looks flat but has subtle gradients / 3D shadows / anti-aliasing.
    detailed  - lots of color detail or photographic elements. Most color layers.

Color snapping (on by default):
    vtracer traces the anti-aliased edges of the source as thin "sliver" paths in
    slightly-off intermediate colors. Big, they look fine; shrunk, those slivers go
    sub-pixel and get re-blurred into a muddy halo along diagonals. Snapping
    extracts the logo's true palette from the source and quantizes the image to it
    BEFORE tracing (hard 0/255 alpha too), so vtracer never sees the ramps and
    emits one clean boundary per color region instead of sliver stacks. Traced
    fills are then snapped back to the exact palette hex values, since vtracer's
    internal color quantization can drift them slightly. Disable with --no-snap
    (real gradients / photos); loosen/tighten the palette with --snap-thresh.

Upscaling (on by default, --upscale 2):
    vtracer fits splines to pixel stair-steps, so at logo resolutions the traced
    curves come out subtly wobbly. Supersampling the source (LANCZOS) before
    tracing gives vtracer finer stairs to fit and visibly smoother curves. The
    output SVG keeps the original intrinsic size via width/height + viewBox.
    --upscale 1 disables.

Every vtracer parameter can be overridden on the command line (see --help).
"""
# /// script
# dependencies = ["vtracer", "pillow"]
# ///

import argparse
import glob
import os
import re
import sys
import tempfile
from collections import Counter
import vtracer
from PIL import Image

# vtracer defaults, then per-preset overrides. Only listed keys differ from base.
BASE = dict(
    colormode="color",        # "color" | "binary"
    hierarchical="stacked",   # "stacked" | "cutout"
    mode="spline",            # "spline" | "polygon" | "none"
    filter_speckle=4,         # drop blobs smaller than this (px)
    color_precision=6,        # bits of color -> higher = more distinct colors kept
    layer_difference=16,      # min color delta between layers -> higher = fewer layers
    corner_threshold=60,      # deg; below this angle is treated as a corner
    length_threshold=4.0,     # 3.5..10
    max_iterations=10,
    splice_threshold=45,      # deg; min angle to splice two splines
    path_precision=2,         # decimal places in path coords. vtracer's default
                              # is 8, which roughly doubles file size for
                              # differences that are invisible (sub-1/100px)
)

PRESETS = {
    # Solid flat colors: crush the palette hard, kill speckle noise, fewer layers.
    "flat": dict(
        filter_speckle=8,
        color_precision=5,
        layer_difference=32,
        corner_threshold=60,
    ),
    # Subtle gradients / 3D shadows: keep more color fidelity and thinner layers
    # so the shading survives, but still filter small anti-aliasing speckle.
    "gradient": dict(
        filter_speckle=4,
        color_precision=7,
        layer_difference=12,
        corner_threshold=60,
    ),
    # Rich detail / photo-ish: max color layers, minimal speckle filtering.
    "detailed": dict(
        filter_speckle=2,
        color_precision=8,
        layer_difference=8,
        corner_threshold=45,
    ),
}

INT_PARAMS = {
    "filter_speckle", "color_precision", "layer_difference",
    "corner_threshold", "max_iterations", "splice_threshold", "path_precision",
}
FLOAT_PARAMS = {"length_threshold"}
STR_PARAMS = {"colormode", "hierarchical", "mode"}


def build_params(preset: str, overrides: dict) -> dict:
    params = dict(BASE)
    params.update(PRESETS[preset])
    params.update({k: v for k, v in overrides.items() if v is not None})
    return params


_FILL_RE = re.compile(r'fill="#([0-9a-fA-F]{6})"')


def _hex(c):
    return "#%02X%02X%02X" % c


def true_palette(path, thresh=50, min_frac=0.004):
    """Extract the logo's real colors from opaque pixels of the source image.

    Greedy-merges the exact-color histogram: the most frequent color anchors a
    cluster; any later color within `thresh` (RGB euclidean) folds into an
    existing anchor, otherwise starts a new one. Colors covering less than
    `min_frac` of opaque pixels are ignored as noise. This recovers the handful
    of flat brand colors while collapsing anti-aliasing variants.
    """
    with Image.open(path) as im:
        raw = im.convert("RGBA").tobytes()  # flat R,G,B,A bytes
    opaque = [(raw[i], raw[i + 1], raw[i + 2])
              for i in range(0, len(raw), 4) if raw[i + 3] > 128]
    if not opaque:
        return []
    total = len(opaque)
    t2 = thresh * thresh
    anchors = []  # list of (r,g,b)
    for col, n in Counter(opaque).most_common():
        if n / total < min_frac:
            break
        if any((a[0] - col[0]) ** 2 + (a[1] - col[1]) ** 2 + (a[2] - col[2]) ** 2 <= t2
               for a in anchors):
            continue
        anchors.append(col)
    return anchors


def quantize_to_palette(rgba, anchors):
    """Snap every pixel of the image to the palette and make alpha hard 0/255.

    vtracer traces the anti-aliased edge ramps as stacked sliver layers, which
    bloats the SVG and shreds edges when rendered small. Removing the ramps
    before vtracer sees the image yields one clean boundary per color region.
    """
    flat = [c for a in anchors for c in a]
    pal = Image.new("P", (1, 1))
    pal.putpalette(flat + list(anchors[0]) * (256 - len(anchors)))
    rgb = rgba.convert("RGB").quantize(palette=pal, dither=Image.Dither.NONE).convert("RGB")
    alpha = rgba.getchannel("A").point(lambda a: 255 if a > 128 else 0)
    rgb.putalpha(alpha)
    return rgb


def snap_svg_colors(svg_text, anchors):
    """Rewrite every fill="#rrggbb" in the SVG to its nearest anchor color."""
    if not anchors:
        return svg_text
    cache = {}

    def nearest(h):
        if h not in cache:
            c = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
            best = min(anchors, key=lambda a: (a[0] - c[0]) ** 2 + (a[1] - c[1]) ** 2 + (a[2] - c[2]) ** 2)
            cache[h] = _hex(best)
        return cache[h]

    return _FILL_RE.sub(lambda m: 'fill="%s"' % nearest(m.group(1)), svg_text)


# Positional order required by vtracer.convert_image_to_svg_py after the two
# path args. The cp314 wheel segfaults on ANY keyword argument, so we must pass
# everything positionally in exactly this order.
PARAM_ORDER = (
    "colormode", "hierarchical", "mode", "filter_speckle", "color_precision",
    "layer_difference", "corner_threshold", "length_threshold",
    "max_iterations", "splice_threshold", "path_precision",
)


def _trace(inp: str, out: str, params: dict) -> None:
    args = [params[k] for k in PARAM_ORDER]
    vtracer.convert_image_to_svg_py(inp, out, *args)


_SVG_TAG_RE = re.compile(r'<svg([^>]*) width="(\d+)" height="(\d+)">')


def _fix_svg_tag(svg_text, orig_w, orig_h):
    """Set intrinsic size to the original image's and add a viewBox.

    vtracer emits width/height in traced-pixel units and NO viewBox. Without a
    viewBox browsers can't re-render the vectors at a different display size;
    they rasterize at intrinsic size and bitmap-scale, which is exactly the
    blurry-when-shrunk failure this tool exists to avoid.
    """
    def repl(m):
        return (f'<svg{m.group(1)} width="{orig_w}" height="{orig_h}"'
                f' viewBox="0 0 {m.group(2)} {m.group(3)}">')
    return _SVG_TAG_RE.sub(repl, svg_text, count=1)


def convert(inp: str, out: str, params: dict, snap=True, snap_thresh=50, upscale=2) -> int:
    """Trace `inp` to SVG `out`. Returns the number of distinct fill colors.

    vtracer segfaults on palette ('P') and some grayscale PNGs, so we normalize
    to RGBA in a temp file first. `upscale` supersamples before tracing for
    smoother splines. When `snap` is on, the palette is extracted from the
    untouched original and the image is quantized to it BEFORE tracing, so
    vtracer never sees anti-aliasing ramps (no sliver layers at all).
    """
    anchors = true_palette(inp, thresh=snap_thresh) if snap else []
    with Image.open(inp) as im:
        orig_w, orig_h = im.size
        needs_norm = im.mode != "RGBA" or upscale > 1 or anchors
        if needs_norm:
            rgba = im.convert("RGBA")
            if upscale > 1:
                rgba = rgba.resize((orig_w * upscale, orig_h * upscale), Image.LANCZOS)
            if anchors:
                rgba = quantize_to_palette(rgba, anchors)
            fd, tmp = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            rgba.save(tmp)
    if needs_norm:
        try:
            # speckle threshold is in traced-image px; scale it so upscaling
            # doesn't start keeping noise it used to filter out
            params = dict(params, filter_speckle=params["filter_speckle"] * upscale)
            _trace(tmp, out, params)
        finally:
            os.remove(tmp)
    else:
        _trace(inp, out, params)

    with open(out, "r", encoding="utf-8") as f:
        svg = f.read()
    svg = _fix_svg_tag(svg, orig_w, orig_h)
    with open(out, "w", encoding="utf-8") as f:
        f.write(svg)

    if anchors:
        # vtracer's own color quantization can drift fills slightly off the
        # palette even on a pre-quantized image; snap them back to exact hex
        svg = snap_svg_colors(svg, anchors)
        with open(out, "w", encoding="utf-8") as f:
            f.write(svg)

    return len(set(_FILL_RE.findall(svg)))


def main() -> int:
    ap = argparse.ArgumentParser(description="Trace raster logos to SVG with vtracer.")
    ap.add_argument("inputs", nargs="*", help="input image file(s)")
    ap.add_argument("-i", "--input", action="append", default=[],
                    help="input image file (repeatable; same as positional inputs)")
    ap.add_argument("-o", "--output",
                    help="output .svg file (single input only) or a directory "
                         "(any number of inputs; created if missing). Default: "
                         "<input-name>.svg in the current working directory")
    ap.add_argument("-p", "--preset", choices=PRESETS, default="gradient")
    ap.add_argument("--no-snap", dest="snap", action="store_false",
                    help="disable snapping fills to the source's true palette")
    ap.add_argument("--snap-thresh", type=int, default=50,
                    help="RGB distance for merging source colors into one palette "
                         "entry (default 50; lower = keep more distinct colors)")
    ap.add_argument("--upscale", type=int, default=2,
                    help="supersample the source Nx before tracing for smoother "
                         "curves (default 2; 1 disables)")

    # Per-param overrides (default None so they only apply when passed)
    for name in sorted(INT_PARAMS):
        ap.add_argument(f"--{name.replace('_', '-')}", dest=name, type=int, default=None)
    for name in sorted(FLOAT_PARAMS):
        ap.add_argument(f"--{name.replace('_', '-')}", dest=name, type=float, default=None)
    for name in sorted(STR_PARAMS):
        ap.add_argument(f"--{name.replace('_', '-')}", dest=name, type=str, default=None)

    args = ap.parse_args()

    # PowerShell passes ~ and * through literally (unlike bash), so expand both
    inputs = []
    for raw in args.inputs + args.input:
        p = os.path.expanduser(raw)
        matches = glob.glob(p) if any(c in p for c in "*?[") else []
        inputs.extend(matches or [p])
    if not inputs:
        ap.error("no input files (pass positionally or with -i/--input)")

    out_file = out_dir = None
    if args.output:
        args.output = os.path.expanduser(args.output)
        if args.output.lower().endswith(".svg"):
            if len(inputs) > 1:
                ap.error("-o/--output pointing at a .svg file needs a single "
                         "input; use a directory for batches")
            out_file = args.output
        else:
            out_dir = args.output
            os.makedirs(out_dir, exist_ok=True)

    overrides = {k: getattr(args, k) for k in INT_PARAMS | FLOAT_PARAMS | STR_PARAMS}
    params = build_params(args.preset, overrides)

    print(f"preset '{args.preset}' -> {params}")
    for inp in inputs:
        if not os.path.isfile(inp):
            print(f"  skip (not found): {inp}", file=sys.stderr)
            continue
        svg_name = os.path.splitext(os.path.basename(inp))[0] + ".svg"
        out = out_file or os.path.join(out_dir or os.getcwd(), svg_name)
        ncolors = convert(inp, out, params, snap=args.snap, snap_thresh=args.snap_thresh,
                          upscale=args.upscale)
        in_kb = os.path.getsize(inp) / 1024
        out_kb = os.path.getsize(out) / 1024
        snap_note = f"{ncolors} colors" if args.snap else "no snap"
        print(f"  {inp} ({in_kb:.0f} KB) -> {out} ({out_kb:.0f} KB, {snap_note})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
