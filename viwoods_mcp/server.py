"""The Viwoods Paper notebooks over MCP, with an allow/deny list of folders.

A standalone MCP server (stdio) for Claude Desktop or Claude Code. It talks to the tablet over
plain adb: the notebook tree through the Paper app's own exported content provider and the pages
from its files, both readable by the ordinary `shell` user, so **no root is needed** (if the
provider says nothing it falls back to a database snapshot, which does).

**Nothing is shared until you say so.** What Claude may see is decided by the policy file
(`~/.config/viwoods-mcp/policy.json`, or $VIWOODS_MCP_POLICY); see policy.py. A notebook that is
not shared behaves as if it did not exist: asking for it by name or id gets the same answer as
for one that isn't there. Encrypted, recycle-bin and deleted notebooks never reach the tools, and
nothing here touches the notebook lock or any of the app's other controls.
"""

import argparse
import asyncio
import base64
import json
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import crop
from .adb import AdbError, Device
from .paper import Notebook, PaperApp, PaperError
from .pdfnote import PdfNoteError
from .policy import POLICY_FILE, Policy, apply_rule, notebook_path, parts

# paperd's ink index (optional): viwoods_search_handwriting reads the text it recognised from your pages.
INDEX = Path(os.environ.get("VIWOODS_MCP_INDEX")
             or Path.home() / ".local/share/paper-console/index.db")
# Where viwoods_show_match saves the crops it makes (pieces of your pages, so the folder is private).
CROP_DIR = Path(os.environ.get("VIWOODS_MCP_CROP_DIR") or Path.home() / ".cache/viwoods-mcp/crops")
MAX_SAVED = 50                     # crops kept; the oldest go first
MAX_PAGES = 8                      # pages per read_pages call
MAX_RESULTS = 20


class NotShared(Exception):
    """The notebook is missing or not shared: one message for both, so it can't be used to probe."""


# What a tool may fail with and still have its message shown to the model.
EXPECTED = (NotShared, ValueError, OSError, PaperError, AdbError, PdfNoteError)


def ranges(nums) -> str:
    """Page numbers as "1-3, 5"; "none" when there are none."""
    nums = sorted(set(nums))
    runs: list[list[int]] = []
    for n in nums:
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs) or "none"


def when(ts: float) -> str:
    """A timestamp as local time with its UTC offset, e.g. 2026-10-17T11:30+02:00."""
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="minutes")


def describe(meta: dict) -> str:
    """The one-line account of a read_pages call that goes above the images."""
    total = meta["page_count"]
    lines = [f"“{meta['notebook']}”, {total} page{'' if total == 1 else 's'}."]
    for key, text in (("no_writing", "No writing on page{s} {r}."),
                      ("not_in_notebook", f"Page{{s}} {{r}} {{is_}} past the end (it has {total})."),
                      ("could_not_read", "Could not read page{s} {r}: the tablet did not give it up; try again.")):
        nums = meta[key]
        if nums:
            lines.append(text.format(s="" if len(nums) == 1 else "s", r=ranges(nums),
                                     is_="is" if len(nums) == 1 else "are"))
    return " ".join(lines)

def cite(nb: Notebook, n: int | None = None) -> str:
    """Where something came from, in one form Claude can quote: Folder › Title · p. 3 · changed 2026-04-07."""
    bits = [" › ".join([p for p in nb.folder.split("/") if p] + [nb.title])]
    if n:
        bits.append(f"p. {n}")
    bits.append(f"changed {when(nb.modified)[:10]}")
    return " · ".join(bits)


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


async def open_in_viewer(path: Path) -> None:
    """Show a file in the PC's default viewer; a failure to is not worth failing a tool call for."""
    cmd = {"darwin": "open", "win32": "explorer"}.get(sys.platform, "xdg-open")
    try:
        proc = await asyncio.create_subprocess_exec(
            cmd, str(path), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), 10)
    except (OSError, asyncio.TimeoutError):
        pass


