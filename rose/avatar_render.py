#!/usr/bin/python3
"""Render each layer of Rose's SVG avatar to a PNG (run with the *system* Python: python3-gi + librsvg + cairo).

    avatar_render.py <svg> <out_dir> <size>

Writes <out_dir>/<layer id>.png (size x size, transparent) for every layer the companion uses. The companion
calls this once and caches the result (the SVG's mtime is in the cache folder name).
"""

import sys

import cairo
import gi

gi.require_version("Rsvg", "2.0")
from gi.repository import Rsvg  # noqa: E402

LAYERS = ["hair-back", "body", "face", "blush", "eye-L-white", "eye-R-white", "iris-L", "iris-R", "lash-L",
          "lash-R", "closed-L", "closed-R", "happy-L", "happy-R", "brow-L", "brow-R", "mouth-smile", "mouth-small",
          "mouth-grin", "mouth-o", "mouth-talk1", "mouth-talk2", "mouth-talk3", "mouth-hmm", "mouth-wavy",
          "hair-front", "ahoge", "rose", "sweat"]


PROP_LAYERS = ["p-headphones", "p-binos", "p-camera", "p-tablet", "p-item", "p-tear", "p-flash"]


def _render(handle, layers, out, size) -> bool:
    vp = Rsvg.Rectangle()
    vp.x, vp.y, vp.width, vp.height = 0, 0, size, size
    for layer in layers:
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        if not handle.render_layer(cairo.Context(surf), "#" + layer, vp):
            print(f"missing layer {layer}", file=sys.stderr)
            return False
        surf.write_to_png(f"{out}/{layer}.png")
    return True


def main() -> int:
    svg, out, size = sys.argv[1], sys.argv[2], int(sys.argv[3])
    if not _render(Rsvg.Handle.new_from_file(svg), LAYERS, out, size):
        return 1
    if len(sys.argv) > 4:  # props: the camera without its flash and the held item without its emoji
        import re  # (the companion draws those itself, the emoji with a colour font)

        text = open(sys.argv[4], encoding="utf-8").read()
        bare = re.sub(r'<g id="p-flash">.*?</g>', "", text, flags=re.S)
        bare = re.sub(r'<text id="p-itemt"[^>]*>.*?</text>', "", bare, flags=re.S)
        if not _render(Rsvg.Handle.new_from_data(bare.encode()), PROP_LAYERS[:-1], out, size):
            return 1
        if not _render(Rsvg.Handle.new_from_data(text.encode()), ["p-flash"], out, size):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
