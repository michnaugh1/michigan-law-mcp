from pathlib import Path

import httpx
import pytest

from mlaw.citations import normalize_citation
from mlaw.pipeline import run_ingest
from mlaw.service import get_document
from mlaw.sources.mcr import McrFilesSource, McrWebSource, parse_chapter
from mlaw.web import PoliteClient, RobotsDisallowed

DATA = Path(__file__).resolve().parent / "data" / "court_rules" / "2026-09-29" / "mcr"
CHAPTERS = [str(i) for i in range(1, 10)]


def _html(ch):
    return (DATA / f"Court_Rules_Chapter_{ch}.htm").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def parsed():
    return {ch: parse_chapter(_html(ch), ch, f"http://x/{ch}.htm") for ch in CHAPTERS}


@pytest.mark.parametrize("ch", CHAPTERS)
def test_doc_count_matches_headings(parsed, ch):
    assert len(parsed[ch].docs) == _html(ch).count('class="Rule_Heading"')


def test_known_counts(parsed):
    assert len(parsed["1"].docs) == 13
    assert len(parsed["6"].docs) == 80


def test_no_duplicates_and_citations_round_trip(parsed):
    for ch, res in parsed.items():
        cites = [d.citation for d in res.docs]
        assert len(cites) == len(set(cites)), ch
        assert not [w for w in res.warnings if "duplicate" in w]
        for d in res.docs:
            c = normalize_citation(d.citation)
            assert c is not None and c.canonical == d.citation and c.source == "mcr"
            assert d.source == "mcr" and d.kind == "rule" and d.chapter == ch


def test_mcr_1_101(parsed):
    d = {x.citation: x for x in parsed["1"].docs}["MCR 1.101"]
    assert d.catchline == "Title; Citation"
    assert "cited by the symbol “MCR __________.”" in d.text
    assert d.status == "active"
    assert d.url.startswith("http://x/1.htm#")


def test_mcr_6_001_labels_and_decoding(parsed):
    d = {x.citation: x for x in parsed["6"].docs}["MCR 6.001"]
    lines = d.text.split("\n")
    assert lines[0].startswith("(A)") and "Felony Cases." in lines[0]
    assert any(ln.startswith("(B)") and "Misdemeanor Cases." in ln for ln in lines)
    assert "MCR 6.006(C)" in d.text
    for res in parsed.values():
        for doc in res.docs:
            assert "&#" not in doc.text and "​" not in doc.text


def test_indentation_levels(parsed):
    d = {x.citation: x for x in parsed["6"].docs}["MCR 6.302"]
    lines = d.text.split("\n")
    assert any(ln.startswith("  (1) ") for ln in lines)
    assert any(ln.startswith("    (a) ") for ln in lines)
    assert not any(ln.startswith("(1)") for ln in lines)


def test_repealed_status(parsed):
    docs = {x.citation: x for x in parsed["2"].docs}
    assert docs["MCR 2.224"].status == "repealed"
    assert {x.citation: x for x in parsed["4"].docs}["MCR 4.003"].status == "repealed"


def test_unknown_class_kept_as_text_and_warned():
    html = ('<p class="Rule_Heading" id="1">Rule 1.001 Foo</p><p class="Weird_Class" id="2">Odd text</p>'
            '<p class="L2_Numbers" id="3">(1) Sub</p>')
    res = parse_chapter(html, "1", "u")
    assert res.docs[0].text == "Odd text\n  (1) Sub"
    assert any("Weird_Class" in w for w in res.warnings)


def test_duplicate_and_reserved():
    html = ('<p class="Rule_Heading" id="1">Rule 1.001 [Reserved]</p>'
            '<p class="Rule_Heading" id="2">Rule 1.002 A</p><p class="Rule_Body">first</p>'
            '<p class="Rule_Heading" id="3">Rule 1.002 A</p><p class="Rule_Body">second</p>'
            '<p class="Rule_Heading" id="4">Rule 1.003 B</p><p class="Rule_Body">[Reserved]</p>')
    res = parse_chapter(html, "1", "u")
    assert [d.citation for d in res.docs] == ["MCR 1.001", "MCR 1.002", "MCR 1.003"]
    assert res.docs[1].text == "first"
    assert [d.status for d in res.docs] == ["reserved", "active", "reserved"]
    assert any("duplicate" in w for w in res.warnings)


def test_main_dates(parsed):
    assert parsed["4"].main_date == "2024-09-01"
    assert parsed["1"].main_date == "2026-01-01"


def test_files_source_and_ingest(parsed, conn):
    src = McrFilesSource(DATA)
    assert src.complete is True and src.covered_chapters is None
    assert src.source_kinds == frozenset({"mcr"})
    assert list(src.iter_public_acts()) == [] and list(src.iter_ranges()) == []
    docs = list(src.iter_documents())
    assert len(docs) == sum(len(r.docs) for r in parsed.values())
    assert "2026-09-02" in src.currency and "4 2024-09-01" in src.currency
    run_ingest(conn, McrFilesSource(DATA), now="2026-09-29T06:00:00Z")
    assert get_document(conn, "MCR 6.201")["text"]
    row = conn.execute("SELECT source_currency FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    assert "2026-09-02" in row[0]


class FakeClient:
    def __init__(self):
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)
        name = url.rsplit("/", 1)[-1]
        return type("R", (), {"text": (DATA / name).read_text(encoding="utf-8")})()


def test_web_source_with_fake_client(parsed):
    client = FakeClient()
    src = McrWebSource(client=client)
    docs = list(src.iter_documents())
    assert len(docs) == sum(len(r.docs) for r in parsed.values())
    assert client.urls[0].endswith("/michigan-court-rules-responsive-html5.zip/whxdata/toc.new.js")
    assert len(client.urls) == 10
    assert "2026-09-02" in src.currency


def test_web_source_refuses_when_robots_disallows():
    seen = []

    def handler(req):
        seen.append(req.url.path)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /")
        return httpx.Response(200, text="should not be requested")

    client = PoliteClient(transport=httpx.MockTransport(handler), sleep=lambda s: None, user_agent="t/1.0")
    with pytest.raises(RobotsDisallowed):
        list(McrWebSource(client=client).iter_documents())
    assert seen == ["/robots.txt"]


def test_indent_styles_keep_their_nesting():
    ch8 = (DATA / "Court_Rules_Chapter_8.htm").read_text(encoding="utf-8")
    docs = {d.citation: d for d in parse_chapter(ch8, "8", "u").docs}
    holiday = [ln for ln in docs["MCR 8.110"].text.splitlines() if "New Year" in ln][0]
    assert holiday.startswith("      ") and not holiday.startswith("        ")  # Indent_4 == L4: 6 spaces
    res = parse_chapter(ch8, "8", "u")
    assert not [w for w in res.warnings if "Indent_" in w]