class ViwoodsAPI:
    """The tools' body. `paper` is a paper.PaperApp (or a fake with notebooks() and png())."""

    def __init__(self, paper, policy: Policy, index: Path = INDEX, device=None, log=None,
                 policy_file: Path | None = None, crop_dir: Path = CROP_DIR, open_image=None):
        self.paper, self.policy, self.index, self.device = paper, policy, Path(index), device
        self.crop_dir = Path(crop_dir)
        self.open_image = open_image            # async (path) -> None, or None: don't open anything
        self.policy_file = policy_file          # re-read on each call, so an edit applies at once
        self.log = log or (lambda msg: print(f"viwoods-mcp: {msg}", file=sys.stderr))

    def _policy(self) -> Policy:
        if self.policy_file is not None:
            self.policy = Policy.load(self.policy_file)
        return self.policy

    async def _split(self) -> tuple[list[Notebook], int]:
        self._policy()
        books = await self.paper.notebooks()
        shared = [b for b in books if self.policy.allows(notebook_path(b))]
        return shared, len(books) - len(shared)

    async def folders(self) -> dict:
        shared, hidden = await self._split()
        below: dict[str, int] = {}             # notebooks in the folder or any folder under it
        inside: dict[str, int] = {}            # notebooks directly in it
        top = 0
        for b in shared:
            names = [p for p in b.folder.split("/") if p]
            if not names:
                top += 1
                continue
            for i in range(1, len(names) + 1):
                key = "/".join(names[:i])
                below[key] = below.get(key, 0) + 1
            inside["/".join(names)] = inside.get("/".join(names), 0) + 1
        return {"folders": [{"path": k, "notebooks": v, "directly_inside": inside.get(k, 0)}
                            for k, v in sorted(below.items())],
                "top_level_notebooks": top,
                "notebooks_shared": len(shared), "notebooks_not_shared": hidden}

    async def notebooks(self, folder: str = "") -> list[dict]:
        shared, _ = await self._split()
        want = parts(folder)
        out = []
        for b in sorted(shared, key=lambda b: b.modified, reverse=True):
            if want and parts(b.folder)[:len(want)] != want:
                continue
            out.append({"id": b.id, "title": b.title, "folder": b.folder,
                        "page_count": len(b.pages), "written_pages": ranges(p.n for p in b.written),
                        "modified": when(b.modified)})
        return out

    async def _find(self, ref: str) -> Notebook:
        shared, _ = await self._split()
        ref = ref.strip()
        low = ref.lower()
        for test in (lambda b: b.id == ref, lambda b: b.title.lower() == low,
                     lambda b: low and low in b.title.lower()):
            hits = [b for b in shared if test(b)]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise NotShared(f"more than one shared notebook matches “{ref}”: "
                                + ", ".join(f"{b.title} ({b.id})" for b in hits[:6]))
        raise NotShared(f"no shared notebook called “{ref}”")

    async def read_pages(self, notebook: str, pages: list[int]) -> tuple[dict, list[tuple[int, bytes]]]:
        nb = await self._find(notebook)
        wanted = sorted({int(n) for n in pages})
        if not wanted:
            raise ValueError("say which page numbers to read (1 = the first page)")
        if len(wanted) > MAX_PAGES:
            raise ValueError(f"ask for at most {MAX_PAGES} pages at a time")
        by_n = {p.n: p for p in nb.pages}
        out, blank, past_end, failed = [], [], [], []
        for n in wanted:
            page = by_n.get(n)
            if page is None:
                past_end.append(n)
            elif not page.written:
                blank.append(n)
            else:
                try:
                    out.append((n, await self.paper.png(page)))
                except (PaperError, OSError):       # the tablet or the file, not a blank page
                    failed.append(n)
        self.log(f"read_pages {nb.id} {wanted}")
        return {"notebook": nb.title, "page_count": len(nb.pages), "no_writing": blank,
                "not_in_notebook": past_end, "could_not_read": failed,
                "references": {n: cite(nb, n) for n, _ in out}}, out

    async def search(self, query: str, limit: int = 10) -> dict:
        """Hits in shared notebooks, newest first: `results` (at most `limit`), how many there were
        in all (`total_matches`), and a reference for each."""
        words = [w for w in query.lower().split() if w][:6]
        empty = {"total_matches": 0, "shown": 0, "results": []}
        if not words or not self.index.exists():
            return empty
        shared, _ = await self._split()
        by_id = {b.id: b for b in shared}
        if not by_id:
            return empty
        limit = max(1, min(int(limit), MAX_RESULTS))
        con = sqlite3.connect(f"file:{self.index}?mode=ro", uri=True)
        # SQLite's own lower() and LIKE fold only ASCII letters, so "Über" would never match "über"
        con.create_function("plower", 1, lambda s: s.lower() if isinstance(s, str) else s,
                            deterministic=True)
        out, total = [], 0
        try:
            where = " and ".join("plower(text) like ? escape '\\'" for _ in words)
            args = ["%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                    for w in words]
            # Newest first, read lazily: rows of notebooks that are not shared are skipped here
            # (dropped, not counted) so they can't crowd the shared ones out of the limit.
            for nb_id, page_id, text in con.execute(
                    f"select nb, page, text from ink_pages where src = 'stock' and "
                    f"status = 'done' and {where} order by ts desc", args):
                nb = by_id.get(nb_id)
                if nb is None:
                    continue
                total += 1
                if len(out) >= limit:
                    continue                                 # only counted
                page = next((p for p in nb.pages if p.id == page_id), None)
                at = max(0, text.lower().find(words[0]) - 60)
                snippet = " ".join(text[at:at + 220].split())
                out.append({"reference": cite(nb, page.n if page else None),
                            "notebook": nb.title, "notebook_id": nb.id, "folder": nb.folder,
                            "page": page.n if page else None,
                            "text": ("…" if at else "") + snippet + ("…" if at + 220 < len(text) else "")})
        finally:
            con.close()
        result = {"total_matches": total, "shown": len(out), "results": out}
        if total > len(out):
            result["note"] = (f"Showing {len(out)} of {total} matches. Give the user the references "
                              "and ask what to look at, or narrow the query.")
        return result

    def _boxes_for(self, nb: Notebook, page_id: str) -> str | None:
        """The index's word boxes (JSON) for one page, or None if that page isn't indexed."""
        con = sqlite3.connect(f"file:{self.index}?mode=ro", uri=True)
        try:
            row = con.execute("select boxes from ink_pages where src = 'stock' and status = 'done' "
                              "and nb = ? and page = ?", (nb.id, page_id)).fetchone()
        finally:
            con.close()
        return row[0] if row else None

    async def show_match(self, notebook: str, page: int, words: str) -> tuple[dict, list[bytes]]:
        """The page of a shared notebook zoomed in on where `words` are written: crops, and where
        they were saved. Needs paperd's index for the word boxes."""
        nb = await self._find(notebook)
        n = int(page)
        pg = next((p for p in nb.pages if p.n == n), None)
        if pg is None:
            raise ValueError(f"“{nb.title}” has {len(nb.pages)} pages; there is no page {n}")
        if not pg.written:
            raise ValueError(f"page {n} of “{nb.title}” has nothing written on it")
        wanted = crop.words_of(words)
        if not wanted:
            raise ValueError("say which words to look for")
        if not self.index.exists():
            raise ValueError("this needs paperd's handwriting index, which isn't there; "
                             "viwoods_read_pages can still show the whole page")
        boxes_json = await asyncio.to_thread(self._boxes_for, nb, pg.id)
        if boxes_json is None:
            raise ValueError(f"page {n} of “{nb.title}” isn't in the handwriting index yet; "
                             "viwoods_read_pages can show the whole page")
        boxes = crop.find_boxes(boxes_json, wanted)
        if not boxes:
            raise ValueError(f"none of {', '.join(wanted)} is on page {n} of “{nb.title}” as the "
                             "index read it (it misreads handwriting); try another word, or "
                             "viwoods_read_pages for the whole page")
        img = await self.paper.image(pg)
        shots, spots = await asyncio.to_thread(crop.zoom, img, boxes)
        saved = self._save(nb, n, shots)
        if self.open_image is not None:
            for path in saved:
                await self.open_image(path)
        self.log(f"show_match {nb.id} p{n} {wanted} -> {len(shots)} crop(s)")
        return {"reference": cite(nb, n), "words": wanted, "matched_words": len(boxes),
                "spots_on_page": spots, "shown": len(shots), "saved": [str(p) for p in saved]}, shots

    def _save(self, nb: Notebook, n: int, shots: list[bytes]) -> list[Path]:
        """Write crops to the private crop folder, keeping only the newest MAX_SAVED."""
        self.crop_dir.mkdir(parents=True, exist_ok=True)
        self.crop_dir.chmod(0o700)
        stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(2).hex()}"    # two calls in a second stay apart
        paths = []
        for i, png in enumerate(shots, 1):
            path = self.crop_dir / f"{stamp}-{nb.id}-p{n}-{i}.png"
            path.write_bytes(png)
            path.chmod(0o600)
            paths.append(path)
        for old in sorted(self.crop_dir.glob("*.png"), key=lambda f: f.stat().st_mtime)[:-MAX_SAVED]:
            old.unlink(missing_ok=True)
        return paths

    async def add_pdf(self, pdf_path: str, title: str, folder: str = "") -> dict:
        policy = self._policy()
        if not policy.write:
            raise PermissionError('writing is off: set "write": true in the policy file to allow it')
        if not policy.allows([p for p in folder.split("/") if p] + [title]):
            raise PermissionError("that folder is not one you have shared")
        path = Path(pdf_path).expanduser()
        if path.suffix.lower() != ".pdf" or not path.is_file():
            raise ValueError("pdf_path must be an existing .pdf file on this PC")
        from .pdfnote import PdfNotes
        name = await PdfNotes(self.device).add(path.read_bytes(), title, folder)
        self.log(f"add_pdf {name!r} -> {folder or '(top level)'}")
        return {"added": name, "folder": folder or "(top level)"}


