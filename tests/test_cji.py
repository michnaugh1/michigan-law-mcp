from pathlib import Path

import pytest

from conftest import DAY1
from mlaw import service
from mlaw.citations import normalize_citation
from mlaw.pipeline import run_ingest
from mlaw.sources.cji import CjiFilesSource, _parse_index, _split_status, parse_cji_text

DATA = Path(__file__).resolve().parent / "data" / "court_rules" / "2026-09-29"
PDF = DATA / "cji" / "criminal-jury-instructions.pdf"
needs_pdf = pytest.mark.skipif(
    not PDF.exists(), reason=f"jury instructions PDF not present (git-ignored, put it at {PDF})")


@pytest.fixture(scope="module")
def src():
    if not PDF.exists():
        pytest.skip(f"jury instructions PDF not present (git-ignored, put it at {PDF})")
    s = CjiFilesSource(PDF)
    s.result  # parse once
    return s


@pytest.fixture(scope="module")
def docs(src):
    return {d.citation: d for d in src.iter_documents()}


@needs_pdf
def test_instruction_count_matches_toc(src):
    r = src.result
    assert r.toc_count == len(r.docs) == 746
    assert src.complete is True
    assert not any(w.startswith(("TOC/body mismatch", "duplicate", "page ")) for w in r.warnings), r.warnings


@needs_pdf
def test_round_trip_and_no_duplicates(src):
    cits = [d.citation for d in src.iter_documents()]
    assert len(cits) == len(set(cits))
    for d in src.iter_documents():
        c = normalize_citation(d.citation)
        assert c is not None and c.source == "cji" and c.canonical == d.citation, d.citation
        assert d.source == "cji" and d.kind == "instruction" and d.status in ("active", "renumbered", "repealed")
        assert d.chapter == d.citation.split()[-1].split(".")[0]
        assert d.url.endswith(f"#page={d.url.rsplit('=', 1)[1]}") and d.url.rsplit("=", 1)[1].isdigit()
        assert d.catchline and d.catchline == d.catchline.strip()


@needs_pdf
def test_cji_5_1(docs):
    d = docs["M Crim JI 5.1"]
    assert d.catchline == "Witnesses - Impeachment by Prior Conviction"
    assert d.text.startswith("(1) You have heard that one witness")
    assert "formerly CJI2d 5.1" in d.history_note
    assert "MRE 609" in d.compilers_notes and "People v Finley" in d.compilers_notes
    assert d.compilers_notes.startswith("Reference Guide: Court Rules: MRE 609.")
    assert d.status == "active" and d.chapter == "5"
    assert d.url.endswith("criminal-jury-instructions.pdf#page=138")


@needs_pdf
def test_cji_5_2_use_note(docs):
    d = docs["M Crim JI 5.2"]
    assert d.catchline == "Weighing Conflicting Evidence - Number of Witnesses"
    assert "Use Note: This instruction may be given where the number of witnesses" in d.compilers_notes


@needs_pdf
def test_status_variants(docs):
    d = docs["M Crim JI 5.8b"]
    assert d.status == "renumbered"
    assert d.catchline == "Evidence of Other Acts of Child Sexual Abuse"
    assert "renumbered MCrim JI 20.28a in May 2008" in d.compilers_notes
    assert docs["M Crim JI 5.8c"].status == "renumbered"
    assert "4.11a" in docs["M Crim JI 5.8c"].compilers_notes
    assert docs["M Crim JI 20.34"].status == "renumbered" and "36.10" in docs["M Crim JI 20.34"].compilers_notes
    assert docs["M Crim JI 40.12"].status == "renumbered"
    assert docs["M Crim JI 3.9"].status == "repealed" and docs["M Crim JI 3.9"].catchline == "Specific Intent"
    # a "[Use for Acts ...]" qualifier is not a status: it stays in the title and the instruction stays active
    d = docs["M Crim JI 31.15"]
    assert d.status == "active" and "[Use for Act(s) Occurring Before April 3, 2013]" in d.catchline
    d = docs["M Crim JI 16.12"]  # both a qualifier and [DELETED]
    assert d.status == "repealed" and d.catchline.endswith("[Use for Acts Occurring Before October 31, 2010]")
    assert d.text.startswith("Note. This instruction was deleted in May 2010")
    # "combined with" is treated as repealed, with the note kept
    d = docs["M Crim JI 7.19"]
    assert d.status == "repealed" and "combined with M Crim JI 7.16" in d.compilers_notes


