"""Michigan Court Rules (MCR) from the courts.michigan.gov RoboHelp help system.

* ``McrFilesSource(dir)``  reads ``toc.new.js`` plus the chapter files (``Court_Rules_Chapter_N.htm``) saved flat
  in one directory. No network.
* ``McrWebSource(...)``    fetches ``whxdata/toc.new.js`` and each chapter through ``PoliteClient`` (robots.txt
  honored, fail-closed). courts.michigan.gov's robots.txt disallows automated access, so in practice the web
  source raises ``RobotsDisallowed`` unless the operator has cleared it; save the files by hand and use the
  files source.

Both share ``_McrSourceBase``. Every TOC "book" entry must fetch/parse and yield at least one rule, otherwise
the run raises (``McrIntegrityError``) instead of storing a partial rule set, so ``complete`` is always True.

Currency ("MCR updated with MSC order(s) effective 2026-09-02; chapters: 1 2026-01-01, ...") is set on
``self.currency`` once every chapter has been parsed; ``pipeline.run_ingest`` reads it after iteration and
records it as ``runs.source_currency``, same as the MCL sources.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

from bs4 import BeautifulSoup

from ..models import ParsedDoc, ParsedPublicAct, ParsedRange
from .rh_help import TocEntry, join_url, normalize_text, parse_toc

BASE = ("https://www.courts.michigan.gov/siteassets/rules-instructions-administrative-orders/"
        "michigan-court-rules/michigan-court-rules-responsive-html5.zip/")
TOC_PATH = "whxdata/toc.new.js"

_RULE_NUM = re.compile(r"^\s*Rule\s+(\d{1,2}\.\d{1,3}[A-Za-z]?)\b\s*(.*)$", re.S)
_LEVEL = re.compile(r"^L(\d)_")
# Less common FrameMaker indent styles, seen in 19 paragraphs of the 2026-09 chapters (e.g. the court
# holidays list in MCR 8.110 is Indent_4, a "(ii)" in MCR 2.002 is Indent_4_Roman_others). Their digit is
# the nesting level, same as L<n>_; RuleBody_Indent is one step in from rule body text.
_INDENT = re.compile(r"^Indent_(\d)")
_ZW_SPAN = re.compile(r"<span[^>]*>\s*(?:&#8203;?|&#x200b;?|​)\s*</span>", re.I)
_DATE = re.compile(r"([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{4})")
_INACTIVE = re.compile(r"^\[?\s*(reserved|repealed|deleted)\b", re.I)
_CHAPTER_NAME = re.compile(r"^Chapter\s+(\d+)\b", re.I)


class McrIntegrityError(RuntimeError):
    """The TOC or a chapter did not parse cleanly; nothing from the run is stored."""


@dataclass
class ChapterResult:
    docs: list[ParsedDoc]
    main_date: str | None = None  # ISO date from the chapter's "Updated with MSC Order(s) Effective on ..." line
    warnings: list[str] = field(default_factory=list)


def _iso(text: str) -> str | None:
    m = _DATE.search(text)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date().isoformat()
    except ValueError:
        return None


def _p_text(p) -> str:
    # The label ("(A)") is followed by a zero-width span used only for spacing: turn it into a space so the
    # label does not run into the first word.
    return normalize_text(_ZW_SPAN.sub(" ", p.decode_contents()))


def _status(catchline: str, body_lines: list[str]) -> str:
    stripped = [ln.strip() for ln in body_lines if ln.strip()]
    if _INACTIVE.match(catchline.strip()):
        first = _INACTIVE.match(catchline.strip()).group(1).lower()
    elif len(stripped) == 1 and _INACTIVE.match(stripped[0]) and stripped[0].rstrip().endswith(("]", ".")):
        first = _INACTIVE.match(stripped[0]).group(1).lower()
    else:
        return "active"
    return "reserved" if first == "reserved" else "repealed"


def parse_chapter(html_text: str, chapter: str, url: str) -> ChapterResult:
    soup = BeautifulSoup(html_text, "lxml")
    docs: list[ParsedDoc] = []
    warnings: list[str] = []
    unknown: set[str] = set()
    main_date: str | None = None
    seen: dict[str, str] = {}
    cur: dict | None = None

    def close() -> None:
        nonlocal cur
        if cur is None:
            return
        cit = cur["citation"]
        if cit in seen:
            warnings.append(f"duplicate citation {cit} in chapter {chapter}; kept the first")
        else:
            seen[cit] = cit
            docs.append(ParsedDoc(
                source="mcr", citation=cit, text="\n".join(cur["lines"]), catchline=cur["catchline"],
                status=_status(cur["catchline"], cur["lines"]), kind="rule", chapter=str(chapter),
                url=f"{url}#{cur['id']}" if cur["id"] else url,
            ))
        cur = None

    for p in soup.find_all("p"):
        classes = p.get("class") or []
        cls = classes[0] if classes else ""
        if cls == "Rule_Heading":
            close()
            text = _p_text(p)
            m = _RULE_NUM.match(text)
            if not m:
                warnings.append(f"Rule_Heading without a rule number in chapter {chapter}: {text[:60]!r}")
                continue
            cur = {"citation": f"MCR {m.group(1)}", "catchline": m.group(2).strip(), "id": p.get("id"), "lines": []}
        elif cls == "Subchapter_Heading":
            close()
        elif cls == "MainDate":
            main_date = main_date or _iso(_p_text(p))
        elif cls == "Chapter_Heading":
            continue
        else:
            if cur is None:
                continue
            text = _p_text(p)
            m = _LEVEL.match(cls) or _INDENT.match(cls)
            if m:
                level = int(m.group(1))
            elif cls == "RuleBody_Indent":
                level = 2
            else:
                level = 0
                if cls != "Rule_Body" and cls not in unknown:
                    unknown.add(cls)
                    warnings.append(f"unknown paragraph class {cls!r} in chapter {chapter}; kept as level-0 text")
            if not text:
                continue
            indent = "  " * (level - 1) if level > 1 else ""
            cur["lines"].append(indent + text)
    close()
    return ChapterResult(docs, main_date, warnings)


class _McrSourceBase:
    name = "mcr"
    complete = True
    covered_chapters = None
    source_kinds = frozenset({"mcr"})
    currency: str | None = None

    def __init__(self):
        self.warnings: list[str] = []
        self.chapter_dates: dict[str, str | None] = {}
        self.toc_date: str | None = None

    # subclasses
    def _read_toc(self) -> str:  # pragma: no cover
        raise NotImplementedError

    def _read_chapter(self, entry: TocEntry) -> str:  # pragma: no cover
        raise NotImplementedError

    def iter_documents(self) -> Iterator[ParsedDoc]:
        try:
            entries = parse_toc(self._read_toc())
        except ValueError as e:
            raise McrIntegrityError(f"toc.new.js: {e}") from e
        books = [e for e in entries if e.type == "book"]
        if not books:
            raise McrIntegrityError("toc.new.js lists no chapters")
        for e in entries:
            if e.type == "item" and e.name.lower().startswith("updated"):
                self.toc_date = _iso(e.name)
        self.chapter_dates = {}
        for e in books:
            m = _CHAPTER_NAME.match(e.name)
            if not m:
                raise McrIntegrityError(f"cannot read a chapter number from TOC entry {e.name!r}")
            chapter = m.group(1)
            url = join_url(BASE, e.path)
            try:
                res = parse_chapter(self._read_chapter(e), chapter, url)
            except McrIntegrityError:
                raise
            except Exception as ex:
                raise McrIntegrityError(f"chapter {chapter}: {type(ex).__name__}: {ex}") from ex
            if not res.docs:
                raise McrIntegrityError(f"chapter {chapter}: no rules found; refusing to treat as empty")
            self.warnings.extend(res.warnings)
            self.chapter_dates[chapter] = res.main_date
            yield from res.docs
        chapters = ", ".join(f"{c} {d or 'unknown'}" for c, d in self.chapter_dates.items())
        self.currency = (f"MCR updated with MSC order(s) effective {self.toc_date or 'unknown'}; "
                         f"chapters: {chapters}")

    def iter_public_acts(self) -> Iterable[ParsedPublicAct]:
        return []

    def iter_ranges(self) -> Iterable[ParsedRange]:
        return []


class McrFilesSource(_McrSourceBase):
    """``toc.new.js`` and the chapter files saved flat in one directory."""

    name = "mcr-files"

    def __init__(self, directory: str | Path):
        super().__init__()
        self.directory = Path(directory)
        if not (self.directory / "toc.new.js").is_file():
            raise ValueError(f"no toc.new.js in {self.directory}")

    def _read_toc(self) -> str:
        return (self.directory / "toc.new.js").read_text(encoding="utf-8")

    def _read_chapter(self, entry: TocEntry) -> str:
        f = self.directory / Path(entry.path).name
        if not f.is_file():
            raise McrIntegrityError(f"missing chapter file {f.name} for TOC entry {entry.name!r}")
        return f.read_text(encoding="utf-8")


class McrWebSource(_McrSourceBase):
    """Fetches the TOC and chapters through an injected client (default ``PoliteClient``)."""

    name = "mcr-web"

    def __init__(self, client=None, base: str = BASE):
        super().__init__()
        if client is None:
            import os

            from ..config import USER_AGENT
            from ..web import PoliteClient

            if "set MLAW_USER_AGENT" in USER_AGENT:
                raise RuntimeError(
                    "Set MLAW_USER_AGENT to something that identifies you with a real contact address "
                    "before crawling."
                )
            client = PoliteClient(min_interval=max(2.0, float(os.environ.get("MLAW_MIN_INTERVAL", "2"))),
                                  timeout=180.0)
        self.client = client
        self.base = base

    def _read_toc(self) -> str:
        return self.client.get(join_url(self.base, TOC_PATH)).text

    def _read_chapter(self, entry: TocEntry) -> str:
        return self.client.get(join_url(self.base, entry.path)).text
