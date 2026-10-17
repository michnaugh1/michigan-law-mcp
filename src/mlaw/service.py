"""Read-side query logic. Pure functions over a sqlite3 connection so they are easy to test;
``server.py`` wraps them as MCP tools.

Every result that returns statutory text carries a ``provenance`` block (source URL, when we
last confirmed the text, when this version was first observed) and the standing NOTICE.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from typing import Any

from .citations import citation_sort_key, normalize_citation
from .config import NOTICE, NOTICE_MJI, notice_for
from .history import parse_history

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _check_date(value: str, name: str) -> str:
    if not _DATE_RE.match(value):
        raise ValueError(f"{name} must be YYYY-MM-DD, got {value!r}")
    try:
        date.fromisoformat(value)
    except ValueError as e:
        raise ValueError(f"{name} is not a real date: {value!r}") from e
    return value


def _require_citation(text: str):
    c = normalize_citation(text)
    if c is None:
        raise ValueError(
            f"Could not recognize {text!r} as a citation. Examples: 'MCL 750.83', 'MCR 6.110', "
            "'MRE 404', 'M Crim JI 5.1', 'SGM OV 10', 'Const 1963, art 1, § 17'."
        )
    return c


def _doc_row(conn: sqlite3.Connection, source: str, citation: str):
    return conn.execute(
        "SELECT * FROM documents WHERE source = ? AND citation = ? AND variant = ''", (source, citation)
    ).fetchone()


def _provenance(conn, doc, ver) -> dict[str, Any]:
    # runs.source is the crawler's name ("mcl-xml-web", "mre-files"), not the document kind in
    # documents.source ("mcl", "mre"), so find the crawler through the run that produced this version and
    # take that crawler's latest currency stamp. (Looking it up by documents.source never matched.)
    cur = conn.execute(
        "SELECT source_currency FROM runs WHERE source = (SELECT source FROM runs WHERE id = ?)"
        " AND status = 'ok' AND source_currency IS NOT NULL ORDER BY id DESC LIMIT 1",
        (ver["run_id"],),
    ).fetchone()
    return {
        "source_url": doc["url"],
        "last_confirmed": ver["last_seen"],
        "version_first_observed": ver["first_seen"],
        "source_currency": cur["source_currency"] if cur else None,
        "notice": notice_for(doc["source"]),
    }


def _range_hits(conn, source: str, citation: str) -> list[dict[str, Any]]:
    key = citation_sort_key(citation)
    out = []
    for r in conn.execute("SELECT * FROM ranges WHERE source = ? AND active = 1", (source,)):
        if citation_sort_key(r["start_citation"]) <= key <= citation_sort_key(r["end_citation"]):
            out.append({
                "range": f"{r['start_citation']} to {r['end_citation']}",
                "status": r["status"],
                "compilers_line": r["note"],
                "act_citation": r["act_citation"],
                "act_name": r["act_name"],
            })
    return out


def _variant_forms(conn, doc) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT d.variant, d.url, d.active, v.* FROM documents d JOIN versions v ON v.document_id = d.id AND v.is_current = 1"
        " WHERE d.source = ? AND d.citation = ? AND d.variant != '' AND d.active = 1 ORDER BY d.variant",
        (doc["source"], doc["citation"]),
    ).fetchall()
    return [
        {
            "variant": r["variant"],
            "effective_date": r["effective_date"],
            "catchline": r["catchline"],
            "text": r["text"],
            "history_note": r["history_note"],
            "compilers_notes": r["compilers_notes"],
            "source_url": r["url"],
            "caution": (
                "Another form of this citation listed by the source. It is NOT the text currently in force unless the "
                "site notice in compilers_notes says it is; read that notice and effective_date before relying on it."
            ),
        }
        for r in rows
    ]


def get_document(conn: sqlite3.Connection, citation: str, as_of: str | None = None) -> dict[str, Any]:
    c = _require_citation(citation)
    doc = _doc_row(conn, c.source, c.canonical)
    if doc is None:
        hits = _range_hits(conn, c.source, c.canonical)
        if hits:
            h = hits[0]
            raise ValueError(
                f"{c.canonical} has no section of its own. The compilers list it inside {h['range']}: "
                f"\"{h['compilers_line']}\" (act: {h['act_name'] or h['act_citation'] or 'unknown'})."
            )
        raise ValueError(
            f"{c.canonical} is not in this database. It may not exist, may not be ingested yet, or the "
            "citation may be formatted differently. Try search() or get_chapter_outline()."
        )

    base: dict[str, Any] = {
        "citation": doc["citation"],
        "source": doc["source"],
        "chapter": doc["chapter"],
        "act_citation": doc["act_citation"],
        "act_name": doc["act_name"],
        "in_source_currently": bool(doc["active"]),
    }
    forms = _variant_forms(conn, doc)
    if forms:
        base["other_forms"] = forms
    if doc["source"] == "mcl":
        sentencing = _sentencing_rows(conn, doc["citation"])
        if sentencing:
            base["sentencing"] = [
                {k: r[k] for k in ("raw_cite", "category", "class", "grid", "description", "stat_max", "current",
                                   "page")}
                for r in sentencing
            ]
            base["sentencing_source"] = (
                f"Sentencing Guidelines Manual felony list, {sentencing[0]['edition']}. Advisory guidelines; "
                "call get_sentencing_class for the full rows and caveats."
            )
    if not doc["active"]:
        base["removed_from_source_at"] = doc["removed_at"]

    if as_of is None:
        ver = conn.execute(
            "SELECT * FROM versions WHERE document_id = ? AND is_current = 1", (doc["id"],)
        ).fetchone()
        if ver is None:
            raise ValueError(f"{c.canonical} has no stored text")
        return {**base, **_version_payload(conn, doc, ver), "as_of": None}

    as_of = _check_date(as_of, "as_of")
    earliest = conn.execute(
        "SELECT MIN(first_seen) FROM versions WHERE document_id = ?", (doc["id"],)
    ).fetchone()[0]
    ver = conn.execute(
        "SELECT * FROM versions WHERE document_id = ? AND substr(first_seen, 1, 10) <= ?"
        " ORDER BY first_seen DESC, id DESC LIMIT 1",
        (doc["id"], as_of),
    ).fetchone()
    if ver is None:
        return {
            **base,
            "as_of": as_of,
            "text": None,
            "warning": (
                f"No version of {c.canonical} was observed on or before {as_of}. This database's history "
                f"for it begins {earliest}. Use an official historical compilation for earlier text."
            ),
            "notice": notice_for(doc["source"]),
        }
    current = conn.execute(
        "SELECT id, first_seen FROM versions WHERE document_id = ? AND is_current = 1", (doc["id"],)
    ).fetchone()
    payload = {**base, **_version_payload(conn, doc, ver), "as_of": as_of}
    payload["warning"] = (
        "Versions reflect when this server first observed each text, not legal effective dates. "
        "For an offense-date question, confirm effective dates against the amending Public Act. "
        + (f"History for this citation begins {earliest}." if earliest else "")
    )
    if current and current["id"] != ver["id"]:
        payload["superseded_by_later_version_observed"] = current["first_seen"]
    return payload


def _version_payload(conn, doc, ver) -> dict[str, Any]:
    return {
        "catchline": ver["catchline"],
        "text": ver["text"],
        "history_note": ver["history_note"],
        "history_entries": [
            {"kind": h.kind, "act": h.act_citation, "immediate_effect": h.immediate_effect,
             "effective_date": h.effective_iso, "effective_text": h.effective_raw, "raw": h.raw}
            for h in parse_history(ver["history_note"] or "")
        ],
        "compilers_notes": ver["compilers_notes"],
        "status": ver["status"],
        "effective_date": ver["effective_date"],
        "provenance": _provenance(conn, doc, ver),
    }


_GRID_CLASSES = ("M2", "A", "B", "C", "D", "E", "F", "G", "H")


def _sgm_pdf_url(conn) -> str | None:
    """The manual's URL, taken from any stored SGM unit (their URLs are "<pdf>#page=N")."""
    row = conn.execute("SELECT url FROM documents WHERE source = 'sgm' AND url IS NOT NULL LIMIT 1").fetchone()
    return row["url"].split("#", 1)[0] if row else None


