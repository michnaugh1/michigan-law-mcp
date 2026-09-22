import pytest

from mlaw import service


def test_get_current(loaded):
    d = service.get_document(loaded, "mcl 750.83(1)")
    assert d["citation"] == "MCL 750.83" and "AMENDED" in d["text"]
    assert d["provenance"]["last_confirmed"] == "2026-09-21T06:00:00Z"
    assert d["provenance"]["notice"]
    assert d["in_source_currently"] is True


def test_as_of_history(loaded):
    old = service.get_document(loaded, "MCL 750.83", as_of="2026-09-20")
    assert "AMENDED" not in old["text"]
    assert old["superseded_by_later_version_observed"] == "2026-09-21T06:00:00Z"
    assert "observed" in old["warning"]

    new = service.get_document(loaded, "MCL 750.83", as_of="2026-09-21")
    assert "AMENDED" in new["text"] and "superseded_by_later_version_observed" not in new

    before = service.get_document(loaded, "MCL 750.83", as_of="2026-09-19")
    assert before["text"] is None and "begins 2026-09-20" in before["warning"]


def test_as_of_validation(loaded):
    with pytest.raises(ValueError):
        service.get_document(loaded, "MCL 750.83", as_of="9/20/2026")
    with pytest.raises(ValueError):
        service.get_document(loaded, "MCL 750.83", as_of="2026-02-31")


def test_removed_document_still_retrievable_but_flagged(loaded):
    d = service.get_document(loaded, "MCL 750.100")
    assert d["in_source_currently"] is False and d["removed_from_source_at"] == "2026-09-21T06:00:00Z"


def test_unknown_and_malformed_citations(loaded):
    with pytest.raises(ValueError, match="not in this database"):
        service.get_document(loaded, "MCL 999.1")
    with pytest.raises(ValueError, match="Could not recognize"):
        service.get_document(loaded, "the penal code")


def test_search(loaded):
    r = service.search(loaded, "assault")
    assert [x["citation"] for x in r["results"]] == ["MCL 750.83"]
    assert "[" in r["results"][0]["snippet"]
    # stemming, phrase, operators, and hostile input do not crash
    assert service.search(loaded, "speeding OR speed")["count"] >= 1
    assert service.search(loaded, '"speed limits"')["count"] == 1
    assert service.search(loaded, 'foo" AND (bar NEAR/2 baz')["count"] == 0
    with pytest.raises(ValueError):
        service.search(loaded, "   ")


def test_search_filters_and_repealed(loaded):
    assert service.search(loaded, "fixture", source="mcl", chapter="257")["count"] == 1
    assert service.search(loaded, "fixture", chapter="chap257")["count"] == 1
    # removed doc (750.100) is gone from the index entirely
    assert all(x["citation"] != "MCL 750.100" for x in service.search(loaded, "repealed", include_repealed=True)["results"])


def test_chapter_outline_sorted_and_paged(loaded):
    o = service.get_chapter_outline(loaded, "750")
    cites = [s["citation"] for s in o["sections"]]
    assert cites == ["MCL 750.83", "MCL 750.84", "MCL 750.85", "MCL 750.520b", "MCL 750.520e"]
    assert o["total"] == 5
    assert service.get_chapter_outline(loaded, "chapter 750", limit=2, offset=3)["sections"][0]["citation"] == "MCL 750.520b"


def test_cross_references(loaded):
    cites = service.get_cross_references(loaded, "MCL 750.84")
    by_key = {r["citation"]: r for r in cites["references"]}
    assert by_key["MCL 750.83"]["in_database"] is True
    assert by_key["MCL 750.83"]["catchline"] == "Fixture assault section"
    assert by_key["MCL 750.520b"]["in_range_endpoint"] is True
    assert by_key["1931 PA 328"]["in_database"] is False  # unresolved cites are kept, flagged
    assert "MCR 6.110" in by_key

    cited_by = service.get_cross_references(loaded, "MCL 750.83", direction="cited_by")
    assert {r["citation"] for r in cited_by["references"]} == {"MCL 750.84", "MCL 750.520b", "MCL 750.85"}
    with pytest.raises(ValueError):
        service.get_cross_references(loaded, "MCL 750.83", direction="sideways")


