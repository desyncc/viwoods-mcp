"""audit.py: the private record of what Claude was shown."""

import json
import os

from viwoods_mcp import audit
from viwoods_mcp.audit import AuditLog, audit_path, describe, tail


def test_where_the_log_goes_and_how_to_turn_it_off(tmp_path):
    assert audit_path(None) == audit_path("") == audit.DEFAULT
    assert audit_path(str(tmp_path / "a.log")) == tmp_path / "a.log"
    for off in ("off", "OFF", "none", "false", "0", " no "):
        assert audit_path(off) is None


def test_an_entry_is_one_json_line_in_a_file_only_its_owner_can_read(tmp_path):
    path = tmp_path / "state" / "audit.log"
    log = AuditLog(path)
    log.record("read_pages", notebook_id="n1", title="Meds", pages="1-3", could_not_read="", missing=None)
    log.record("search", query="anxiety", total=0, shown=0)
    lines = path.read_text().splitlines()
    first = json.loads(lines[0])
    assert first["tool"] == "read_pages" and first["pages"] == "1-3" and "T" in first["ts"]
    assert "could_not_read" not in first and "missing" not in first          # empty fields are left out
    assert oct(path.stat().st_mode)[-3:] == "600" and oct(path.parent.stat().st_mode)[-3:] != "777"
    assert len(lines) == 2


def test_a_log_that_is_off_or_cannot_be_written_never_breaks_the_call(tmp_path):
    AuditLog(None).record("search", query="x")                                # off: nothing, no error
    blocked = tmp_path / "file"
    blocked.write_text("in the way")
    AuditLog(blocked / "sub" / "audit.log").record("search", query="x")      # its folder can't be made
    assert blocked.read_text() == "in the way"


def test_the_log_is_kept_to_a_size_and_the_previous_one_is_kept_once(tmp_path):
    path = tmp_path / "audit.log"
    log = AuditLog(path, max_bytes=400)
    for i in range(30):
        log.record("search", query=f"word {i}")
    old = path.with_name("audit.log.1")
    assert old.exists() and path.stat().st_size <= 400
    assert json.loads(path.read_text().splitlines()[-1])["query"] == "word 29"
    assert len(list(tmp_path.iterdir())) == 2                                  # no .2, .3 ...


def test_tail_reads_the_last_entries_and_skips_what_is_not_an_entry(tmp_path):
    path = tmp_path / "audit.log"
    path.write_text('{"tool": "a"}\nnot json\n[1, 2]\n{"tool": "b"}\n{"tool": "c"}\n')
    assert [e["tool"] for e in tail(path, 2)] == ["b", "c"]
    assert [e["tool"] for e in tail(path, 50)] == ["a", "b", "c"]
    assert tail(tmp_path / "missing.log") == []


def test_an_entry_reads_as_one_line_for_every_kind_of_call():
    ts = "2026-10-17T14:03:09-04:00"
    lines = [describe({"ts": ts, **e}) for e in (
        {"tool": "read_pages", "title": "Meds", "pages": "1-3", "could_not_read": "4"},
        {"tool": "show_match", "title": "Meds", "page": 2, "words": "anxiety", "crops": 2, "opened": True},
        {"tool": "search", "query": "dad", "since": "30d", "total": 31, "shown": 10},
        {"tool": "list_notebooks", "folder": "Health", "query": "recipes", "returned": 3},
        {"tool": "list_folders", "shared": 102}, {"tool": "add_pdf", "added": "Plan.pdf", "folder": "Health"},
        {"tool": "refused", "notebook": "Private"}, {"tool": "future_tool", "x": 1})]
    assert all(line.startswith("2026-10-17 14:03:09  ") and "\n" not in line for line in lines)
    assert "“Meds” pages 1-3 (could not read 4)" in lines[0]
    assert "p. 2 for “anxiety” → 2 crop(s) (opened)" in lines[1]
    assert "“dad” since=30d → 10 of 31 shown" in lines[2]
    assert "folder=Health query=recipes" in lines[3] and "102 notebooks shared" in lines[4]
    assert "“Plan.pdf” into Health" in lines[5] and "asked for “Private”: not shared" in lines[6]
    assert "x=1" in lines[7]
