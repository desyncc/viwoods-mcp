"""The allow/deny policy and what each tool is allowed to see."""

import asyncio
import io
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from viwoods_mcp import crop as crop_mod
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

    async def image(self, page):
        self.read.append(page.id)
        return Image.new("RGB", (1800, 2400), "white")


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
    con.executemany("insert into ink_pages values ('stock',?,?,'h',1,'t','done',0,'e',?,?)",
                    [(*r, "[]") if len(r) == 3 else r for r in rows])
    con.commit()
    con.close()


def test_search_drops_hits_in_notebooks_that_are_not_shared(tmp_path):
    idx = tmp_path / "index.db"
    make_index(idx, [("h1", "h1-p1", "take the blue pills at night"),
                     ("j1", "j1-p1", "the blue door in my dream"),
                     ("h3", "h3-p1", "blue feelings I keep to myself")])
    a = api(Policy(allow=["Health", "Journals"], deny=["Journals", "Health/Private"]), index=idx)
    hits = run(a.search("blue"))["results"]
    assert [h["notebook"] for h in hits] == ["Meds"] and hits[0]["page"] == 1
    assert "pills" in hits[0]["text"]
    assert run(a.search(""))["results"] == [] and run(api(Policy(allow=["*"]), index=tmp_path / "none").search("x"))["results"] == []
    # a % or _ in the query is text, not a wildcard
    make_index(tmp_path / "i2.db", [("h1", "h1-p1", "100% sure"), ("h1", "h1-p2", "no match")])
    assert len(run(api(Policy(allow=["*"]), index=tmp_path / "i2.db").search("100%"))["results"]) == 1
    assert run(api(Policy(allow=["*"]), index=tmp_path / "i2.db").search("%"))["results"] != []


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
    assert [h["page"] for h in run(a.search("über"))["results"]] == [1]
    assert [h["page"] for h in run(a.search("École"))["results"]] == [2]
    assert [h["page"] for h in run(a.search("ÜBER"))["results"]] == [1]


def test_unshared_notebooks_cannot_crowd_shared_ones_out_of_the_results(tmp_path):
    rows = [("j1", "j1-p1", f"blue {i}") for i in range(500)] + [("h1", "h1-p1", "blue pills")]
    idx = tmp_path / "i.db"
    make_index(idx, rows)
    con = sqlite3.connect(idx)
    con.execute("update ink_pages set ts = case when nb = 'j1' then 9 else 1 end")     # the denied ones are newer
    con.commit()
    con.close()
    a = api(Policy(allow=["*"], deny=["Journals"]), index=idx)
    assert [h["notebook"] for h in run(a.search("blue"))["results"]] == ["Meds"]


def test_search_limit_is_at_least_one_and_at_most_the_cap(tmp_path):
    make_index(tmp_path / "i.db", [("h1", f"h1-p{i % 2 + 1}", f"blue {i}") for i in range(30)])
    a = api(Policy(allow=["*"]), index=tmp_path / "i.db")
    assert len(run(a.search("blue", 0))["results"]) == 1 and len(run(a.search("blue", -5))["results"]) == 1
    assert len(run(a.search("blue", 3))["results"]) == 3 and len(run(a.search("blue", 500))["results"]) == V.MAX_RESULTS


def test_snippets_say_when_they_are_cut():
    long = "x" * 100 + " blue " + "y" * 400
    make = lambda t: [("h1", "h1-p1", t)]
    import tempfile, pathlib
    d = pathlib.Path(tempfile.mkdtemp())
    make_index(d / "i.db", make(long) + [("h1", "h1-p2", "blue")])
    hits = run(api(Policy(allow=["*"]), index=d / "i.db").search("blue"))["results"]
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


# ---------------------------------------------------------------- citations, counts and the zoomed crop

def test_a_reference_names_the_folder_notebook_page_and_date():
    book = nb("c1", "Dad Notes", "Journals/2026")
    assert V.cite(book, 3) == f"Journals › 2026 › Dad Notes · p. 3 · changed {V.when(book.modified)[:10]}"
    assert V.cite(nb("c2", "Scratch", "")).startswith("Scratch · changed ")           # top level, no page


