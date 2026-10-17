"""Read-only access to the stock Viwoods Paper app (com.wisky.notewriter) over adb.

The notebook tree comes from the app's own exported content provider, which the plain `shell`
user can query (no root); the page PNGs are read from the app's folder under /sdcard/Android/data,
which the shell user can read too. If the provider says nothing, the app's Room database is
snapshotted instead, which does need `adb root`.
"""

import asyncio
import io
import re
import shlex
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from PIL import Image

PKG = "com.wisky.notewriter"
DB = f"/data/data/{PKG}/databases/NoteDatabase.db"
FILES = f"/sdcard/Android/data/{PKG}/files/note_writer"
PROVIDER = "content://com.wisky.libnotewriter.contentprovider.FileInfoProvider"
SNAPSHOT = "/data/local/tmp/viwoods-mcp-notes.db"

FOLDER, NOTEBOOK, PAGE = 10, 20, 30                  # tb_file_info.fileType
INK, SHOT = 1, 2                                     # tb_page_resource.resourceType
# fileState bits (the app's own): 2 encrypted, 4 recycle bin, 16 deleted forever, 64 a PDF template.
# A locked notebook is never listed here; none of the others is a notebook to read.
HIDDEN_STATES = 2 | 4 | 16 | 64
TTL = 30.0                     # seconds a notebook listing is reused
MAX_EDGE = 1568                # what Claude's vision takes without downscaling


class PaperError(Exception):
    pass


@dataclass
class Page:
    n: int                     # 1-based position in the notebook
    id: str
    user: str                  # the app's user folder holding the files
    shot: str | None = None
    ink: str | None = None
    modified: float = 0.0      # when the page last changed (0 if the app didn't say)

    @property
    def written(self) -> bool:
        return bool(self.shot or self.ink)


@dataclass
class Notebook:
    id: str
    title: str
    folder: str                # "Health/Therapy": the folder names above it
    modified: float
    pages: list[Page] = field(default_factory=list)

    @property
    def written(self) -> list[Page]:
        return [p for p in self.pages if p.written]

    def changed(self, page: Page) -> float:
        """When `page` last changed: its own time, or the notebook's if the app gave none."""
        return page.modified or self.modified


# run(*adb args) -> stdout bytes
Run = Callable[..., Awaitable[bytes]]


def parse_rows(text: str) -> list[dict]:
    """`content query` output as dicts. Values may hold commas, so split only on ", key=" ."""
    out = []
    for line in text.splitlines():
        if not line.startswith("Row:"):
            continue
        body = line.split(" ", 2)[2] if line.count(" ") >= 2 else ""
        parts = re.split(r", (?=[A-Za-z][A-Za-z0-9_]*=)", body)
        out.append({k: v for k, _, v in (p.partition("=") for p in parts)})
    return out


