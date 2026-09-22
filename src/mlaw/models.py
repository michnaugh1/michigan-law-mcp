"""Data contracts between source parsers and the ingest pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Iterator, Protocol


@dataclass
class ParsedDoc:
    """One citable unit as produced by a source parser."""

    source: str  # mcl | mcr | mre | const
    citation: str  # canonical, e.g. "MCL 750.83"
    text: str
    catchline: str = ""
    history_note: str = ""
    compilers_notes: str = ""  # all labeled notes other than History, e.g. "Compiler's Notes: ..."
    status: str = "active"  # active | repealed | reserved
    kind: str = "section"
    chapter: str | None = None
    act_citation: str | None = None
    act_name: str | None = None
    url: str | None = None
    effective_date: str | None = None  # ISO date, only if the source states it (repeal date, or when a variant takes effect)
    variant: str = ""  # "" = the ordinary node; "amended" / "added" / "[1]" = an additional form the site lists for the same citation


@dataclass
class ParsedRange:
    """A span the compilers list on one line instead of section by section, e.g. "37.1-37.9 Repealed. 1976, Act 453"."""

    source: str
    start_citation: str  # canonical, e.g. "MCL 37.1"
    end_citation: str
    note: str  # verbatim line
    status: str = "repealed"
    chapter: str | None = None
    act_citation: str | None = None
    act_name: str | None = None


@dataclass
class ParsedPublicAct:
    year: int
    number: int
    title: str = ""
    effective_date: str | None = None
    url: str | None = None
    affected_citations: list[str] = field(default_factory=list)


class Source(Protocol):
    """A source yields documents. ``complete`` says whether one iteration covers the whole
    corpus (only then may missing documents be marked removed)."""

    name: str
    complete: bool
    # Optional. If set, a complete crawl covered only these chapters (e.g. {"37", "38"}): removals and the
    # partial-crawl guard apply to those chapters alone, everything else in the database is left untouched.
    covered_chapters: "set[str] | None"
    # Optional (pipeline.py falls back to {name} if absent). The set of ParsedDoc.source values this
    # source can legitimately produce in one run -- most sources emit exactly one (their own name), but
    # the MCL XML/HTML sources can also emit "const" alongside "mcl" (Chapter 1.xml is the Constitution,
    # delivered through the same whole-chapter mechanism). Used for previously-active/removal accounting
    # so a mixed run doesn't undercount or silently skip removal detection for the documents under the
    # kind that isn't ``name`` itself.
    source_kinds: "set[str] | frozenset[str]"

    def iter_documents(self) -> Iterator[ParsedDoc]: ...

    def iter_public_acts(self) -> Iterable[ParsedPublicAct]: ...

    def iter_ranges(self) -> Iterable[ParsedRange]: ...