def test_search_hits_carry_references_and_say_how_many_there_were_in_all(tmp_path):
    rows = [("h1", "h1-p1", f"blue {i}") for i in range(7)] + [("j1", "j1-p1", "blue hidden")]
    make_index(tmp_path / "i.db", rows)
    a = api(Policy(allow=["*"], deny=["Journals"]), index=tmp_path / "i.db")
    found = run(a.search("blue", 3))
    assert found["total_matches"] == 7 and found["shown"] == 3 and len(found["results"]) == 3   # hidden not counted
    assert found["results"][0]["reference"].startswith("Health › Meds · p. 1 · changed ")
    assert "Showing 3 of 7" in found["note"]
    everything = run(a.search("blue", 20))
    assert everything["total_matches"] == everything["shown"] == 7 and "note" not in everything
    assert run(a.search("nothing like this")) == {"total_matches": 0, "shown": 0, "results": []}


def test_read_pages_images_are_captioned_with_their_reference():
    meta, images = run(api(Policy(allow=["Health"])).read_pages("Meds", [1, 2]))
    assert list(meta["references"]) == [1] and meta["references"][1].startswith("Health › Meds · p. 1")


WORDS = [("I", 100, 100, 130, 160), ("feel", 140, 100, 260, 160), ("anxious", 270, 100, 500, 160),
         ("tonight", 510, 100, 700, 160), ("later", 100, 1500, 260, 1560), ("anxiety", 270, 1500, 460, 1560)]


def crop_api(tmp_path, books=BOOKS, policy=None, opened=None, **kw):
    boxes = json.dumps([list(w) for w in WORDS])
    make_index(tmp_path / "i.db", [("h1", "h1-p1", "I feel anxious tonight", boxes)])
    async def record(path):
        opened.append(path)
    return api(policy or Policy(allow=["*"], deny=["Journals"]), books, index=tmp_path / "i.db",
               crop_dir=tmp_path / "crops", open_image=record if opened is not None else None, **kw)


def test_show_match_crops_each_place_the_words_are_written_and_saves_them_privately(tmp_path):
    a = crop_api(tmp_path)
    meta, shots = run(a.show_match("Meds", 1, "anxi"))
    assert meta["matched_words"] == 2 and meta["spots_on_page"] == 2 and meta["shown"] == 2 == len(shots)
    assert meta["reference"].startswith("Health › Meds · p. 1")
    saved = [Path(p) for p in meta["saved"]]
    assert [p.read_bytes() for p in saved] == shots
    assert oct(saved[0].parent.stat().st_mode)[-3:] == "700" and oct(saved[0].stat().st_mode)[-3:] == "600"
    assert Image.open(io.BytesIO(shots[0])).width > 700


def test_show_match_opens_the_crops_only_when_that_is_switched_on(tmp_path):
    (tmp_path / "off").mkdir()
    (tmp_path / "on").mkdir()
    assert run(crop_api(tmp_path / "off").show_match("Meds", 1, "anxious"))[0]["shown"] == 1   # open_image is None
    opened = []
    meta, _ = run(crop_api(tmp_path / "on", opened=opened).show_match("Meds", 1, "anxious"))
    assert [str(p) for p in opened] == meta["saved"]
    assert V.truthy("1") and V.truthy(" On ") and V.truthy("true")
    assert not V.truthy("0") and not V.truthy("") and not V.truthy(None) and not V.truthy("off")


def test_show_match_says_what_is_missing_rather_than_guessing(tmp_path):
    a = crop_api(tmp_path)
    for args, message in [(("Meds", 2, "anxious"), "nothing written"), (("Meds", 9, "anxious"), "no page 9"),
                          (("Meds", 1, "  "), "which words"), (("Meds", 1, "pizza"), "none of pizza"),
                          (("Therapy notes", 1, "anxious"), "isn't in the handwriting index")]:
        with pytest.raises(ValueError, match=message):
            run(a.show_match(*args))
    no_index = api(Policy(allow=["*"]), index=tmp_path / "missing.db", crop_dir=tmp_path / "c")
    with pytest.raises(ValueError, match="index, which isn't there"):
        run(no_index.show_match("Meds", 1, "anxious"))


