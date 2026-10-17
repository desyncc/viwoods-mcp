"""crop.py: finding words' boxes and zooming a page image in on them."""

import io
import json

from PIL import Image

from viwoods_mcp import crop
from viwoods_mcp.paper import MAX_EDGE

PAGE = (1800, 2400)


def reddish(img: Image.Image) -> bool:
    """Is there red in it? (Enlarging blurs the outline, so its exact colour is gone.)"""
    return any(r > 150 and g < 150 and b < 150 for _, (r, g, b) in img.getcolors(maxcolors=10**7))


def boxes_json(*entries) -> str:
    return json.dumps([list(e) for e in entries])


def shot(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png))


def test_words_are_found_whatever_their_case_or_punctuation_and_bad_entries_are_skipped():
    raw = boxes_json(("Anxiety,", 100, 100, 300, 160), ("calm", 320, 100, 400, 160),
                     ("ANXIOUS", 100, 300, 300, 360), ("anxiety", 0, 0, 0, 0))       # the last has no area
    raw = json.loads(raw) + [["short"], "junk", ["x", "a", 1, 2, 3]]
    assert crop.find_boxes(json.dumps(raw), ["anxiety"]) == [(100, 100, 300, 160)]
    assert crop.find_boxes(json.dumps(raw), ["anx"]) == [(100, 100, 300, 160), (100, 300, 300, 360)]
    assert crop.find_boxes("not json", ["a"]) == [] and crop.find_boxes("", ["a"]) == []
    assert crop.words_of("Anxiety, and  dread!") == ["anxiety", "and", "dread"]


def test_boxes_on_neighbouring_lines_make_one_spot_and_far_apart_ones_make_several():
    near = [(100, 100, 300, 160), (400, 120, 500, 180), (120, 200, 260, 260)]
    assert crop.spots(near, gap=60) == [(100, 100, 500, 260)]
    far = near + [(100, 1500, 300, 1560)]
    assert crop.spots(far, gap=60) == [(100, 100, 500, 260), (100, 1500, 300, 1560)]


def test_a_crop_is_never_smaller_than_the_minimum_and_stays_inside_the_page():
    for spot in [(100, 100, 240, 160), (0, 0, 50, 50), (1750, 2350, 1800, 2400), (800, 1200, 940, 1260)]:
        x0, y0, x1, y1 = crop.region(spot, 60, PAGE)
        assert 0 <= x0 < x1 <= PAGE[0] and 0 <= y0 < y1 <= PAGE[1]
        assert x1 - x0 >= crop.MIN_W and y1 - y0 >= crop.MIN_H
        assert x0 <= spot[0] and y0 <= spot[1] and x1 >= spot[2] and y1 >= spot[3]     # the words are in it
    assert crop.region((10, 10, 20, 20), 60, (300, 200)) == (0, 0, 300, 200)          # a page smaller than that


def test_zoom_enlarges_small_crops_outlines_the_words_and_caps_the_spots():
    img = Image.new("RGB", PAGE, "white")
    shots, found = crop.zoom(img, [(800, 1200, 940, 1260)])
    assert found == 1 and len(shots) == 1
    out = shot(shots[0])
    assert out.width > crop.MIN_W and max(out.size) <= MAX_EDGE                       # enlarged, not past the cap
    assert reddish(out), "the matched word should be outlined"
    plain, _ = crop.zoom(img, [(800, 1200, 940, 1260)], mark=False)
    assert not reddish(shot(plain[0]))
    many = [(100, 100 + i * 500, 240, 160 + i * 500) for i in range(5)]
    shots, found = crop.zoom(img, many)
    assert found == 5 and len(shots) == crop.MAX_CROPS
    assert crop.zoom(img, []) == ([], 0) and crop.zoom(img, [(5000, 5000, 5100, 5100)]) == ([], 0)
