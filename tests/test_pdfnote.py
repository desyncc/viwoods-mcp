"""pdfnote.py: a PDF becomes a notebook in the stock Paper app (PdfNoteProvider)."""

import asyncio
import shlex

import pytest

from viwoods_mcp.paper import parse_rows as rows
from viwoods_mcp.pdfnote import PdfNoteError, PdfNotes, file_name

ROOT = "NOTE_USER_DIR_ID_1"


def test_file_name_is_one_safe_line_ending_pdf():
    assert file_name("Week plan") == "Week plan.pdf"
    assert file_name("a/b\\c:d*e?f\"g<h>i|j'k`l$m") == "a b c d e f g h i j k l m.pdf"
    assert file_name("  report.PDF ") == "report.pdf"
    assert file_name("") == "Untitled.pdf" and file_name("///") == "Untitled.pdf"
    assert file_name("x" * 200) == "x" * 80 + ".pdf"
    assert "\n" not in file_name("two\nlines")


def test_rows_keeps_commas_inside_values():
    text = ("Row: 0 id=abc, fileName=Notes, 2nd draft, pid=NOTE_USER_DIR_ID_1\n"
            "Row: 1 id=def, fileName=Plain, pid=NOTE_USER_DIR_ID_1\nsomething else\n")
    assert rows(text) == [{"id": "abc", "fileName": "Notes, 2nd draft", "pid": ROOT},
                          {"id": "def", "fileName": "Plain", "pid": ROOT}]


class FakeDevice:
    """Answers the shell commands PdfNotes sends, keeping a tiny library."""

    def __init__(self, folders=None, refuse=False):
        self.files, self.library, self.cmds = {}, {}, []
        self.folders = folders or {}            # name -> (id, pid)
        self.refuse = refuse

    async def run(self, *args, timeout=15):
        cmd = args[1]
        self.cmds.append(cmd)
        if cmd.startswith("content query"):
            where = shlex.split(cmd)[shlex.split(cmd).index("--where") + 1]   # as the device shell sees it
            if "pid LIKE" in where:
                return f"Row: 0 pid={ROOT}\n".encode()
            if "fileType=10" in where:
                name = next((n for n in self.folders if f"'{n}'" in where), None)
                if name:
                    i, pid = self.folders[name]
                    return f"Row: 0 id={i}, pid={pid}\n".encode()
                return b"No result found.\n"
            hit = [i for n, i in self.library.items() if f"fileName='{n}'" in where]
            return "".join(f"Row: {k} id={i}\n" for k, i in enumerate(hit)).encode() or b"No result found.\n"
        if cmd.startswith("mkdir"):
            return "\n".join(p.rsplit("/", 1)[1] for p in self.files).encode()
        if cmd.startswith("content insert"):
            argv = shlex.split(cmd)
            path = next(a for a in argv if a.startswith("path:s:"))[7:]
            if not any(a.startswith("folderId:s:") for a in argv):
                raise AssertionError("folderId must always be passed")
            if self.refuse:
                return b"java.lang.IllegalStateException\n"
            self.library[path.rsplit("/", 1)[1]] = f"id{len(self.library)}"
            return b""
        if cmd.startswith("rm -f"):
            self.files.pop(shlex.split(cmd)[2], None)
            return b""
        raise AssertionError(cmd)

    async def put(self, path, data, timeout=15):
        self.files[path] = data


def run(coro):
    return asyncio.run(coro)


def test_add_writes_the_pdf_then_inserts_with_the_root_folder():
    t = FakeDevice()
    name = run(PdfNotes(t, "/sdcard/Documents/paperd").add(b"%PDF-1.4 x", "Week plan"))
    assert name == "Week plan.pdf"
    assert t.files == {"/sdcard/Documents/paperd/Week plan.pdf": b"%PDF-1.4 x"}
    ins = shlex.split(next(c for c in t.cmds if c.startswith("content insert")))
    assert f"folderId:s:{ROOT}" in ins and any("PdfNoteProvider" in a for a in ins)


def test_a_taken_name_gets_a_number_and_nothing_is_overwritten():
    t = FakeDevice()
    t.files["/sdcard/Documents/paperd/Plan.pdf"] = b"old"
    n = PdfNotes(t, "/sdcard/Documents/paperd")
    assert run(n.add(b"%PDF new", "Plan")) == "Plan (2).pdf"
    assert t.files["/sdcard/Documents/paperd/Plan.pdf"] == b"old"


def test_a_named_folder_is_used_and_a_missing_one_is_said_plainly():
    t = FakeDevice(folders={"Thinking": ("fid-1", ROOT)})
    run(PdfNotes(t).add(b"%PDF x", "A", folder="Thinking"))
    assert "folderId:s:fid-1" in shlex.split(next(c for c in t.cmds if c.startswith("content insert")))
    with pytest.raises(PdfNoteError, match="no folder called"):
        run(PdfNotes(t).add(b"%PDF x", "B", folder="Nope"))


def test_not_a_pdf_and_a_refused_import_leave_nothing_behind():
    t = FakeDevice()
    with pytest.raises(PdfNoteError, match="isn't a PDF"):
        run(PdfNotes(t).add(b"<html>", "x"))
    assert not t.files
    t = FakeDevice(refuse=True)
    with pytest.raises(PdfNoteError):
        run(PdfNotes(t).add(b"%PDF x", "x"))
    assert not t.files                                   # the unreadable file is removed again


def test_a_title_with_a_quote_is_never_part_of_the_shell_or_sql():
    t = FakeDevice()
    run(PdfNotes(t).add(b"%PDF x", "O'Brien; rm -rf /"))
    assert all("O'Brien" not in c for c in t.cmds)       # the quote is stripped from the file name