def _subsection(raw_cite: str) -> str:
    """"(1)(a)" from "MCL 750.81(1)(a)" / "MCL 28.468(1) (c)" / "MCL 257.309(6)*"; "" for a bare section."""
    m = re.match(r"^(?:MCL\s*)?[0-9A-Za-z]+\.[0-9A-Za-z]+(.*)$", raw_cite.strip(), re.I)
    return re.sub(r"[\s*]", "", m.group(1)).lower() if m else ""


def _sentencing_rows(conn: sqlite3.Connection, canonical: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT rowid, * FROM sentencing_offenses WHERE mcl_citation = ? ORDER BY current DESC, page, rowid",
        (canonical,),
    ).fetchall()
    pdf = _sgm_pdf_url(conn) if rows else None
    out = []
    for r in rows:
        cls = r["offense_class"]
        grid = f"SGM Grid {cls}" if cls in _GRID_CLASSES else None
        item: dict[str, Any] = {
            "raw_cite": r["raw_cite"],
            "listed_in": r["listed_in"],
            "category": r["category"],
            "class": cls,
            "grid": grid,
            "description": r["description"],
            "stat_max": r["stat_max"],
            "statutory_maximum": r["stat_max"],  # same value; the name crimdef-persona-ollama reads
            "amendments": r["amendments"],
            "current": bool(r["current"]),
            "page": r["page"],
            "source_url": f"{pdf}#page={r['page']}" if pdf else None,
            "edition": r["edition"],
        }
        if grid is None:
            item["grid_note"] = (
                f"The manual prints class {cls!r} here, which is not one of the grid classes (M2, A-H), so no "
                "single grid applies. Read the manual page and the statute it is listed under "
                f"({r['listed_in'] or 'see the page'}) before scoring."
            )
        out.append(item)
    return out


