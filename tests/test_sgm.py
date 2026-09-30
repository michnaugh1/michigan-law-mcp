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
        expected = 0 if n in DIFFERING_OVS else 1
        assert cits.count(f"SGM OV {n}") == expected
    for d in manual.docs:
        c = normalize_citation(d.citation)
        assert c is not None and c.canonical == d.citation and c.source == "sgm"
        assert d.source == "sgm" and d.kind == "guidelines"
        assert re.fullmatch(re.escape(sgm.DEFAULT_URL) + r"#page=\d+", d.url)


@needs_pdf
def test_ov_differences_are_flagged_not_resolved(manual, src):
    """The 2026 manual's OV copies are NOT identical across groups. No copy is picked: the OV is left
    out, a warning names it and the groups, the variants are kept for review, and the source is marked
    incomplete so the pipeline never treats this run as a full crawl."""
    assert set(manual.ov_variants) == DIFFERING_OVS
    assert len(manual.warnings) == len(DIFFERING_OVS)
    for n in DIFFERING_OVS:
        assert any(w.startswith(f"SGM OV {n}:") for w in manual.warnings)
        assert list(manual.ov_variants[n]) == manual.ov_groups[n]
    assert src.complete is False
    v = manual.ov_variants
    assert "and death results" in v[3][PERSON] and "where death results" in v[3][ORDER]
    assert "MCL 777.22(1)." in v[18][PERSON] and "MCL 777.22(5)." in v[18][SAFETY]
    assert "home invasion" in v[16][PERSON] and "home invasion" not in v[16][PROPERTY]
    assert "10 — Three or more contemporaneous" in v[12][ORDER]
    assert "10 — Three or more contemporaneous" not in v[12][PERSON]
    # every other OV agreed across all its groups
    assert "SGM OV 1:" not in " ".join(manual.warnings)


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
    assert web.name == "sgm-web" and web.complete is False  # same OV differences as the files source
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
    assert run["complete_crawl"] == 0
    d = get_document(conn, "SGM OV 10")
    assert d["citation"] == "SGM OV 10" and d["source"] == "sgm"
    assert "Predatory conduct was involved." in d["text"]
    assert d["provenance"]["source_url"].endswith("#page=56")
    assert get_document(conn, "sgm prv 1")["citation"] == "SGM PRV 1"
