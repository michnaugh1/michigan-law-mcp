"""The Legislature's file directory as a manifest: ``https://legislature.mi.gov/documents/mcl/``.

One request to that IIS directory listing gives, for every published ``Chapter N.xml``: its size and last-modified
time. That is enough to (a) discover which chapters exist without walking pages, (b) skip files that have not
changed since the last run, and (c) sanity-check a run (a chapter that vanishes from the listing is an alarm, not
a repeal). Times in the listing carry no time zone; they are kept as the site printed them.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import unquote, urljoin

_ROW = re.compile(
    r"(?P<date>\d{1,2}/\d{1,2}/\d{4})\s+(?P<time>\d{1,2}:\d{2}\s+[AP]M)\s+(?P<size>&lt;dir&gt;|<dir>|\d+)\s+"
    r'<A\s+HREF="(?P<href>[^"]+)"',
    re.I,
)
_CHAPTER_FILE = re.compile(r"^Chapter (?P<n>\d{1,3}[A-Za-z]?)\.xml$", re.I)


@dataclass(frozen=True)
class DirEntry:
    name: str  # "Chapter 37.xml"
    url: str
    is_dir: bool
    size: int | None
    modified: str  # "2026-02-06T20:12" as printed by the server (no time zone)

    @property
    def chapter(self) -> str | None:
        m = _CHAPTER_FILE.match(self.name)
        return m.group("n").upper() if m else None


def parse_directory_listing(html: str | bytes, base_url: str) -> list[DirEntry]:
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    out: list[DirEntry] = []
    for m in _ROW.finditer(html):
        href = m.group("href")
        if href.rstrip("/").endswith("/.."):
            continue
        ts = datetime.strptime(f"{m.group('date')} {m.group('time')}", "%m/%d/%Y %I:%M %p")
        is_dir = not m.group("size").isdigit()
        out.append(DirEntry(
            name=unquote(href.rstrip("/").rsplit("/", 1)[-1]),
            url=urljoin(base_url, href),
            is_dir=is_dir,
            size=None if is_dir else int(m.group("size")),
            modified=ts.strftime("%Y-%m-%dT%H:%M"),
        ))
    return out


def chapter_files(entries: list[DirEntry]) -> list[DirEntry]:
    files = [e for e in entries if not e.is_dir and e.chapter]
    names = [e.chapter for e in files]
    if len(set(names)) != len(names):
        raise ValueError("directory listing has two files for the same chapter")
    return sorted(files, key=lambda e: (int(re.match(r"\d+", e.chapter).group()), e.chapter))


# ---- change detection ---------------------------------------------------------------------------------------
MANIFEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_files (
    source      TEXT NOT NULL,
    name        TEXT NOT NULL,
    size        INTEGER,
    modified    TEXT,           -- as printed by the directory listing
    sha256      TEXT,           -- of the bytes last parsed successfully
    fetched_at  TEXT,
    PRIMARY KEY (source, name)
);
"""


@dataclass(frozen=True)
class FetchPlan:
    fetch: list[DirEntry]  # new or changed since the last successful parse
    unchanged: list[DirEntry]
    vanished: list[str]  # known before, not in the listing now: never treated as a repeal


def plan_fetch(conn: sqlite3.Connection, source: str, entries: list[DirEntry], *, force: bool = False) -> FetchPlan:
    conn.executescript(MANIFEST_SCHEMA)
    known = {r["name"]: r for r in conn.execute("SELECT * FROM source_files WHERE source = ?", (source,))}
    fetch, same = [], []
    for e in entries:
        k = known.get(e.name)
        if force or k is None or k["size"] != e.size or k["modified"] != e.modified or not k["sha256"]:
            fetch.append(e)
        else:
            same.append(e)
    vanished = sorted(set(known) - {e.name for e in entries})
    return FetchPlan(fetch, same, vanished)


def record_parsed(conn: sqlite3.Connection, source: str, entry: DirEntry, sha256: str, fetched_at: str) -> None:
    conn.executescript(MANIFEST_SCHEMA)
    conn.execute(
        "INSERT INTO source_files (source, name, size, modified, sha256, fetched_at) VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(source, name) DO UPDATE SET size=excluded.size, modified=excluded.modified,"
        " sha256=excluded.sha256, fetched_at=excluded.fetched_at",
        (source, entry.name, entry.size, entry.modified, sha256, fetched_at),
    )
