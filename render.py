"""
render.py - Render SVGs to PNG at any resolution with resvg.

Usage:
    python render.py logo.svg                     # intrinsic size, writes ./logo-<W>w.png (cwd)
    python render.py logo.svg --width 4000        # exact pixel width (height keeps aspect)
    python render.py logo.svg --scale 4           # multiple of the SVG's intrinsic size
    python render.py *.svg --scale 4 -o pngs/     # batch into a directory (created if missing)
    python render.py logo.svg -o exact-name.png   # explicit output file (single input only)
    python render.py logo.svg --background white  # flatten onto a color (default: transparent)

Output files are named <input>-<width>w.png by default so different sizes never
collide, and so rendering logo.svg next to the logo.png it was traced from
can't overwrite the original.
"""
# /// script
# dependencies = ["resvg-py", "pillow"]
# ///

import argparse
import glob
import io
import os
import sys
import resvg_py
from PIL import Image


def render(svg_path, width=None, height=None, scale=None, background=None):
    """Render to PNG bytes. Returns (png_bytes, (w, h))."""
    kwargs = {}
    if width:
        kwargs["width"] = width
    if height:
        kwargs["height"] = height
    if scale and not (width or height):
        kwargs["zoom"] = float(scale)
    if background:
        kwargs["background"] = background
    png = bytes(resvg_py.svg_to_bytes(svg_path=svg_path, **kwargs))
    with Image.open(io.BytesIO(png)) as im:
        size = im.size
    return png, size


def main() -> int:
    ap = argparse.ArgumentParser(description="Render SVGs to PNG with resvg.")
    ap.add_argument("inputs", nargs="*", help="input .svg file(s)")
    ap.add_argument("-i", "--input", action="append", default=[],
                    help="input .svg file (repeatable; same as positional inputs)")
    ap.add_argument("-o", "--output",
                    help="output .png file (single input only) or a directory "
                         "(any number of inputs; created if missing). Default: "
                         "<input-name>-<width>w.png in the current working directory")
    ap.add_argument("--width", type=int, help="output width in px (height keeps aspect)")
    ap.add_argument("--height", type=int, help="output height in px (width keeps aspect)")
    ap.add_argument("--scale", type=float,
                    help="multiply the SVG's intrinsic size (ignored if --width/--height given)")
    ap.add_argument("--background", help="flatten onto a color, e.g. white or '#1c1e1c' "
                                         "(default: keep transparency)")
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
        if args.output.lower().endswith(".png"):
            if len(inputs) > 1:
                ap.error("-o/--output pointing at a .png file needs a single "
                         "input; use a directory for batches")
            out_file = args.output
        else:
            out_dir = args.output
            os.makedirs(out_dir, exist_ok=True)

    for inp in inputs:
        if not os.path.isfile(inp):
            print(f"  skip (not found): {inp}", file=sys.stderr)
            continue
        png, (w, h) = render(inp, width=args.width, height=args.height,
                             scale=args.scale, background=args.background)
        if out_file:
            out = out_file
        else:
            name = f"{os.path.splitext(os.path.basename(inp))[0]}-{w}w.png"
            out = os.path.join(out_dir or os.getcwd(), name)
        with open(out, "wb") as f:
            f.write(png)
        print(f"  {inp} -> {out} ({w}x{h}, {os.path.getsize(out) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
