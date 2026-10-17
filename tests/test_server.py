"""The allow/deny policy and what each tool is allowed to see."""

import asyncio
import json
import sqlite3
from datetime import datetime

import pytest

from viwoods_mcp import paper as paper_mod
from viwoods_mcp import server as V
from viwoods_mcp.paper import Notebook, Page
from viwoods_mcp.policy import Policy, apply_rule, notebook_path
from viwoods_mcp.server import NotShared, ViwoodsAPI


def nb(id, title, folder="", pages=2, written=(1,)):
    b = Notebook(id, title, folder, 1000.0 + len(id))
    for n in range(1, pages + 1):
        b.pages.append(Page(n, f"{id}-p{n}", "u", shot="s.png" if n in written else None))
    return b


BOOKS = [
    nb("h1", "Meds", "Health"),
    nb("h2", "Therapy notes", "Health/Therapy"),
    nb("h3", "Private thoughts", "Health/Private"),
    nb("j1", "2026-09-06", "Journals"),
    nb("r1", "Recipes", "Reference"),
    nb("t1", "Scratch", ""),
]


class FakePaper:
    def __init__(self, books=BOOKS):
        self.books, self.read = books, []

    async def notebooks(self, fresh=False):
        return list(self.books)

    async def png(self, page):
        self.read.append(page.id)
        return b"PNG:" + page.id.encode()


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- the rules

def test_a_rule_covers_its_folder_everything_under_it_and_a_notebook_by_title():
    c = Policy.covers
    assert c("Health", ["Health", "Meds"]) and c("health", ["Health", "Therapy", "Notes"])
    assert c("Health/Therapy", ["Health", "Therapy", "Notes"]) and not c("Health/Therapy", ["Health", "Meds"])
    assert c("Health/Meds", ["Health", "Meds"])                       # a notebook, by title
    assert not c("Health", ["Healthy", "x"]) and not c("Journals", ["Health", "Journals"])
    assert c("*", ["anything", "at all"]) and c("Ref*", ["Reference", "Recipes"])
    assert not c("", ["Health"]) and not c("Health/Meds/Extra", ["Health", "Meds"])


def test_deny_wins_and_what_nothing_allows_is_not_shared():
    p = Policy(allow=["Health"], deny=["Health/Private"])
    assert p.allows(["Health", "Meds"]) and p.allows(["Health", "Therapy", "Notes"])
    assert not p.allows(["Health", "Private", "Thoughts"])             # deny wins over allow
    assert not p.allows(["Journals", "2026-09-06"]) and not p.allows(["Scratch"])
    everything = Policy(allow=["*"], deny=["Journals"])
    assert everything.allows(["Reference", "Recipes"]) and not everything.allows(["Journals", "x"])
    assert not Policy().allows(["Health", "Meds"])                     # no rules: nothing


def test_the_policy_file_round_trips_and_a_missing_or_broken_one_shares_nothing(tmp_path):
    f = tmp_path / "p.json"
    assert Policy.load(f).allow == [] and not Policy.load(f).write
    f.write_text("{not json")
    assert Policy.load(f) == Policy()
    p = Policy(["Health"], ["Journals"], write=True)
    p.save(f)
    assert oct(f.stat().st_mode)[-3:] == "600"
    assert Policy.load(f) == p
    f.write_text(json.dumps({"allow": "Health", "deny": [1, "x"], "write": True}))
    q = Policy.load(f)
    assert q.allow == [] and q.deny == ["1", "x"] and q.write is True


def test_only_a_real_true_switches_writing_on_and_a_malformed_policy_shares_nothing(tmp_path):
    f = tmp_path / "p.json"
    for not_true in ("false", "no", 1, "true", None, [True]):
        f.write_text(json.dumps({"allow": ["*"], "write": not_true}))
        assert Policy.load(f).write is False and Policy.load(f).allow == ["*"]
    for bad in ("[]", '"x"', '{"allow": ["*"], "deny": "Journals"}', '{"allow": ["*"], "deny": {"a": 1}}'):
        f.write_text(bad)                                  # a deny list we can't read must not mean "deny nothing"
        assert Policy.load(f) == Policy() and not Policy.load(f).allows(["Journals", "x"])


