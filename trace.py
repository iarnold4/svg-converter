"""
trace.py - Convert raster logos (PNG/JPG) to SVG with vtracer.

Usage:
    python trace.py input.png                 # 'gradient' preset, writes ./input.svg (cwd)
    python trace.py -i input.png -o out.svg   # explicit input/output paths
    python trace.py *.png -o svgs/            # batch into a directory (created if missing)
    python trace.py input.png -p flat         # pick a preset
    python trace.py input.png --clean --snap-colors 32 --upscale 3 --smooth 5 --length-threshold 8
                                              # AI-generated / heavy-gradient logo
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
    For heavily gradiented logos where the clustered palette bands too coarsely,
    --snap-colors N (try 24-32) adds an adaptive N-color median-cut palette on
    top, spending the color budget proportionally on the gradients.

Cleaning (--clean, off by default):
    Sources that had a white background deleted (AI-generated logos
    especially) carry residue: anti-aliased edges still blended toward the old
    white (traces as a silver rim around every shape) and leftover near-white
    flecks. --clean un-mattes the edges (the blend is invertible given alpha)
    and drops the flecks before palette extraction. Don't use it on artwork
    with real white elements.

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
from PIL import Image, ImageFilter

# vtracer defaults, then per-preset overrides. Only listed keys differ from base.
BASE = dict(
    colormode="color",        # "color" | "binary"
    hierarchical="stacked",   # "stacked" | "cutout"
    mode="spline",            # "spline" | "polygon" | "none"
    filter_speckle=4,         # drop blobs smaller than this (px)
    color_precision=6,        # bits of color -> higher = more distinct colors kept
    layer_difference=16,      # min color delta between layers -> higher = fewer layers
    corner_threshold=60,      # deg; below this angle is treated as a corner
    length_threshold=8.0,     # 3.5..10; vtracer's default is 4, but longer
                              # segments give smoother curves + smaller files
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


def clean_source(rgba, matte=(255, 255, 255), ghost_thresh=16):
    """Repair background-removal residue (AI-generated / white-matted sources).

    Sources that had a white background deleted carry three defects that
    survive into the trace:
    - Anti-aliased edge pixels keep their blend against the old white
      background, so quantization invents pale halo colors that render as a
      silver rim. The blend is exactly invertible given the alpha:
      true = (blended*255 - (255-a)*matte) / a. Un-matte them.
    - Background removal ("ghosts"), by connectivity, not color alone: a
      near-matte pixel is background only if it is reachable from the image
      border through near-matte pixels (the surrounding background, including
      an intact opaque white background), or belongs to an enclosed pocket
      with an essentially-pure-matte core (letter counters, gaps between
      shapes). Near-matte pixels *inside* artwork - the bright highlight sheen
      on a gradient - are art and stay. A flat color threshold here bites
      holes in exactly those highlights.

    (An earlier version also median-filtered the alpha channel; at typical
    logo resolutions that lumps up small text more than it fixes edges.)
    """
    from collections import deque
    out = rgba.copy()
    px = out.load()
    w, h = out.size
    mr, mg, mb = matte
    g2 = ghost_thresh * ghost_thresh
    near = bytearray(w * h)   # traversable: transparent or near-matte
    seeds = deque()
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a == 0:
                near[y * w + x] = 1
                continue
            if a < 255:
                r = min(255, max(0, (r * 255 - (255 - a) * mr + a // 2) // a))
                g = min(255, max(0, (g * 255 - (255 - a) * mg + a // 2) // a))
                b = min(255, max(0, (b * 255 - (255 - a) * mb + a // 2) // a))
                px[x, y] = (r, g, b, a)
            d2 = (r - mr) ** 2 + (g - mg) ** 2 + (b - mb) ** 2
            if d2 <= g2:
                near[y * w + x] = 1
                # pure-matte core anywhere seeds a fill: catches enclosed
                # pockets (counters of P/R/a) the border flood can't reach
                if d2 <= 25 or x in (0, w - 1) or y in (0, h - 1):
                    seeds.append(y * w + x)
    visited = bytearray(w * h)
    for i in seeds:
        visited[i] = 1
    while seeds:
        i = seeds.popleft()
        x, y = i % w, i // w
        for j in ((i - w) if y else -1, (i + w) if y < h - 1 else -1,
                  (i - 1) if x else -1, (i + 1) if x < w - 1 else -1):
            if j >= 0 and near[j] and not visited[j]:
                visited[j] = 1
                seeds.append(j)
    for y in range(h):
        for x in range(w):
            if visited[y * w + x]:
                r, g, b, a = px[x, y]
                if a:
                    px[x, y] = (r, g, b, 0)

    # Collapse the opaque anti-aliased ring left against the removed
    # background. Those pixels are art/background blends at full alpha, so
    # there is no alpha to invert; left alone they quantize to pale anchors
    # and trace as a dirty outline around every shape. Snap each ring pixel
    # to whichever side of the blend it is on: closer to the matte than to
    # its most artlike neighbor -> transparent, else that neighbor's color.
    frontier = set()
    for y in range(h):
        for x in range(w):
            if px[x, y][3] == 0:
                for nx, ny in ((x-1, y), (x+1, y), (x, y-1), (x, y+1)):
                    if 0 <= nx < w and 0 <= ny < h and px[nx, ny][3] == 255:
                        frontier.add((nx, ny))
    for _ in range(3):
        cleared = []
        changes = []
        for x, y in frontier:
            r, g, b, a = px[x, y]
            if a != 255:
                continue
            # art reference must be a SOLID neighbor (not itself an edge
            # blend in the frontier): comparing against other blends lets
            # erosion cascade through thin strokes and eat small text whole
            art = None
            art_d = -1
            for nx in (x-1, x, x+1):
                for ny in (y-1, y, y+1):
                    if (0 <= nx < w and 0 <= ny < h and (nx, ny) != (x, y)
                            and (nx, ny) not in frontier):
                        nr, ng, nb, na = px[nx, ny]
                        if na:
                            d = (nr-mr)**2 + (ng-mg)**2 + (nb-mb)**2
                            if d > art_d:
                                art_d = d
                                art = (nr, ng, nb)
            if art is None:
                continue  # thin blend structure (small text): keep it
            d_bg = (r-mr)**2 + (g-mg)**2 + (b-mb)**2
            if d_bg <= (r-art[0])**2 + (g-art[1])**2 + (b-art[2])**2:
                cleared.append((x, y))
            elif (r, g, b) != art:
                changes.append((x, y, art))
        for x, y, c in changes:
            px[x, y] = (*c, 255)
        for x, y in cleared:
            r, g, b, _ = px[x, y]
            px[x, y] = (r, g, b, 0)
        if not cleared:
            break
        frontier = set()
        for x, y in cleared:
            for nx, ny in ((x-1, y), (x+1, y), (x, y-1), (x, y+1)):
                if 0 <= nx < w and 0 <= ny < h and px[nx, ny][3] == 255:
                    frontier.add((nx, ny))
    return out


def true_palette(rgba, thresh=50, min_frac=0.004):
    """Extract the logo's real colors from opaque pixels of the source image.

    Greedy-merges the exact-color histogram: the most frequent color anchors a
    cluster; any later color within `thresh` (RGB euclidean) folds into the
    nearest anchor, otherwise starts a new one. Clusters whose TOTAL coverage
    is below `min_frac` of opaque pixels are dropped as noise. Judging clusters
    (not individual exact colors) matters for gradient/anti-aliased regions: a
    gradient can cover thousands of pixels without any single exact color
    clearing min_frac, and cutting the histogram early would miss it entirely.
    """
    raw = rgba.tobytes()  # flat R,G,B,A bytes
    opaque = [(raw[i], raw[i + 1], raw[i + 2])
              for i in range(0, len(raw), 4) if raw[i + 3] > 128]
    if not opaque:
        return []
    total = len(opaque)
    t2 = thresh * thresh
    anchors = []  # list of [(r,g,b), cluster_pixel_count]
    for col, n in Counter(opaque).most_common():
        best = None
        best_d = t2 + 1
        for a in anchors:
            d = (a[0][0] - col[0]) ** 2 + (a[0][1] - col[1]) ** 2 + (a[0][2] - col[2]) ** 2
            if d <= t2 and d < best_d:
                best, best_d = a, d
        if best is not None:
            best[1] += n
        else:
            anchors.append([col, n])
    keep = sorted(([tuple(a[0]), a[1]] for a in anchors if a[1] / total >= min_frac),
                  key=lambda a: -a[1])
    # Suppress blend tones: a minor anchor lying near the straight RGB line
    # between two larger anchors is an anti-aliasing/dither ramp shade, not a
    # brand color. Left in, it traces as a colored outline hugging every
    # region boundary. Only small clusters qualify (<3% of pixels) so real
    # accent colors that happen to sit between two others are kept.
    out = []
    for i, (c, n) in enumerate(keep):
        blend = False
        if n / total < 0.03:
            for j in range(i):
                a = keep[j][0]
                for k in range(i):
                    if k == j:
                        continue
                    b = keep[k][0]
                    ab2 = sum((a[q] - b[q]) ** 2 for q in range(3))
                    if not ab2:
                        continue
                    t = max(0.0, min(1.0, sum((c[q] - a[q]) * (b[q] - a[q])
                                              for q in range(3)) / ab2))
                    d2 = sum((c[q] - a[q] - t * (b[q] - a[q])) ** 2 for q in range(3))
                    if d2 <= 30 * 30:
                        blend = True
                        break
                if blend:
                    break
        if not blend:
            out.append(c)
    return out


def adaptive_palette(rgba, n):
    """Build an N-color palette from the opaque pixels via median cut.

    For heavily gradiented logos, `true_palette` clustering bands the gradients
    into a few coarse blobs. Median cut instead spends the palette budget
    proportionally on whatever color ranges dominate the art, so gradients get
    enough steps to read as smooth once traced. Transparent pixels are excluded
    so the background can't steal palette slots.

    Counts are sqrt-weighted before the cut: median cut splits the most
    populous box, so a big flat region (a wordmark) would otherwise burn a
    dozen slots on invisible variants of one color while rarer ramps (gray
    anti-aliasing) get starved and snap to whatever hue is left - which is how
    a gray letter comes out green. Sqrt keeps dominant colors dominant without
    letting them monopolize the budget.
    """
    raw = rgba.tobytes()
    opaque = [(raw[i], raw[i + 1], raw[i + 2])
              for i in range(0, len(raw), 4) if raw[i + 3] > 128]
    if not opaque:
        return []
    weighted = []
    for col, cnt in Counter(opaque).items():
        weighted.extend([col] * max(1, int(cnt ** 0.5)))
    step = max(1, len(weighted) // 65500)  # stay under Pillow's max image width
    weighted = weighted[::step]
    strip = Image.new("RGB", (len(weighted), 1))
    strip.putdata(weighted)
    q = strip.quantize(colors=n, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    pal = q.getpalette()
    return [tuple(pal[i * 3:i * 3 + 3]) for i in sorted(set(q.tobytes()))]


def _hue_dist2(a, c):
    """Hue-aware color distance: luma plus chroma axes, chroma weighted 3x.

    No longer used by the trace pipeline itself (spatial voting in
    quantize_to_palette replaced it) but external scripts import it for
    classification, e.g. the pfas-snare reconstruction script.

    Plain RGB euclidean is hue-blind: a dark blue-gray (text drop shadow) is
    numerically closer to mid-green or olive-brown than to the gray it belongs
    with, which paints colored smudges over neutral areas when snapping. A
    neutral must never lose to a saturated anchor over a luma difference.
    """
    dy = (a[0] + 2 * a[1] + a[2] - c[0] - 2 * c[1] - c[2]) / 4
    dcr = (a[0] - a[1]) - (c[0] - c[1])
    dcb = (a[2] - a[1]) - (c[2] - c[1])
    return dy * dy + 3 * (dcr * dcr + dcb * dcb)


def quantize_to_palette(rgba, anchors):
    """Snap every pixel of the image to the palette and make alpha hard 0/255.

    vtracer traces the anti-aliased edge ramps as stacked sliver layers, which
    bloats the SVG and shreds edges when rendered small. Removing the ramps
    before vtracer sees the image yields one clean boundary per color region.

    Assignment is two-stage. Pixels close to an anchor (plain RGB) snap
    directly. Pixels far from every anchor are blends - anti-aliasing ramps,
    shadows - and no color metric places them reliably: RGB flips dark
    neutrals to whatever hue is nearby, chroma-weighted metrics flip
    desaturated ramps to darker anchors (a dark outline around every shape).
    A blend belongs to whatever its NEIGHBORHOOD is, so ambiguous pixels take
    the majority anchor among confident pixels in a 5x5 window instead.
    """
    w, h = rgba.size
    raw = bytearray(rgba.tobytes())
    n = w * h
    assign = bytearray(n)   # anchor index + 1; 0 = transparent
    conf = bytearray(n)
    cache = {}
    T2 = 25 * 25
    for i in range(n):
        j = 4 * i
        if raw[j + 3] > 128:
            c = (raw[j], raw[j + 1], raw[j + 2])
            r = cache.get(c)
            if r is None:
                best, bd = 0, 1 << 30
                for ai, a in enumerate(anchors):
                    d = (a[0]-c[0])**2 + (a[1]-c[1])**2 + (a[2]-c[2])**2
                    if d < bd:
                        bd, best = d, ai
                r = cache[c] = (best + 1, 1 if bd <= T2 else 0)
            assign[i], conf[i] = r
    for _ in range(2):  # two rounds lets confidence grow into wide ramps
        changes = []
        for i in range(n):
            if assign[i] and not conf[i]:
                x, y = i % w, i // w
                tally = {}
                for ny in range(max(0, y-2), min(h, y+3)):
                    base = ny * w
                    for nx in range(max(0, x-2), min(w, x+3)):
                        k = base + nx
                        if conf[k] and assign[k]:
                            tally[assign[k]] = tally.get(assign[k], 0) + 1
                if tally:
                    changes.append((i, max(tally, key=tally.get)))
        if not changes:
            break
        for i, v in changes:
            assign[i], conf[i] = v, 1
    for i in range(n):
        j = 4 * i
        if assign[i]:
            a = anchors[assign[i] - 1]
            raw[j], raw[j+1], raw[j+2], raw[j+3] = a[0], a[1], a[2], 255
        else:
            raw[j] = raw[j+1] = raw[j+2] = raw[j+3] = 0
    return Image.frombytes("RGBA", rgba.size, bytes(raw))


def snap_svg_colors(svg_text, anchors):
    """Rewrite every fill="#rrggbb" in the SVG to its nearest anchor color."""
    if not anchors:
        return svg_text
    cache = {}

    def nearest(h):
        if h not in cache:
            c = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
            cache[h] = _hex(min(anchors, key=lambda a:
                                (a[0]-c[0])**2 + (a[1]-c[1])**2 + (a[2]-c[2])**2))
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


def smooth_labels(rgba, anchors, size):
    """Mode-filter the quantized image as a label map to de-jitter boundaries.

    vtracer turns every one-pixel irregularity along a region boundary into a
    permanent squiggle in the spline, which reads as fuzzy edges once the SVG
    is blown up. Filtering the palette-index map (majority vote in a size x
    size window; transparent is a label too) straightens single-pixel noise
    while leaving real corners intact. Runs at the traced (upscaled)
    resolution, so `size` is small relative to features and doesn't eat
    detail the way filtering the source would.
    """
    idx = {a: i for i, a in enumerate(anchors)}
    raw = rgba.tobytes()
    lab = bytearray(len(raw) // 4)
    for i in range(0, len(raw), 4):
        lab[i // 4] = 255 if raw[i + 3] == 0 else idx[(raw[i], raw[i + 1], raw[i + 2])]
    lab = Image.frombytes("L", rgba.size, bytes(lab)).filter(
        ImageFilter.ModeFilter(size)).tobytes()
    out = bytearray(len(raw))
    for i, v in enumerate(lab):
        j = i * 4
        if v != 255:
            a = anchors[v]
            out[j], out[j + 1], out[j + 2], out[j + 3] = a[0], a[1], a[2], 255
    return Image.frombytes("RGBA", rgba.size, bytes(out))


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


def convert(inp: str, out: str, params: dict, snap=True, snap_thresh=50, upscale=2,
            snap_colors=0, clean=False, smooth=0) -> int:
    """Trace `inp` to SVG `out`. Returns the number of distinct fill colors.

    vtracer segfaults on palette ('P') and some grayscale PNGs, so we normalize
    to RGBA in a temp file first. `clean` repairs white-matte residue before
    anything else (see clean_source). `upscale` supersamples before tracing for
    smoother splines. When `snap` is on, the palette is extracted from the
    cleaned but un-upscaled image and the image is quantized to it BEFORE
    tracing, so vtracer never sees anti-aliasing ramps (no sliver layers at
    all). `snap_colors > 0` swaps the palette source: an adaptive N-color
    median-cut palette instead of histogram clustering, for heavily gradiented
    logos.
    """
    with Image.open(inp) as im:
        src_mode = im.mode
        rgba = im.convert("RGBA")
    orig_w, orig_h = rgba.size
    if clean:
        rgba = clean_source(rgba)
    if snap_colors:
        # union: histogram clusters guarantee the flat brand colors (a small
        # flat region can't win a median-cut box of its own), adaptive anchors
        # cover the gradients. Skip adaptive anchors that near-duplicate a
        # cluster color.
        anchors = true_palette(rgba, thresh=snap_thresh)
        for c in adaptive_palette(rgba, snap_colors):
            if all((a[0] - c[0]) ** 2 + (a[1] - c[1]) ** 2 + (a[2] - c[2]) ** 2 > 100
                   for a in anchors):
                anchors.append(c)
    else:
        anchors = true_palette(rgba, thresh=snap_thresh) if snap else []
    needs_norm = src_mode != "RGBA" or upscale > 1 or anchors or clean
    if needs_norm:
        if upscale > 1:
            rgba = rgba.resize((orig_w * upscale, orig_h * upscale), Image.LANCZOS)
        if anchors:
            rgba = quantize_to_palette(rgba, anchors)
            if smooth:
                rgba = smooth_labels(rgba, anchors, smooth)
        fd, tmp = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        rgba.save(tmp)
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


def auto_settings(inp, snap=True, snap_thresh=50):
    """Pick per-image settings by measuring the image, so the default command
    needs no flag incantations. Every choice is printed and any explicit flag
    overrides it.

    - upscale: small sources need more supersampling.
    - clean: an opaque near-white border means the artwork sits on an intact
      white background (or matte residue) - unambiguous case for --clean.
    - snap_colors: if the clustered brand palette covers most opaque pixels,
      the logo is flat -> plain snap. Poor coverage means real gradients ->
      add the adaptive palette.
    """
    with Image.open(inp) as im:
        rgba = im.convert("RGBA")
    w, h = rgba.size
    out = {"upscale": 3 if max(w, h) < 400 else (2 if max(w, h) < 1400 else 1),
           "smooth": 3, "clean": False, "snap_colors": 0, "coverage": None}
    px = rgba.load()
    border = ([px[x, y] for x in range(w) for y in (0, h - 1)]
              + [px[0, y] for y in range(h)] + [px[w - 1, y] for y in range(h)])
    whiteish = sum(1 for r, g, b, a in border
                   if a == 255 and r > 240 and g > 240 and b > 240)
    out["clean"] = whiteish > 0.6 * len(border)
    if snap:
        anchors = true_palette(rgba, thresh=snap_thresh)
        raw = rgba.tobytes()
        t2 = snap_thresh * snap_thresh
        opq = cov = 0
        for i in range(0, len(raw), 16):  # every 4th pixel is plenty
            if raw[i + 3] > 128:
                opq += 1
                c = (raw[i], raw[i + 1], raw[i + 2])
                if any((a[0]-c[0])**2 + (a[1]-c[1])**2 + (a[2]-c[2])**2 <= t2
                       for a in anchors):
                    cov += 1
        if opq:
            out["coverage"] = cov / opq
            if out["coverage"] < 0.85:
                out["snap_colors"] = 32
    return out


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
    ap.add_argument("--clean", action=argparse.BooleanOptionalAction, default=None,
                    help="repair white-background / matte residue before tracing: "
                         "un-matte edges, remove background by connectivity, "
                         "collapse the blend ring. Auto-detected by default; "
                         "--clean forces on, --no-clean forces off (use "
                         "--no-clean for logos with real white artwork)")
    ap.add_argument("--smooth", type=int, default=None,
                    help="mode-filter the quantized image with an NxN majority "
                         "vote before tracing, to de-jitter region boundaries "
                         "(runs at traced resolution). Auto: 3. 0 disables")
    ap.add_argument("--snap-colors", type=int, default=None,
                    help="add an adaptive N-color palette (median cut) on top of "
                         "the clustered brand colors, for heavily gradiented "
                         "logos. Auto: 32 when the clustered palette covers "
                         "<85%% of pixels, else 0. Explicit value overrides")
    ap.add_argument("--upscale", type=int, default=None,
                    help="supersample the source Nx before tracing for smoother "
                         "curves. Auto: 3 below 400px, 2 below 1400px, else 1")
    ap.add_argument("--no-auto", action="store_true",
                    help="disable per-image auto settings; unset flags fall back "
                         "to plain defaults (upscale 2, no clean/smooth/"
                         "snap-colors)")

    # Per-param overrides (default None so they only apply when passed)
    for name in sorted(INT_PARAMS):
        ap.add_argument(f"--{name.replace('_', '-')}", dest=name, type=int, default=None)
    for name in sorted(FLOAT_PARAMS):
        ap.add_argument(f"--{name.replace('_', '-')}", dest=name, type=float, default=None)
    for name in sorted(STR_PARAMS):
        ap.add_argument(f"--{name.replace('_', '-')}", dest=name, type=str, default=None)

    args = ap.parse_args()

    # PowerShell passes ~ and * through literally (unlike bash), so expand
    # both. A directory input means every image file inside it.
    IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
    inputs = []
    for raw in args.inputs + args.input:
        p = os.path.expanduser(raw)
        if os.path.isdir(p):
            found = sorted(f for f in glob.glob(os.path.join(p, "*"))
                           if f.lower().endswith(IMAGE_EXTS))
            if not found:
                print(f"  skip (no images in directory): {p}", file=sys.stderr)
            inputs.extend(found)
            continue
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
        elif (os.path.splitext(args.output)[1] and not os.path.isdir(args.output)):
            # "-o out.png" would otherwise create a *directory* named out.png
            ap.error(f"output file must end in .svg (got '{args.output}'); "
                     "pass a .svg path or an output directory")
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
        if args.no_auto:
            auto = {"upscale": 2, "smooth": 0, "clean": False, "snap_colors": 0}
        else:
            auto = auto_settings(inp, snap=args.snap, snap_thresh=args.snap_thresh)
        upscale = args.upscale if args.upscale is not None else auto["upscale"]
        smooth = args.smooth if args.smooth is not None else auto["smooth"]
        clean = args.clean if args.clean is not None else auto["clean"]
        snap_colors = (args.snap_colors if args.snap_colors is not None
                       else auto["snap_colors"])
        if not args.no_auto:
            cov = auto.get("coverage")
            cov_note = f", palette coverage {cov:.0%}" if cov is not None else ""
            print(f"  auto: upscale={upscale} smooth={smooth} "
                  f"clean={'on' if clean else 'off'} "
                  f"snap-colors={snap_colors or 'off'}{cov_note}")
        ncolors = convert(inp, out, params, snap=args.snap, snap_thresh=args.snap_thresh,
                          upscale=upscale, snap_colors=snap_colors,
                          clean=clean, smooth=smooth)
        in_kb = os.path.getsize(inp) / 1024
        out_kb = os.path.getsize(out) / 1024
        snap_note = f"{ncolors} colors" if args.snap else "no snap"
        print(f"  {inp} ({in_kb:.0f} KB) -> {out} ({out_kb:.0f} KB, {snap_note})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
