"""The allow/deny policy and what each tool is allowed to see."""

import asyncio
import json
import sqlite3

import pytest

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
    f.write_text(json.dumps({"allow": "Health", "deny": [1, "x"], "write": 1}))
    q = Policy.load(f)
    assert q.allow == [] and q.deny == ["1", "x"] and q.write is True


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
    assert [n for n, _ in images] == [1] and meta["nothing_on"] == [2, 9] and meta["of"] == 2
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
