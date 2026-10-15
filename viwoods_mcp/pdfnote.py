"""Put a PDF into the stock Paper app as a notebook you write on.

The app's own PDF import is `PdfNoteProvider` (exported, no permission): `insert` with `path` (an
existing .pdf the app can read) and `folderId` makes a notebook with one writable page per PDF
page and the PDF as the page template. What that needs, learned live:

* **The PDF is referenced, not copied** (the app stores its absolute path), so it is written to a
  folder of its own that is never cleaned up, under a name that is unique.
* **`folderId` must be passed.** Left out, the app uses the PDF's parent directory name, which is
  no folder: the notebook would have no parent. The library root is the `pid` of the top-level
  rows (`NOTE_USER_DIR_ID_<user>`), found by query.
* The notebook is named after the file, extension included.
* The provider answers success even when it refuses a corrupt PDF, so a row must appear.
"""

import re
import shlex

from .paper import FOLDER, PROVIDER, parse_rows

DIR = "/sdcard/Documents/viwoods-mcp"
PDF_PROVIDER = "content://com.wisky.modulenotemanager.contentprovider.PdfNoteProvider"
ROOT_PREFIX = "NOTE_USER_DIR_ID_"
MAX_PDF = 64 * 1024 * 1024


class PdfNoteError(Exception):
    """Why a PDF didn't become a notebook; the text is fit to show."""


def file_name(title: str) -> str:
    """A title as a file name the app and FAT-style storage both take, always ending .pdf."""
    t = re.sub(r"[\\/:*?\"<>|'`$\x00-\x1f]", " ", title or "")
    t = re.sub(r"\s+", " ", t).strip(" .")[:80].strip(" .")
    if t.lower().endswith(".pdf"):
        t = t[:-4].strip(" .")
    return f"{t or 'Untitled'}.pdf"


def _sql(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class PdfNotes:
    def __init__(self, device, directory: str = DIR):
        self.device, self.dir = device, directory.rstrip("/")

    async def _sh(self, cmd: str, timeout: float = 30) -> str:
        return (await self.device.run("shell", cmd, timeout=timeout)).decode(errors="replace")

    async def _query(self, where: str, projection: str) -> list[dict]:
        return parse_rows(await self._sh(
            f"content query --uri {PROVIDER}/file_info --where {shlex.quote(where)} "
            f"--projection {projection}"))

    async def folder_id(self, folder: str = "") -> str:
        """The id to import under: the named top-level folder, or the library root."""
        if folder:
            found = await self._query(f"fileType={FOLDER} AND fileName={_sql(folder)}", "id:pid")
            if not found:
                raise PdfNoteError(f"There is no folder called “{folder}” in the Paper app.")
            top = [f for f in found if f.get("pid", "").startswith(ROOT_PREFIX)]
            return (top or found)[0]["id"]
        found = await self._query("pid LIKE 'NOTE_USER_DIR_ID_%'", "pid")
        if not found:
            raise PdfNoteError("Couldn't find the Paper app's library (is it set up on the tablet?).")
        return found[0]["pid"]

    async def _free_name(self, name: str) -> str:
        have = set((await self._sh(f"mkdir -p {shlex.quote(self.dir)}; ls -1 {shlex.quote(self.dir)}"))
                   .splitlines())
        if name not in have:
            return name
        stem, n = name[:-4], 2
        while f"{stem} ({n}).pdf" in have:
            n += 1
        return f"{stem} ({n}).pdf"

    async def add(self, pdf: bytes, title: str, folder: str = "") -> str:
        """Create a notebook from `pdf`. Returns its name in the Paper app."""
        if not pdf.startswith(b"%PDF"):
            raise PdfNoteError("That isn't a PDF.")
        if len(pdf) > MAX_PDF:
            raise PdfNoteError("That PDF is too large to put on the tablet (64 MB at most).")
        parent = await self.folder_id(folder)
        name = await self._free_name(file_name(title))
        path = f"{self.dir}/{name}"
        before = {r.get("id") for r in await self._query(f"fileName={_sql(name)}", "id")}
        await self.device.put(path, pdf, timeout=60)
        out = await self._sh(f"content insert --uri {PDF_PROVIDER} --bind path:s:{shlex.quote(path)} "
                             f"--bind folderId:s:{shlex.quote(parent)}")
        failed = "Exception" in out or "Error" in out
        if not failed:
            after = {r.get("id") for r in await self._query(f"fileName={_sql(name)}", "id")}
            failed = not (after - before)
        if failed:
            await self._sh(f"rm -f {shlex.quote(path)}")
            raise PdfNoteError("The Paper app couldn't open that PDF.")
        return name
