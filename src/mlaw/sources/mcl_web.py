"""MCL sources built on whole-chapter documents: the HTML rendering (``chapterdoc.py``) and the
Legislature's own machine-readable XML (``mclxml.py``), confirmed as the canonical source in
``docs/lsb-reply-2026-09-21.md``.

Four ways to feed the two parsers:

* ``MclFilesSource(dir)``      reads chapter HTML files someone saved (no network).
* ``MclWebSource(...)``        fetches ``Home/RenderDoc?objectName=mcl-chapNN`` from legislature.mi.gov,
  one request per chapter, through ``PoliteClient`` (robots.txt honored and fail-closed, identified,
  rate-limited). Chapters are discovered by following "Next Document" links on the small per-chapter
  download pages (``Home/Document?objectName=mcl-chap1`` -> ``mcl-chap2`` ...) or given explicitly.
* ``MclXmlFilesSource(dir)``   reads chapter XML files someone saved (e.g. ``Chapter 37.xml``), no network.
* ``MclXmlWebSource(...)``     fetches ``documents/mcl/Chapter N.xml`` from legislature.mi.gov, one request
  per chapter, through the same ``PoliteClient``. Chapters are discovered from the directory listing
  (``manifest.py``) or given explicitly. Unlike the HTML source, these files carry no currency stamp of
  their own (see ``mclxml.py``), so ``currency``/``through_act`` stay ``None`` for this source.

Safety properties (shared by all four, via ``_ChapterSourceBase``)
* A chapter whose sections fail the completeness check aborts the run (``ChapterIntegrityError``) rather than
  storing truncated law; the daily job then leaves the previous database in place.
* ``covered_chapters`` limits removal detection to the chapters actually fetched, so refreshing one chapter
  can never mark the rest of the MCL removed.
* The site's own currency stamp ("Complete Through PA 91 of 2026"), when the source has one, is recorded on
  the run. If chapters fetched in one run carry different stamps (the site updated mid-crawl) the oldest is
  recorded.

``MultiChapter`` files fetched by explicit chapter list: a file like Chapter 760.xml declares itself
chapter "760" but actually holds sections for chapters 760-777 plus 767A and 771A (see ``mclxml.py``).
``MclXmlFilesSource`` scans the whole file up front to compute the true covered-chapter set before
anything is ingested; ``MclXmlWebSource`` can't do that cheaply for an explicit ``chapters=[...]`` list --
the file has to be fetched to know what it actually covers, and ``covered_chapters`` must be known
*before* the fetch happens (``pipeline.py`` reads it up front). Fixed via ``_KNOWN_MULTI_CHAPTER_FILES``
below: a small, explicitly-confirmed table (empirically verified against the real file, 2026-09-21 --
see ``claude/status-2026-09-21.md``) mapping each chapter a MultiChapter file actually covers back to the
file's own name, so a feed-driven refresh asking for e.g. ``chapters=["767A"]`` correctly fetches
``Chapter 760.xml`` and declares the *whole* real covered set up front, not just "767A". This only helps
for the one file already confirmed this way; an entirely new MultiChapter file the table doesn't know
about would still hit the old failure mode (a scope ``ValueError`` on the first out-of-list section) --
loud and safe, but a sign the table needs updating (re-run ``MclXmlFilesSource`` against a fresh saved
copy of the new file to get its real covered set, the same way this table's entry was produced).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, Iterator

from ..chapterdoc import ChapterDocument, parse_chapter_document
from ..mclsite import parse_document_page
from ..mclxml import parse_chapter_xml
from ..models import ParsedDoc, ParsedPublicAct, ParsedRange

BASE = "https://www.legislature.mi.gov"
_CHAP_OBJECT = re.compile(r"^mcl-chap(\d{1,3}[A-Za-z]?)$", re.I)
_THROUGH = re.compile(r"PA\s+(\d+)\s+of\s+(\d{4})")
_XML_NAME = re.compile(r"<Name>\s*(\d{1,3}[A-Za-z]?)\s*</Name>")
_XML_MULTI_CHAPTER = re.compile(r"<MultiChapter>\s*true\s*</MultiChapter>", re.I)
_XML_MCLNUMBER_CHAPTER = re.compile(r"<MCLNumber>\s*(\d{1,3}[A-Za-z]?)\.")

# Chapters a MultiChapter file actually covers, keyed by the file's own declared name (see the module
# docstring above). Confirmed empirically 2026-09-21 by scanning a saved copy of Chapter 760.xml with
# MclXmlFilesSource -- "CODE OF CRIMINAL PROCEDURE" -- the only MultiChapter file seen among the chapters
# looked at so far. If the Legislature ever restructures or a new MultiChapter file turns up, add its
# entry here the same way: scan a saved copy with MclXmlFilesSource(chapters=[...]) and read the result's
# covered_chapters.
_KNOWN_MULTI_CHAPTER_FILES: dict[str, set[str]] = {
    "760": {"760", "761", "762", "763", "764", "765", "766", "767", "767A", "768", "769", "770",
            "771", "771A", "772", "773", "774", "775", "776", "777"},
}


class ChapterIntegrityError(RuntimeError):
    """A chapter file did not parse cleanly; nothing from the run is stored."""


def _through_key(currency: str | None) -> tuple[int, int]:
    m = _THROUGH.search(currency or "")
    return (int(m.group(2)), int(m.group(1))) if m else (0, 0)


class _ChapterSourceBase:
    name = "mcl"
    complete = True
    covered_chapters: set[str] | None = None
    currency: str | None = None
    # Chapter 1.xml is the Michigan Constitution, delivered through this same whole-chapter mechanism (see
    # mclxml.py's module docstring) -- a run over the ordinary MCL corpus can legitimately yield both "mcl"
    # and "const" documents in one pass (Chapter 1 sits right alongside the rest in the same directory/
    # crawl, confirmed 2026-09-22 against the real file). pipeline.py uses this, not the single ``name``
    # label, wherever a run's previously-active/removal accounting needs to cover everything this source
    # can legitimately produce.
    source_kinds = frozenset({"mcl", "const"})

    def __init__(self, strict: bool = True):
        self.strict = strict
        self._ranges: list[ParsedRange] = []
        self._currencies: set[str] = set()
        self.chapters_seen: list[str] = []
        self.sections_seen = 0

    # subclasses: yield (chapter, raw) pairs -- raw is whatever self._parse_chapter expects (str for
    # HTML, bytes for XML, so the UTF-16 declaration is honored by the XML parser)
    def _chapter_html(self) -> Iterator[tuple[str, str | bytes]]:  # pragma: no cover
        raise NotImplementedError

    # subclasses: parse one chapter's raw content into a ChapterDocument; default is the HTML parser
    def _parse_chapter(self, raw: str | bytes) -> ChapterDocument:
        return parse_chapter_document(raw)

    def iter_documents(self) -> Iterator[ParsedDoc]:
        for expected, raw in self._chapter_html():
            # Every other failure mode below is wrapped with "chapter {expected}: ..." so a crawl of
            # hundreds of chapters is diagnosable from the exception alone -- but a raw parse failure
            # (e.g. parse_chapter_xml's ValueError for a file with no <Name>) previously escaped
            # un-wrapped, leaving no way to tell which chapter it was without re-instrumenting and
            # re-running the whole crawl (real case, 2026-09-22: a live crawl hit exactly this with no
            # chapter number anywhere in the traceback). Wrap it here too, for the same reason.
            try:
                cd: ChapterDocument = self._parse_chapter(raw)
            except ChapterIntegrityError:
                raise
            except Exception as e:
                raise ChapterIntegrityError(f"chapter {expected}: {type(e).__name__}: {e}") from e
            problems = cd.integrity_problems()
            if cd.chapter != expected.upper():
                problems.append(f"INTEGRITY: requested chapter {expected} but the file is chapter {cd.chapter}")
            if problems and self.strict:
                raise ChapterIntegrityError(f"chapter {expected}: " + "; ".join(problems[:5]))
            if not cd.sections() and not cd.ranges():
                raise ChapterIntegrityError(f"chapter {expected}: no sections found; refusing to treat as empty")
            if cd.currency:
                self._currencies.add(cd.currency)
                self.currency = min(self._currencies, key=_through_key)
            self._ranges.extend(cd.ranges())
            self.chapters_seen.append(cd.chapter)
            for d in cd.docs():
                self.sections_seen += 1
                yield d

    def iter_public_acts(self) -> Iterable[ParsedPublicAct]:
        return []

    def iter_ranges(self) -> Iterable[ParsedRange]:
        return list(self._ranges)


class MclFilesSource(_ChapterSourceBase):
    """Chapter renderings saved to a directory (e.g. ``Chapter 37.html``). Files that are not chapter
    renderings are skipped."""

    _HEAD = re.compile(r'<h1 class="h1">\s*Chapter\s+(\d{1,3}[A-Za-z]?)\s*</h1>', re.I)

    def __init__(self, path: str | Path, strict: bool = True):
        super().__init__(strict)
        self.path = Path(path)
        files = [self.path] if self.path.is_file() else sorted(self.path.glob("*.htm*"))
        self._files: list[tuple[str, Path]] = []
        for f in files:
            m = self._HEAD.search(f.read_text(encoding="utf-8", errors="replace"))
            if m:
                self._files.append((m.group(1).upper(), f))
        if not self._files:
            raise ValueError(f"no chapter renderings (div.chapterWrapper) found in {self.path}")
        chapters = [c for c, _ in self._files]
        if len(set(chapters)) != len(chapters):
            raise ValueError(f"the same chapter appears in more than one file: {sorted(chapters)}")
        self.covered_chapters = set(chapters)

    def _chapter_html(self):
        for c, f in self._files:
            yield c, f.read_text(encoding="utf-8", errors="replace")


class MclWebSource(_ChapterSourceBase):
    def __init__(self, client=None, chapters: list[str] | None = None, start: str = "mcl-chap1",
                 base: str = BASE, max_chapters: int = 1000, strict: bool = True):
        super().__init__(strict)
        if client is None:
            from ..config import USER_AGENT
            from ..web import PoliteClient

            if "set MLAW_USER_AGENT" in USER_AGENT:
                raise RuntimeError(
                    "Set MLAW_USER_AGENT to something that identifies you with a real contact address "
                    "(e.g. 'north-coast-legal-mlaw/0.1 (mike@thenorthcoastlegal.com)') before crawling."
                )
            # The Bureau asks for at most 1 request per second (and says that may change): stay well under it.
            client = PoliteClient(
                min_interval=max(2.0, float(os.environ.get("MLAW_MIN_INTERVAL", "2"))),
                timeout=180.0,
                robots_error_ok=os.environ.get("MLAW_ROBOTS_ERROR_OK") == "1",
            )
        self.client = client
        self.base = base.rstrip("/")
        self.start = start
        self.max_chapters = max_chapters
        self._explicit = [c.upper() for c in chapters] if chapters else None
        if self._explicit is not None:
            self.covered_chapters = set(self._explicit)  # whole-MCL runs leave this None

    def discover_chapters(self) -> list[str]:
        """Follow Next Document links from ``start`` over the download pages; returns chapter ids in order."""
        out: list[str] = []
        obj = self.start
        seen: set[str] = set()
        while obj and len(out) < self.max_chapters:
            m = _CHAP_OBJECT.match(obj)
            if not m or obj in seen:
                break  # left the chapter list, or looped
            seen.add(obj)
            page = parse_document_page(self.client.get(f"{self.base}/Home/Document?objectName={obj}").text)
            if page.object_name != obj:
                raise ChapterIntegrityError(f"download page for {obj} reports {page.object_name!r}")
            out.append(m.group(1).upper())
            obj = page.next
        if not out:
            raise ChapterIntegrityError(f"could not start chapter discovery at {self.start!r}")
        return out

    def _chapter_html(self):
        chapters = self._explicit or self.discover_chapters()
        for c in chapters:
            yield c, self.client.get(f"{self.base}/Home/RenderDoc?objectName=mcl-chap{c}").text


class MclXmlFilesSource(_ChapterSourceBase):
    """Chapter XML files saved to a directory (e.g. ``Chapter 37.xml``). Files that are not chapter XML
    are skipped. The chapter number is read from the first ``<Name>`` in the file (a cheap text scan --
    the full file, up to tens of MB for the largest chapters, is only handed to the XML parser itself).

    A file can declare ``<MultiChapter>true</MultiChapter>`` and hold sections for many chapters beyond
    the one named in ``<Name>`` (confirmed: Chapter 760.xml, "CODE OF CRIMINAL PROCEDURE", spans chapters
    760-777 plus 767A and 771A in one file -- see ``mclxml.py``'s module docstring). ``covered_chapters``
    has to be the *complete* set before any document is yielded (``pipeline.py`` reads it up front, before
    iteration begins), so for a multi-chapter file the full file is scanned here for every ``<MCLNumber>``
    prefix rather than trusting the single declared chapter -- still a cheap text scan, not a full parse.

    ``chapters``, when given (feed-driven refresh's use case -- see ``refresh.py``), restricts ingestion to
    files whose coverage *intersects* the requested set, so ``path`` can be one big directory holding every
    chapter ever saved and a refresh still only touches the files it actually needs. A file is included
    whole once any one of its chapters is wanted (there's no cheaper unit to fetch or re-verify), so
    ``covered_chapters`` -- and thus removal-detection scope -- naturally ends up covering everything in
    the included files, which can be a superset of what was asked for."""

    def __init__(self, path: str | Path, strict: bool = True, chapters: Iterable[str] | None = None):
        super().__init__(strict)
        self.path = Path(path)
        wanted = {c.upper() for c in chapters} if chapters is not None else None
        files = [self.path] if self.path.is_file() else sorted(self.path.glob("*.xml"))
        self._files: list[tuple[str, Path]] = []
        covered: set[str] = set()
        for f in files:
            head = f.read_bytes()[:4096].decode("utf-16", errors="ignore")
            if "<MCLChapterInfo" not in head:
                continue
            m = _XML_NAME.search(head)
            if not m:
                continue
            chapter = m.group(1).upper()
            file_chapters = {chapter}
            if _XML_MULTI_CHAPTER.search(head):
                full = f.read_bytes().decode("utf-16", errors="ignore")
                found = {c.upper() for c in _XML_MCLNUMBER_CHAPTER.findall(full)}
                if not found:
                    raise ValueError(f"{f}: MultiChapter=true but no <MCLNumber> chapters could be found")
                file_chapters |= found
            if wanted is not None and file_chapters.isdisjoint(wanted):
                continue
            self._files.append((chapter, f))
            covered |= file_chapters
        if not self._files:
            reason = f" matching chapters {sorted(wanted)}" if wanted else ""
            raise ValueError(f"no chapter XML files (MCLChapterInfo) found in {self.path}{reason}")
        seen_names = [c for c, _ in self._files]
        if len(set(seen_names)) != len(seen_names):
            raise ValueError(f"the same chapter appears in more than one file: {sorted(seen_names)}")
        self.covered_chapters = covered

    def _parse_chapter(self, raw: bytes) -> ChapterDocument:
        return parse_chapter_xml(raw)

    def _chapter_html(self):
        for c, f in self._files:
            yield c, f.read_bytes()


class MclXmlWebSource(_ChapterSourceBase):
    """Fetches ``documents/mcl/Chapter N.xml`` -- the Legislature's own canonical machine-readable format
    (``docs/lsb-reply-2026-09-21.md``). Chapters are discovered from the directory listing at
    ``documents/mcl/`` (see ``manifest.py``) unless given explicitly."""

    def _parse_chapter(self, raw: bytes) -> ChapterDocument:
        return parse_chapter_xml(raw)

    def __init__(self, client=None, chapters: list[str] | None = None, base: str = BASE,
                 listing_path: str = "/documents/mcl/", strict: bool = True):
        super().__init__(strict)
        if client is None:
            from ..config import USER_AGENT
            from ..web import PoliteClient

            if "set MLAW_USER_AGENT" in USER_AGENT:
                raise RuntimeError(
                    "Set MLAW_USER_AGENT to something that identifies you with a real contact address "
                    "(e.g. 'north-coast-legal-mlaw/0.1 (mike@thenorthcoastlegal.com)') before crawling."
                )
            client = PoliteClient(
                min_interval=max(2.0, float(os.environ.get("MLAW_MIN_INTERVAL", "2"))),
                timeout=180.0,
                robots_error_ok=os.environ.get("MLAW_ROBOTS_ERROR_OK") == "1",
            )
        self.client = client
        self.base = base.rstrip("/")
        self.listing_path = listing_path
        if chapters:
            # Expand each requested chapter to the real file that covers it (see
            # _KNOWN_MULTI_CHAPTER_FILES above): a chapter that's part of a known MultiChapter file
            # resolves to that file's own name, and covered_chapters ends up as the *whole* real
            # coverage of every file this pulls in, not just what was literally asked for -- otherwise
            # the pipeline's scope check would reject the file's other, unrequested sections.
            file_keys: set[str] = set()
            covered: set[str] = set()
            for c in (c.upper() for c in chapters):
                for file_key, members in _KNOWN_MULTI_CHAPTER_FILES.items():
                    if c in members:
                        file_keys.add(file_key)
                        covered |= members
                        break
                else:
                    file_keys.add(c)
                    covered.add(c)
            self._explicit = sorted(file_keys)
            self.covered_chapters = covered  # whole-MCL runs (chapters=None) leave this None
        else:
            self._explicit = None

    def discover_entries(self):
        from ..manifest import chapter_files, parse_directory_listing

        listing_url = f"{self.base}{self.listing_path}"
        html = self.client.get(listing_url).text
        return chapter_files(parse_directory_listing(html, listing_url))

    def _chapter_html(self):
        if self._explicit:
            for c in self._explicit:
                url = f"{self.base}{self.listing_path}Chapter%20{c}.xml"
                yield c, self.client.get(url).content
            return
        for entry in self.discover_entries():
            yield entry.chapter, self.client.get(entry.url).content
