"""Zoom a page image in on where some words are written.

paperd's ink index stores a box for every word it recognised, `[word, x0, y0, x1, y1]`, in the
pixels of the page image (paperd itself draws them straight onto that image). Here the boxes of the
words asked for are found, grouped into spots, and each spot is cropped with the lines it sits on
(as far as the writing on them goes, up to MAX_W), outlined and enlarged so it can be read.
"""

import json
import re
from statistics import median

from PIL import Image, ImageDraw

from .paper import MAX_EDGE, to_png

Box = tuple[int, int, int, int]

MAX_CROPS = 3                  # spots shown per page; a word written all over a page is not a spot each
MIN_W, MIN_H = 700, 420        # a crop is never smaller than this, so one word still has context
MAX_W = 1500                   # nor wider by default: a Paper line runs ~1500-1700 px of an 1800 px page,
                               # so a wider crop is no longer enlarged (VIWOODS_MCP_CROP_WIDTH changes it)
MAX_ZOOM = 2.0                 # how far a small crop is enlarged
MARK = (214, 39, 40)           # the outline round a matched word


def words_of(query: str) -> list[str]:
    return [w for w in re.split(r"\W+", query.lower()) if w][:6]


def find_boxes(boxes_json: str, words: list[str]) -> list[Box]:
    """Boxes of the recognised words that contain any of `words` (case-insensitive)."""
    try:
        entries = json.loads(boxes_json or "[]")
    except ValueError:
        return []
    out: list[Box] = []
    for entry in entries:
        try:
            text, x0, y0, x1, y1 = entry
            low = str(text).lower()
            x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
        except (TypeError, ValueError):
            continue
        if x1 > x0 and y1 > y0 and any(w in low for w in words):    # a transcript line has no place
            out.append((x0, y0, x1, y1))
    return out


def spots(boxes: list[Box], gap: int) -> list[Box]:
    """Boxes that sit on the same or neighbouring lines (within `gap` pixels) merged into one
    region each, top to bottom."""
    merged: list[Box] = []
    for x0, y0, x1, y1 in sorted(boxes, key=lambda b: (b[1], b[0])):
        if merged and y0 <= merged[-1][3] + gap:
            a = merged[-1]
            merged[-1] = (min(a[0], x0), min(a[1], y0), max(a[2], x1), max(a[3], y1))
        else:
            merged.append((x0, y0, x1, y1))
    return merged


def _fit(lo: int, hi: int, minimum: int, limit: int) -> tuple[int, int]:
    """Stretch [lo, hi) to at least `minimum` around its middle, then slide it inside [0, limit)."""
    if hi - lo < minimum:
        extra = minimum - (hi - lo)
        lo, hi = lo - extra // 2, hi + (extra - extra // 2)
    if hi - lo >= limit:
        return 0, limit
    if lo < 0:
        lo, hi = 0, hi - lo
    if hi > limit:
        lo, hi = lo - (hi - limit), limit
    return lo, hi


def region(spot: Box, line_h: int, size: tuple[int, int], context: list[Box] = (),
           max_w: int = MAX_W) -> Box:
    """The part of the page to show for a spot: its words and the writing round them. `context` is
    every word box on the page; the crop reaches as far along the spot's own lines as they are
    written (so a line isn't cut off mid-sentence), but no wider than `max_w`."""
    w, h = size
    x0, y0, x1, y1 = spot
    pad_x, pad_y = max(80, 3 * line_h), max(60, int(2.5 * line_h))
    y0, y1 = _fit(y0 - pad_y, y1 + pad_y, MIN_H, h)
    left, right = x0 - pad_x, x1 + pad_x
    shown = [b for b in context if b[1] < spot[3] and b[3] > spot[1]]   # words on the spot's own lines
    if shown:
        left = min(left, min(b[0] for b in shown) - pad_x // 2)
        right = max(right, max(b[2] for b in shown) + pad_x // 2)
    if right - left > max_w:
        mid = (x0 + x1) // 2
        left, right = min(mid - max_w // 2, x0), max(mid + max_w // 2, x1)
    x0, x1 = _fit(left, right, MIN_W, w)
    return x0, y0, x1, y1


def zoom(img: Image.Image, boxes: list[Box], mark: bool = True, context: list[Box] = (),
         max_w: int = MAX_W) -> tuple[list[bytes], int]:
    """PNGs of up to MAX_CROPS spots of `img` round `boxes`, and how many spots there were.
    `context` is every word box on the page, for how far along its lines a crop reaches."""
    w, h = img.size
    boxes = [(max(0, a), max(0, b), min(w, c), min(h, d)) for a, b, c, d in boxes]
    boxes = [b for b in boxes if b[2] > b[0] and b[3] > b[1]]
    if not boxes:
        return [], 0
    line_h = int(median(b[3] - b[1] for b in boxes))
    found = spots(boxes, gap=line_h)
    if mark:
        img = img.copy()
        draw = ImageDraw.Draw(img)
        for x0, y0, x1, y1 in boxes:
            draw.rectangle((x0 - 3, y0 - 3, x1 + 3, y1 + 3), outline=MARK, width=3)
    shots = []
    for spot in found[:MAX_CROPS]:
        part = img.crop(region(spot, line_h, img.size, context, max_w))
        scale = min(MAX_ZOOM, MAX_EDGE / max(part.size))
        if scale > 1.05:
            part = part.resize((round(part.width * scale), round(part.height * scale)), Image.LANCZOS)
        shots.append(to_png(part))
    return shots, len(found)
