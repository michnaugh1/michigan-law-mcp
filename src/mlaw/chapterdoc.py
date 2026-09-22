"""Parser for the whole-chapter HTML rendering (``Home/RenderDoc?objectName=mcl-chapNN``).

One file holds an entire chapter, so the full MCL is a few hundred fetches instead of one per section.
Structure (all by nesting; the file has no ids)::

    div.chapterWrapper
      div.chapterHeading            h1 "Chapter 37", h2 "CIVIL RIGHTS"
      div.statuteWrapper *          an act, an E.R.O., or an initiated law
        div.statuteHeading          h1.h2 = name, h2.h3 = "Act 242 of 2023" | "E.R.O. No. 1966-1"
        p                           the act's title ("AN ACT to ...") or a one-line range stub
                                    ("37.1-37.9 Repealed. 1976, Act 453, Eff. Mar. 31, 1977.")
        div.editorials              act-level History / Compiler's Notes
        div.divisionWrapper *       (may nest: article > part) with div.divisionHeading
          div.sectionWrapper *
        div.sectionWrapper *
      footer                        "Rendered 9/21/2026 2:53 PM" and the currency stamp

Sections are parsed by the same function as single section pages, so the two sources are interchangeable
and both carry the completeness check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from .citations import MCL_TOKEN, extract_narrative_ranges, normalize_citation
from .history import parse_history
from .mclsite import SectionContent, _clean, parse_section_wrapper
from .models import ParsedDoc, ParsedRange

_CHAPTER = re.compile(r"^Chapter\s+(\d{1,3}[A-Za-z]?)$", re.I)
_CURRENCY = re.compile(r"Complete Through\s+PA\s+(\d+)\s+of\s+(\d{4})", re.I)
_RENDERED = re.compile(r"Rendered\s+(\d{1,2})/(\d{1,2})/(\d{4})")
_ACT_LABEL = re.compile(r"^Act\s+(\d+)\s+of\s+(\d{4})(?:\s*\((?P<sess>[^)]*)\))?$", re.I)
_ERO_LABEL = re.compile(r"^E\.R\.O\.\s+No\.\s+(\d{4})-(\d+)$", re.I)
# "Rejected" (as in "260.1-260.10 Rejected by voters on Nov. 5, 1974.") covers an act that was submitted
# to referendum and voted down -- confirmed live 2026-09-22, real Chapter 260.xml ("TRANSPORTATION
# SYSTEMS"): it never took effect at all, which is a distinct status from Repealed/Expired (something
# that *was* law and stopped being); kept as its own word here (status=m.group("status").lower() in
# _stub_ranges below stores it verbatim as "rejected") rather than folded into "repealed", the same
# reasoning as the Constitution's "abrogated" status in mclxml.py.
_STUB = re.compile(
    rf"^(?P<a>{MCL_TOKEN})(?:\s*(?:-|–|—|to|through)\s*(?P<b>{MCL_TOKEN}))?\s+"
    r"(?P<status>Repealed|Expired|Reserved|Rejected)\b(?P<rest>.*)$",
    re.I | re.S,
)


@dataclass
class SectionRecord:
    doc: ParsedDoc
    division_path: tuple[str, ...]
    warnings: list[str] = field(default_factory=list)


@dataclass
class StatuteBlock:
    label: str  # "Act 242 of 2023"
    name: str
    act_citation: str  # "2023 PA 242" | "E.R.O. 1966-1" | the label verbatim when unrecognized
    kind: str  # act | ero | other
    preamble: str
    history: str
    notes: str
    sections: list[SectionRecord] = field(default_factory=list)
    ranges: list[ParsedRange] = field(default_factory=list)


@dataclass
class ChapterDocument:
    chapter: str  # "37"
    name: str  # "CIVIL RIGHTS"
    currency: str | None  # "Michigan Compiled Laws Complete Through PA 91 of 2026"
    through_act: str | None  # "2026 PA 91"
    rendered_on: str | None  # ISO date from the footer
    statutes: list[StatuteBlock]
    warnings: list[str] = field(default_factory=list)

    def sections(self) -> list[SectionRecord]:
        return [s for st in self.statutes for s in st.sections]

    def docs(self) -> list[ParsedDoc]:
        return [s.doc for s in self.sections()]

    def ranges(self) -> list[ParsedRange]:
        return [r for st in self.statutes for r in st.ranges]

    def integrity_problems(self) -> list[str]:
        return [w for w in self.warnings if w.startswith("INTEGRITY")] + [
            f"{s.doc.citation}: {w}" for s in self.sections() for w in s.warnings if w.startswith("INTEGRITY")
        ]


def section_object_name(citation: str, variant: str = "") -> str:
    """'MCL 750.145p' -> 'mcl-750-145p'; 'MCL 333.16335' + 'amended' -> 'mcl-333-16335-amended'."""
    base = "mcl-" + citation.split()[1].replace(".", "-")
    if not variant:
        return base
    return base + (variant if variant.startswith("[") else f"-{variant}")


def _act_info(label: str) -> tuple[str, str]:
    m = _ACT_LABEL.match(label)
    if m:
        cite = f"{m.group(2)} PA {int(m.group(1))}"
        return (f"{cite} ({m.group('sess')})" if m.group("sess") else cite), "act"
    m = _ERO_LABEL.match(label)
    if m:
        return f"E.R.O. {m.group(1)}-{int(m.group(2))}", "ero"
    if label.strip().upper() == "CONSTITUTION OF MICHIGAN OF 1963":
        # Chapter 1.xml's single MCLStatuteInfo, confirmed against the real file -- recognized here so it
        # doesn't fire the "statute label not recognized" warning on every ingest, and so every section
        # gets a sensible act_citation instead of the raw statute Name.
        return "Const 1963", "const"
    return label, "other"


def _division_title(div) -> str:
    head = div.find("div", class_="divisionHeading", recursive=False)
    return _clean(head.get_text(" ")) if head else ""


def _stub_ranges(text: str, chapter: str, act_citation: str, act_name: str) -> ParsedRange | None:
    m = _STUB.match(_clean(text))
    if not m:
        return None
    a = normalize_citation(m.group("a"))
    b = normalize_citation(m.group("b") or m.group("a"))
    if a is None or b is None:
        return None
    return ParsedRange(
        source="mcl", start_citation=a.canonical, end_citation=b.canonical, note=_clean(text),
        status=m.group("status").lower(), chapter=chapter, act_citation=act_citation, act_name=act_name,
    )


def _to_doc(c: SectionContent, chapter: str, act_citation: str, act_name: str) -> ParsedDoc:
    """``chapter`` is the chapter the enclosing file was fetched/declared as; it is used here only as a
    fallback. The document's real ``chapter`` is always derived from its own citation instead, so a file
    that legitimately spans many chapters (the XML source's ``MultiChapter`` files -- see mclxml.py's
    module docstring, e.g. Chapter 760.xml covering 760-777/767A/771A) stores each section under the
    chapter it actually belongs to. For an ordinary single-chapter file this is a no-op: the two always
    agree there (checked by the "appears inside chapter X" integrity check in both parsers). A const
    citation ("Const 1963, art 1, § 3") has no "." in its second token, so this falls through to the
    ``chapter`` fallback unchanged -- "1", matching how the Legislature files the Constitution."""
    parts = c.citation.split(maxsplit=1)
    doc_chapter = parts[1].split(".")[0].upper() if len(parts) > 1 and "." in parts[1] else chapter
    # No confirmed per-section URL pattern exists yet for the Constitution (unlike ordinary MCL sections,
    # whose objectName slug is derived straight from the dotted citation) -- every const document points
    # at the whole-chapter rendering Mike already confirmed live (Laws/MCL?objectName=MCL-CHAP1) rather
    # than guessing a slug that might 404 or point at the wrong section. Revisit once the real per-section
    # URL is confirmed.
    url = (
        "https://www.legislature.mi.gov/Laws/MCL?objectName=MCL-CHAP1"
        if c.source == "const"
        else f"https://www.legislature.mi.gov/Laws/MCL?objectName={section_object_name(c.citation, c.variant)}"
    )
    return ParsedDoc(
        source=c.source, citation=c.citation, text=c.text, catchline=c.catchline, history_note=c.history_note,
        compilers_notes=c.compilers_notes, status=c.status, kind="section", chapter=doc_chapter,
        act_citation=act_citation, act_name=c.act_name or act_name, url=url,
        effective_date=c.effective_date, variant=c.variant,
    )


def parse_chapter_document(html: str | bytes) -> ChapterDocument:
    soup = BeautifulSoup(html, "html.parser")
    warnings: list[str] = []
    cw = soup.select_one("div.chapterWrapper")
    if cw is None:
        raise ValueError("not a chapter rendering: no div.chapterWrapper")

    h1 = cw.select_one("div.chapterHeading h1")
    m = _CHAPTER.match(_clean(h1.get_text())) if h1 else None
    if not m:
        raise ValueError("chapter heading not recognized")
    chapter = m.group(1).upper()
    h2 = cw.select_one("div.chapterHeading h2")
    name = _clean(h2.get_text()) if h2 else ""

    text = _clean(soup.get_text(" "))
    cm = _CURRENCY.search(text)
    currency = f"Michigan Compiled Laws Complete Through PA {cm.group(1)} of {cm.group(2)}" if cm else None
    through = f"{cm.group(2)} PA {int(cm.group(1))}" if cm else None
    if currency is None:
        warnings.append("no currency stamp found in file")
    rm = _RENDERED.search(text)
    rendered = f"{rm.group(3)}-{int(rm.group(1)):02d}-{int(rm.group(2)):02d}" if rm else None

    statutes: list[StatuteBlock] = []
    seen_keys: set[tuple[str, str]] = set()
    consumed_sections = 0

    for st in cw.find_all("div", class_="statuteWrapper", recursive=False):
        head = st.find("div", class_="statuteHeading", recursive=False)
        sname = _clean(head.select_one("h1").get_text()) if head and head.select_one("h1") else ""
        label = _clean(head.select_one("h2").get_text()) if head and head.select_one("h2") else ""
        act_citation, kind = _act_info(label)
        if kind == "other":
            warnings.append(f"statute label not recognized: {label!r}")
        block = StatuteBlock(label, sname, act_citation, kind, preamble="", history="", notes="")

        preamble: list[str] = []
        for child in st.find_all(recursive=False):
            if child is head:
                continue
            cls = child.get("class") or []
            if child.name == "p":
                t = _clean(child.get_text())
                if not t:
                    continue
                rng = _stub_ranges(t, chapter, act_citation, sname)
                if rng:
                    block.ranges.append(rng)
                else:
                    preamble.append(t)
            elif "editorials" in cls:
                bits_h, bits_n = [], []
                for p in child.find_all("p"):
                    from .mclsite import _labeled_blocks

                    for lab, txt in _labeled_blocks([p]):
                        (bits_h if lab.lower() == "history" else bits_n).append(
                            txt if lab.lower() == "history" else f"{lab}: {txt}"
                        )
                block.history = " ".join(bits_h)
                block.notes = "\n".join(bits_n)
            elif "divisionWrapper" in cls or "sectionWrapper" in cls:
                pass  # handled below, in document order
            elif child.name in ("br", "div") and not _clean(child.get_text()):
                continue
            elif child.name == "div" and "enact" in _clean(child.get_text()).lower():
                continue  # "The People of the State of Michigan enact:"
            elif child.name != "br":
                warnings.append(f"{label}: unhandled <{child.name}> {cls} kept out of the data: {_clean(child.get_text())[:60]!r}")
        block.preamble = " ".join(preamble)

        def walk(node, path: tuple[str, ...]) -> None:
            nonlocal consumed_sections
            for child in node.find_all(recursive=False):
                cls = child.get("class") or []
                if "divisionWrapper" in cls:
                    walk(child, path + (_division_title(child),))
                elif "sectionWrapper" in cls:
                    consumed_sections += 1
                    try:
                        c = parse_section_wrapper(child)
                    except ValueError as e:
                        h = child.find("h1", class_="h4")
                        warnings.append(f"INTEGRITY: {label}: unparseable section {_clean(h.get_text())[:40] if h else '?'!r}: {e}")
                        continue
                    if c.citation.split()[1].split(".")[0].upper() != chapter:
                        warnings.append(f"INTEGRITY: {c.citation} appears inside chapter {chapter}")
                    key = (c.citation, c.variant)
                    if key in seen_keys:
                        warnings.append(f"INTEGRITY: duplicate section {c.citation} {c.variant!r} in one chapter file")
                        continue
                    seen_keys.add(key)
                    block.sections.append(SectionRecord(_to_doc(c, chapter, act_citation, sname), path, c.warnings))
                    block.ranges.extend(extract_narrative_ranges(
                        c.text, c.compilers_notes, act_citation=act_citation, act_name=sname))

        walk(st, ())
        statutes.append(block)

    total_wrappers = len(cw.select("div.sectionWrapper"))
    if total_wrappers != consumed_sections:
        warnings.append(f"INTEGRITY: {total_wrappers} section wrappers in file but {consumed_sections} reached by the walker")
    if total_wrappers != len(cw.select("div.sectionWrapper > h1.h4")):
        warnings.append("INTEGRITY: a section wrapper has no heading")
    return ChapterDocument(chapter, name, currency, through, rendered, statutes, warnings)
