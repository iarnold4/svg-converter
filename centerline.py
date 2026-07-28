"""
centerline.py - Trace line-art / script logos to a single-stroke centerline SVG.

Where trace.py (vtracer) outlines the *silhouette* of each shape as a filled
path, this tool recovers the *pen path*: one open path running down the middle
of the stroke, from endpoint to endpoint. That is the form you want for
stroke-drawing animations (stroke-dasharray / stroke-dashoffset), plotters,
and engraving - a filled outline would animate the perimeter, not the stroke.

Usage:
    python centerline.py logo.png                  # writes ./logo.centerline.svg
    python centerline.py -i logo.png -o out.svg    # explicit paths
    python centerline.py *.png -o svgs/            # batch into a directory
    python centerline.py logo.png --ink '#3B2314'  # only these colors are stroke
    python centerline.py logo.png --stroke-width 8 # override measured width

How it works:
    1. Binarize: ink = opaque pixels (or non-background, or --ink colors).
    2. Skeletonize with a medial-axis transform (scikit-image), which also
       yields the local half-width used to estimate stroke-width.
    3. Prune short skeleton spurs (artifacts at corners and stroke ends).
    4. Walk the skeleton from endpoint to endpoint. At junctions - where the
       stroke crosses or touches itself - continue along the branch that best
       matches the incoming direction, the way a pen would.
    5. Least-squares-fit a smoothing spline and emit compact cubic Beziers.

    The result is stroke geometry: fill="none", stroke at the measured (or
    overridden) width, round caps/joins.

Good inputs are strokes of roughly constant width: script wordmarks,
signatures, line icons. Solid filled shapes have no meaningful centerline;
keep using trace.py for those. Both outputs can coexist - the default output
name is <input>.centerline.svg precisely so it can sit next to trace.py's
<input>.svg.

Animating the result (the reason this exists):
    path { stroke-dasharray: <length>; stroke-dashoffset: <length>; }
    animate stroke-dashoffset to 0. Get <length> from path.getTotalLength(),
    or pass --measure to print it. Path direction runs from the first printed
    endpoint to the second; reverse with --reverse if the draw should start
    at the other end.
"""
# /// script
# dependencies = ["pillow", "numpy", "scipy", "scikit-image"]
# ///

import argparse
import glob
import os
import sys
import numpy as np
from PIL import Image, UnidentifiedImageError
from scipy import interpolate, ndimage
from skimage.morphology import medial_axis

# 8-neighbour offsets, used for degree counts and skeleton walks.
NBRS = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


# ---------------------------------------------------------------- binarize --

def ink_mask(rgba: np.ndarray, ink_colors, ink_thresh: int) -> np.ndarray:
    """Boolean mask of stroke pixels.

    Priority: explicit --ink colors; else alpha (if the image actually uses
    transparency); else distance from the background color, taken as the most
    common border pixel (white-background JPEGs/PNGs).
    """
    rgb = rgba[..., :3].astype(int)
    alpha = rgba[..., 3]
    if ink_colors:
        t2 = ink_thresh * ink_thresh
        m = np.zeros(alpha.shape, bool)
        for c in ink_colors:
            m |= ((rgb - np.array(c)) ** 2).sum(axis=-1) <= t2
        return m & (alpha > 128)
    if (alpha < 128).any():
        return alpha > 128
    border = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
    vals, counts = np.unique(border.reshape(-1, 3), axis=0, return_counts=True)
    bg = vals[counts.argmax()]
    return ((rgb - bg) ** 2).sum(axis=-1) > ink_thresh * ink_thresh


# ------------------------------------------------------------- skeleton ops --

def _neighbors(p, skel_set):
    y, x = p
    return [(y + dy, x + dx) for dy, dx in NBRS if (y + dy, x + dx) in skel_set]


