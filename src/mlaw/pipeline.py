"""Ingest pipeline: upsert documents, version on content change, track removals.

Safety properties
* Content-hash versioning: a new ``versions`` row only when normalized content changes.
* Removal is only inferred from a *complete* crawl, and only if the crawl saw at least
  ``removal_guard`` of the previously active documents. Otherwise the run aborts and the
  database is left untouched (a half-loaded site must never look like mass repeal).
* The whole run is one transaction; any error rolls back and is recorded on the run row.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .citations import extract_xrefs, normalize_citation
from .models import ParsedDoc, ParsedRange, Source


class PartialCrawlError(RuntimeError):
    """Raised when a supposedly complete crawl saw suspiciously few documents."""


def _scope(source) -> set[str] | None:
    cov = getattr(source, "covered_chapters", None)
    return None if cov is None else {str(c) for c in cov}


@dataclass
class RunReport:
    run_id: int
    source: str
    docs_seen: int = 0
    docs_new: int = 0
    docs_modified: int = 0
    docs_removed: int = 0
    docs_restored: int = 0


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_ws(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def content_hash(doc: ParsedDoc) -> str:
    payload = json.dumps(
        [_norm_ws(doc.catchline), _norm_ws(doc.text), _norm_ws(doc.history_note), _norm_ws(doc.compilers_notes), doc.status],
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _fts_replace(conn: sqlite3.Connection, doc_id: int, citation: str, catchline: str, text: str) -> None:
    conn.execute("DELETE FROM fts WHERE doc_id = ?", (doc_id,))
    conn.execute(
        "INSERT INTO fts (citation, catchline, text, doc_id) VALUES (?, ?, ?, ?)",
        (citation, catchline or "", text, doc_id),
    )


def _fts_delete(conn: sqlite3.Connection, doc_id: int) -> None:
    conn.execute("DELETE FROM fts WHERE doc_id = ?", (doc_id,))


def _replace_xrefs(conn: sqlite3.Connection, doc_id: int, citation: str, text: str, history: str) -> None:
    conn.execute("DELETE FROM xrefs WHERE from_document_id = ?", (doc_id,))
    for x in extract_xrefs(f"{text}\n{history}", self_citation=citation):
        conn.execute(
            "INSERT INTO xrefs (from_document_id, to_kind, to_key, raw_text, in_range) VALUES (?,?,?,?,?)",
            (doc_id, x.kind, x.key, x.raw_text, int(x.in_range)),
        )


def run_ingest(
    conn: sqlite3.Connection,
    source: Source,
    *,
    removal_guard: float = 0.9,
    now: str | None = None,
) -> RunReport:
    now = now or utcnow()
    conn.commit()
    cur = conn.execute(
        "INSERT INTO runs (source, started_at, complete_crawl, scope) VALUES (?, ?, ?, ?)",
        (source.name, now, int(source.complete),
         None if _scope(source) is None else "chapters:" + ",".join(sorted(_scope(source)))),
    )
    run_id = int(cur.lastrowid)
    conn.commit()

    report = RunReport(run_id=run_id, source=source.name)
    try:
        conn.execute("BEGIN")
        _ingest_documents(conn, source, report, run_id, now, removal_guard)
        _ingest_public_acts(conn, source)
        _ingest_ranges(conn, source, now)
        conn.execute(
            "UPDATE runs SET status='ok', finished_at=?, docs_seen=?, docs_new=?, docs_modified=?,"
            " docs_removed=?, docs_restored=?, source_currency=? WHERE id=?",
            (now, report.docs_seen, report.docs_new, report.docs_modified,
             report.docs_removed, report.docs_restored, getattr(source, "currency", None), run_id),
        )
        conn.commit()
    except BaseException as e:
        conn.rollback()
        conn.execute(
            "UPDATE runs SET status='failed', finished_at=?, error=? WHERE id=?",
            (utcnow(), f"{type(e).__name__}: {e}", run_id),
        )
        conn.commit()
        raise
    return report


def _ingest_documents(conn, source, report: RunReport, run_id: int, now: str, removal_guard: float) -> None:
    scope = _scope(source)
    in_scope = lambda ch: scope is None or (ch is not None and str(ch) in scope)  # noqa: E731
    # Most sources only ever emit documents under their own .name; the MCL XML/HTML sources can also emit
    # "const" (Chapter 1.xml is the Constitution -- see mcl_web.py's _ChapterSourceBase). Previously-active
    # and removal accounting has to cover every kind a source can legitimately produce, not just .name, or
    # a mixed run would silently undercount and never detect const documents going missing.
    kinds = tuple(getattr(source, "source_kinds", None) or {source.name})
    placeholders = ",".join("?" * len(kinds))
    prev_active = sum(
        1 for r in conn.execute(
            f"SELECT chapter FROM documents WHERE source IN ({placeholders}) AND active = 1", kinds)
        if in_scope(r["chapter"])
    )
    seen_ids: set[int] = set()

    for doc in source.iter_documents():
        canon = normalize_citation(doc.citation)
        if canon is None or canon.canonical != doc.citation or canon.source != doc.source:
            raise ValueError(f"Parser produced non-canonical citation {doc.citation!r} for its own source {doc.source!r}")
        if doc.source not in kinds:
            raise ValueError(f"{doc.citation} has source {doc.source!r}, outside this source's declared kinds {sorted(kinds)}")
        if scope is not None and not in_scope(doc.chapter):
            raise ValueError(f"{doc.citation} is in chapter {doc.chapter!r}, outside this source's declared scope {sorted(scope)}")
        report.docs_seen += 1
        is_variant = bool(doc.variant)

        row = conn.execute(
            "SELECT id, active FROM documents WHERE source = ? AND citation = ? AND variant = ?",
            (doc.source, doc.citation, doc.variant),
        ).fetchone()
        if row is None:
            doc_id = conn.execute(
                "INSERT INTO documents (source, citation, variant, kind, chapter, act_citation, act_name, url, first_seen)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (doc.source, doc.citation, doc.variant, doc.kind, doc.chapter, doc.act_citation, doc.act_name,
                 doc.url, now),
            ).lastrowid
            was_active, is_new_doc = True, True
        else:
            doc_id, was_active, is_new_doc = row["id"], bool(row["active"]), False
            conn.execute(
                "UPDATE documents SET kind=?, chapter=?, act_citation=?, act_name=?, url=?, active=1, removed_at=NULL"
                " WHERE id=?",
                (doc.kind, doc.chapter, doc.act_citation, doc.act_name, doc.url, doc_id),
            )
        seen_ids.add(doc_id)

        h = content_hash(doc)
        cur = conn.execute(
            "SELECT id, content_hash FROM versions WHERE document_id = ? AND is_current = 1", (doc_id,)
        ).fetchone()

        if cur is not None and cur["content_hash"] == h:
            conn.execute("UPDATE versions SET last_seen=? WHERE id=?", (now, cur["id"]))
            if not was_active:
                if not is_variant:
                    _fts_replace(conn, doc_id, doc.citation, doc.catchline, doc.text)
                conn.execute(
                    "INSERT INTO changes (run_id, document_id, version_id, change_type, at) VALUES (?,?,?,?,?)",
                    (run_id, doc_id, cur["id"], "restored", now),
                )
                report.docs_restored += 1
            continue

        if cur is not None:
            conn.execute("UPDATE versions SET is_current = 0 WHERE id = ?", (cur["id"],))
        vid = conn.execute(
            "INSERT INTO versions (document_id, catchline, text, history_note, compilers_notes, status,"
            " effective_date, content_hash, first_seen, last_seen, is_current, run_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,1,?)",
            (doc_id, doc.catchline, doc.text, doc.history_note, doc.compilers_notes, doc.status, doc.effective_date,
             h, now, now, run_id),
        ).lastrowid
        if not is_variant:  # extra forms (e.g. text not yet in force) are never searchable as if they were the law
            _fts_replace(conn, doc_id, doc.citation, doc.catchline, doc.text)
            _replace_xrefs(conn, doc_id, doc.citation, doc.text, f"{doc.history_note}\n{doc.compilers_notes}")

        if is_new_doc or cur is None:
            ctype, report.docs_new = "new", report.docs_new + 1
        elif not was_active:
            ctype, report.docs_restored = "restored", report.docs_restored + 1
        else:
            ctype, report.docs_modified = "modified", report.docs_modified + 1
        conn.execute(
            "INSERT INTO changes (run_id, document_id, version_id, change_type, at) VALUES (?,?,?,?,?)",
            (run_id, doc_id, vid, ctype, now),
        )

    if not source.complete:
        return

    if prev_active and report.docs_seen < removal_guard * prev_active:
        raise PartialCrawlError(
            f"Complete crawl saw {report.docs_seen} of {prev_active} previously active documents "
            f"(< {removal_guard:.0%}); refusing to mark the rest removed."
        )
    missing = conn.execute(
        f"SELECT id, chapter FROM documents WHERE source IN ({placeholders}) AND active = 1", kinds
    ).fetchall()
    for r in missing:
        if r["id"] in seen_ids or not in_scope(r["chapter"]):
            continue
        conn.execute("UPDATE documents SET active = 0, removed_at = ? WHERE id = ?", (now, r["id"]))
        _fts_delete(conn, r["id"])
        cur_v = conn.execute(
            "SELECT id FROM versions WHERE document_id = ? AND is_current = 1", (r["id"],)
        ).fetchone()
        conn.execute(
            "INSERT INTO changes (run_id, document_id, version_id, change_type, at) VALUES (?,?,?,?,?)",
            (run_id, r["id"], cur_v["id"] if cur_v else None, "removed", now),
        )
        report.docs_removed += 1


def _ingest_public_acts(conn: sqlite3.Connection, source: Source) -> None:
    for pa in source.iter_public_acts():
        conn.execute(
            "INSERT INTO public_acts (year, number, title, effective_date, url) VALUES (?,?,?,?,?)"
            " ON CONFLICT(year, number) DO UPDATE SET title=excluded.title,"
            " effective_date=excluded.effective_date, url=excluded.url",
            (pa.year, pa.number, pa.title, pa.effective_date, pa.url),
        )
        conn.execute("DELETE FROM public_act_targets WHERE year=? AND number=?", (pa.year, pa.number))
        for c in pa.affected_citations:
            canon = normalize_citation(c)
            if canon is None:
                raise ValueError(f"Public act {pa.year} PA {pa.number}: bad citation {c!r}")
            conn.execute(
                "INSERT OR IGNORE INTO public_act_targets (year, number, citation) VALUES (?,?,?)",
                (pa.year, pa.number, canon.canonical),
            )


def _ingest_ranges(conn: sqlite3.Connection, source, now: str) -> None:
    it = getattr(source, "iter_ranges", None)
    if it is None:
        return
    scope = _scope(source)
    seen: set[tuple[str, str]] = set()
    for r in it():  # type: ParsedRange
        for c in (r.start_citation, r.end_citation):
            canon = normalize_citation(c)
            if canon is None or canon.canonical != c or canon.source != r.source:
                raise ValueError(f"Range has non-canonical citation {c!r}")
        conn.execute(
            "INSERT INTO ranges (source, start_citation, end_citation, chapter, act_citation, act_name, note, status,"
            " active, first_seen) VALUES (?,?,?,?,?,?,?,?,1,?)"
            " ON CONFLICT(source, start_citation, end_citation) DO UPDATE SET chapter=excluded.chapter,"
            " act_citation=excluded.act_citation, act_name=excluded.act_name, note=excluded.note,"
            " status=excluded.status, active=1",
            (r.source, r.start_citation, r.end_citation, r.chapter, r.act_citation, r.act_name, r.note, r.status, now),
        )
        seen.add((r.start_citation, r.end_citation))
    if not source.complete:
        return
    for row in conn.execute("SELECT id, start_citation, end_citation, chapter FROM ranges WHERE source = ? AND active = 1",
                            (source.name,)).fetchall():
        if (row["start_citation"], row["end_citation"]) in seen:
            continue
        if scope is not None and str(row["chapter"]) not in scope:
            continue
        conn.execute("UPDATE ranges SET active = 0 WHERE id = ?", (row["id"],))