def test_only_star_is_a_wildcard_so_brackets_and_question_marks_in_names_match_themselves():
    deny = Policy(allow=["*"], deny=["Journal [private]", "Why?"])
    assert not deny.allows(["Journal [private]", "x"]) and not deny.allows(["Why?"])
    assert deny.allows(["Journal p", "x"]) and deny.allows(["Why"])        # not a character class / any char
    assert Policy.covers("Notes [2024]", ["Notes [2024]", "a"])
    assert not Policy.covers("Notes [2024]", ["Notes 2", "a"])


def test_a_deny_rule_still_matches_a_title_with_edge_whitespace_or_a_slash():
    p = Policy(allow=["*"], deny=["Health/Meds", "Health/Dose 1/2"])
    assert not p.allows(["Health", "Meds "]) and not p.allows(["Health", " Meds"])
    assert not p.allows(["Health", "Dose 1/2"])
    assert p.allows(["Health", "Other"])
    # an allow rule must line up with the names: "Meds 1" does not share a notebook called "Meds 1/2"
    assert not Policy(allow=["Health/Meds 1"]).allows(["Health", "Meds 1/2"])
    assert Policy(allow=["Health/Meds"]).allows(["Health", "Meds "])       # edge whitespace is ignored


def test_apply_rule_keeps_a_rule_in_one_list_only():
    p = Policy(allow=["Health"], deny=["Journals"])
    apply_rule(p, "deny", "health")
    assert p.allow == [] and p.deny == ["Journals", "health"]
    apply_rule(p, "allow", "Journals/")
    assert p.allow == ["Journals"] and p.deny == ["health"]
    apply_rule(p, "remove", "JOURNALS")
    assert p.allow == []
    with pytest.raises(ValueError):
        apply_rule(p, "allow", "  ")


# ---------------------------------------------------------------- what the tools see

def api(policy, books=BOOKS, **kw):
    return ViwoodsAPI(FakePaper(books), policy, log=lambda m: None, **kw)


def test_only_shared_notebooks_and_folders_are_listed_and_the_rest_is_a_count():
    a = api(Policy(allow=["Health"], deny=["Health/Private"]))
    out = run(a.folders())
    assert {f["path"] for f in out["folders"]} == {"Health", "Health/Therapy"}
    health = next(f for f in out["folders"] if f["path"] == "Health")
    assert health["notebooks"] == 2 and health["directly_inside"] == 1       # itself + the Therapy one
    assert out["top_level_notebooks"] == 0
    assert out["notebooks_shared"] == 2 and out["notebooks_not_shared"] == 4
    titles = [n["title"] for n in run(a.notebooks())]
    assert sorted(titles) == ["Meds", "Therapy notes"]
    assert [n["title"] for n in run(a.notebooks("health/therapy"))] == ["Therapy notes"]
    assert run(api(Policy()).notebooks()) == []


def test_a_denied_notebook_cannot_be_read_found_or_told_apart_from_a_missing_one():
    a = api(Policy(allow=["*"], deny=["Journals", "Health/Private"]))
    for ref in ("2026-09-06", "j1", "Private thoughts", "h3"):
        with pytest.raises(NotShared) as denied:
            run(a.read_pages(ref, [1]))
        with pytest.raises(NotShared) as missing:
            run(a.read_pages("no such notebook " + ref, [1]))
        assert str(denied.value).startswith("no shared notebook called")
        assert str(missing.value).startswith("no shared notebook called")
    assert a.paper.read == []                                           # nothing was fetched


def test_read_pages_returns_written_pages_only_and_caps_the_count():
    a = api(Policy(allow=["Health"]))
    meta, images = run(a.read_pages("Meds", [1, 2, 9]))
    assert [n for n, _ in images] == [1] and meta["no_writing"] == [2] and meta["page_count"] == 2
    assert meta["not_in_notebook"] == [9] and meta["could_not_read"] == []
    assert images[0][1] == b"PNG:h1-p1"
    with pytest.raises(ValueError):
        run(a.read_pages("Meds", list(range(1, 12))))
    with pytest.raises(ValueError):
        run(a.read_pages("Meds", []))


def test_an_ambiguous_title_asks_which_but_only_among_shared_notebooks():
    books = [nb("a", "Notes", "Health"), nb("b", "Notes", "Journals"), nb("c", "Notes", "Reference")]
    one = api(Policy(allow=["Health"]), books)
    assert run(one.read_pages("Notes", [1]))[0]["notebook"] == "Notes"      # only one is shared
    two = api(Policy(allow=["Health", "Reference"]), books)
    with pytest.raises(NotShared, match="more than one"):
        run(two.read_pages("Notes", [1]))


