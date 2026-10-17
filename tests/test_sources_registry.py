"""get_source dispatch for every source kind, and the court sources end to end through ``mlaw ingest`` --
the calls crimdef-persona-ollama's legal_context.py makes (get_document / search source="cji" /
get_sentencing_class) against a DB built only by the CLI."""

import sqlite3
from pathlib import Path

import pytest

from mlaw import cli, config, service
from mlaw.db import connect, init_db
from mlaw.sources import SOURCE_KINDS, get_source

DATA = Path(__file__).resolve().parent / "data" / "court_rules" / "2026-09-29"
CJI_PDF = DATA / "cji" / "criminal-jury-instructions.pdf"
SGM_PDF = DATA / "sgm" / "sgm.pdf"
MRE_PDF = DATA / "mre" / "michigan-rules-of-evidence.pdf"
needs_pdfs = pytest.mark.skipif(
    not (CJI_PDF.exists() and SGM_PDF.exists() and MRE_PDF.exists()),
    reason="court-rules PDFs not present (git-ignored, see tests/data/court_rules/2026-09-29/README.md)")


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setattr(config, "USER_AGENT", "michigan-law-mcp/test (+mailto:test@example.com)")


@pytest.mark.parametrize("kind,cls,needs", [
    ("mcr-files", "McrFilesSource", str(DATA / "mcr")),
    ("mre-files", "MreFilesSource", str(MRE_PDF)),
    ("cji-files", "CjiFilesSource", str(CJI_PDF)),
])
def test_files_kinds_dispatch(kind, cls, needs):
    assert type(get_source(kind, needs)).__name__ == cls


@needs_pdfs
def test_sgm_files_dispatch():
    s = get_source("sgm-files", str(SGM_PDF))
    assert type(s).__name__ == "SgmFilesSource" and s.complete


@pytest.mark.parametrize("kind,cls", [("mcr-web", "McrWebSource"), ("mre-web", "MreWebSource"),
                                      ("sgm-web", "SgmWebSource")])
def test_web_kinds_dispatch_without_fetching(agent, kind, cls):
    s = get_source(kind)
    assert type(s).__name__ == cls and s.client is not None


@pytest.mark.parametrize("kind", ["mre-web"])
def test_web_kinds_refuse_placeholder_user_agent(monkeypatch, kind):
    monkeypatch.setattr(config, "USER_AGENT", "x (+contact: set MLAW_USER_AGENT with a real contact address)")
    with pytest.raises(RuntimeError, match="MLAW_USER_AGENT"):
        get_source(kind)


@pytest.mark.parametrize("kind", ["mcr-files", "mre-files", "cji-files", "sgm-files"])
def test_files_kinds_need_path(kind):
    with pytest.raises(ValueError, match="needs --path"):
        get_source(kind)


def test_unknown_kind_lists_every_choice():
    with pytest.raises(ValueError) as e:
        get_source("cji-web")
    for k in SOURCE_KINDS:
        assert k in str(e.value)