@needs_pdf
def test_wrapped_headings_and_page_typo(docs):
    assert docs["M Crim JI 12.2b"].catchline.endswith("Gamma-butyrolactone to Commit Criminal Sexual Conduct")
    assert docs["M Crim JI 3.10a"].catchline.endswith("Criminal Sexual Conduct Cases")
    assert docs["M Crim JI 16.13"].text.startswith("(1) The defendant is charged")  # PDF heading lacks the "M"
    assert docs["M Crim JI 17.1"].catchline == "Definition of Assault [For Use Where There Has Been No Battery]"


@needs_pdf
def test_instruction_continues_across_pages(docs):
    d = docs["M Crim JI 3.5"]  # body/footnotes spill onto a second page
    assert "If helpful, describe the exhibits admitted into evidence." in d.compilers_notes
    assert "M Crim JI 3.5 (formerly CJI2d 3.5)" in d.history_note


@needs_pdf
def test_no_furniture(docs):
    import re

    bad = re.compile(r"Michigan Model Criminal Jury Instructions|Page \d+-\d+|INDEX-|\f|^Chapter \d+: ", re.M)
    for cit, d in docs.items():
        for name, s in (("text", d.text), ("history", d.history_note), ("notes", d.compilers_notes)):
            assert not bad.search(s), (cit, name)
        assert d.text == d.text.strip() and "\n\n\n" not in d.text
        assert "\nHistory\n" not in d.text and "\nReference Guide\n" not in d.text


@needs_pdf
def test_chapter_44_index(src, docs):
    assert "Statutes (index): MCL 768.21a" in docs["M Crim JI 7.11"].compilers_notes
    assert "MCL 768.21a" in src.result.statute_index["7.11"]
    assert "MCL 780.972" in src.result.statute_index["7.24"]  # despite ", ,7.24"
    assert docs["M Crim JI 7.24"].compilers_notes.splitlines()[-1] == "Statutes (index): MCL 780.972"
    assert "MCL 768.329a" in src.result.statute_index["7.9"] and "MCL 330.1100a" in src.result.statute_index["7.9"]
    assert "MCL 257.617a" in src.result.statute_index["15.13a"]  # ".617a" shorthand
    assert "MCL 750.338b" in src.result.statute_index["20.31"]
    assert not any("Statutes (index)" in d.text for d in docs.values())
    # the 20.39 rows carry "[28.729(1)]" penalty brackets, which are not attributed
    assert src.result.statute_index["20.39"] == ["MCL 28.724"]


@needs_pdf
def test_currency_and_source_shape(src):
    assert src.result.currency_date == "2026-08-31"
    assert src.currency == "M Crim JI updated August 31, 2026 (2026-08-31)"
    assert src.name == "cji-files" and src.source_kinds == {"cji"} and src.covered_chapters is None
    assert list(src.iter_public_acts()) == [] and list(src.iter_ranges()) == []


@needs_pdf
def test_toc_mismatch_makes_source_incomplete():
    from mlaw.sources.cji import pdf_to_text

    text = pdf_to_text(PDF)
    marker = "\nMCrim JI 5.2 Weighing Conflicting Evidence - Number of Witnesses\n"
    assert marker in text
    r = parse_cji_text(text.replace(marker, "\nMCrim JI 5.2 Weighing Conflicting Evidence - Number of Witnesses X\n", 1), "u")
    assert len(r.docs) == 745 and any(w.startswith("TOC/body mismatch") for w in r.warnings)
    s = CjiFilesSource(PDF)
    s._result = r
    assert s.complete is False