def test_policy_edits_apply_to_the_next_call_without_a_restart(tmp_path):
    f = tmp_path / "p.json"
    Policy(allow=["Health"]).save(f)
    a = api(Policy(), policy_file=f)
    assert len(run(a.notebooks())) == 3
    Policy(allow=["Health"], deny=["Health/Therapy"]).save(f)
    assert len(run(a.notebooks())) == 2


def make_index(path, rows):
    con = sqlite3.connect(path)
    con.execute("create table ink_pages(src, nb, page, h, ts, title, status, tries, engine, text, boxes)")
    con.executemany("insert into ink_pages values ('stock',?,?,'h',1,'t','done',0,'e',?,'[]')", rows)
    con.commit()
    con.close()


def test_search_drops_hits_in_notebooks_that_are_not_shared(tmp_path):
    idx = tmp_path / "index.db"
    make_index(idx, [("h1", "h1-p1", "take the blue pills at night"),
                     ("j1", "j1-p1", "the blue door in my dream"),
                     ("h3", "h3-p1", "blue feelings I keep to myself")])
    a = api(Policy(allow=["Health", "Journals"], deny=["Journals", "Health/Private"]), index=idx)
    hits = run(a.search("blue"))
    assert [h["notebook"] for h in hits] == ["Meds"] and hits[0]["page"] == 1
    assert "pills" in hits[0]["text"]
    assert run(a.search("")) == [] and run(api(Policy(allow=["*"]), index=tmp_path / "none").search("x")) == []
    # a % or _ in the query is text, not a wildcard
    make_index(tmp_path / "i2.db", [("h1", "h1-p1", "100% sure"), ("h1", "h1-p2", "no match")])
    assert len(run(api(Policy(allow=["*"]), index=tmp_path / "i2.db").search("100%"))) == 1
    assert run(api(Policy(allow=["*"]), index=tmp_path / "i2.db").search("%")) != []


def test_adding_a_pdf_needs_writing_on_an_allowed_folder_and_a_real_pdf(tmp_path, monkeypatch):
    pdf = tmp_path / "plan.pdf"
    pdf.write_bytes(b"%PDF-1.4 x")
    added = []

    class FakeNotes:
        def __init__(self, tablet):
            pass

        async def add(self, data, title, folder):
            added.append((data, title, folder))
            return title + ".pdf"
    monkeypatch.setattr("viwoods_mcp.pdfnote.PdfNotes", FakeNotes)
    with pytest.raises(PermissionError, match="writing is off"):
        run(api(Policy(allow=["Health"])).add_pdf(str(pdf), "Plan", "Health"))
    on = api(Policy(allow=["Health"], deny=["Health/Private"], write=True))
    with pytest.raises(PermissionError, match="not one you have shared"):
        run(on.add_pdf(str(pdf), "Plan", "Journals"))
    with pytest.raises(PermissionError):
        run(on.add_pdf(str(pdf), "Plan", "Health/Private"))
    with pytest.raises(ValueError):
        run(on.add_pdf(str(tmp_path / "missing.pdf"), "Plan", "Health"))
    txt = tmp_path / "x.txt"
    txt.write_text("hi")
    with pytest.raises(ValueError):
        run(on.add_pdf(str(txt), "Plan", "Health"))
    out = run(on.add_pdf(str(pdf), "Plan", "Health"))
    assert out["added"] == "Plan.pdf" and added == [(b"%PDF-1.4 x", "Plan", "Health")]


def test_the_show_text_counts_what_a_policy_exposes():
    text = V._show(Policy(allow=["Health"], deny=["Health/Private"]), BOOKS)
    assert "2 of 6 notebooks would be shared" in text and "Health: 2 of 3 shared" in text
    assert "Journals: 0 of 1 shared" in text
    assert notebook_path(BOOKS[1]) == ["Health", "Therapy", "Therapy notes"]


# ---------------------------------------------------------------- the bugs found in review

def test_a_page_the_tablet_would_not_give_up_is_not_reported_as_blank():
    class Flaky(FakePaper):
        async def png(self, page):
            if page.n == 1:
                raise paper_mod.PaperError("adb said no")
            return await super().png(page)
    books = [nb("f1", "Flaky", "Health", pages=3, written=(1, 2))]
    meta, images = run(ViwoodsAPI(Flaky(books), Policy(allow=["*"]), log=lambda m: None).read_pages("Flaky", [1, 2, 3, 4]))
    assert meta["could_not_read"] == [1] and meta["no_writing"] == [3] and meta["not_in_notebook"] == [4]
    assert [n for n, _ in images] == [2]
    text = V.describe(meta)
    assert "Could not read page 1" in text and "No writing on page 3" in text and "Page 4 is past the end (it has 3)" in text


