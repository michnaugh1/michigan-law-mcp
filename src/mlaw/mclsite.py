"""Parsers for legislature.mi.gov page types, written against saved pages (tests/data/pages).

Covered so far: chapter page, act page, division page, section page (normal and repealed), the
"Document" download page, the site-wide currency banner and disclaimer.
NOT yet covered (no samples yet): variant-node pages, sections containing tables, the chapter index
(Laws/ChapterIndex), the statute index (Laws/Index), and statute/chapter-level download pages.

Site model (from saved pages):
    Chapter (or chapter group)  Laws/MCL?objectName=mcl-chapters-760-777 | mcl-chap752
      Act / E.R.O.              Laws/MCL?objectName=mcl-Act-175-of-1927 | mcl-E-R-O-No-2014-1
        Division                Laws/MCL?objectName=mcl-175-1927-II       (description carries "(762.1...762.16)")
          Section               Laws/MCL?objectName=mcl-750-83  (also Home/GetObject?..., as linked from the feed)
    Download page               Home/Document?objectName=mcl-750-145p -> HTML (Home/RenderDoc?...) and PDF
                                (documents/mcl/pdf/MCL-750-145P.pdf, note upper-case name)
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from .citations import MCL_TOKEN, citation_sort_key, normalize_citation
from .history import HistoryEntry, parse_history
from .models import ParsedDoc

_TOKEN = r"\d{1,3}[A-Za-z]?\.\d{1,5}[A-Za-z]{0,3}"
_RANGE_TAIL = re.compile(r"\((?P<body>[^()]*)\)\s*$")  # "(762.1...762.16)", "(766.1...766.19-766.22)", "(767A.1...767A.9)"
_CURRENCY = re.compile(r"Complete Through\s+PA\s+(\d+)\s+of\s+(\d{4})", re.I)


def _soup(html: str | bytes) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _clean(s: str | None) -> str:
    return " ".join((s or "").split())


def object_name(href: str | None) -> str | None:
    if not href:
        return None
    q = parse_qs(urlsplit(href).query)
    for k, v in q.items():
        if k.lower() == "objectname" and v:
            return v[0]
    return None


@dataclass(frozen=True)
class Currency:
    text: str  # "Michigan Compiled Laws Complete Through PA 91 of 2026"
    through_act: str  # "2026 PA 91"


def parse_currency(html: str | bytes) -> Currency | None:
    """The banner on every page states how current the compilation is."""
    span = _soup(html).select_one(".banner-center span.desktop, .banner-center span")
    if span is None:
        return None
    text = _clean(span.get_text())
    m = _CURRENCY.search(text)
    return Currency(text, f"{m.group(2)} PA {int(m.group(1))}") if m else None


def parse_disclaimer(html: str | bytes) -> str | None:
    p = _soup(html).select_one("footer p")
    return _clean(p.get_text()) if p else None


@dataclass(frozen=True)
class IndexRow:
    object_name: str
    url: str
    label: str
    type: str  # Statute | Division | ...
    description: str
    first_citation: str | None = None
    last_citation: str | None = None


def _index_rows(soup: BeautifulSoup) -> list[IndexRow]:
    table = soup.select_one("main table")
    rows: list[IndexRow] = []
    if table is None:
        return rows
    for tr in table.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        a = tds[0].find("a")
        oname = object_name(a.get("href")) if a else None
        if not oname:
            continue
        desc = _clean(tds[2].get_text())
        first = last = None
        m = _RANGE_TAIL.search(desc)
        if m:
            cites = [c.canonical for c in map(normalize_citation, re.findall(_TOKEN, m.group("body"))) if c]
            if len(cites) >= 2:
                first, last = min(cites, key=citation_sort_key), max(cites, key=citation_sort_key)
        rows.append(IndexRow(oname, a["href"], _clean(a.get_text()), _clean(tds[1].get_text()), desc, first, last))
    return rows


def _nav(soup: BeautifulSoup, label: str) -> str | None:
    for a in soup.select("main a"):
        if _clean(a.get_text()).lower() == label.lower():
            return object_name(a.get("href"))
    return None


def _link(soup: BeautifulSoup, label: str) -> str | None:
    for a in soup.select("main a"):
        if _clean(a.get_text()).lower() == label.lower():
            return a.get("href")
    return None


@dataclass
class ChapterPage:
    object_name: str | None
    heading: str  # "Chapter 760"
    title: str  # "CODE OF CRIMINAL PROCEDURE"
    acts: list[IndexRow]
    previous: str | None
    next: str | None
    download_url: str | None


def parse_chapter_page(html: str | bytes) -> ChapterPage:
    s = _soup(html)
    parent = s.select_one('input[name="mclParent"]')
    h1, h2 = s.select_one(".chapterHeading h1"), s.select_one(".chapterHeading h2")
    return ChapterPage(
        object_name=parent.get("value") if parent else None,
        heading=_clean(h1.get_text()) if h1 else "",
        title=_clean(h2.get_text()) if h2 else "",
        acts=_index_rows(s),
        previous=_nav(s, "Previous Chapter"),
        next=_nav(s, "Next Chapter"),
        download_url=_link(s, "Download Chapter"),
    )


@dataclass
class ActPage:
    object_name: str | None
    title: str  # "THE CODE OF CRIMINAL PROCEDURE"
    act_label: str  # "Act 175 of 1927"
    act_citation: str | None  # "1927 PA 175"
    long_title: str
    history_raw: str
    history: list[HistoryEntry]
    divisions: list[IndexRow]
    chapter_object: str | None
    previous: str | None
    next: str | None
    download_url: str | None
    bills_affecting_url: str | None = None
    warnings: list[str] = field(default_factory=list)


def parse_act_page(html: str | bytes) -> ActPage:
    s = _soup(html)
    parent = s.select_one('input[name="mclParent"]')
    title = s.select_one(".statuteHeading h1")
    label = s.select_one(".statuteHeading h2")
    label_text = _clean(label.get_text()) if label else ""
    m = re.match(r"^Act\s+(\d+)\s+of\s+(\d{4})$", label_text, re.I)
    act_citation = f"{m.group(2)} PA {int(m.group(1))}" if m else None

    wrapper = s.select_one(".statuteWrapper")
    long_title = ""
    if wrapper is not None:
        p = wrapper.find("p", recursive=False)
        long_title = _clean(p.get_text()) if p else ""
    hist_p = s.select_one(".editorials p")
    hist_raw = ""
    if hist_p is not None:
        hist_raw = _clean(re.sub(r"^\s*History:\s*", "", hist_p.get_text()))

    warnings = []
    if act_citation is None:
        warnings.append(f"unrecognized act label {label_text!r}")
    chapter = None
    for a in s.select("main .col-12.text-center a"):
        chapter = object_name(a.get("href"))
        break
    return ActPage(
        object_name=parent.get("value") if parent else None,
        title=_clean(title.get_text()) if title else "",
        act_label=label_text,
        act_citation=act_citation,
        long_title=long_title,
        history_raw=hist_raw,
        history=parse_history(hist_raw) if hist_raw else [],
        divisions=_index_rows(s),
        chapter_object=chapter,
        previous=_nav(s, "Previous Statute"),
        next=_nav(s, "Next Statute"),
        download_url=_link(s, "Download Statute"),
        bills_affecting_url=_link(s, "Bills Affecting this Statute"),
        warnings=warnings,
    )


# ---------------------------------------------------------------------------------------------
# object names, divisions, sections
# ---------------------------------------------------------------------------------------------

_SECTION_OBJECT = re.compile(
    r"^mcl-(?P<chap>\d{1,3}[A-Za-z]?)-(?P<sec>\d{1,5}[A-Za-z]{0,3})(?:-(?P<dotv>[a-z]+)|\[(?P<brk>[A-Za-z0-9]+)\])?$"
)  # variant words are lowercase; division ids such as mcl-175-1927-II are not, so they never match here.
# The bracket disambiguator ([1], [a], ...) is letters-or-digits, not digit-only -- see mclxml.py's module
# docstring ("bracket suffix" section) for the real MCL 752.863[a] example this was broadened for.
_ACT_OBJECT = re.compile(r"^mcl-Act-(\d+)-of-(\d{4})$", re.I)


def section_from_object_name(name: str | None) -> tuple[str, str | None] | None:
    """'mcl-750-145p' -> ('MCL 750.145p', None); 'mcl-333-16335-amended' -> ('MCL 333.16335', 'amended')."""
    if not name:
        return None
    m = _SECTION_OBJECT.match(name)
    if not m:
        return None
    c = normalize_citation(f"{m.group('chap')}.{m.group('sec')}")
    if c is None:
        return None
    variant = m.group("dotv") if m.group("dotv") else (f"[{m.group('brk')}]" if m.group('brk') else None)
    return c.canonical, variant


def act_from_object_name(name: str | None) -> str | None:
    m = _ACT_OBJECT.match(name or "")
    return f"{m.group(2)} PA {int(m.group(1))}" if m else None


def _breadcrumbs(soup: BeautifulSoup) -> dict[str, str]:
    """Chapter / act / division object names from the centered link block under the toolbar."""
    out: dict[str, str] = {}
    for a in soup.select("main .col-12.text-center a"):
        oname = object_name(a.get("href"))
        if not oname:
            continue
        if oname.startswith("mcl-chap"):
            out["chapter"] = oname
        elif _ACT_OBJECT.match(oname):
            out["act"] = oname
        else:
            out["division"] = oname
    return out


@dataclass
class DivisionPage:
    object_name: str | None
    act_name: str  # "THE MICHIGAN PENAL CODE (EXCERPT)" as shown
    act_label: str  # "Act 328 of 1931"
    heading: str  # "Chapter XXA"
    title: str  # "VULNERABLE ADULTS"
    chapter_object: str | None
    act_object: str | None
    sections: list[IndexRow]
    previous: str | None
    next: str | None
    download_url: str | None


def parse_division_page(html: str | bytes) -> DivisionPage:
    s = _soup(html)
    parent = s.select_one('input[name="mclParent"]')
    crumbs = _breadcrumbs(s)
    ex = s.select_one(".divisionWrapper .excerpt")
    h2 = ex.select_one("h1.h2") if ex else None
    h3 = ex.select_one("h1.h3") if ex else None
    dh1, dh2 = s.select_one(".divisionHeading h1"), s.select_one(".divisionHeading h2")
    rows: list[IndexRow] = []
    table = s.select_one("main table")
    if table is not None:
        for tr in table.select("tbody tr"):
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue
            a = tds[1].find("a")
            oname = object_name(a.get("href")) if a else None
            if not oname:
                continue
            sec = section_from_object_name(oname)
            rows.append(IndexRow(oname, a["href"], _clean(a.get_text()), _clean(tds[2].get_text()),
                                 _clean(tds[3].get_text()), sec[0] if sec else None, sec[0] if sec else None))
    return DivisionPage(
        object_name=parent.get("value") if parent else None,
        act_name=_clean(h2.get_text()) if h2 else "",
        act_label=_clean(h3.get_text()) if h3 else "",
        heading=_clean(dh1.get_text()) if dh1 else "",
        title=_clean(dh2.get_text()) if dh2 else "",
        chapter_object=crumbs.get("chapter"),
        act_object=crumbs.get("act"),
        sections=rows,
        previous=_nav(s, "Previous Division"),
        next=_nav(s, "Next Division"),
        download_url=_link(s, "Download Division"),
    )


_STATUS_WORDS = {
    "repealed": "repealed", "expired": "expired", "reserved": "reserved",
    # See the identical entry and comment in mclxml.py's _section_content: a genuine compiler typo
    # ("Reepaled.") confirmed live 2026-09-22 on MCL 205.96a, kept narrow to that exact misspelling.
    "reepaled": "repealed",
    # See the identical entry and comment in mclxml.py's _section_content: an Executive (Reorganization)
    # Order undone by a later E.O., confirmed live 2026-09-22 on MCL 388.996 ("Rescinded. 2005, E.O. No.
    # 2005-4, Eff. Feb. 15, 2005."). Kept as its own status, not folded into "repealed".
    "rescinded": "rescinded",
}
# "37.11 Short title." | "333.16335.amended Physical therapy; fees." | "333.5474c[1] ..." | "752.863[a] ..."
# (the last confirmed live 2026-09-22, real Chapter 752.xml -- see mclxml.py's module docstring)
_H4 = re.compile(
    r"^(?P<cite>\d{1,3}[A-Za-z]?\.\d{1,5}[A-Za-z]{0,3})(?P<variant>\.[a-z]+|\[[A-Za-z0-9]+\])?\s*(?P<rest>.*)$",
    re.S,
)
_BANNER_DATE = re.compile(r"EFFECTIVE\s+([A-Z][A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", re.I)
_INLINE = {"span", "a", "b", "i", "em", "strong", "u", "sup", "sub", "small"}


def _is_bold_span(el) -> bool:
    return getattr(el, "name", None) == "span" and "bold" in (el.get("style") or "").replace(" ", "")


def _labeled_blocks(editorials) -> list[tuple[str, str]]:
    """Split editorial paragraphs into (label, text) pairs on bold "Label:" spans."""
    out: list[tuple[str, str]] = []
    for p in editorials:
        label, buf = None, []

        def flush():
            if label is not None:
                out.append((label, _clean("".join(buf))))

        for node in p.children:
            if _is_bold_span(node):
                flush()
                label, buf = _clean(node.get_text()).rstrip(":").strip(), []
            else:
                buf.append(node.get_text() if hasattr(node, "get_text") else str(node))
        flush()
    return out


@dataclass
class SectionContent:
    """One <div class="sectionWrapper">, however it was reached (single page or whole-chapter file)."""

    citation: str
    variant: str  # "" | "amended" | "added" | "[1]" ...
    catchline: str
    status: str
    effective_date: str | None  # repeal date for repealed sections; takes-effect date for banner variants
    text: str
    history_note: str
    compilers_notes: str
    banner: str  # the site's own notice, e.g. "***** ... THIS AMENDED SECTION IS EFFECTIVE JANUARY 22, 2028 *****"
    act_name: str
    act_label: str
    warnings: list[str] = field(default_factory=list)
    # "mcl" (default, every HTML-source section and ordinary XML chapters) or "const" (Chapter 1.xml's own
    # sections, recognized via citations.normalize_const_mclnumber -- see mclxml.py). Threaded through to
    # ParsedDoc.source by chapterdoc.py's _to_doc, since the HTML/XML parsers otherwise share this shape.
    source: str = "mcl"


def _iso_from_banner(text: str) -> str | None:
    m = _BANNER_DATE.search(text)
    if not m:
        return None
    from datetime import datetime

    try:
        return datetime.strptime(f"{m.group(1)[:3].title()} {m.group(2)} {m.group(3)}", "%b %d %Y").date().isoformat()
    except ValueError:
        return None


def _table_text(table) -> str:
    return "\n".join(" | ".join(_clean(c.get_text()) for c in tr.find_all(["td", "th"])) for tr in table.find_all("tr"))


def parse_section_wrapper(wrapper) -> SectionContent:
    """Extract a section from its ``.sectionWrapper`` element.

    Every child node is visited, including bare text and inline elements: on the real site a table can sit
    in the middle of a paragraph, with the following subsection as a loose text node after ``</table>``
    (see MCL 333.16335.amended). A completeness check compares what was extracted with all the text inside
    the wrapper and records a warning if anything was dropped, so text cannot silently go missing.
    """
    warnings: list[str] = []
    h4 = wrapper.select_one("h1.h4")
    m = _H4.match(_clean(h4.get_text())) if h4 else None
    if m is None:
        raise ValueError("section has no recognizable citation heading")
    cite = normalize_citation(m.group("cite"))
    if cite is None:
        raise ValueError(f"bad citation in heading: {m.group('cite')!r}")
    variant = (m.group("variant") or "").lstrip(".")
    catchline = m.group("rest").strip()

    status, effective = "active", None
    # Require the status word to be immediately followed by a period, or by whitespace leading straight
    # into a year (no period at all -- confirmed live 2026-09-22, MCL 388.1623g/Chapter 388.xml: "Repealed
    # 2026, Act 25, Imd. Eff. July 21, 2026.", while its own sibling 388.1623h does have the period) -- see
    # mclxml.py's identical fix. A plain \b boundary is not enough on its own: MCL 89.4's CatchLine
    # "Repealed ordinances; re-enactment." is an active section's title, not a status prefix (confirmed
    # live 2026-09-22, Chapter 81.xml). This parser mirrors that same heuristic against the HTML
    # rendering's catchline text, so it carries the same false-positive risk even though there is no XML
    # Repealed flag here to catch it via an integrity check -- it would have silently mis-tagged the
    # section instead.
    # Some sections' CatchLine echoes the section's own number before the status word -- see mclxml.py's
    # identical fix (2026-09-22, MCL 324.32724/Chapter 324.xml: "324.32724 Repealed. 2008, Act 181, Imd.
    # Eff. July 9, 2008.") -- tolerated here too as an optional leading citation-shaped token.
    first_word = re.match(rf"^(?:{MCL_TOKEN}\s+)?([A-Za-z]+)(?:\.|\s+(?=\d))", catchline)
    if first_word and first_word.group(1).lower() in _STATUS_WORDS:
        status = _STATUS_WORDS[first_word.group(1).lower()]
        entries = parse_history(re.sub(rf"^(?:{MCL_TOKEN}\s+)?[A-Za-z]+\.?\s*", "", catchline))
        if entries and entries[0].effective_iso:
            effective = entries[0].effective_iso

    body: list[str] = []
    inline: list[str] = []
    history, notes, banner = "", [], ""

    def flush_inline() -> None:
        t = _clean("".join(inline))
        inline.clear()
        if t:
            body.append(t)

    for child in wrapper.children:
        if child is h4:
            continue
        if isinstance(child, str):
            inline.append(child)
            continue
        cls = child.get("class") or []
        if child.name == "br":
            flush_inline()
        elif "excerpt" in cls:
            continue
        elif "commentary" in cls:
            flush_inline()
            banner = _clean(child.get_text())
        elif "editorials" in cls:
            flush_inline()
            for label, text in _labeled_blocks(child.find_all("p")):
                if label.lower() == "history":
                    history = text
                elif label or text:
                    notes.append(f"{label}: {text}")
        elif child.name == "table":
            flush_inline()
            body.append(_table_text(child))
        elif child.name == "p":
            flush_inline()
            t = _clean(child.get_text())
            if t:
                body.append(t)
        elif child.name in _INLINE:
            inline.append(child.get_text())
        else:
            flush_inline()
            t = _clean(child.get_text())
            if t:
                warnings.append(f"unhandled <{child.name}> element kept as text")
                body.append(t)
    flush_inline()

    # completeness check: all visible text in the wrapper, minus heading/notes/banner, must be in the body
    probe = copy.copy(wrapper)
    for el in probe.select("h1.h4, .editorials, .excerpt, .commentary"):
        el.decompose()
    strip = lambda t: re.sub(r"[\s|]+", "", t)  # noqa: E731
    if strip(probe.get_text()) != strip("".join(body)):
        warnings.append("INTEGRITY: extracted body differs from the text inside the section wrapper")

    if banner and not variant:
        warnings.append("section carries a site notice but no variant suffix in its heading")
    if banner and variant:
        effective = _iso_from_banner(banner) or effective
        notes.append(f"Site notice: {banner}")

    ex = wrapper.select_one(".excerpt")
    act_name = _clean(ex.select_one("h1.h2").get_text()) if ex and ex.select_one("h1.h2") else ""
    act_label = _clean(ex.select_one("h1.h3").get_text()) if ex and ex.select_one("h1.h3") else ""
    return SectionContent(
        citation=cite.canonical, variant=variant, catchline=catchline, status=status, effective_date=effective,
        text="\n".join(body), history_note=history, compilers_notes="\n".join(notes), banner=banner,
        act_name=re.sub(r"\s*\(EXCERPT\)\s*$", "", act_name, flags=re.I), act_label=act_label, warnings=warnings,
    )


@dataclass
class SectionPage:
    object_name: str | None
    citation: str
    variant: str  # "" for the ordinary node
    act_name: str  # "THE MICHIGAN PENAL CODE" (the site's "(EXCERPT)" suffix removed)
    act_label: str
    act_citation: str | None
    catchline: str
    status: str
    effective_date: str | None  # repeal date, or the takes-effect date of a variant with a site notice
    text: str
    history_note: str
    compilers_notes: str
    banner: str
    chapter_object: str | None
    act_object: str | None
    division_object: str | None
    previous: str | None
    next: str | None
    download_url: str | None
    warnings: list[str] = field(default_factory=list)

    @property
    def url(self) -> str | None:
        return f"https://www.legislature.mi.gov/Laws/MCL?objectName={self.object_name}" if self.object_name else None

    def to_parsed_doc(self) -> ParsedDoc:
        if any(w.startswith("INTEGRITY") for w in self.warnings):
            raise ValueError(f"{self.object_name}: refusing to store text that failed the completeness check")
        return ParsedDoc(
            source="mcl", citation=self.citation, text=self.text, catchline=self.catchline,
            history_note=self.history_note, compilers_notes=self.compilers_notes, status=self.status,
            kind="section", chapter=self.citation.split()[1].split(".")[0], act_citation=self.act_citation,
            act_name=self.act_name, url=self.url, effective_date=self.effective_date, variant=self.variant,
        )


def parse_section_page(html: str | bytes) -> SectionPage:
    s = _soup(html)
    wrapper = s.select_one(".sectionWrapper")
    if wrapper is None:
        raise ValueError("not a section page: no .sectionWrapper")
    c = parse_section_wrapper(wrapper)
    oname = object_name(_link(s, "Download Section"))
    sec = section_from_object_name(oname)
    warnings = list(c.warnings)
    if sec and (sec[0] != c.citation or (sec[1] or "") != c.variant):
        warnings.append(f"heading {c.citation}{'.' + c.variant if c.variant else ''} differs from object name {oname}")
    crumbs = _breadcrumbs(s)
    return SectionPage(
        object_name=oname, citation=c.citation, variant=c.variant, act_name=c.act_name, act_label=c.act_label,
        act_citation=act_from_object_name(crumbs.get("act")), catchline=c.catchline, status=c.status,
        effective_date=c.effective_date, text=c.text, history_note=c.history_note,
        compilers_notes=c.compilers_notes, banner=c.banner,
        chapter_object=crumbs.get("chapter"), act_object=crumbs.get("act"), division_object=crumbs.get("division"),
        previous=_nav(s, "Previous Section"), next=_nav(s, "Next Section"),
        download_url=_link(s, "Download Section"), warnings=warnings,
    )


@dataclass
class DocumentPage:
    """The "Download" landing page: links to an HTML rendering and a static PDF."""

    title: str
    html_url: str | None
    pdf_url: str | None
    previous: str | None
    next: str | None
    object_name: str | None = None  # e.g. "mcl-chap37", "mcl-750-145p"
    label: str = ""  # e.g. "Chapter 37"
    heading: str = ""  # e.g. "CIVIL RIGHTS" (chapter/act name; empty for sections)


def parse_document_page(html: str | bytes) -> DocumentPage:
    s = _soup(html)
    h1 = s.select_one("main h1")
    html_url = pdf_url = None
    for a in s.select("main table a"):
        href = a.get("href") or ""
        if "/Home/RenderDoc" in href:
            html_url = href
        elif href.lower().endswith(".pdf"):
            pdf_url = href
    def nav(label):
        for a in s.select("main a"):
            if _clean(a.get_text()).lower() == label:
                return object_name(a.get("href"))
        return None
    label = heading = ""
    cell = s.select_one("main table.docTable tbody td")
    if cell:
        strong = cell.find("strong")
        label = _clean(strong.get_text()) if strong else ""
        heading = _clean(cell.get_text(" ")).removeprefix(label).strip()
    return DocumentPage(_clean(h1.get_text()) if h1 else "", html_url, pdf_url,
                        nav("previous document"), nav("next document"),
                        object_name=object_name(html_url), label=label, heading=heading)