# ------------------------------------------------------------------ the MCP surface

INSTRUCTIONS = (
    "The user's notebooks in the stock Viwoods Paper app on their e-ink tablet, read-only except "
    "where noted. Only folders the user has shared are visible: anything else behaves as if it "
    "doesn't exist, so don't ask for it or guess at it. Page numbers are positions in the notebook "
    "(1 = first). If nothing is listed, nothing has been shared yet: tell the user to run "
    "`viwoods-mcp allow <folder>`. Notebooks are private writing: answer what was asked, with each "
    "point's `reference` (Folder › Title · p. N) so the user can find the page, and short quotes "
    "rather than long retellings. To show the user where something is written, use "
    "viwoods_show_match; open whole pages with viwoods_read_pages only when you need to read them.")

server = None            # built in build(), so importing this module needs no mcp install
_API: ViwoodsAPI | None = None


def _api() -> ViwoodsAPI:
    if _API is None:
        raise RuntimeError("viwoods-mcp: start the server with the viwoods-mcp command")
    return _API


@contextmanager
def _expected():
    """Failures we saw coming reach the model as an error with their own message."""
    from mcp.server.mcpserver.exceptions import ToolError
    try:
        yield
    except EXPECTED as e:
        raise ToolError(str(e) or type(e).__name__) from e


