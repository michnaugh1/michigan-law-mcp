"""SQLite schema and connection helpers.

Design notes
------------
* ``documents`` holds one row per citable unit (an MCL section, a court rule, ...).
* ``versions`` holds every distinct text we have ever observed for a document. A new row is
  written only when the content hash changes, so history accumulates from the first ingest.
  ``first_seen`` / ``last_seen`` are *observation* times, not legal effective dates.
* ``xrefs`` stores outgoing citations found in a document's text. Resolution to a target
  document happens at query time by joining on the canonical citation, so cites to sections
  ingested later resolve automatically and unresolved cites are never dropped.
* ``fts`` is a standalone FTS5 table containing only the *current* text of active documents.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY,
    source         TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL DEFAULT 'running',   -- running | ok | failed
    complete_crawl INTEGER NOT NULL DEFAULT 0,
    scope          TEXT,                              -- NULL = whole source; 'chapters:37,38' = only those
    docs_seen      INTEGER NOT NULL DEFAULT 0,
    docs_new       INTEGER NOT NULL DEFAULT 0,
    docs_modified  INTEGER NOT NULL DEFAULT 0,
    docs_removed   INTEGER NOT NULL DEFAULT 0,
    docs_restored  INTEGER NOT NULL DEFAULT 0,
    source_currency TEXT,                             -- the site's own stamp, e.g. 'Complete Through PA 91 of 2026'
    error          TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,                        -- mcl | mcr | mre | const
    citation    TEXT NOT NULL,                        -- canonical, e.g. "MCL 750.83"
    variant     TEXT NOT NULL DEFAULT '',                -- '' = ordinary node; 'amended' | 'added' | '[1]' ... = extra forms the site lists
    kind        TEXT NOT NULL DEFAULT 'section',
    chapter     TEXT,                                 -- e.g. "750"
    act_citation TEXT,                                -- e.g. "1931 PA 328"
    act_name    TEXT,
    url         TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    first_seen  TEXT NOT NULL,
    removed_at  TEXT,
    UNIQUE (source, citation, variant)
);
CREATE INDEX IF NOT EXISTS documents_chapter ON documents (source, chapter);

CREATE TABLE IF NOT EXISTS versions (
    id             INTEGER PRIMARY KEY,
    document_id    INTEGER NOT NULL REFERENCES documents (id),
    catchline      TEXT,
    text           TEXT NOT NULL,
    history_note   TEXT,
    compilers_notes TEXT,
    status         TEXT NOT NULL DEFAULT 'active',    -- active | repealed | expired | reserved
    effective_date TEXT,                              -- only if the parser can supply it
    content_hash   TEXT NOT NULL,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    is_current     INTEGER NOT NULL DEFAULT 1,
    run_id         INTEGER REFERENCES runs (id)
);
CREATE INDEX IF NOT EXISTS versions_doc ON versions (document_id, first_seen);
CREATE INDEX IF NOT EXISTS versions_current ON versions (document_id, is_current);

CREATE TABLE IF NOT EXISTS changes (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER NOT NULL REFERENCES runs (id),
    document_id INTEGER NOT NULL REFERENCES documents (id),
    version_id  INTEGER REFERENCES versions (id),
    change_type TEXT NOT NULL,                        -- new | modified | removed | restored
    at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS changes_at ON changes (at);

CREATE TABLE IF NOT EXISTS xrefs (
    id               INTEGER PRIMARY KEY,
    from_document_id INTEGER NOT NULL REFERENCES documents (id),
    to_kind          TEXT NOT NULL,                   -- mcl | mcr | mre | const | public_act
    to_key           TEXT NOT NULL,                   -- canonical citation
    raw_text         TEXT,
    in_range         INTEGER NOT NULL DEFAULT 0       -- cite is an endpoint of a "to"/"through" range
);
CREATE INDEX IF NOT EXISTS xrefs_from ON xrefs (from_document_id);
CREATE INDEX IF NOT EXISTS xrefs_to ON xrefs (to_kind, to_key);

-- Spans the compilers list as one line instead of individual sections, e.g. "37.1-37.9 Repealed. 1976, Act 453".
-- Lets get_document explain "MCL 37.5" instead of just saying it is missing.
CREATE TABLE IF NOT EXISTS ranges (
    id           INTEGER PRIMARY KEY,
    source       TEXT NOT NULL,
    start_citation TEXT NOT NULL,
    end_citation TEXT NOT NULL,
    chapter      TEXT,
    act_citation TEXT,
    act_name     TEXT,
    note         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'repealed',
    active       INTEGER NOT NULL DEFAULT 1,
    first_seen   TEXT NOT NULL,
    UNIQUE (source, start_citation, end_citation)
);

CREATE TABLE IF NOT EXISTS public_acts (
    year           INTEGER NOT NULL,
    number         INTEGER NOT NULL,
    title          TEXT,
    effective_date TEXT,
    url            TEXT,
    PRIMARY KEY (year, number)
);
CREATE TABLE IF NOT EXISTS public_act_targets (
    year     INTEGER NOT NULL,
    number   INTEGER NOT NULL,
    citation TEXT NOT NULL,
    PRIMARY KEY (year, number, citation)
);

CREATE TABLE IF NOT EXISTS feed_events (
    id             INTEGER PRIMARY KEY,
    seen_at        TEXT NOT NULL,
    feed_built_at  TEXT,
    kind           TEXT NOT NULL,
    citation       TEXT,
    act_citation   TEXT,
    object_name    TEXT,
    url            TEXT,
    catchline_hint TEXT,
    status_hint    TEXT,
    raw_title      TEXT NOT NULL,
    variant        TEXT,
    UNIQUE (feed_built_at, raw_title, object_name)
);
CREATE INDEX IF NOT EXISTS feed_events_citation ON feed_events (citation);

CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5 (
    citation,
    catchline,
    text,
    doc_id UNINDEXED,
    tokenize = 'porter unicode61'
);
"""


def connect(path: str | Path, readonly: bool = False) -> sqlite3.Connection:
    path = str(path)
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    existing = None
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        existing = row[0] if row else None
    except sqlite3.OperationalError:
        pass  # no meta table yet: fresh database
    if existing is not None and existing != str(SCHEMA_VERSION):
        raise RuntimeError(
            f"Database schema version {existing} does not match this code ({SCHEMA_VERSION}). "
            "Delete the database file and re-run ingest (nothing else has been stored yet)."
        )
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
