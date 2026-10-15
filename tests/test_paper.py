"""paper.py: the notebook tree from the app's provider (no root), the database as a fallback."""

import asyncio
import io
import sqlite3

import pytest
from PIL import Image

from viwoods_mcp import paper
from viwoods_mcp.paper import PaperApp, PaperError, parse_rows

FILE_INFO = """\
Row: 0 id=f1, pid=NOTE_USER_DIR_ID_1, userId=u1, fileName=Health, Care, fileType=10, fileState=1, lastModifiedTime=0, order=0
Row: 1 id=n1, pid=f1, userId=u1, fileName=Meds, fileType=20, fileState=1, lastModifiedTime=3000, order=0
Row: 2 id=n2, pid=f1, userId=u1, fileName=Locked, fileType=20, fileState=3, lastModifiedTime=2000, order=1
Row: 3 id=n3, pid=f1, userId=u1, fileName=Old.pdf, fileType=20, fileState=69, lastModifiedTime=1000, order=2
Row: 4 id=p1, pid=n1, userId=u1, fileName=1, fileType=30, fileState=1, lastModifiedTime=3000, order=0
Row: 5 id=p2, pid=n1, userId=u1, fileName=2, fileType=30, fileState=1, lastModifiedTime=3000, order=1
"""
RESOURCES = """\
Row: 0 r_id=1, r_pid=p1, resourceType=2, r_fileName=screenshotBmp_a.png
Row: 1 r_id=2, r_pid=p1, resourceType=9, r_fileName=Thumbnail_a.png
Row: 2 r_id=3, r_pid=p2, resourceType=4, r_fileName=ignored.png
"""


def png(rgba=(0, 0, 0, 255), size=(40, 60)) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", size, rgba).save(out, "PNG")
    return out.getvalue()


class Adb:
    def __init__(self, provider=True, db=None):
        self.provider, self.db, self.cmds = provider, db, []

    async def __call__(self, *args):
        cmd = args[-1]
        self.cmds.append(cmd)
        if cmd.startswith("content query"):
            if not self.provider:
                return b"No result found.\n"
            return (FILE_INFO if cmd.endswith("/file_info") else RESOURCES).encode()
        if cmd.startswith("ls "):
            return b"screenshotBmp_a.png\nThumbnail_a.png\n"
        if cmd.startswith("sqlite3"):
            return b""
        if cmd.startswith("cat " + paper.SNAPSHOT):
            if self.db is None:
                return b""
            return self.db
        if cmd.startswith("cat '"):
            return png()
        raise AssertionError(cmd)


def run(coro):
    return asyncio.run(coro)


def test_parse_rows_keeps_commas_inside_values():
    assert parse_rows("Row: 0 id=a, fileName=Notes, 2nd draft, pid=x\nnoise\n") == [
        {"id": "a", "fileName": "Notes, 2nd draft", "pid": "x"}]


def test_the_provider_builds_the_tree_and_hides_locked_bin_and_template_notebooks():
    adb = Adb()
    books = run(PaperApp(adb).notebooks())
    assert [b.title for b in books] == ["Meds"]
    meds = books[0]
    assert meds.folder == "Health, Care"
    assert [p.n for p in meds.pages] == [1, 2] and [p.written for p in meds.pages] == [True, False]
    assert meds.pages[0].shot == "screenshotBmp_a.png" and meds.pages[0].thumb == "Thumbnail_a.png"
    assert not any(c.startswith("sqlite3") for c in adb.cmds)           # no root needed


def test_when_the_provider_is_silent_the_database_is_used():
    con = sqlite3.connect(":memory:")
    con.executescript("""
        create table tb_file_info (id text, pid text, userId text, fileName text, fileType int,
          fileState int, lastModifiedTime int, `order` int);
        create table tb_page_resource (r_pid text, resourceType int, r_fileName text);
        insert into tb_file_info values ('n9','root','u1','From DB',20,1,5000,0);
        insert into tb_file_info values ('p9','n9','u1','1',30,1,5000,0);
        insert into tb_page_resource values ('p9',2,'screenshotBmp_a.png');
    """)
    books = run(PaperApp(Adb(provider=False, db=bytes(con.serialize()))).notebooks())
    assert [b.title for b in books] == ["From DB"] and books[0].pages[0].written


def test_with_neither_a_clear_error_says_root_is_needed():
    with pytest.raises(PaperError, match="adb root"):
        run(PaperApp(Adb(provider=False, db=None)).notebooks())


def test_a_page_comes_back_as_an_rgb_png_flattened_on_white_and_capped():
    app = PaperApp(Adb())
    meds = run(app.notebooks())[0]
    out = run(app.png(meds.pages[0]))
    img = Image.open(io.BytesIO(out))
    assert img.mode == "RGB" and img.size == (40, 60)
    with pytest.raises(PaperError):
        run(app.png(meds.pages[1]))                                      # nothing written on it
    big = paper._flatten(png(size=(4000, 2000)))
    assert max(Image.open(io.BytesIO(big)).size) == paper.MAX_EDGE
    faint = Image.open(io.BytesIO(paper._flatten(png(rgba=(0, 0, 0, 0))))).getpixel((0, 0))
    assert faint == (255, 255, 255)