@needs_pdf
def test_ingest_and_cross_references(conn):
    s = CjiFilesSource(PDF)
    rep = run_ingest(conn, s, now=DAY1)
    assert rep.docs_seen == 746
    doc = service.get_document(conn, "M Crim JI 5.1")
    assert doc["citation"] == "M Crim JI 5.1" and doc["text"].startswith("(1) You have heard")
    run = conn.execute("SELECT source_currency, complete_crawl FROM runs").fetchone()
    assert run["source_currency"].endswith("(2026-08-31)") and run["complete_crawl"] == 1

    cited = service.get_cross_references(conn, "MCL 768.21a", direction="cited_by", limit=500)
    assert "M Crim JI 7.11" in {r["citation"] for r in cited["references"]}
    cites = service.get_cross_references(conn, "M Crim JI 5.1", direction="cites")
    assert any(r["citation"] == "MRE 609" and r["kind"] == "mre" for r in cites["references"])
    cited = service.get_cross_references(conn, "MCL 780.972", direction="cited_by", limit=500)
    assert "M Crim JI 7.24" in {r["citation"] for r in cited["references"]}


# ---- unit tests that need no PDF
def test_split_status():
    assert _split_status("Specific Intent [DELETED]")[:3] == ("Specific Intent", "repealed", None)
    t, st, tgt, note = _split_status("Pandering [renumbered M Crim JI 36.10]")
    assert (t, st, tgt) == ("Pandering", "renumbered", "36.10") and "36.10" in note
    t, st, tgt, _ = _split_status("X [amended and renumbered M Crim JI 4.11a in February 2010]")
    assert (t, st, tgt) == ("X", "renumbered", "4.11a")
    assert _split_status("OWI [OWI]")[:2] == ("OWI [OWI]", "active")


def test_parse_index_quirks():
    w: list[str] = []
    rows = [
        "II.Defenses",
        "A.                                          Insanity",
        "768.21a(1)                                  7.11, 7.13",
        "768.329a, 330.1100a                         7.9",
        "257.617(2), .617a                           15.13a",
        "205.428(3), (4), (12)                       12.10a",
        "780.972                                     7.15, 7.21, 7.22, ,7.24",
        "750.335a                                    20.33",
        "   w/ 750.10a                               20.33(6)",
        "28.724(5), (6) [28.729(1)]                   20.39",
        "750.520b                                    20.1 w/ 20.3 to 20.11",
        "999.1                                       7.99",
        "Michigan Model Criminal Jury Instructions   INDEX-16",
    ]
    known = {"7.11", "7.13", "7.9", "15.13a", "12.10a", "7.15", "7.21", "7.22", "7.24", "20.33", "20.39", "20.1"}
    idx = _parse_index(rows, known, w)
    assert idx["7.11"] == ["MCL 768.21a"] and idx["7.13"] == ["MCL 768.21a"]
    assert idx["7.9"] == ["MCL 768.329a", "MCL 330.1100a"]
    assert idx["15.13a"] == ["MCL 257.617", "MCL 257.617a"]
    assert idx["12.10a"] == ["MCL 205.428"]
    assert idx["7.24"] == ["MCL 780.972"]
    assert idx["20.33"] == ["MCL 750.335a", "MCL 750.10a"]
    assert idx["20.39"] == ["MCL 28.724"]
    assert idx["20.1"] == ["MCL 750.520b"]
    assert any("20.1 w/ 20.3 to 20.11" in x for x in w)  # companions are not expanded, and it says so
    assert any("999.1" in x for x in w)  # unparseable MCL section is reported, not guessed
    assert any("7.99" in x for x in w)  # instruction not in the PDF is reported