def prune_spurs(skel_set: set, max_len: float) -> set:
    """Iteratively delete endpoint branches shorter than max_len pixels.

    Skeletonization grows small spurs at corners, stroke ends, and anywhere
    the outline wobbles; left in, they become fake branches at junctions.
    A branch that ends at another *endpoint* (i.e. it is the whole component)
    is never pruned.
    """
    while True:
        removed = False
        endpoints = [p for p in skel_set if len(_neighbors(p, skel_set)) == 1]
        for ep in endpoints:
            if ep not in skel_set:
                continue
            chain, prev, cur = [ep], None, ep
            while True:
                nxt = [n for n in _neighbors(cur, skel_set) if n != prev]
                if len(nxt) != 1:            # junction (>=2) or dead end (0)
                    break
                prev, cur = cur, nxt[0]
                chain.append(cur)
                if len(chain) > max_len:
                    break
            at_junction = len(_neighbors(cur, skel_set)) > 2
            if at_junction and len(chain) - 1 <= max_len:
                for p in chain[:-1]:         # keep the junction pixel itself
                    skel_set.discard(p)
                removed = True
        if not removed:
            return skel_set


def extract_segments(skel_set: set):
    """Split the skeleton into (nodes, segments).

    Junction pixels tend to come in small 8-connected clusters; each cluster
    is collapsed to one node (its centroid). Returns:
      nodes:    node_id -> (y, x) float centroid
      segments: list of (node_a, node_b, [pixel, ...]) polylines between nodes
      node_of:  pixel -> node_id for every node pixel
    A component with no nodes at all is a pure loop; it is returned as a
    single closed segment (node_a == node_b == None).
    """
    deg = {p: len(_neighbors(p, skel_set)) for p in skel_set}
    node_px = {p for p, d in deg.items() if d != 2}

    node_of, nodes = {}, {}
    for p in node_px:
        if p in node_of:
            continue
        cluster, stack = [p], [p]
        node_of[p] = len(nodes)
        while stack:
            q = stack.pop()
            for n in _neighbors(q, skel_set):
                if n in node_px and n not in node_of:
                    node_of[n] = len(nodes)
                    cluster.append(n)
                    stack.append(n)
        ys, xs = zip(*cluster)
        nodes[len(nodes)] = (sum(ys) / len(ys), sum(xs) / len(xs))

    segments, walked = [], set()
    for p in node_px:
        for start in _neighbors(p, skel_set):
            if start in node_px or (p, start) in walked:
                continue
            chain, prev, cur = [p, start], p, start
            while cur not in node_px:
                nxt = [n for n in _neighbors(cur, skel_set)
                       if n != prev and (n not in chain[-8:-1])]
                if not nxt:
                    break
                prev, cur = cur, nxt[0]
                chain.append(cur)
            walked.add((p, chain[1]))
            walked.add((cur, chain[-2]) if cur in node_px else (cur, prev))
            a = node_of[p]
            b = node_of.get(cur)
            if b is None:                    # degenerate: ended off-node
                b = a
            if a == b and len(chain) < 4:    # micro self-loop at a junction
                continue
            segments.append((a, b, chain))

    # direct node-to-node adjacency (two junction clusters touching)
    for p in node_px:
        for n in _neighbors(p, skel_set):
            if n in node_px and node_of[p] < node_of[n]:
                if not any({s[0], s[1]} == {node_of[p], node_of[n]} and len(s[2]) <= 2
                           for s in segments):
                    segments.append((node_of[p], node_of[n], [p, n]))

    if not node_px and skel_set:             # pure cycle
        start = next(iter(skel_set))
        chain, prev, cur = [start], None, start
        while True:
            nxt = [n for n in _neighbors(cur, skel_set) if n != prev]
            if not nxt or nxt[0] == start:
                break
            prev, cur = cur, nxt[0]
            chain.append(cur)
        segments.append((None, None, chain))
    return nodes, segments