def get_sentencing_class(conn: sqlite3.Connection, citation: str) -> dict[str, Any]:
    """Offense class / category rows for an MCL section from the Sentencing Guidelines Manual's felony list.

    Matches on the canonical section; a subsection in the input ("MCL 750.84(1)(a)") is reported against
    each row's printed subsection but never filters rows out."""
    c = _require_citation(citation)
    if c.source != "mcl":
        raise ValueError(f"{c.canonical} is not an MCL section; the felony list is indexed by MCL citation.")
    wanted = _subsection(citation)
    rows = _sentencing_rows(conn, c.canonical)
    loaded = conn.execute("SELECT edition FROM sentencing_offenses LIMIT 1").fetchone()
    last = conn.execute(
        "SELECT finished_at FROM runs WHERE source LIKE 'sgm%' AND status = 'ok' AND source_currency = ?"
        " ORDER BY id DESC LIMIT 1", (loaded["edition"] if loaded else None,),
    ).fetchone()
    result: dict[str, Any] = {
        "citation": c.canonical,
        "requested_subsection": wanted or None,
        "listed": bool(rows),
        "count": len(rows),
    }
    if not rows:
        result["message"] = (
            f"{c.canonical} is not in the sentencing guidelines felony list ({loaded['edition']}). It may not be "
            "a guidelines offense (e.g. a misdemeanor, or a felony the guidelines do not cover), or the manual "
            "may list it under a different section number. Check the manual itself before concluding."
            if loaded else
            "The sentencing guidelines felony list has not been loaded into this database (no SGM ingest yet)."
        )
    for r in rows:
        sub = _subsection(r["raw_cite"])
        if not wanted:
            r["subsection_match"] = None
        elif sub == wanted:
            r["subsection_match"] = "exact"
        elif sub.startswith(wanted):
            r["subsection_match"] = "narrower"  # the row is a part of the requested subsection
        elif not sub or wanted.startswith(sub):
            r["subsection_match"] = "broader"  # the row covers the whole section / an enclosing subsection
        else:
            r["subsection_match"] = "other"
    result["rows"] = rows
    result["matched_raw_cites"] = sorted({r["raw_cite"] for r in rows})
    if wanted:
        result["matched_subsection_cites"] = sorted(
            {r["raw_cite"] for r in rows if r["subsection_match"] in ("exact", "narrower", "broader")})
    result["provenance"] = {
        "source": "Michigan Sentencing Guidelines Manual (Michigan Judicial Institute), MCL # Felony List",
        "edition": loaded["edition"] if loaded else None,
        "source_url": _sgm_pdf_url(conn),
        "last_loaded": last["finished_at"] if last else None,
        "notice": NOTICE_MJI,
    }
    result["note"] = (
        "Rows are listed current first. current=false means the manual shades the row: the statute governing "
        "that offense description is no longer current. The class selects the sentencing grid (grid). The "
        "manual states the guidelines are advisory after People v Lockridge, 498 Mich 358 (2015), though the "
        "court must still score them and take the range into account. Descriptions are the manual's shorthand, "
        "not the statute; verify against the manual page and the statute before relying on a row."
    )
    return result


