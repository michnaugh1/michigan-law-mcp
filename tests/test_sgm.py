"""Sentencing Guidelines Manual text units (src/mlaw/sources/sgm.py).

Everything runs offline: the saved 2026 manual (tests/data/.../sgm/sgm.pdf, git-ignored) and the saved
content-API excerpt, through fake clients. Tests that need the PDF skip when it is absent."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from mlaw.citations import normalize_citation
from mlaw.pipeline import run_ingest
from mlaw.service import get_document
from mlaw.sources import sgm
from mlaw.sources.courts_api import API_BASE
from mlaw.web import PoliteClient, RobotsDisallowed

DATA = Path(__file__).parent / "data" / "court_rules" / "2026-09-29" / "sgm"
PDF = DATA / "sgm.pdf"
EXCERPT = DATA / "sentencing-guidelines-manuals.api.excerpt.json"

needs_pdf = pytest.mark.skipif(
    not PDF.is_file(),
    reason=f"{PDF} not present (git-ignored; save the 2026 Sentencing Guidelines Manual PDF there to run)",
)

PERSON = "Crimes Against a Person"
PROPERTY = "Crimes Against Property"
CS = "Controlled Substance Crimes"
ORDER = "Public Order and Public Trust"
SAFETY = "Crimes Against Public Safety"
GROUPS = [PERSON, PROPERTY, CS, ORDER, SAFETY]
COVERAGE = {
    PERSON: set(range(1, 15)) | set(range(16, 21)),
    PROPERTY: {1, 2, 3, 4, 9, 10, 12, 13, 14, 16, 19, 20},
    CS: {1, 2, 3, 12, 13, 14, 15, 19, 20},
    ORDER: {1, 3, 4, 9, 10, 12, 13, 14, 16, 19, 20},
    SAFETY: {1, 3, 4, 9, 10, 12, 13, 14, 16, 18, 19, 20},
}
# The 2026 manual prints these OVs with different text in different offense groups (verified against the
# raw pdftotext output, not a parser artifact):
#   OV 3  "A victim was killed, and death results" (Person, ...) vs "..., where death results" (Public Order/Trust)
#   OV 12 the 10- and 5-point rows are combined rows in Person/Property/CS, split into four rows in Order/Safety
#   OV 16 Person adds "Score for a crime against a person only if ... MCL 750.110a (home invasion)"
#   OV 18 cites "MCL 777.22(1)" under Person, "MCL 777.22(5)" under Public Safety
DIFFERING_OVS = {3, 12, 16, 18}


@pytest.fixture(scope="module")
def src():
    if not PDF.is_file():
        pytest.skip(f"{PDF} not present")
    return sgm.SgmFilesSource(PDF)


@pytest.fixture(scope="module")
def manual(src):
    return src.manual


@pytest.fixture(scope="module")
def docs(manual):
    return {d.citation: d for d in manual.docs}


# ---------------------------------------------------------------------------------------------- basics


@needs_pdf
def test_current_through_and_page_ranges(manual, src):
    assert manual.current_through == "2026-05-20"
    assert manual.grid_pages == (125, 134)
    assert manual.felony_list_pages == (135, 212)
    assert manual.edition_label is None
    assert src.currency == "SGM 2026 (current through 2026-05-20)"
    assert src.name == "sgm-files" and src.source_kinds == frozenset({"sgm"}) and src.covered_chapters is None
    assert list(src.iter_public_acts()) == [] and list(src.iter_ranges()) == []


@needs_pdf
def test_pdf_to_text_pages():
    pages = sgm._text_pages(sgm.pdf_to_text(PDF))
    assert len(pages) == 212
    assert "Online version current through 5/20/2026" in pages[0]


def test_pdf_to_text_missing_tool(monkeypatch, tmp_path):
    monkeypatch.setattr(sgm.shutil, "which", lambda name: None)
    with pytest.raises(sgm.SgmParseError, match="pdftotext not found"):
        sgm.pdf_to_text(tmp_path / "x.pdf")


def test_locate_requires_cover_stamp():
    with pytest.raises(sgm.SgmParseError, match="current through"):
        sgm._locate(["no stamp here"], [])


@needs_pdf
def test_all_units_once_and_canonical(manual):
    cits = [d.citation for d in manual.docs]
    assert len(cits) == len(set(cits))
    for n in range(1, 8):
        assert cits.count(f"SGM PRV {n}") == 1
    for n in range(1, 21):
        assert n in manual.ov_groups, f"OV {n} not printed in any group"
        assert cits.count(f"SGM OV {n}") == 1
    for d in manual.docs:
        c = normalize_citation(d.citation)
        assert c is not None and c.canonical == d.citation and c.source == "sgm"
        assert d.source == "sgm" and d.kind == "guidelines"
        assert re.fullmatch(re.escape(sgm.DEFAULT_URL) + r"#page=\d+", d.url)


def _sections(text: str) -> list[tuple[str, str]]:
    """(header line, section text) for each "== Version for: ... ==" section."""
    parts = re.split(r"^(== Version for: .+ ==)$", text, flags=re.M)
    return list(zip(parts[1::2], parts[2::2]))


@needs_pdf
def test_ov_versions_by_group(manual, src, docs):
    """OVs the manual prints differently per crime group are one unit holding every distinct version,
    labeled with its groups; that is expected, so no warning and the source stays complete."""
    assert manual.warnings == [] and src.complete is True
    assert set(manual.ov_variants) == DIFFERING_OVS
    for n in DIFFERING_OVS:
        assert list(manual.ov_variants[n]) == manual.ov_groups[n]
        d = docs[f"SGM OV {n}"]
        assert d.text.startswith("Scored for: " + "; ".join(manual.ov_groups[n]) + "\n\n== Version for: ")
        grouped = [g for h, _ in _sections(d.text) for g in h[len("== Version for: "):-3].split("; ")]
        assert sorted(grouped) == sorted(manual.ov_groups[n])  # every group in exactly one version

    ov16 = _sections(docs["SGM OV 16"].text)
    assert len(ov16) == 2 and docs["SGM OV 16"].text.count("== Version for:") == 2
    assert ov16[0][0] == "== Version for: Crimes Against a Person =="
    assert "MCL 750.110a (home invasion)" in ov16[0][1]
    assert "home invasion" not in ov16[1][1]
    assert docs["SGM OV 16"].catchline == "Property Obtained, Damaged, Lost, or Destroyed"

    ov18 = dict(_sections(docs["SGM OV 18"].text))
    assert "MCL 777.22(1)" in ov18[f"== Version for: {PERSON} =="]
    assert "MCL 777.22(5)" in ov18[f"== Version for: {SAFETY} =="]

    ov3 = _sections(docs["SGM OV 3"].text)
    assert "and death results" in ov3[0][1] and "where death results" in dict(ov3)[f"== Version for: {ORDER} =="]
    ov12 = dict(_sections(docs["SGM OV 12"].text))
    assert "10 — Three or more contemporaneous" in ov12[f"== Version for: {ORDER}; {SAFETY} =="]

    assert "== Version for:" not in docs["SGM OV 1"].text
    assert "\n\n" not in docs["SGM OV 1"].text


def test_differing_titles_warn(monkeypatch):
    """A new kind of disagreement (titles) is still a warning and makes the source incomplete."""
    m = sgm.SgmManual(docs=[], current_through="2026-05-20", edition_label=None, grid_pages=(1, 1),
                      felony_list_pages=(2, 2), warnings=["SGM OV 7: title differs between groups: ..."])

    class Src(sgm._SgmSourceBase):
        name = "t"

        def _load(self):
            return m

    s = Src()
    list(s.iter_documents())
    assert s.complete is False


@needs_pdf
def test_ov_group_coverage(manual):
    for n in range(1, 21):
        expected = [g for g in GROUPS if n in COVERAGE[g]]
        assert manual.ov_groups[n] == expected, f"OV {n}"


# ---------------------------------------------------------------------------------------------- content


@needs_pdf
def test_prv1(docs):
    d = docs["SGM PRV 1"]
    assert d.catchline == "Prior High Severity Felony Convictions"
    lines = d.text.splitlines()
    assert lines[0] == "MCL 777.51"
    assert "75 — 3 or more prior high severity felony convictions. MCL 777.51(1)(a)." in lines
    assert "50 — 2 prior high severity felony convictions. MCL 777.51(1)(b)." in lines
    assert "25 — 1 prior high severity felony conviction. MCL 777.51(1)(c)." in lines
    assert "0 — No prior high severity felony convictions. MCL 777.51(1)(d)." in lines
    assert "Instructions" in lines
    # footnote reference kept as a marker, footnote text kept and labeled
    assert "See MCL 777.50(1).[Note 1]" in d.text
    assert "Note 1: See Step I, Part D in the General Information and Instructions section of this manual " \
           "for a detailed discussion of the 10-year-gap rule." in lines
    assert "Note 3: 2006 PA 655, effective January 9, 2007." in lines
    assert d.url.endswith("#page=39")


@needs_pdf
def test_scored_for(docs):
    assert docs["SGM OV 15"].text.splitlines()[0] == "Scored for: Controlled Substance Crimes"
    assert docs["SGM OV 1"].text.splitlines()[0] == "Scored for: " + "; ".join(GROUPS)
    assert docs["SGM OV 15"].text.splitlines()[1] == "MCL 777.45"


@needs_pdf
def test_ov10(docs):
    d = docs["SGM OV 10"]
    assert d.catchline == "Exploitation of Vulnerable Victim"
    assert "15 — Predatory conduct was involved. MCL 777.40(1)(a)." in d.text.splitlines()


@needs_pdf
def test_shaded_rows_marked(docs):
    """Rows the manual shades ("not scored for current offenses") are marked; others are not."""
    rows = [ln for ln in docs["SGM OV 13"].text.splitlines() if re.match(r"^\d+ — ", ln)]
    shaded = [ln for ln in rows if ln.endswith("[shaded row]")]
    assert len(rows) == 10 and len(shaded) == 3
    assert all("committed before" in ln for ln in shaded)
    assert not any(ln.endswith("[shaded row]") for ln in docs["SGM PRV 1"].text.splitlines())


@needs_pdf
def test_superscripts_are_not_merged_into_numbers(docs):
    step5 = docs["SGM Instructions Step V"].text
    assert "MCL 769.34(3).[Note 10]" in step5  # pdftotext: "MCL 769.34(3).10"
    assert "Chapter 1.[Note 12]" in step5  # pdftotext: "Chapter 1.12"
    assert "Note 10: Note that the term “departure sentence”" in step5  # footnote on the following page
    ov19 = docs["SGM OV 19"]
    assert ov19.catchline.endswith("Rendering of Emergency Services") and "[Note" not in ov19.catchline
    assert "Title: Threat to Security" in ov19.text and "Emergency Services[Note 1]" in ov19.text


@needs_pdf
def test_every_note_reference_has_text(docs):
    for d in docs.values():
        refs = set(int(n) for n in re.findall(r"\[Note (\d+)\]", d.text))
        notes = set(int(n) for n in re.findall(r"^Note (\d+): \S", d.text, re.M))
        assert refs == notes, d.citation


@needs_pdf
def test_no_page_furniture(docs):
    for d in docs.values():
        for ln in d.text.splitlines():
            if "== Version for:" not in d.text and not d.citation.startswith("SGM Grid"):  # grids: Markdown
                assert ln.strip(), f"{d.citation}: blank line"
            assert not re.fullmatch(r"\s*\d{1,3}\s*", ln), f"{d.citation}: page-number-only line {ln!r}"
            assert not re.search(r"\bPRVs$", ln), f"{d.citation}: tab label {ln!r}"
            assert ln != "Sentencing Grids"
        for bad in ("OVs - Crimes", "Score each offense variable", "Score each prior record variable",
                    "Continued on next page", "continued)", "General Information and Instructions\n"):
            assert bad not in d.text, f"{d.citation}: {bad!r}"
        if not d.citation.startswith("SGM Instructions"):
            assert "Sentencing Grids" not in d.text
    # In the Steps "Sentencing Grids" only appears as manual content (Step IV, Part A's heading).
    assert "A. Using the Sentencing Grids" in docs["SGM Instructions Step IV"].text


@needs_pdf
def test_steps(docs):
    steps = [c for c in docs if c.startswith("SGM Instructions Step ")]
    assert steps == [f"SGM Instructions Step {r}" for r in ("I", "II", "III", "IV", "V")]
    for c in steps:
        n = normalize_citation(c)
        assert n is not None and n.canonical == c
    assert docs["SGM Instructions Step I"].catchline == "Score the Prior Record Variables"
    assert docs["SGM Instructions Step III"].catchline == "Identify the Crime Class and Proper Sentencing Grid"
    s1 = docs["SGM Instructions Step I"].text
    assert s1.startswith("A. All seven prior record variables (PRVs) should be scored for all offenses.")
    assert "D. Ten-year gap rule." in s1
    assert "Legislative History" not in docs["SGM Instructions Step V"].text
    assert "STEP II" not in s1


# ---------------------------------------------------------------------------------------------- web


class FakeResponse:
    def __init__(self, text: str = "", content: bytes = b""):
        self.text = text
        self.content = content


class FakeClient:
    def __init__(self):
        self.urls: list[str] = []

    def get(self, url, headers=None, **kw):
        self.urls.append(url)
        if url.startswith(API_BASE):
            assert (headers or {}).get("Accept") == "application/json"
            return FakeResponse(text=EXCERPT.read_text(encoding="utf-8"))
        if url.endswith("/sgm.pdf"):
            return FakeResponse(content=PDF.read_bytes())
        raise AssertionError(f"unexpected URL {url}")


@needs_pdf
def test_web_source_matches_files_source(manual):
    client = FakeClient()
    web = sgm.SgmWebSource(client=client)
    got = list(web.iter_documents())
    assert web.pdf_url == ("https://www.courts.michigan.gov/49ddcf/siteassets/offices/mji/"
                           "felony-sentencing-online-resources/sgm.pdf")
    assert web.edition_label == "Sentencing Guidelines Manual 2026"
    assert web.manual.edition_label == "Sentencing Guidelines Manual 2026"
    assert web.currency == "SGM 2026 (current through 2026-05-20)"
    assert web.name == "sgm-web" and web.complete is True
    assert got == manual.docs
    assert len(client.urls) == 2 and "former" not in " ".join(client.urls).lower()


def test_web_source_rejects_non_pdf():
    class C(FakeClient):
        def get(self, url, headers=None, **kw):
            if url.endswith("/sgm.pdf"):
                return FakeResponse(content=b"<html>not a pdf</html>")
            return super().get(url, headers=headers)

    with pytest.raises(sgm.SgmParseError, match="did not return a PDF"):
        list(sgm.SgmWebSource(client=C()).iter_documents())


def test_web_source_honors_robots():
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /")
        raise AssertionError(f"request made despite robots.txt: {req.url}")

    client = PoliteClient(transport=httpx.MockTransport(handler), user_agent="test-agent/1.0",
                          sleep=lambda s: None)
    with pytest.raises(RobotsDisallowed):
        list(sgm.SgmWebSource(client=client).iter_documents())
    assert seen == ["https://www.courts.michigan.gov/robots.txt"]


# ---------------------------------------------------------------------------------------------- ingest


@needs_pdf
def test_ingest(conn, src):
    r = run_ingest(conn, src, now="2026-09-29T06:00:00Z")
    assert r.docs_new == len(src.manual.docs) and r.docs_removed == 0
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (r.run_id,)).fetchone()
    assert run["source"] == "sgm-files" and run["status"] == "ok"
    assert run["source_currency"] == "SGM 2026 (current through 2026-05-20)"
    assert run["complete_crawl"] == 1
    d = get_document(conn, "SGM OV 10")
    assert d["citation"] == "SGM OV 10" and d["source"] == "sgm"
    assert "Predatory conduct was involved." in d["text"]
    assert d["provenance"]["source_url"].endswith("#page=56")
    assert get_document(conn, "sgm prv 1")["citation"] == "SGM PRV 1"


# ---------------------------------------------------------------------------------------------- felony list


@pytest.fixture(scope="module")
def felony(manual):
    return manual.felony


def _rows(felony, raw_cite):
    return [r for r in felony.rows if r.raw_cite == raw_cite]


def _one(felony, raw_cite):
    got = _rows(felony, raw_cite)
    assert len(got) == 1, (raw_cite, got)
    return got[0]


@needs_pdf
def test_felony_list_counts_and_value_sets(felony):
    rows = felony.rows
    shaded = [r for r in rows if not r.current]
    print(f"\nfelony list: {len(rows)} numerical rows ({len(shaded)} shaded), "
          f"{len(felony.alphabetical)} alphabetical rows")
    print("categories:", sorted({r.category for r in rows}))
    print("classes:", sorted({r.offense_class for r in rows}))
    assert felony.structural == []
    # the numerical list (pp. 135-174) is the data; the alphabetical list (pp. 175-212) reprints the same
    # rows and agrees with it row for row (see test_felony_review_notes for the one wording difference)
    assert len(rows) == 1542 and len(felony.alphabetical) == 1542
    assert len(shaded) == 385 and sum(not r.current for r in felony.alphabetical) == 385  # incl. MCL 445.67 (0.753 gray)
    assert {r.page for r in rows} == set(range(135, 175))
    assert {r.page for r in felony.alphabetical} == set(range(175, 213))
    assert {r.category for r in rows} == {"Person", "Property", "CS", "Pub ord", "Pub saf", "Pub trst", "Variable"}
    assert {r.offense_class for r in rows} == {"M2", "A", "B", "C", "D", "E", "F", "G", "H", "SPEC", "Variable", "I"}
    assert all(r.listed_in and re.fullmatch(r"MCL 777\.1\d[a-z]{0,2}", r.listed_in) for r in rows)
    assert all(r.listed_in is None for r in felony.alphabetical)
    assert all(normalize_citation(r.mcl_citation).canonical == r.mcl_citation for r in rows)
    # class "I" is printed only on two shaded MIOSHA rows; M2 only for second-degree murder
    assert [(r.raw_cite, r.current) for r in rows if r.offense_class == "I"] == [
        ("MCL 408.1035(5)", False), ("MCL 408.1035a(5)", False)]
    assert [(r.raw_cite, r.category, r.stat_max) for r in rows if r.offense_class == "M2"] == [
        ("MCL 750.317", "Person", "LIFE")]


@needs_pdf
def test_felony_review_notes(felony):
    """Disagreements are reported for a human, never resolved: shading vs the amendments column, and the
    numerical vs the alphabetical printing."""
    notes = felony.review
    for n in notes:
        print("review:", n)
    assert len(notes) == 7
    # MCL 445.67 used to be flagged "not shaded, but replaced" -- it IS shaded (0.753 gray); see
    # test_darker_shaded_row_is_not_current_mcl_445_67.
    assert not any("MCL 445.67" in n for n in notes)
    for cite in ("MCL 257.625n(10)", "MCL 333.7402(2)(a)", "MCL 750.116", "MCL 750.335a(2)(c)"):
        assert any(cite in n and "shaded (not current)" in n for n in notes), cite
    assert sum(n.startswith("MCL 750.492a(1)(a) ") for n in notes) == 2  # "placement false" vs "placement of false"
    assert any("MCL 750.181(5)" in n and "Stat Max cell is blank" in n for n in notes)


@needs_pdf
def test_felony_page_135_shading(felony):
    p135 = [r for r in felony.rows if r.page == 135]
    assert [r.raw_cite for r in p135 if not r.current] == [
        "MCL 18.366(1)", "MCL 18.1268(9)", "MCL 28.295(1)(a)", "MCL 28.422", "MCL 28.425o(3)(c)", "MCL 28.454(1)"]
    for cite in ("MCL 4.421(1)", "MCL 4.421(2)", "MCL 15.324(1)(d)", "MCL 18.366(1)(c)", "MCL 18.366(1)(d)",
                 "MCL 21.154", "MCL 28.214(6)(b)", "MCL 28.293(1)"):
        assert _one(felony, cite).current is True, cite
    # every shaded row on p.135 says so in the amendments column too
    assert all(re.search(r"deleted|replaced", r.amendments, re.I) for r in p135 if not r.current)
    # 28.295(1)(a) is printed twice: the old class H row (shaded) and the current class D row
    assert [(r.offense_class, r.current) for r in _rows(felony, "MCL 28.295(1)(a)")] == [("H", False), ("D", True)]


@needs_pdf
def test_felony_page_135_values(felony):
    r = _one(felony, "MCL 4.421(1)")
    assert (r.mcl_citation, r.listed_in, r.category, r.offense_class, r.description, r.stat_max, r.amendments) == (
        "MCL 4.421", "MCL 777.11a", "Pub trst", "G", "Lobbyists – compensation contingent on outcome of action", "3", "")
    r = _one(felony, "MCL 18.366(1)")
    assert (r.category, r.offense_class, r.stat_max, r.current) == ("Property", "E", "10", False)
    assert r.description == "False presentation to crime victim services commission to obtain more than $100"
    assert r.amendments == "Deleted, 2001 PA 150 & 154, Eff. 1/1/2002"  # wrapped onto two lines
    r = _one(felony, "MCL 28.425o(3)(c)")
    assert (r.mcl_citation, r.listed_in, r.category, r.offense_class, r.stat_max, r.current) == (
        "MCL 28.425o", "MCL 777.11b", "Pub saf", "F", "4", False)
    assert r.description == "Carrying concealed pistol in prohibited place – third or subsequent offense"
    assert r.amendments == "7/1/2001; replaced, 2012 PA 124, Eff. 8/6/2012"
    assert _one(felony, "MCL 18.1268(9)").stat_max == "Fine"
    assert _one(felony, "MCL 28.295(5)").stat_max == "Variable"
    # a three-line description, joined
    assert _one(felony, "MCL 28.295(2)").description == (
        "Selling counterfeited or forged state ID card or possessing counterfeited or forged state ID card with "
        "intent to deliver to another person or possessing 2 or more counterfeited or forged state ID cards")
    # the cite is kept as printed (with its stray space); the section is still recognized
    r = _one(felony, "MCL 28.468(1) (c)")
    assert (r.mcl_citation, r.offense_class) == ("MCL 28.468", "E")


@needs_pdf
def test_felony_hand_checked_sections(felony):
    """Values read off the rendered pages 144, 145, 155, 156 and 171."""
    def summary(section):
        return [(r.raw_cite, r.category, r.offense_class, r.stat_max, r.current)
                for r in felony.rows if r.mcl_citation == section]

    for section in ("MCL 750.81", "MCL 750.84", "MCL 750.520b", "MCL 333.7401"):
        print(section, summary(section))
    assert summary("MCL 750.81") == [
        ("MCL 750.81(4)", "Person", "G", "2", False),
        ("MCL 750.81(4)", "Person", "G", "2", False),
        ("MCL 750.81(4)", "Person", "E", "5", False),
        ("MCL 750.81(5)", "Person", "E", "5", True),
    ]
    assert summary("MCL 750.84") == [
        ("MCL 750.84(1)(a)", "Person", "D", "10", True),
        ("MCL 750.84(1)(b)", "Person", "D", "10", True),
    ]
    assert _one(felony, "MCL 750.84(1)(b)").description == "Assault by strangulation or suffocation"
    assert summary("MCL 750.520b") == [("MCL 750.520b(2)", "Person", "A", "LIFE", True)]
    cs = summary("MCL 333.7401")
    assert len(cs) == 24 and all(c[1] == "CS" for c in cs)
    current = [c[:4] for c in cs if c[4]]
    assert current == [
        ("MCL 333.7401(2)(a)(i)", "CS", "A", "LIFE"), ("MCL 333.7401(2)(a)(ii)", "CS", "A", "30"),
        ("MCL 333.7401(2)(a)(iii)", "CS", "B", "20"), ("MCL 333.7401(2)(a)(iv)", "CS", "D", "20"),
        ("MCL 333.7401(2)(b)(i)", "CS", "B", "20"), ("MCL 333.7401(2)(b)(ii)", "CS", "E", "7"),
        ("MCL 333.7401(2)(c)", "CS", "F", "4"), ("MCL 333.7401(2)(d)(i)", "CS", "C", "15"),
        ("MCL 333.7401(2)(d)(ii)", "CS", "D", "7"), ("MCL 333.7401(2)(d)(iii)", "CS", "F", "4"),
        ("MCL 333.7401(2)(e)", "CS", "G", "2"), ("MCL 333.7401(2)(f)", "CS", "D", "7"),
    ]
    # the last row of p.144 sits below a rule drawn only across the group column; it must not be lost
    assert [(r.page, r.current, r.description) for r in _rows(felony, "MCL 333.7401(2)(d)(ii)")] == [
        (144, False, "Delivery or manufacture of 5-45 kilos of marijuana"),
        (145, True, "Delivery or manufacture of 5 or more but less than 45 kilograms of marihuana or synthetic "
                    "equivalents of marihuana"),
    ]


@needs_pdf
def test_felony_layout_edge_cases(felony):
    # SPEC rows under MCL 777.18, "Variable" stat max
    spec = [r for r in felony.rows if r.raw_cite == "MCL 333.7410"]
    assert len(spec) == 4 and {(r.offense_class, r.listed_in, r.stat_max) for r in spec} == {
        ("SPEC", "MCL 777.18", "Variable")}
    # two-letter group bars that overflow the narrow label column
    assert {r.listed_in for r in felony.rows} >= {"MCL 777.16aa", "MCL 777.16bb"}
    # a word the manual hyphenates at the cell edge is rejoined ("dam-" / "age")
    assert _one(felony, "MCL 750.210(2)(b)").description == (
        "Possessing or carrying an explosive or combustible substance causing property damage")
    # "Vari-" / "able" in the narrow class column
    assert _one(felony, "MCL 750.213a(3)(a)").offense_class == "Variable"
    # cells that pdfplumber's word grouping glued together ("VariableReplaced,") stay separate
    assert spec[0].stat_max == "Variable" and spec[0].amendments.startswith("Replaced, 2000 PA 279")
    # footnote asterisk kept on the printed cite
    assert _one(felony, "MCL 333.7407(1)(g)*").mcl_citation == "MCL 333.7407"


def _fake_source(rows, structural=(), review=()):
    src = sgm.SgmFilesSource.__new__(sgm.SgmFilesSource)
    src.manual = sgm.SgmManual(docs=[], current_through="2026-05-20", edition_label=None, grid_pages=(1, 1),
                               felony_list_pages=(2, 2),
                               felony=sgm.FelonyList(list(rows), [], list(structural), list(review)))
    src.currency = "SGM 2026 (current through 2026-05-20)"
    src.warnings = []
    return src


def _frow(raw="MCL 750.84(1)(a)", cls="D", current=True):
    return sgm.FelonyRow(raw_cite=raw, mcl_citation=normalize_citation(raw).canonical, listed_in="MCL 777.16d",
                         category="Person", offense_class=cls, description="d", stat_max="10", amendments="",
                         current=current, page=156)


def test_apply_extras_replaces_rows_and_reports_review(conn):
    from mlaw.pipeline import RunReport

    rep = RunReport(run_id=0, source="sgm-files")
    _fake_source([_frow(), _frow("MCL 750.84(1)(b)")], review=["page 1, x: disagreement"]).apply_extras(conn, rep)
    _fake_source([_frow(cls="C")]).apply_extras(conn, rep)
    got = [tuple(r) for r in conn.execute(
        "SELECT mcl_citation, raw_cite, offense_class, current, edition FROM sentencing_offenses")]
    assert got == [("MCL 750.84", "MCL 750.84(1)(a)", "C", 1, "SGM 2026 (current through 2026-05-20)")]
    assert rep.warnings == ["SGM felony list, check against the PDF: page 1, x: disagreement"]


def test_apply_extras_keeps_old_rows_on_structural_failure(conn):
    from mlaw.pipeline import RunReport

    rep = RunReport(run_id=0, source="sgm-files")
    _fake_source([_frow(), _frow("MCL 750.84(1)(b)")]).apply_extras(conn, rep)
    _fake_source([_frow(cls="C")], structural=["felony list page 150: no offense rows parsed"]).apply_extras(conn, rep)
    assert conn.execute("SELECT COUNT(*) FROM sentencing_offenses").fetchone()[0] == 2
    assert len(rep.warnings) == 1 and rep.warnings[0].startswith("sentencing_offenses NOT updated")
    assert "page 150" in rep.warnings[0]


@needs_pdf
def test_felony_ingest_and_lookup(conn, src):
    from conftest import DAY1, FIX
    from mlaw.service import get_sentencing_class
    from mlaw.sources.fixture import FixtureSource

    run_ingest(conn, FixtureSource(FIX / "day1"), now=DAY1)  # MCL 750.84 / 257.627a documents
    r = run_ingest(conn, src, now="2026-09-29T06:00:00Z")
    assert conn.execute("SELECT COUNT(*) FROM sentencing_offenses").fetchone()[0] == 1542
    assert {e for (e,) in conn.execute("SELECT DISTINCT edition FROM sentencing_offenses")} == {src.currency}
    assert len(r.warnings) == len(src.manual.felony.review) == 7

    res = get_sentencing_class(conn, "MCL 750.84")
    assert res["listed"] is True and res["count"] == 2
    assert [(x["raw_cite"], x["class"], x["grid"], x["category"]) for x in res["rows"]] == [
        ("MCL 750.84(1)(a)", "D", "SGM Grid D", "Person"), ("MCL 750.84(1)(b)", "D", "SGM Grid D", "Person")]
    assert res["rows"][0]["source_url"].endswith("sgm.pdf#page=156")
    assert res["provenance"]["edition"] == src.currency and res["provenance"]["notice"]
    assert res["provenance"]["last_loaded"] == "2026-09-29T06:00:00Z"

    d = get_document(conn, "MCL 750.84")
    assert [(x["raw_cite"], x["grid"]) for x in d["sentencing"]] == [
        ("MCL 750.84(1)(a)", "SGM Grid D"), ("MCL 750.84(1)(b)", "SGM Grid D")]
    assert "sentencing" not in get_document(conn, "MCL 257.627a")  # not a guidelines felony

    run_ingest(conn, src, now="2026-09-30T06:00:00Z")  # replaces, never appends
    assert conn.execute("SELECT COUNT(*) FROM sentencing_offenses").fetchone()[0] == 1542
    assert get_sentencing_class(conn, "750.84")["count"] == 2


@needs_pdf
def test_darker_shaded_row_is_not_current_mcl_445_67(felony):
    # Found at Checkpoint A (2026-09-29): this row's fill is 0.753 gray, not the usual 0.847.
    rows = [r for r in felony.rows if r.raw_cite == "MCL 445.67" and r.page == 149]  # p.150 has the D/C repeat-offense rows (white)
    assert [(r.description.split()[0], r.current, r.page) for r in rows] == [
        ("Obtain/possess/sell/transfer", False, 149),
        ("Solicit/obtain/possess/sell/transfer", True, 149),
    ]
    assert not felony.structural


# ---------------------------------------------------------------------------------------------- grids
#
# Every cell asserted below was read off a 110-dpi render of the page (pdftoppm) on 2026-09-29, not taken
# from the parser output. Tuples: (ov, prv, min, [base, HO2, HO3, HO4] maxima, [shaded per status]).

GRID_PAGES = {"M2": 125, "A": 126, "B": 127, "C": 128, "D": 129, "E": 130, "F": 131, "G": 132, "H": 133}
_N = (False, False, False, False)
VERIFIED_CELLS = {
    "M2": [("I", "A", "90", ["150", "187", "225", "300"], _N),
           ("II", "D", "225", ["375/L", "468/L", "562/L", "750/L"], _N),
           ("III", "F", "365", ["600/L", "750/L", "900/L", "1200/L"], _N)],
    "A": [("I", "A", "21", ["35", "43", "52", "70"], _N),
          ("VI", "F", "270", ["450/L", "562/L", "675/L", "900/L"], _N)],
    "B": [("I", "A", "0", ["18*", "22", "27", "36"], (False, True, True, True)),
          ("II", "A", "12", ["20", "25", "30", "40"], (True, True, True, True)),
          ("VI", "F", "117", ["160", "200", "240", "320"], _N)],
    "C": [("I", "A", "0", ["11*", "13*", "16*", "22"], (False, False, False, True)),
          ("II", "B", "5", ["17*", "21", "25", "34"], (False, True, True, True)),  # min cell painted white
          ("VI", "F", "62", ["114", "142", "171", "228"], _N)],
    "D": [("I", "A", "0", ["6*", "7*", "9*", "12*"], _N),
          ("I", "D", "0", ["17*", "21", "25", "34"], (False, True, True, True)),
          ("III", "C", "5", ["23", "28", "34", "46"], (True, True, True, True)),
          ("VI", "F", "43", ["76", "95", "114", "152"], _N)],
    "E": [("I", "A", "0", ["3*", "3*", "4*", "6*"], _N),
          ("V", "A", "0", ["14*", "17*", "21", "28"], (False, False, True, True)),
          ("VI", "F", "24", ["38", "47", "57", "76"], _N)],
    "F": [("I", "A", "0", ["3*", "3*", "4*", "6*"], _N),
          ("III", "B", "0", ["17*", "21", "25", "34"], (False, True, True, True)),
          ("IV", "F", "17", ["30", "37", "45", "60"], _N)],
    "G": [("I", "A", "0", ["3*", "3*", "4*", "6*"], _N),
          ("III", "F", "7", ["23", "28", "34", "46"], (True, True, True, True))],
    "H": [("I", "A", "0", ["1*", "1*", "1*", "2*"], _N),
          ("III", "F", "5", ["17*", "21", "25", "34"], (False, True, True, True))],
}
GRID_SHAPE = {"M2": 3, "A": 6, "B": 6, "C": 6, "D": 6, "E": 6, "F": 4, "G": 3, "H": 3}  # OV levels


@pytest.fixture(scope="module")
def grids(manual):
    return {g.klass: g for g in manual.grids}


@needs_pdf
def test_grids_found_once_each_on_their_pages(manual, grids):
    assert [g.klass for g in manual.grids] == list(sgm.FELONY_CLASSES)
    assert {k: g.pages for k, g in grids.items()} == {k: (p,) for k, p in GRID_PAGES.items()}
    for k, g in grids.items():
        assert g.statute == f"MCL 777.6{list(GRID_PAGES).index(k) + 1}"
        assert g.title.startswith(f"Sentencing Grid for Class {k}") and g.title.endswith(g.statute)
        assert g.subtitle == "Includes Ranges Calculated for Habitual Offenders (MCL 777.21(3)(a)-(c))"
        assert g.prv_levels == [("A", "0 Points"), ("B", "1-9 Points"), ("C", "10-24 Points"),
                                ("D", "25-49 Points"), ("E", "50-74 Points"), ("F", "75+ Points")]
        assert [o for o, _ in g.ov_levels] == ["I", "II", "III", "IV", "V", "VI"][: GRID_SHAPE[k]]
        assert len(g.cells) == GRID_SHAPE[k] * 6 * 4


@needs_pdf
def test_grid_ov_points(grids):
    assert grids["M2"].ov_levels == [("I", "0-49 Points"), ("II", "50-99 Points"), ("III", "100+ Points")]
    assert grids["A"].ov_levels[-1] == ("VI", "100+ Points")
    assert grids["F"].ov_levels == [("I", "0-9 Points"), ("II", "10-34 Points"), ("III", "35-74 Points"),
                                    ("IV", "75+ Points")]
    assert grids["H"].ov_levels == [("I", "0-9 Points"), ("II", "10-15 Points"), ("III", "16+ Points")]


@needs_pdf
@pytest.mark.parametrize("klass", list(VERIFIED_CELLS))
def test_grid_cells_verified_against_render(grids, klass):
    g = grids[klass]
    for ov, prv, mn, maxima, shaded in VERIFIED_CELLS[klass]:
        got = [g.cells[(ov, prv, s)] for s in sgm.GRID_STATUSES]
        assert [(c.min, c.max, c.shaded) for c in got] == list(zip([mn] * 4, maxima, shaded)), (klass, ov, prv)


@needs_pdf
def test_grids_validate_cleanly(manual, grids):
    for g in grids.values():
        assert g.structural == [] and g.review == [], g.klass
    assert not manual.warnings


@needs_pdf
def test_grid_notes_verbatim(grids):
    hab = ("The statutory percentage increases for habitual offenders are rounded down to the nearest whole month; "
           "accordingly, the cell range may be less than the maximum possible minimum sentence by a fraction of a "
           "month.")
    ho4 = ("Certain fourth habitual offenders may be subject to a mandatory minimum sentence of 25 years’ "
           "imprisonment. See MCL 769.12(1)(a).")
    legend = ("Intermediate sanction cells are marked by asterisks following the upper end of the minimum sentence "
              "range, straddle cells are shaded, and prison cells are unmarked.")
    for k in ("M2", "A"):
        assert grids[k].notes == [hab, ho4]
    for k in "BCDEFGH":
        assert grids[k].notes == [legend, hab, ho4]


def _mutated(grids, klass="D"):
    import copy

    g = copy.deepcopy(grids[klass])
    g.structural, g.review = [], []
    return g


@needs_pdf
def test_grid_validation_catches_problems(grids):
    g = _mutated(grids)
    g.cells[("VI", "F", "HO2")].max = "70"  # below base 76: decreasing across statuses
    sgm._validate_grid(g)
    assert any("decrease across offender status" in s for s in g.structural)
    assert any("OV VI / PRV F / HO2: printed 70, but base 76 +25% rounded down is 95" in r for r in g.review)

    g = _mutated(grids)
    del g.cells[("II", "C", "HO3")]
    sgm._validate_grid(g)
    assert any("1 cells missing" in s for s in g.structural)

    g = _mutated(grids)
    g.cells[("I", "E", "base")].shaded = False  # 5-23 must be a straddle cell
    sgm._validate_grid(g)
    want = "OV I / PRV E / base: 5-23 is printed as prison, but its numbers make it straddle"
    assert any(want in r for r in g.review)
    assert any("OV I / PRV E: minimum cell is shaded" in r for r in g.review)

    g = _mutated(grids)
    g.cells[("II", "B", "base")].max = "12*"  # left of C's 17*, right of A's 9*: still fine; now break order
    g.cells[("II", "C", "base")].max = "10*"
    sgm._validate_grid(g)
    assert any("base, OV II: PRV B 0-12* > PRV C 0-10*" in r for r in g.review)
    assert not g.structural

    g = _mutated(grids)
    g.notes = g.notes[1:]  # the legend is gone but asterisks / shading remain
    sgm._validate_grid(g)
    assert any("no intermediate-sanction legend" in s for s in g.structural)
    assert any("no straddle-cell legend" in s for s in g.structural)


def test_grid_text_for_failed_grid_points_to_pdf():
    g = sgm.Grid("C", "t", "s", "MCL 777.64", (128,), structural=["broken"])
    assert sgm.grid_text(g, sgm.GRID_UNITS_QUOTE) == "This grid could not be extracted reliably; see the PDF page 128."


@needs_pdf
def test_failed_grid_is_a_pointer_and_source_incomplete(monkeypatch):
    real = sgm.parse_grids

    def broken(pdf, pages):
        gs = real(pdf, pages)
        gs[4].structural.append("SGM Grid D (page 129): simulated failure")
        return gs

    monkeypatch.setattr(sgm, "parse_grids", broken)
    s = sgm.SgmFilesSource(PDF)
    assert s.complete is False and any("simulated failure" in w for w in s.warnings)
    d = {x.citation: x for x in s.manual.docs}
    assert d["SGM Grid D"].text == "This grid could not be extracted reliably; see the PDF page 129."
    assert d["SGM Grid D"].url.endswith("#page=129") and d["SGM Grid D"].status == "active"
    assert "90–150" in d["SGM Grid M2"].text


@needs_pdf
def test_grid_docs(docs):
    for k, p in GRID_PAGES.items():
        d = docs[f"SGM Grid {k}"]
        c = normalize_citation(d.citation)
        assert c.canonical == d.citation and c.source == "sgm"
        assert normalize_citation(f"sgm grid {k.lower()}").canonical == d.citation
        assert d.kind == "guidelines" and d.url.endswith(f"sgm.pdf#page={p}")
        assert d.catchline.startswith(f"Sentencing Grid for Class {k}")
        assert d.text.count("\n## ") == 5  # Base, HO2, HO3, HO4, notes
        assert sgm.GRID_UNITS_QUOTE in d.text
    m2 = docs["SGM Grid M2"].text
    assert m2.startswith("Sentencing Grid for Class M2 (Second-Degree Murder)—MCL 777.61\n")
    base, ho2, ho3, ho4 = (m2.split(f"## {h}")[1] for h in ("Base", "HO2", "HO3", "HO4"))
    assert "| I (0-49 Points) | 90–150 | 144–240 |" in base
    assert "| I (0-49 Points) | 90–187 |" in ho2 and "| I (0-49 Points) | 90–225 |" in ho3
    assert "| I (0-49 Points) | 90–300 |" in ho4
    assert "| III (100+ Points) |" in ho4 and "365–1200/L |" in ho4
    d = docs["SGM Grid D"].text
    assert "| I (0-9 Points) | 0–6* | 0–9* | 0–11* | 0–17* | 5–23 [shaded] | 10–23 [shaded] |" in d


@needs_pdf
def test_grid_ingest(conn, src):
    from conftest import DAY1, FIX
    from mlaw.service import get_sentencing_class
    from mlaw.sources.fixture import FixtureSource

    run_ingest(conn, FixtureSource(FIX / "day1"), now=DAY1)
    r = run_ingest(conn, src, now="2026-09-29T06:00:00Z")
    assert r.docs_removed == 0
    for k in GRID_PAGES:
        assert get_document(conn, f"SGM Grid {k}")["citation"] == f"SGM Grid {k}"
    d = get_document(conn, "sgm grid d")
    assert d["citation"] == "SGM Grid D" and d["provenance"]["source_url"].endswith("#page=129")
    assert "## HO4" in d["text"] and "| VI (75+ Points) | 10–46 [shaded] | 19–76 |" in d["text"]
    # the grid named by the felony list for MCL 750.84 exists as a document
    grids = {x["grid"] for x in get_sentencing_class(conn, "MCL 750.84")["rows"]}
    assert grids == {"SGM Grid D"}
    for gname in grids:
        assert get_document(conn, gname)["citation"] == gname