def contract_junctions(nodes, segments, max_len: float):
    """Merge nodes joined by segments shorter than max_len.

    An X-crossing of the stroke often skeletonizes as two nearby 3-way
    junctions joined by a stub; contracting the stub restores the clean 4-way
    node that straightest-continuation walking needs.
    """
    parent = {n: n for n in nodes}

    def find(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    for a, b, chain in segments:
        if a is not None and a != b and len(chain) <= max_len:
            parent[find(b)] = find(a)

    out = []
    for a, b, chain in segments:
        if a is None:
            out.append((a, b, chain))
            continue
        ra, rb = find(a), find(b)
        if ra == rb and len(chain) <= max_len:
            continue                         # the contracted stub itself
        out.append((ra, rb, chain))
    return nodes, out


def _heading(pixels, from_end: bool, k: int = 6):
    """Unit direction over the last (or first) k pixels of a segment."""
    pts = np.array(pixels[-k:] if from_end else pixels[:k][::-1], float)
    v = pts[-1] - pts[0]
    n = np.hypot(*v)
    return v / n if n else v


def assemble_paths(nodes, segments):
    """Chain segments into pen paths.

    Start at an endpoint (a node with one attached segment); at each junction
    take the unused segment that continues straightest through it, like a pen
    crossing its own stroke. Leftover segments start additional paths.
    Returns a list of (points, closed) with points as float (y, x) arrays.
    """
    unused = list(range(len(segments)))
    attach = {}                              # node -> [seg indices]
    for i, (a, b, _) in enumerate(segments):
        if a is not None:
            attach.setdefault(a, []).append(i)
            attach.setdefault(b, []).append(i)

    def seg_points(i, enter_node):
        a, b, chain = segments[i]
        pts = [(float(y), float(x)) for y, x in chain]
        return pts if a == enter_node else pts[::-1]

    def other(i, node):
        a, b, _ = segments[i]
        return b if node == a else a

    paths = []
    while unused:
        ends = []
        for n, segs in attach.items():
            live = [i for i in segs if i in unused]
            if len(live) == 1:
                ends.append((n, live[0]))
        if ends:
            node, seg = min(ends)            # deterministic start
        else:
            seg = unused[0]                  # only loops remain
            node = segments[seg][0]
        pts, closed = [], False
        cur_node, cur_seg = node, seg
        while True:
            unused.remove(cur_seg)
            if segments[cur_seg][0] is None:  # detached pure cycle
                pts, closed = seg_points(cur_seg, None), True
                break
            new = seg_points(cur_seg, cur_node)
            pts.extend(new if not pts else new[1:])
            cur_node = other(cur_seg, cur_node)
            cands = [i for i in attach.get(cur_node, []) if i in unused]
            if not cands:
                closed = len(pts) > 3 and pts[0] == pts[-1]
                break
            head = _heading(pts, from_end=True)
            cur_seg = max(cands, key=lambda i: float(
                np.dot(head, _heading(seg_points(i, cur_node), from_end=False))))
        paths.append((np.array(pts), closed))
    return paths


def splice_paths(paths, tol: float):
    """Merge stray pieces into the longest path as out-and-back excursions.

    Where the stroke merges with itself tangentially (very common in script:
    the up-stroke and down-stroke of a letter sharing a stretch), the skeleton
    collapses the doubled stretch into one segment and the walk leaves the far
    branch behind as a separate path. A real pen covers that branch by going
    out and coming back, so splice each leftover piece into the trunk at its
    nearest point, traversed out and back. Pieces farther than `tol` from the
    trunk (genuinely separate strokes) stay separate paths.
    """
    if len(paths) < 2:
        return paths
    trunk_i = max(range(len(paths)), key=lambda i: len(paths[i][0]))
    trunk, trunk_closed = paths[trunk_i]
    rest = [p for i, p in enumerate(paths) if i != trunk_i]

    changed = True
    while changed and rest:
        changed = False
        for ri, (pts, closed) in enumerate(rest):
            if closed:
                d = np.hypot(*(pts[:, None] - trunk[None]).T.reshape(2, -1))
                d = d.reshape(len(trunk), len(pts)).T
                flat = int(d.argmin())
                pi, ti = divmod(flat, len(trunk))
                if d[pi, ti] > tol:
                    continue
                loop = np.vstack([pts[pi:], pts[:pi + 1]])
                excursion = loop
            else:
                d0 = np.hypot(*(trunk - pts[0]).T)
                d1 = np.hypot(*(trunk - pts[-1]).T)
                if min(d0.min(), d1.min()) > tol:
                    continue
                if d0.min() <= d1.min():
                    ti, oriented = int(d0.argmin()), pts
                else:
                    ti, oriented = int(d1.argmin()), pts[::-1]
                excursion = np.vstack([oriented, oriented[::-1][1:]])
            trunk = np.vstack([trunk[:ti + 1], excursion, trunk[ti:]])
            rest.pop(ri)
            changed = True
            break
    return [(trunk, trunk_closed)] + rest


# ------------------------------------------------------------- curve output --

def fit_path(pts: np.ndarray, closed: bool, smooth: float, step: float):
    """Smooth pixel chains into sparse sample points on a fitted spline."""
    keep = np.r_[True, (np.diff(pts, axis=0) != 0).any(axis=1)]
    pts = pts[keep]
    if len(pts) < 4:
        return pts[:, ::-1]                  # (x, y)
    s = len(pts) * smooth * smooth
    try:
        (tck, _) = interpolate.splprep([pts[:, 1], pts[:, 0]], s=s,
                                       per=bool(closed), k=3)
    except Exception:
        return pts[:, ::-1]
    seg_len = np.hypot(*np.diff(pts, axis=0).T).sum()
    n = max(int(seg_len / step), 8)
    u = np.linspace(0, 1, n)
    x, y = interpolate.splev(u, tck)
    return np.column_stack([x, y])


def to_bezier_d(samples: np.ndarray, closed: bool, prec: int = 2) -> str:
    """Catmull-Rom through the sample points, emitted as SVG cubic Beziers."""
    f = f"%.{prec}f"

    def fmt(p):
        return f % p[0] + " " + f % p[1]

    p = samples
    if closed and not np.allclose(p[0], p[-1]):
        p = np.vstack([p, p[0]])
    d = ["M " + fmt(p[0])]
    n = len(p)
    for i in range(n - 1):
        p0 = p[i - 1] if i > 0 else (p[n - 2] if closed else p[0])
        p3 = p[i + 2] if i + 2 < n else (p[1] if closed else p[n - 1])
        c1 = p[i] + (p[i + 1] - p0) / 6.0
        c2 = p[i + 1] - (p3 - p[i]) / 6.0
        d.append("C " + fmt(c1) + " " + fmt(c2) + " " + fmt(p[i + 1]))
    if closed:
        d.append("Z")
    return " ".join(d)


def _hex(c):
    return "#%02X%02X%02X" % tuple(int(v) for v in c)


def _parse_hex(s):
    s = s.lstrip("#")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


# ------------------------------------------------------------------ convert --

def convert(inp, out, args):
    with Image.open(inp) as im:
        rgba = im.convert("RGBA")
        orig_w, orig_h = rgba.size
        if args.upscale > 1:
            rgba = rgba.resize((orig_w * args.upscale, orig_h * args.upscale),
                               Image.LANCZOS)
    arr = np.asarray(rgba)
    scale = args.upscale
    mask = ink_mask(arr, [_parse_hex(c) for c in args.ink], args.ink_thresh)
    if not mask.any():
        print(f"  skip (no ink pixels found): {inp}", file=sys.stderr)
        return None

    skel, dist = medial_axis(mask, return_distance=True)
    width_px = 2.0 * np.median(dist[skel])   # robust: dots/blobs don't skew it
    prune = args.prune if args.prune is not None else max(width_px, 3.0)

    labels, ncomp = ndimage.label(mask, structure=np.ones((3, 3)))
    svg_paths, endpoints = [], []
    for comp in range(1, ncomp + 1):
        comp_skel = skel & (labels == comp)
        skel_set = set(zip(*np.nonzero(comp_skel)))
        if len(skel_set) < max(3, prune):    # speckle
            continue
        skel_set = prune_spurs(skel_set, prune)
        nodes, segments = extract_segments(skel_set)
        if not segments:
            continue
        nodes, segments = contract_junctions(nodes, segments, prune)

        ys, xs = zip(*skel_set)
        color = args.stroke or _hex(np.median(
            arr[np.array(ys), np.array(xs), :3], axis=0))

        paths = assemble_paths(nodes, segments)
        paths = splice_paths(paths, tol=max(prune * 1.5, width_px))
        for pts, closed in paths:
            if args.reverse:
                pts = pts[::-1]
            samples = fit_path(pts, closed, args.smooth * scale,
                               step=max(3.0 * scale, width_px / 2))
            samples = samples / scale
            svg_paths.append((to_bezier_d(samples, closed), color))
            if not closed:
                endpoints.append((tuple(samples[0]), tuple(samples[-1])))

    if not svg_paths:
        print(f"  skip (no strokes recovered): {inp}", file=sys.stderr)
        return None

    sw = args.stroke_width or round(width_px / scale, 2)
    body = "\n".join(
        f'<path d="{d}" fill="none" stroke="{c}" stroke-width="{sw}" '
        f'stroke-linecap="round" stroke-linejoin="round"/>'
        for d, c in svg_paths)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
           f'width="{orig_w}" height="{orig_h}" '
           f'viewBox="0 0 {orig_w} {orig_h}">\n{body}\n</svg>\n')
    with open(out, "w", encoding="utf-8") as f:
        f.write(svg)
    return dict(npaths=len(svg_paths), width=sw, endpoints=endpoints)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Trace line-art logos to single-stroke centerline SVGs "
                    "(for stroke-drawing animation, plotting, engraving).")
    ap.add_argument("inputs", nargs="*", help="input image file(s)")
    ap.add_argument("-i", "--input", action="append", default=[],
                    help="input image file (repeatable; same as positional)")
    ap.add_argument("-o", "--output",
                    help="output .svg file (single input) or directory. "
                         "Default: <input-name>.centerline.svg in the cwd "
                         "(so it can sit next to trace.py's <input-name>.svg)")
    ap.add_argument("--ink", action="append", default=[],
                    help="hex color(s) that count as stroke, e.g. '#3B2314' "
                         "(repeatable). Default: all opaque / non-background "
                         "pixels. Use this to exclude decorative elements")
    ap.add_argument("--ink-thresh", type=int, default=80,
                    help="RGB distance for --ink matching and background "
                         "separation (default 80)")
    ap.add_argument("--stroke", help="output stroke color (default: sampled "
                                     "from the source along the centerline)")
    ap.add_argument("--stroke-width", type=float,
                    help="output stroke-width (default: measured median "
                         "width of the source stroke)")
    ap.add_argument("--upscale", type=int, default=2,
                    help="supersample Nx before skeletonizing for a smoother "
                         "centerline (default 2; 1 disables)")
    ap.add_argument("--smooth", type=float, default=1.5,
                    help="spline smoothing in source px; higher = smoother "
                         "but less faithful (default 1.5)")
    ap.add_argument("--prune", type=float, default=None,
                    help="max skeleton spur length to prune, in traced px "
                         "(default: the measured stroke width)")
    ap.add_argument("--reverse", action="store_true",
                    help="reverse path direction (animation draws from the "
                         "other endpoint)")
    args = ap.parse_args()

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

    for inp in inputs:
        if not os.path.isfile(inp):
            print(f"  skip (not found): {inp}", file=sys.stderr)
            continue
        name = os.path.splitext(os.path.basename(inp))[0] + ".centerline.svg"
        out = out_file or os.path.join(out_dir or os.getcwd(), name)
        try:
            info = convert(inp, out, args)
        except UnidentifiedImageError:
            hint = ("; render SVGs to a PNG first, e.g. "
                    f"'python render.py {inp} --scale 3'"
                    if inp.lower().endswith(".svg") else "")
            print(f"  skip (not a raster image{hint}): {inp}", file=sys.stderr)
            continue
        if info is None:
            continue
        out_kb = os.path.getsize(out) / 1024
        print(f"  {inp} -> {out} ({out_kb:.0f} KB, {info['npaths']} path(s), "
              f"stroke-width {info['width']})")
        for i, (a, b) in enumerate(info["endpoints"]):
            print(f"    path {i}: ({a[0]:.0f}, {a[1]:.0f}) -> "
                  f"({b[0]:.0f}, {b[1]:.0f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