def test_search_finds_non_ascii_text_whatever_its_case(tmp_path):
    make_index(tmp_path / "i.db", [("h1", "h1-p1", "Über die Straße gehen"), ("h1", "h1-p2", "ÉCOLE du soir")])
    a = api(Policy(allow=["*"]), index=tmp_path / "i.db")
    assert [h["page"] for h in run(a.search("über"))] == [1]
    assert [h["page"] for h in run(a.search("École"))] == [2]
    assert [h["page"] for h in run(a.search("ÜBER"))] == [1]


def test_unshared_notebooks_cannot_crowd_shared_ones_out_of_the_results(tmp_path):
    rows = [("j1", "j1-p1", f"blue {i}") for i in range(500)] + [("h1", "h1-p1", "blue pills")]
    idx = tmp_path / "i.db"
    make_index(idx, rows)
    con = sqlite3.connect(idx)
    con.execute("update ink_pages set ts = case when nb = 'j1' then 9 else 1 end")     # the denied ones are newer
    con.commit()
    con.close()
    a = api(Policy(allow=["*"], deny=["Journals"]), index=idx)
    assert [h["notebook"] for h in run(a.search("blue"))] == ["Meds"]


def test_search_limit_is_at_least_one_and_at_most_the_cap(tmp_path):
    make_index(tmp_path / "i.db", [("h1", f"h1-p{i % 2 + 1}", f"blue {i}") for i in range(30)])
    a = api(Policy(allow=["*"]), index=tmp_path / "i.db")
    assert len(run(a.search("blue", 0))) == 1 and len(run(a.search("blue", -5))) == 1
    assert len(run(a.search("blue", 3))) == 3 and len(run(a.search("blue", 500))) == V.MAX_RESULTS


def test_snippets_say_when_they_are_cut():
    long = "x" * 100 + " blue " + "y" * 400
    make = lambda t: [("h1", "h1-p1", t)]
    import tempfile, pathlib
    d = pathlib.Path(tempfile.mkdtemp())
    make_index(d / "i.db", make(long) + [("h1", "h1-p2", "blue")])
    hits = run(api(Policy(allow=["*"]), index=d / "i.db").search("blue"))
    by_page = {h["page"]: h["text"] for h in hits}
    assert by_page[1].startswith("…") and by_page[1].endswith("…") and by_page[2] == "blue"


def test_page_numbers_are_shown_as_ranges_and_times_with_their_offset():
    assert V.ranges([1, 2, 3, 5, 7, 8]) == "1-3, 5, 7-8" and V.ranges([]) == "none" and V.ranges([4]) == "4"
    assert V.ranges([3, 1, 2, 2]) == "1-3"
    stamp = V.when(1_760_000_000)
    assert abs(datetime.fromisoformat(stamp).timestamp() - 1_760_000_000) < 60     # to the minute, offset kept
    assert len(stamp) == len("2025-10-09T10:13+00:00")
    out = run(api(Policy(allow=["Health"])).notebooks())
    meds = next(n for n in out if n["title"] == "Meds")
    assert meds["page_count"] == 2 and meds["written_pages"] == "1" and "pages" not in meds
    assert abs(datetime.fromisoformat(meds["modified"]).timestamp() - BOOKS[0].modified) < 60


def test_the_tools_report_expected_failures_as_errors_with_their_message(monkeypatch, tmp_path):
    from mcp.server.mcpserver.exceptions import ToolError
    monkeypatch.setattr(V, "_API", api(Policy(allow=["Health"]), index=tmp_path / "none"))
    srv = V.build()
    with pytest.raises(ToolError, match="no shared notebook called"):
        run(srv.call_tool("viwoods_read_pages", {"notebook": "Journals", "pages": [1]}))
    ok = run(srv.call_tool("viwoods_read_pages", {"notebook": "Meds", "pages": [1, 2]}))
    blocks = ok if isinstance(ok, list) else ok.content
    assert blocks[0].text.startswith("“Meds”, 2 pages. No writing on page 2.")
    assert [c.type for c in blocks] == ["text", "text", "image"]
    with pytest.raises(ToolError, match="index isn't there"):
        run(srv.call_tool("viwoods_search_handwriting", {"query": "x"}))