def build():
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ImageContent, TextContent

    srv = MCPServer("viwoods", instructions=INSTRUCTIONS)

    @srv.tool()
    async def viwoods_list_folders() -> str:
        """The folders of shared notebooks in the Viwoods tablet's Paper app (not Nextcloud folders).
        `notebooks` counts a folder and everything under it, `directly_inside` only its own; also
        the shared notebooks at the top level, and how many notebooks are not shared (a count only)."""
        with _expected():
            return json.dumps(await _api().folders(), ensure_ascii=False)

    @srv.tool()
    async def viwoods_list_notebooks(folder: str = "") -> str:
        """Shared notebooks in the Viwoods tablet's Paper app, newest first: id, title, folder, page
        count, which pages have writing (e.g. "1-3, 5") and when it was last changed. `folder` limits
        it to a folder and the folders under it."""
        with _expected():
            return json.dumps(await _api().notebooks(folder), ensure_ascii=False)

    @srv.tool()
    async def viwoods_read_pages(notebook: str, pages: list[int]) -> list:
        """Pages of a shared Viwoods Paper notebook as images, to read the handwriting. `notebook` is
        its title or id; `pages` are positions (1 = first), at most 8 per call. Pages with nothing
        written are not sent; the first line says which."""
        with _expected():
            meta, images = await _api().read_pages(notebook, pages)
        blocks: list = [TextContent(type="text", text=describe(meta))]
        for n, png in images:
            blocks.append(TextContent(type="text", text=f"{meta['references'][n]} (page {n} of "
                                                        f"{meta['page_count']}):"))
            blocks.append(ImageContent(type="image", data=base64.b64encode(png).decode(),
                                       mimeType="image/png"))
        return blocks

    @srv.tool()
    async def viwoods_search_handwriting(query: str, limit: int = 10) -> str:
        """Find words in the handwriting of shared Viwoods Paper notebooks: the text paperd's index read
        from the pages (it needs paperd's index file, VIWOODS_MCP_INDEX, and only covers pages it has
        read). Returns newest first, each hit with a `reference` (Folder › Title · p. N · changed date),
        the page and a snippet, plus `total_matches` in all. Cite the reference rather than retelling the
        page; to show the user the spot, use viwoods_show_match."""
        api = _api()
        if not api.index.exists():
            raise ToolError(f"The handwriting index isn't there ({api.index}); it comes from paperd. "
                            "Set VIWOODS_MCP_INDEX if it lives somewhere else.")
        with _expected():
            found = await api.search(query, limit)
        if not found["results"]:
            return f"No matches for “{query}” in the shared notebooks' indexed handwriting."
        return json.dumps(found, ensure_ascii=False)

    @srv.tool()
    async def viwoods_show_match(notebook: str, page: int, words: str) -> list:
        """Show the user where something is written: the page of a shared notebook zoomed in on `words`
        (outlined in red, up to 3 spots), also saved to a folder on their PC and, if they've turned it
        on, opened there. Use it when they ask to *see* or be shown where they wrote something; take
        `notebook` and `page` from viwoods_search_handwriting. Needs paperd's index for where words sit."""
        with _expected():
            meta, shots = await _api().show_match(notebook, page, words)
        spots = (f" ({meta['spots_on_page']} places on the page; showing {meta['shown']})"
                 if meta["spots_on_page"] > meta["shown"] else "")
        blocks: list = [TextContent(type="text", text=(
            f"{meta['reference']}: “{' '.join(meta['words'])}” outlined in red{spots}. "
            f"Saved to {', '.join(meta['saved'])}"))]
        for png in shots:
            blocks.append(ImageContent(type="image", data=base64.b64encode(png).decode(),
                                       mimeType="image/png"))
        return blocks

    @srv.tool()
    async def viwoods_add_pdf_notebook(pdf_path: str, title: str, folder: str = "") -> str:
        """Put a PDF from this PC on the Viwoods tablet as a new Paper notebook to write on, into a shared top
        level `folder` (or the top level). Only when the user has switched writing on."""
        with _expected():
            return json.dumps(await _api().add_pdf(pdf_path, title, folder), ensure_ascii=False)

    return srv