def test_show_match_cannot_reach_a_notebook_that_is_not_shared(tmp_path):
    a = crop_api(tmp_path, policy=Policy(allow=["*"], deny=["Health"]))
    with pytest.raises(NotShared, match="no shared notebook called"):
        run(a.show_match("Meds", 1, "anxious"))
    assert a.paper.read == [] and not (tmp_path / "crops").exists()                  # nothing fetched or saved


def test_only_the_newest_crops_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(V, "MAX_SAVED", 3)
    a = crop_api(tmp_path)
    for _ in range(4):
        run(a.show_match("Meds", 1, "anxi"))                      # two crops each
    assert len(list((tmp_path / "crops").glob("*.png"))) == 3


def test_the_show_match_tool_returns_a_caption_with_the_reference_and_the_images(tmp_path, monkeypatch):
    monkeypatch.setattr(V, "_API", crop_api(tmp_path))
    blocks = run(V.build().call_tool("viwoods_show_match", {"notebook": "Meds", "page": 1, "words": "anxious"}))
    blocks = blocks if isinstance(blocks, list) else blocks.content
    assert [b.type for b in blocks] == ["text", "image"]
    assert "Health › Meds · p. 1" in blocks[0].text and "outlined in red" in blocks[0].text and "Saved to" in blocks[0].text


def test_show_match_gives_the_crop_every_word_box_on_the_page_for_the_line_widths(tmp_path, monkeypatch):
    seen = {}
    real = V.crop.zoom

    def spy(img, boxes, mark=True, context=(), max_w=crop_mod.MAX_W):
        seen["matches"], seen["context"], seen["max_w"] = list(boxes), list(context), max_w
        return real(img, boxes, mark, context, max_w)
    monkeypatch.setattr(V.crop, "zoom", spy)
    run(crop_api(tmp_path).show_match("Meds", 1, "anxious"))
    assert seen["matches"] == [(270, 100, 500, 160)]
    assert len(seen["context"]) == len(WORDS)                    # the words that weren't asked for too
    assert seen["max_w"] == crop_mod.MAX_W
    monkeypatch.setenv("VIWOODS_MCP_CROP_WIDTH", "900")
    (tmp_path / "again").mkdir()
    run(crop_api(tmp_path / "again").show_match("Meds", 1, "anxious"))
    assert seen["max_w"] == 900


def test_the_crop_width_setting_is_clamped_and_a_bad_value_means_the_default():
    assert V.crop_width("900") == 900 and V.crop_width("100") == 600 and V.crop_width("99999") == 1800
    assert V.crop_width("wide") == V.crop.MAX_W and V.crop_width("") == V.crop.MAX_W


# ---------------------------------------------------------------- time filters

def day(d, h=12) -> float:
    return datetime(2026, 10, d, h).astimezone().timestamp()


def timed(id, title, folder, times, written=None):
    """A notebook whose pages changed at `times` (None: the app gave no time for that page)."""
    book = nb(id, title, folder, pages=len(times), written=written or range(1, len(times) + 1))
    for page, t in zip(book.pages, times):
        page.modified = t or 0.0
    return book


TIMED = [timed("a", "Journal", "Journals", [day(1), day(10), day(11)]),
         timed("b", "Recent", "Health", [day(12)]),
         timed("c", "Ancient", "Health", [day(1), day(2)]),
         timed("d", "Untimed", "Health", [None])]
TIMED[3].modified = day(11)                               # no page time: the notebook's is used