def test_cli_help_names_every_kind(capsys):
    with pytest.raises(SystemExit):
        cli.main(["ingest", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    for k in SOURCE_KINDS:
        assert k in out


def test_notice_for():
    assert config.notice_for("mcl") == config.notice_for("const") == config.notice_for() == config.NOTICE
    assert config.notice_for("cji") == config.notice_for("cji-files") == config.NOTICE_MSC
    assert config.notice_for("sgm-web") == config.NOTICE_MJI
    assert config.notice_for("mcl", "cji", "mre") == f"{config.NOTICE} {config.NOTICE_MSC}"


# ------------------------------------------------------------------------------------- end to end (CLI)

@pytest.fixture(scope="module")
def court_db(tmp_path_factory):
    if not (CJI_PDF.exists() and SGM_PDF.exists()):
        pytest.skip("court-rules PDFs not present")
    db = tmp_path_factory.mktemp("court") / "mlaw.sqlite3"
    assert cli.main(["--db", str(db), "init-db"]) == 0
    assert cli.main(["--db", str(db), "ingest", "--source", "cji-files", "--path", str(CJI_PDF)]) == 0
    assert cli.main(["--db", str(db), "ingest", "--source", "sgm-files", "--path", str(SGM_PDF)]) == 0
    conn = connect(db, readonly=True)
    yield conn
    conn.close()


def test_cji_search_by_statute_number_finds_instruction(court_db):
    """legal_context.py: search({"query": '"750.411h"', "source": "cji"}). M Crim JI 17.25 names the statute
    only in its Use Note, so this is found through fts_notes."""
    r = service.search(court_db, '"750.411h"', source="cji", limit=5)
    hits = {x["citation"]: x for x in r["results"]}
    assert "M Crim JI 17.25" in hits and hits["M Crim JI 17.25"]["catchline"] == "Stalking"
    assert hits["M Crim JI 17.25"]["matched_in"] == "notes"
    assert "411h" in hits["M Crim JI 17.25"]["snippet"]
    assert all(x["source"] == "cji" for x in r["results"]) and r["notice"] == config.NOTICE_MSC


def test_text_matches_rank_before_notes_matches(court_db):
    r = service.search(court_db, "stalking", source="cji", limit=50)
    where = [x["matched_in"] for x in r["results"]]
    assert where[0] == "text" and where == sorted(where, key=lambda w: w != "text")
    assert len({x["citation"] for x in r["results"]}) == len(where)  # no document listed twice


def test_cji_get_document_text_and_notice(court_db):
    d = service.get_document(court_db, "M Crim JI 17.25")
    assert d["text"].startswith("(1) [The defendant is charged with") and "750.411h" in d["compilers_notes"]
    assert d["provenance"]["notice"] == config.NOTICE_MSC
    assert d["provenance"]["source_currency"].startswith("M Crim JI updated")


def test_get_sentencing_class_row_keys(court_db):
    """legal_context.py reads category / class / grid / statutory_maximum / description / current."""
    g = service.get_sentencing_class(court_db, "MCL 750.411h")
    assert g["listed"] and g["rows"]
    row = g["rows"][0]
    assert {k: row[k] for k in ("raw_cite", "category", "class", "grid", "statutory_maximum", "current")} == {
        "raw_cite": "MCL 750.411h(2)(b)", "category": "Person", "class": "E", "grid": "SGM Grid E",
        "statutory_maximum": "5", "current": True}
    assert row["description"] and row["stat_max"] == row["statutory_maximum"]
    assert g["provenance"]["notice"] == config.NOTICE_MJI


def test_data_status_notices(court_db):
    s = service.data_status(court_db)
    by = {x["source"]: x["notice"] for x in s["sources"]}
    assert by == {"cji-files": config.NOTICE_MSC, "sgm-files": config.NOTICE_MJI}
    assert s["notice"] == f"{config.NOTICE_MSC} {config.NOTICE_MJI}"


# ------------------------------------------------------------------- databases built before fts_notes

def test_old_database_gets_fts_notes_backfilled(loaded, tmp_path):
    """A DB from before fts_notes (the live one): search still works, and the next init_db (every ingest
    runs one on its working copy) indexes the notes of exactly the documents in fts."""
    loaded.execute("UPDATE versions SET compilers_notes = 'Popular name: Zebra Act' WHERE is_current = 1")
    loaded.execute("DROP TABLE fts_notes")
    loaded.execute("DELETE FROM meta WHERE key = 'fts_notes_backfilled'")
    loaded.commit()
    assert service.search(loaded, "zebra")["count"] == 0  # no such table: text-only, no error

    init_db(loaded)
    n_fts = loaded.execute("SELECT COUNT(*) FROM fts").fetchone()[0]
    assert loaded.execute("SELECT COUNT(*) FROM fts_notes").fetchone()[0] == n_fts > 0
    r = service.search(loaded, "zebra", limit=50)
    assert r["count"] == n_fts and {x["matched_in"] for x in r["results"]} == {"notes"}

    init_db(loaded)  # once only
    assert loaded.execute("SELECT COUNT(*) FROM fts_notes").fetchone()[0] == n_fts


def test_removed_document_leaves_fts_notes(tmp_path):
    from mlaw.pipeline import _fts_delete, _fts_replace

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    _fts_replace(conn, 7, "M Crim JI 1.1", "x", "body", "Use Note: MCL 750.1")
    assert conn.execute("SELECT COUNT(*) FROM fts_notes WHERE doc_id = 7").fetchone()[0] == 1
    _fts_replace(conn, 7, "M Crim JI 1.1", "x", "body", None)
    assert conn.execute("SELECT COUNT(*) FROM fts_notes WHERE doc_id = 7").fetchone()[0] == 0
    _fts_replace(conn, 7, "M Crim JI 1.1", "x", "body", "Use Note")
    _fts_delete(conn, 7)
    assert conn.execute("SELECT COUNT(*) FROM fts_notes").fetchone()[0] == 0


def test_saved_pdf_sources_cite_the_official_url_not_the_local_file():
    from mlaw.sources.mre import PDF_URL

    s = get_source("mre-files", str(MRE_PDF))
    assert s.url == PDF_URL and "file:" not in s.url
    assert all(d.url.startswith("https://www.courts.michigan.gov/") for d in s.iter_documents())
