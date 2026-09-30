import json
from pathlib import Path

import httpx
import pytest

from conftest import DAY1
from mlaw import service
from mlaw.citations import normalize_citation
from mlaw.pipeline import run_ingest
from mlaw.sources import courts_api
from mlaw.sources.mre import MreFilesSource, MreWebSource, parse_mre_text, pdf_to_text
from mlaw.web import Fetched, PoliteClient, RobotsDisallowed

DATA = Path(__file__).resolve().parent / "data" / "court_rules" / "2026-09-29"
PDF = DATA / "mre" / "michigan-rules-of-evidence.pdf"


@pytest.fixture(scope="module")
def src():
    s = MreFilesSource(PDF)
    s.result  # parse once
    return s


@pytest.fixture(scope="module")
def docs(src):
    return {d.citation: d for d in src.iter_documents()}


def indent(text, prefix):
    line = next(ln for ln in text.split("\n") if ln.lstrip().startswith(prefix))
    return len(line) - len(line.lstrip())


def test_rule_count_matches_toc(src):
    r = src.result
    assert r.toc_rule_count == len(r.docs) == 65
    assert src.complete is True


def test_expected_citations_and_round_trip(docs):
    for n in (101, 404, 608, 609, 613, 702, 803, 1101):
        assert f"MRE {n}" in docs
    for cit, d in docs.items():
        c = normalize_citation(cit)
        assert c is not None and c.source == "mre" and c.canonical == cit


def test_chapter_is_article_number(docs):
    assert docs["MRE 609"].chapter == "6"
    assert docs["MRE 1101"].chapter == "11"
    assert docs["MRE 803A"].chapter == "8"
    assert docs["MRE 609"].kind == "rule" and docs["MRE 609"].status == "active"


def test_wrapped_headings_reassembled(docs):
    assert docs["MRE 105"].catchline.endswith("for Other Purposes")
    assert docs["MRE 403"].catchline.endswith("Waste of Time, or Other Reasons")
    assert docs["MRE 403"].text.startswith("The court may exclude")


def test_mre_609(docs):
    d = docs["MRE 609"]
    assert d.catchline == "Impeachment by Evidence of a Criminal Conviction"
    assert "(a) In General." in d.text
    assert "(2) the crime contained an element of theft; and" in d.text
    assert not d.text.startswith("Rule 609")
    assert indent(d.text, "(a) In General.") < indent(d.text, "(2) the crime contained") < indent(
        d.text, "(A) in the convicting jurisdiction")
    # third level under (A): MRE 404(a)(2)(B)(i)
    t = docs["MRE 404"].text
    assert indent(t, "(a) Character") < indent(t, "(2) Exceptions") < indent(t, "(B) in a homicide") < indent(
        t, "(i) offer evidence")


def test_no_furniture_in_texts(docs):
    for cit, d in docs.items():
        for bad in ("Updated with MSC order", "effective on 1/28/2026", "\f", "....", "RULES 101", "Table of Contents"):
            assert bad not in d.text, (cit, bad)
        # the only rules whose own text names the publication
        if cit not in ("MRE 1101", "MRE 1102"):
            assert "Michigan Rules of Evidence" not in d.text, cit
        assert d.text == d.text.strip() and "\n\n\n" not in d.text


def test_currency(src):
    assert src.result.currency_date == "2026-01-28"
    assert src.currency == "MRE updated with MSC order(s) effective 2026-01-28"
    assert src.source_kinds == {"mre"} and src.covered_chapters is None
    assert list(src.iter_public_acts()) == [] and list(src.iter_ranges()) == []


def test_toc_mismatch_makes_source_incomplete(tmp_path, monkeypatch):
    text = pdf_to_text(PDF)
    bad = text.replace("\nRule 613 Witness", "\nRule 613X Witness", 1) if "\nRule 613 Witness" in text else None
    assert bad is not None
    r = parse_mre_text(bad, "u")
    assert len(r.docs) == 64 and any("mismatch" in w for w in r.warnings)
    s = MreFilesSource(PDF)
    s._result = r
    assert s.complete is False


def test_ingest_and_get_document(conn):
    s = MreFilesSource(PDF)
    rep = run_ingest(conn, s, now=DAY1)
    assert rep.docs_seen == 65
    doc = service.get_document(conn, "MRE 404")
    assert doc["citation"] == "MRE 404"
    assert "Character Evidence; Other Crimes, Wrongs, or Acts" in json.dumps(doc)
    assert "Prohibited Uses" in doc["text"]
    run = conn.execute("SELECT source_currency, complete_crawl FROM runs").fetchone()
    assert run["source_currency"].endswith("2026-01-28") and run["complete_crawl"] == 1


class FakeClient:
    def __init__(self):
        self.urls = []
        self.kids = json.loads((DATA / "catalog.children.excerpt.json").read_text())
        self.pdf = PDF.read_bytes()

    def get(self, url, headers=None, **kw):
        self.urls.append(url)
        if "contentUrl=" in url:
            page = [{"contentLink": {"id": 4242, "url": "x"}, "name": "Current Rules"}]
            return Fetched(url, 200, json.dumps(page))
        if url.endswith("/4242/children?top=100"):
            return Fetched(url, 200, json.dumps(self.kids))
        assert url.endswith("michigan-rules-of-evidence.pdf"), url
        return Fetched(url, 200, "", content=self.pdf)


def test_web_source_with_fake_client(conn):
    c = FakeClient()
    s = MreWebSource(c)
    assert s.complete is True
    docs = list(s.iter_documents())
    assert len(docs) == 65 and docs[0].url.endswith("/michigan-rules-of-evidence.pdf")
    assert s.currency.endswith("2026-01-28")
    assert len(s.sha256) == 64
    assert len(c.urls) == 3  # page, children, pdf -- and parsing is cached
    run_ingest(conn, s, now=DAY1)
    assert len(c.urls) == 3


def test_web_source_missing_link():
    c = FakeClient()
    c.kids = [k for k in c.kids if k["name"] != "Michigan Rules of Evidence"]
    with pytest.raises(courts_api.CourtsApiError):
        MreWebSource(c).result


def test_real_client_refuses_when_robots_disallows():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /")
        return httpx.Response(200, text="[]")

    client = PoliteClient(transport=httpx.MockTransport(handler), sleep=lambda s: None)
    with pytest.raises(RobotsDisallowed):
        MreWebSource(client).result
    assert calls == ["/robots.txt"]


def test_pdftotext_missing(monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise FileNotFoundError("pdftotext")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(RuntimeError, match="pdftotext"):
        pdf_to_text(PDF)