def _fts_query(user_query: str) -> str:
    """Turn agent/user text into a safe FTS5 expression. Supports "quoted phrases" and the
    uppercase operators AND / OR / NOT; every other word is matched as a stemmed term."""
    parts: list[str] = []
    for m in re.finditer(r'"([^"]+)"|(\S+)', user_query):
        phrase, word = m.group(1), m.group(2)
        if phrase is not None:
            parts.append('"' + phrase.replace('"', " ").strip() + '"')
        elif word in ("AND", "OR", "NOT"):
            parts.append(word)
        else:
            cleaned = re.sub(r'["\'\*\(\)\^:{}\[\]]', " ", word).strip()
            if cleaned:
                parts.append('"' + cleaned + '"')
    while parts and parts[-1] in ("AND", "OR", "NOT"):
        parts.pop()
    while parts and parts[0] in ("AND", "OR", "NOT"):
        parts.pop(0)
    if not parts:
        raise ValueError("Empty search query")
    return " ".join(parts)


def search(
    conn: sqlite3.Connection,
    query: str,
    source: str | None = None,
    chapter: str | None = None,
    include_repealed: bool = False,
    limit: int = 10,
) -> dict[str, Any]:
    """Ranked full-text search over current text. If that finds fewer than ``limit`` documents, matches found
    only in a document's notes (``fts_notes``: an M Crim JI's Use Note / statute index, MCL compiler's notes)
    follow, marked ``matched_in: "notes"``; text matches always rank first."""
    limit = max(1, min(int(limit), 50))
    match = _fts_query(query)
    filters, fargs = "", []
    if source:
        filters += " AND d.source = ?"
        fargs.append(source.lower())
    if chapter:
        filters += " AND d.chapter = ?"
        fargs.append(str(chapter).lower().replace("chap", "").strip().upper())
    if not include_repealed:
        filters += " AND v.status = 'active'"
    cols = "d.id, d.citation, d.source, d.chapter, d.url, v.catchline, v.status, v.last_seen"
    try:
        rows = conn.execute(
            f"SELECT {cols}, snippet(fts, 2, '[', ']', ' … ', 24) AS snippet, bm25(fts, 10.0, 5.0, 1.0) AS score"
            " FROM fts JOIN documents d ON d.id = fts.doc_id"
            " JOIN versions v ON v.document_id = d.id AND v.is_current = 1"
            f" WHERE fts MATCH ? AND d.active = 1{filters} ORDER BY score LIMIT ?",
            [match, *fargs, limit],
        ).fetchall()
        found = [(r, "text") for r in rows]
        if len(found) < limit:
            seen = [r["id"] for r in rows]
            try:
                notes = conn.execute(
                    f"SELECT {cols}, snippet(fts_notes, 0, '[', ']', ' … ', 24) AS snippet"
                    " FROM fts_notes JOIN documents d ON d.id = fts_notes.doc_id"
                    " JOIN versions v ON v.document_id = d.id AND v.is_current = 1"
                    f" WHERE fts_notes MATCH ? AND d.active = 1{filters}"
                    + (f" AND d.id NOT IN ({','.join('?' * len(seen))})" if seen else "") +
                    " ORDER BY bm25(fts_notes) LIMIT ?",
                    [match, *fargs, *seen, limit - len(found)],
                ).fetchall()
            except sqlite3.OperationalError as e:
                if "no such table" not in str(e):  # a database not yet re-ingested since fts_notes was added
                    raise
                notes = []
            found += [(r, "notes") for r in notes]
    except sqlite3.OperationalError as e:
        raise ValueError(f"Could not run that search ({e}). Try simpler terms or a quoted phrase.") from e
    return {
        "query": query,
        "count": len(found),
        "results": [
            {
                "citation": r["citation"],
                "source": r["source"],
                "chapter": r["chapter"],
                "catchline": r["catchline"],
                "status": r["status"],
                "snippet": r["snippet"],
                "matched_in": where,
                "source_url": r["url"],
                "last_confirmed": r["last_seen"],
            }
            for r, where in found
        ],
        "notice": notice_for(source) if source else notice_for(*dict.fromkeys(r["source"] for r, _ in found)),
    }