def test_cross_references_and_get_document_flag_known_repealed_ranges(conn):
    """A dangling MCL cross-reference into a span the compilers themselves recorded as no longer having
    live text (see citations.extract_narrative_ranges -- real examples: MCL 333.13607's own body declaring
    "Sections 13601 to 13606 ... are repealed"; a Compiler's Note reading "Former MCL 333.20701-333.20773
    Expired...") is flagged with the range info instead of a bare 'in_database: false', and get_document
    gives a specific "no section of its own" error rather than a generic "not in this database" for any
    citation the range covers, not just its endpoints."""
    doc_id = conn.execute(
        "INSERT INTO documents (source, citation, chapter, act_citation, act_name, url, first_seen)"
        " VALUES ('mcl', 'MCL 333.13607', '333', '1978 PA 368', 'PUBLIC HEALTH CODE', 'https://x', 'now')"
    ).lastrowid
    conn.execute(
        "INSERT INTO versions (document_id, text, content_hash, first_seen, last_seen)"
        " VALUES (?, 'Sections 13601 to 13606 ... are repealed effective Dec 31, 1993.', 'h', 'now', 'now')",
        (doc_id,),
    )
    conn.execute(
        "INSERT INTO xrefs (from_document_id, to_kind, to_key, raw_text, in_range) VALUES (?, 'mcl', 'MCL 333.13606', '333.13606', 1)",
        (doc_id,),
    )
    conn.execute(
        "INSERT INTO ranges (source, start_citation, end_citation, chapter, act_citation, act_name, note, status, first_seen)"
        " VALUES ('mcl', 'MCL 333.13601', 'MCL 333.13606', '333', '1978 PA 368', 'PUBLIC HEALTH CODE',"
        " 'being sections 333.13601 to 333.13606 of the Michigan Compiled Laws, are repealed', 'repealed', 'now')"
    )

    r = service.get_cross_references(conn, "MCL 333.13607")
    ref = next(x for x in r["references"] if x["citation"] == "MCL 333.13606")
    assert ref["in_database"] is False
    assert ref["known_repealed_range"] == {
        "range": "MCL 333.13601 to MCL 333.13606", "status": "repealed",
        "compilers_line": "being sections 333.13601 to 333.13606 of the Michigan Compiled Laws, are repealed",
        "act_citation": "1978 PA 368", "act_name": "PUBLIC HEALTH CODE",
    }

    with pytest.raises(ValueError, match="has no section of its own"):
        service.get_document(conn, "MCL 333.13603")  # inside the range, not just an endpoint


def test_list_changes(loaded):
    r = service.list_changes(loaded, "2026-09-21")
    kinds = {(c["citation"], c["change_type"]) for c in r["changes"]}
    assert kinds == {("MCL 750.83", "modified"), ("MCL 750.85", "new"), ("MCL 750.100", "removed")}
    assert service.list_changes(loaded, "2026-09-21", change_type="new")["count"] == 1
    assert service.list_changes(loaded, "2026-09-20")["count"] == 9  # 6 new + 3 day-2 changes
    with pytest.raises(ValueError):
        service.list_changes(loaded, "yesterday")


def test_public_act_and_status(loaded):
    pa = service.lookup_public_act(loaded, 2026, 50)
    assert pa["affected_citations"] == ["MCL 750.83", "MCL 750.85"]
    with pytest.raises(ValueError):
        service.lookup_public_act(loaded, 1999, 1)
    st = service.data_status(loaded)["sources"][0]
    assert st["source"] == "mcl" and st["active_documents"] == 6 and st["removed_documents"] == 1
    assert st["last_run_status"] == "ok"