# ------------------------------------------------------------------ the command line

def _show(policy: Policy, books: list[Notebook] | None = None, file: Path = POLICY_FILE) -> str:
    lines = [f"policy file: {file}",
             f"allow: {policy.allow or '(nothing: nothing is shared)'}",
             f"deny:  {policy.deny or '[]'}",
             f"write: {'on' if policy.write else 'off'}"]
    if books is not None:
        shared = [b for b in books if policy.allows(notebook_path(b))]
        lines.append(f"\n{len(shared)} of {len(books)} notebooks would be shared:")
        tops: dict[str, list[int]] = {}
        for b in books:
            top = (b.folder.split("/")[0] if b.folder else "(top level)")
            seen = tops.setdefault(top, [0, 0])
            seen[1] += 1
            seen[0] += policy.allows(notebook_path(b))
        for top, (a, n) in sorted(tops.items()):
            lines.append(f"  {top}: {a} of {n} shared")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    global server, _API
    ap = argparse.ArgumentParser(prog="viwoods-mcp",
                                 description="Viwoods notebooks over MCP (stdio), with allow/deny folders")
    ap.add_argument("--serial", default=os.environ.get("VIWOODS_MCP_SERIAL", ""),
                    help="adb serial or host:port; empty means the only attached device")
    ap.add_argument("--policy", default=str(POLICY_FILE), help="the allow/deny file")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("allow", "deny", "remove"):
        p = sub.add_parser(name, help=f"{name} folder paths in the policy file")
        p.add_argument("rules", nargs="+", metavar="FOLDER")
    w = sub.add_parser("write", help="switch writing (viwoods_add_pdf_notebook) on or off")
    w.add_argument("state", choices=["on", "off"])
    sub.add_parser("policy", help="show the policy and what it shares (reads the tablet)")
    args = ap.parse_args(argv)

    file = Path(args.policy)
    policy = Policy.load(file)
    if args.cmd in ("allow", "deny", "remove"):
        for r in args.rules:
            apply_rule(policy, args.cmd, r)
        policy.save(file)
        print(_show(policy, file=file))
        return
    if args.cmd == "write":
        policy.write = args.state == "on"
        policy.save(file)
        print(_show(policy, file=file))
        return

    import asyncio
    device = Device(args.serial)

    async def run(*a: str) -> bytes:
        try:
            return await device.run(*a, timeout=60)
        except AdbError as e:
            raise PaperError(str(e) or "adb failed") from e

    paper = PaperApp(run)
    if args.cmd == "policy":
        try:
            books = asyncio.run(paper.notebooks())
        except PaperError as e:
            books = None
            print(f"(the tablet could not be read: {e})")
        print(_show(policy, books, file))
        return
    _API = ViwoodsAPI(paper, policy, device=device, policy_file=file,
                      open_image=open_in_viewer if truthy(os.environ.get("VIWOODS_MCP_OPEN_IMAGES")) else None)
    server = build()
    server.run()


if __name__ == "__main__":
    main()