def get_chapter_outline(
    conn: sqlite3.Connection, chapter: str, source: str = "mcl", limit: int = 200, offset: int = 0
) -> dict[str, Any]:
    ch = str(chapter).lower().replace("chapter", "").replace("chap", "").strip().upper()
    rows = conn.execute(
        "SELECT d.citation, d.act_citation, d.act_name, v.catchline, v.status FROM documents d"
        " JOIN versions v ON v.document_id = d.id AND v.is_current = 1"
        " WHERE d.source = ? AND d.chapter = ? AND d.active = 1 AND d.variant = ''",
        (source.lower(), ch),
    ).fetchall()
    rows = sorted(rows, key=lambda r: citation_sort_key(r["citation"]))
    total = len(rows)
    limit = max(1, min(int(limit), 500))
    page = rows[max(0, offset): max(0, offset) + limit]
    return {
        "chapter": ch,
        "source": source.lower(),
        "total": total,
        "offset": max(0, offset),
        "sections": [
            {
                "citation": r["citation"],
                "catchline": r["catchline"],
                "status": r["status"],
                "act_citation": r["act_citation"],
                "act_name": r["act_name"],
            }
            for r in page
        ],
        "notice": notice_for(source),
    }


def get_cross_references(
    conn: sqlite3.Connection, citation: str, direction: str = "cites", limit: int = 100
) -> dict[str, Any]:
    if direction not in ("cites", "cited_by"):
        raise ValueError("direction must be 'cites' or 'cited_by'")
    c = _require_citation(citation)
    doc = _doc_row(conn, c.source, c.canonical)
    limit = max(1, min(int(limit), 500))

    if direction == "cites":
        if doc is None:
            raise ValueError(f"{c.canonical} is not in this database")
        rows = conn.execute(
            "SELECT x.to_kind, x.to_key, x.raw_text, x.in_range, t.catchline AS catchline,"
            " td.id IS NOT NULL AS resolved FROM xrefs x"
            " LEFT JOIN documents td ON td.source = x.to_kind AND td.citation = x.to_key AND td.active = 1 AND td.variant = ''"
            " LEFT JOIN versions t ON t.document_id = td.id AND t.is_current = 1"
            " WHERE x.from_document_id = ? LIMIT ?",
            (doc["id"], limit),
        ).fetchall()
        refs = []
        for r in rows:
            ref = {
                "kind": r["to_kind"],
                "citation": r["to_key"],
                "raw_text": r["raw_text"],
                "in_range_endpoint": bool(r["in_range"]),
                "in_database": bool(r["resolved"]),
                "catchline": r["catchline"],
            }
            if not ref["in_database"] and r["to_kind"] == "mcl":
                hits = _range_hits(conn, "mcl", r["to_key"])
                if hits:
                    ref["known_repealed_range"] = hits[0]
            refs.append(ref)
    else:
        rows = conn.execute(
            "SELECT d.citation, d.source, v.catchline, x.raw_text, x.in_range FROM xrefs x"
            " JOIN documents d ON d.id = x.from_document_id AND d.active = 1 AND d.variant = ''"
            " JOIN versions v ON v.document_id = d.id AND v.is_current = 1"
            " WHERE x.to_kind = ? AND x.to_key = ? LIMIT ?",
            (c.source, c.canonical, limit),
        ).fetchall()
        refs = [
            {
                "citation": r["citation"],
                "source": r["source"],
                "catchline": r["catchline"],
                "raw_text": r["raw_text"],
                "in_range_endpoint": bool(r["in_range"]),
            }
            for r in rows
        ]
    return {
        "citation": c.canonical,
        "direction": direction,
        "count": len(refs),
        "references": refs,
        "note": (
            "Cross-references are extracted automatically from explicit citations in the text. "
            "Range endpoints ('X to Y') do not list the sections in between. References written "
            "without a citation (e.g. 'this section') are not captured. An unresolved MCL reference "
            "('in_database': false) carries 'known_repealed_range' when the compilers themselves have "
            "recorded that citation as repealed or expired; if that key is absent, the citation is "
            "simply not in this database (not yet ingested, or genuinely nonexistent -- e.g. a typo in "
            "the source text)."
        ),
        "notice": notice_for(c.source, *(r.get("source") or r.get("kind") for r in refs)),
    }