def _int(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


class PaperApp:
    def __init__(self, run: Run):
        self._run = run
        self._books: dict[str, Notebook] = {}
        self._at = 0.0
        self._lock = asyncio.Lock()

    async def notebooks(self, fresh: bool = False) -> list[Notebook]:
        async with self._lock:
            if fresh or not self._books or time.monotonic() - self._at > TTL:
                self._books = await self._load()
                self._at = time.monotonic()
        return sorted(self._books.values(), key=lambda b: b.modified, reverse=True)

    # -- the two ways to read the tree
    async def _read_provider(self):
        async def table(name: str) -> list[dict]:
            raw = await self._run("shell", f"content query --uri {PROVIDER}/{name}")
            return parse_rows(raw.decode(errors="replace"))
        info = await table("file_info")
        if not any(r.get("fileType") == str(NOTEBOOK) for r in info):
            raise PaperError("the Paper app's provider returned no notebooks")
        rows = [(r.get("id", ""), r.get("pid", ""), r.get("userId", ""), r.get("fileName", ""),
                 _int(r.get("fileType")), _int(r.get("fileState")), _int(r.get("lastModifiedTime")),
                 _int(r.get("order"))) for r in info
                if _int(r.get("fileType")) in (FOLDER, NOTEBOOK, PAGE)]
        res = [(r.get("r_pid", ""), _int(r.get("resourceType")), r.get("r_fileName", ""))
               for r in await table("page_resource")
               if _int(r.get("resourceType")) in (INK, SHOT)]
        return rows, res

    async def _read_db(self):
        import sqlite3
        try:
            await self._run("shell", f"sqlite3 {DB} '.backup {SNAPSHOT}'")
            raw = await self._run("exec-out", f"cat {SNAPSHOT}; rm -f {SNAPSHOT}")
        except Exception as e:
            raise PaperError(f"couldn't read the Paper app's notebooks (needs adb root): {e}") from e
        if raw[:16] != b"SQLite format 3\x00":
            raise PaperError("couldn't read the Paper app's notebooks: the provider said nothing and "
                             "the database snapshot was empty (needs adb root)")
        raw = raw[:18] + b"\x01\x01" + raw[20:]           # the WAL flag can't be opened in memory
        con = sqlite3.connect(":memory:")
        con.deserialize(raw)
        try:
            rows = con.execute(
                "select id, pid, userId, fileName, fileType, fileState, lastModifiedTime, `order` "
                "from tb_file_info where fileType in (?, ?, ?)", (FOLDER, NOTEBOOK, PAGE)).fetchall()
            res = con.execute("select r_pid, resourceType, r_fileName from tb_page_resource "
                              "where resourceType in (?, ?)", (INK, SHOT)).fetchall()
        finally:
            con.close()
        return rows, res

    async def _load(self) -> dict[str, Notebook]:
        try:
            rows, res = await self._read_provider()
        except Exception as provider_error:
            try:
                rows, res = await self._read_db()
            except PaperError as e:
                raise PaperError(f"{e} (the provider said: {provider_error})") from e

        parent = {r[0]: (r[1], r[3]) for r in rows if r[4] == FOLDER}

        def folder(pid: str) -> str:
            names = []
            while pid in parent and len(names) < 8:
                pid, name = parent[pid][0], parent[pid][1]
                names.append(name)
            return "/".join(reversed(names))

        books = {r[0]: Notebook(r[0], r[3], folder(r[1]), r[6] / 1000)
                 for r in rows if r[4] == NOTEBOOK and not r[5] & HIDDEN_STATES}
        have: dict[str, set[str]] = {}
        for user in {r[2] for r in rows if r[4] == NOTEBOOK and r[0] in books}:
            listing = await self._run("shell", f"ls {shlex.quote(f'{FILES}/{user}')}")
            have[user] = set(listing.decode(errors="replace").split())

        by_page: dict[str, dict[int, str]] = {}
        for pid, kind, name in res:
            by_page.setdefault(pid, {})[kind] = name
        pages = sorted((r for r in rows if r[4] == PAGE and r[1] in books), key=lambda r: r[7])
        for r in pages:
            nb = books[r[1]]
            user, got = r[2], by_page.get(r[0], {})
            there = lambda kind: got.get(kind) if got.get(kind) in have.get(user, ()) else None
            nb.pages.append(Page(len(nb.pages) + 1, r[0], user, shot=there(SHOT), ink=there(INK),
                                 modified=r[6] / 1000))
        return books

    # -- pictures
    async def _raw(self, page: Page) -> bytes:
        name = page.shot or page.ink
        if not name:
            raise PaperError(f"page {page.n} has nothing written on it")
        return await self._run("exec-out", f"cat {shlex.quote(f'{FILES}/{page.user}/{name}')}")

    async def png(self, page: Page) -> bytes:
        """The page as an RGB PNG, flattened onto white and no larger than MAX_EDGE."""
        return await asyncio.to_thread(_flatten, await self._raw(page))

    async def image(self, page: Page) -> Image.Image:
        """The page as an RGB image at the size the app stored it, flattened onto white."""
        return await asyncio.to_thread(_open, await self._raw(page))


def _open(raw: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(raw))
    img.load()
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        img = Image.alpha_composite(Image.new("RGBA", img.size, (255, 255, 255, 255)), img)
    return img.convert("RGB")


def to_png(img: Image.Image, edge: int = MAX_EDGE) -> bytes:
    """`img` as PNG bytes, shrunk to fit `edge` on its longer side (never enlarged)."""
    if max(img.size) > edge:
        img = img.copy()
        img.thumbnail((edge, edge), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def _flatten(raw: bytes) -> bytes:
    return to_png(_open(raw))