def test_list_notebooks_can_be_limited_to_those_with_pages_changed_in_a_time():
    a = api(Policy(allow=["*"]), TIMED)
    assert all("pages_changed" not in n for n in run(a.notebooks()))
    out = {n["title"]: n for n in run(a.notebooks(since="2026-10-09", until="2026-10-12"))}
    assert set(out) == {"Journal", "Recent", "Untimed"}                        # not Ancient
    assert out["Journal"]["pages_changed"] == "2-3" and out["Recent"]["pages_changed"] == "1"
    assert out["Untimed"]["pages_changed"] == "1"                              # falls back to the notebook's time
    assert {n["title"] for n in run(a.notebooks(since="2026-10-12"))} == {"Recent"}
    assert {n["title"] for n in run(a.notebooks("Health", until="2026-10-02"))} == {"Ancient"}
    assert run(a.notebooks(since="2026-11-01")) == []
    with pytest.raises(ValueError, match="can't read"):
        run(a.notebooks(since="whenever"))


def test_a_time_filter_never_shows_a_notebook_that_is_not_shared():
    a = api(Policy(allow=["*"], deny=["Journals"]), TIMED)
    assert "Journal" not in {n["title"] for n in run(a.notebooks(since="2026-10-01"))}


def test_search_can_be_limited_to_pages_that_changed_in_a_time_and_lists_the_newest_page_first(tmp_path):
    rows = [("a", "a-p1", "blue one"), ("a", "a-p2", "blue two"), ("a", "a-p3", "blue three"),
            ("b", "b-p1", "blue four"), ("c", "c-p1", "blue five")]
    make_index(tmp_path / "i.db", rows)
    a = api(Policy(allow=["*"]), TIMED, index=tmp_path / "i.db")
    everything = run(a.search("blue"))
    assert [h["page"] for h in everything["results"]] == [1, 3, 2, 1, 1]      # by page time, newest first
    assert [h["notebook"] for h in everything["results"]][:3] == ["Recent", "Journal", "Journal"]
    assert datetime.fromisoformat(everything["results"][0]["changed"]).timestamp() - day(12) < 60
    week = run(a.search("blue", since="2026-10-09"))
    assert week["total_matches"] == 3 and [h["text"] for h in week["results"]] == ["blue four", "blue three", "blue two"]
    assert run(a.search("blue", limit=1, since="2026-10-09"))["total_matches"] == 3      # counts what the filter keeps
    assert run(a.search("blue", since="2026-10-01", until="2026-10-02"))["total_matches"] == 2   # a p1 and c p1
    assert run(a.search("blue", since="2027-01-01"))["results"] == []


def test_the_search_tool_says_when_nothing_matched_in_the_time(tmp_path, monkeypatch):
    make_index(tmp_path / "i.db", [("a", "a-p1", "blue one")])
    monkeypatch.setattr(V, "_API", api(Policy(allow=["*"]), TIMED, index=tmp_path / "i.db"))
    srv = V.build()

    def ask(**kw):
        return str(run(srv.call_tool("viwoods_search_handwriting", {"query": "blue", **kw})))
    assert "in that time" in ask(since="2027-01-01")
    assert "in that time" not in ask(since="2026-10-01") and "reference" in ask(since="2026-10-01")
    with pytest.raises(Exception, match="can't read"):
        ask(since="sometime")


def test_a_notebooks_modified_and_the_list_order_follow_its_newest_page():
    lagging = timed("x", "Lagging", "Health", [day(12)])
    lagging.modified = day(1)                                  # the app's own time for it is old
    steady = timed("y", "Steady", "Health", [day(5)])
    steady.modified = day(5)
    out = run(api(Policy(allow=["*"]), [steady, lagging]).notebooks())
    assert [n["title"] for n in out] == ["Lagging", "Steady"]               # not by the notebook's own time
    assert abs(datetime.fromisoformat(out[0]["modified"]).timestamp() - day(12)) < 60


def test_a_reference_dates_the_page_it_names_and_a_bare_notebook_by_its_newest_page():
    book = timed("z", "Dated", "Journals", [day(1), day(12)])
    book.modified = day(1)
    stamp = lambda t: V.when(t)[:10]
    assert V.cite(book, 1).endswith(f"p. 1 · changed {stamp(day(1))}")
    assert V.cite(book, 2).endswith(f"p. 2 · changed {stamp(day(12))}")
    assert V.cite(book).endswith(f"changed {stamp(day(12))}") and "p." not in V.cite(book)