def list_changes(
    conn: sqlite3.Connection,
    since: str,
    source: str | None = None,
    change_type: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    since = _check_date(since, "since")
    limit = max(1, min(int(limit), 500))
    sql = (
        "SELECT c.at, c.change_type, d.citation, d.variant, d.source, v.catchline FROM changes c"
        " JOIN documents d ON d.id = c.document_id LEFT JOIN versions v ON v.id = c.version_id"
        " WHERE substr(c.at, 1, 10) >= ?"
    )
    args: list[Any] = [since]
    if source:
        sql += " AND d.source = ?"
        args.append(source.lower())
    if change_type:
        if change_type not in ("new", "modified", "removed", "restored"):
            raise ValueError("change_type must be new, modified, removed, or restored")
        sql += " AND c.change_type = ?"
        args.append(change_type)
    sql += " ORDER BY c.at DESC, d.id LIMIT ?"
    args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    first_run = conn.execute("SELECT MIN(started_at) FROM runs WHERE status = 'ok'").fetchone()[0]
    return {
        "since": since,
        "count": len(rows),
        "changes": [dict(r) for r in rows],
        "note": (
            "The first successful ingest reports every section as 'new'; changes are meaningful only "
            f"after it. First successful ingest: {first_run}."
        ),
    }


def lookup_public_act(conn: sqlite3.Connection, year: int, number: int) -> dict[str, Any]:
    pa = conn.execute(
        "SELECT * FROM public_acts WHERE year = ? AND number = ?", (int(year), int(number))
    ).fetchone()
    if pa is None:
        raise ValueError(f"{year} PA {number} is not in this database")
    targets = conn.execute(
        "SELECT citation FROM public_act_targets WHERE year = ? AND number = ?", (int(year), int(number))
    ).fetchall()
    return {
        "public_act": f"{pa['year']} PA {pa['number']}",
        "title": pa["title"],
        "effective_date": pa["effective_date"],
        "source_url": pa["url"],
        "affected_citations": sorted((t["citation"] for t in targets), key=citation_sort_key),
        "notice": NOTICE,
    }


def data_status(conn: sqlite3.Connection) -> dict[str, Any]:
    sources = []
    for r in conn.execute("SELECT DISTINCT source FROM runs ORDER BY source"):
        s = r["source"]
        last_ok = conn.execute(
            "SELECT * FROM runs WHERE source = ? AND status = 'ok' ORDER BY id DESC LIMIT 1", (s,)
        ).fetchone()
        last_any = conn.execute(
            "SELECT * FROM runs WHERE source = ? ORDER BY id DESC LIMIT 1", (s,)
        ).fetchone()
        # Filter by the *crawler's* source (runs.source, e.g. "mcl") via each document's current version's
        # own run, not by documents.source directly -- those are different namespaces. documents.source is
        # the document's own kind (mcl | mcr | mre | const | cji | sgm, ParsedDoc.source), and one crawler run can
        # legitimately produce more than one kind: MclXmlWebSource/MclXmlFilesSource (name="mcl") emit both
        # "mcl" and "const" documents in the same run (Chapter 1.xml sits in the same crawl as every other
        # chapter -- see pipeline.py's Source.source_kinds). The original `WHERE source = ?` filtered
        # documents.source = 'mcl' literally, so it silently excluded every const document from this report
        # entirely -- confirmed live 2026-09-22: after the first full production crawl (43,929 docs_new,
        # including the Constitution's 281), `mlaw status` reported only 43,648 total (43,424 active + 224
        # variants) for "mcl" -- missing exactly 281, the Constitution's whole section count. Joining through
        # versions/runs instead groups by which crawl actually produced each document, matching what a
        # human means by "how did the mcl-xml-web crawl go" regardless of the internal document-kind split.
        counts = conn.execute(
            "SELECT SUM(d.active = 1 AND d.variant = '') AS active, SUM(d.active = 0 AND d.variant = '') AS removed,"
            " SUM(d.active = 1 AND d.variant != '') AS variants, COUNT(*) AS total"
            " FROM documents d JOIN versions v ON v.document_id = d.id AND v.is_current = 1"
            " JOIN runs r ON r.id = v.run_id WHERE r.source = ?",
            (s,),
        ).fetchone()
        sources.append(
            {
                "source": s,
                "active_documents": counts["active"] or 0,
                "removed_documents": counts["removed"] or 0,
                "extra_forms_listed": counts["variants"] or 0,
                "last_successful_run": last_ok["finished_at"] if last_ok else None,
                "source_currency": last_ok["source_currency"] if last_ok else None,
                "last_full_crawl": (lambda r: r["finished_at"] if r else None)(conn.execute(
                    "SELECT finished_at FROM runs WHERE source = ? AND status = 'ok' AND complete_crawl = 1"
                    " AND scope IS NULL ORDER BY id DESC LIMIT 1", (s,)).fetchone()),
                "last_run_status": last_any["status"] if last_any else None,
                "last_run_error": last_any["error"] if last_any and last_any["status"] == "failed" else None,
            }
        )
    for x in sources:
        x["notice"] = notice_for(x["source"])
    return {"sources": sources, "notice": notice_for(*(x["source"] for x in sources))}
