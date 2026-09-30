"""Michigan Sentencing Guidelines Manual (MJI) -- the manual's text units.

One PDF (``sgm.pdf``, ~212 pages) published by the Michigan Judicial Institute on courts.michigan.gov.
This module turns the *text* parts of it into citable units:

* ``SGM Instructions Step I`` .. ``Step V``   "General Information and Instructions", one unit per Step
* ``SGM PRV 1`` .. ``SGM PRV 7``               Prior Record Variables
* ``SGM OV 1``  .. ``SGM OV 20``               Offense Variables (each OV is printed once per offense
                                               group that scores it; the copies must agree -- see below)

The sentencing grids and the felony lists are *not* parsed here (later tasks), but their PDF page
ranges are located and exposed on :class:`SgmManual` so those tasks can reuse this module.

Why pdfplumber and not only ``pdftotext -layout``
    The layout text loses exactly the information that matters for quoting this manual accurately:

    * footnote reference marks are superscripts glued onto the preceding token, so ``1999.4`` (a
      footnote 4 after "1999.") or ``MCL 769.34(3).10`` are indistinguishable from real numbers;
    * footnote text is interleaved into the body at every page bottom;
    * the points column of each PRV/OV table is vertically offset from its description, and
    * rows that the manual *shades* ("not scored for current offenses") are only marked by a filled
      rectangle -- invisible in text output.

    pdfplumber gives each word's font size, weight and position and the page's filled rectangles, so
    superscripts (small, raised digits), footnotes (9 pt at the page bottom), running headers (10 pt at
    the page top), the rotated right/left-margin tab labels ("PRVs", "OVs - Crimes Against ...") and
    shaded rows can all be identified from typography instead of guessed from text. ``pdftotext`` is
    still used (it is fast) for the cover's currency stamp and for locating the grid / felony-list pages.

Furniture removed (concretely)
    * every non-upright (rotated) character -- the margin tab labels;
    * 10 pt lines within 60 pt of the page top -- the running header ("General Information and
      Instructions", "PRVs", "OVs - Crimes Against a Person") and each OV/PRV section's preamble ("Score
      each offense variable "by determining ..." MCL 777.31 to MCL 777.49a.") that repeats on every page;
      a line there that matches none of the known patterns is reported as a warning, not silently dropped;
    * "Continued on next page" lines; bare page-number lines at the page bottom;
    * the blue "Points" table-header bar (rendered as a plain "Points" line instead).

Accuracy guards (any of these produces a warning; any warning makes the source ``complete = False`` so a
doubtful parse can never mark stored units removed):

    * every footnote reference in a unit must have footnote text, and every footnote must be referenced;
    * a shaded row with no "shaded" explanation in the unit, or vice versa; shading outside a table;
    * an OV whose title differs between the groups that print it. (Differing *text* is expected -- the
      2026 manual prints OV 3, 12, 16 and 18 differently per crime group -- and is stored, not reconciled:
      one unit per OV with a "== Version for: <groups> ==" section per distinct version.)
    * a PRV/OV missing, duplicated, or Steps out of order; orphan superscripts; unexpected headings.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from ..citations import normalize_citation
from ..models import ParsedDoc, ParsedPublicAct, ParsedRange

DEFAULT_URL = (
    "https://www.courts.michigan.gov/49ddcf/siteassets/offices/mji/felony-sentencing-online-resources/sgm.pdf"
)
LISTING_PATH = "/publications/felony-sentencing-resources/sentencing-guidelines-manuals/"
CURRENT_HEADING = "Current Version"

N_PRV = 7
N_OV = 20


class SgmParseError(RuntimeError):
    """The manual could not be parsed with confidence; nothing should be stored."""


@dataclass
class SgmManual:
    docs: list[ParsedDoc]
    current_through: str  # ISO date from the cover: "Online version current through 5/20/2026"
    edition_label: str | None  # e.g. "Sentencing Guidelines Manual 2026" (the web page's link text)
    grid_pages: tuple[int, int]  # 1-based, inclusive
    felony_list_pages: tuple[int, int]  # 1-based, inclusive
    warnings: list[str] = field(default_factory=list)
    ov_groups: dict[int, list[str]] = field(default_factory=dict)  # OV number -> groups printing it
    # OVs whose copies differ between groups: OV number -> {group: title + text}. In ``docs`` such an OV is
    # one unit holding every distinct version, each under "== Version for: <groups> ==".
    ov_variants: dict[int, dict[str, str]] = field(default_factory=dict)


# --------------------------------------------------------------------------------------------------
# pdftotext


def pdf_to_text(path: str | Path, timeout: float = 120.0) -> str:
    """``pdftotext -layout`` of the whole file; pages are separated by form feeds."""
    exe = shutil.which("pdftotext")
    if exe is None:
        raise SgmParseError("pdftotext not found on PATH (install poppler-utils)")
    try:
        r = subprocess.run([exe, "-layout", "-enc", "UTF-8", str(path), "-"],
                           capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise SgmParseError(f"pdftotext timed out after {timeout}s on {path}") from e
    if r.returncode != 0:
        raise SgmParseError(f"pdftotext failed ({r.returncode}) on {path}: {r.stderr.decode(errors='replace')[:500]}")
    return r.stdout.decode("utf-8")


def _text_pages(text: str) -> list[str]:
    pages = text.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages


_CURRENT_THROUGH = re.compile(r"Online version current through\s+(\d{1,2})/(\d{1,2})/(\d{4})", re.I)
_FELONY_HEADER = re.compile(r"Felony List[–—-]\s*Current Through\s+(\d{1,2})/(\d{1,2})/(\d{2,4})", re.I)
_GRID_TITLE = re.compile(r"Sentencing Grid for Class (M2|[A-H])\b")


def _iso(m: int, d: int, y: int) -> str:
    if y < 100:
        y += 2000
    return f"{y:04d}-{m:02d}-{d:02d}"


def _locate(pages: list[str], warnings: list[str]) -> tuple[str, tuple[int, int], tuple[int, int]]:
    m = _CURRENT_THROUGH.search(pages[0] if pages else "")
    if not m:
        raise SgmParseError("cover page has no 'Online version current through M/D/YYYY' stamp")
    current = _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    felony = [i + 1 for i, p in enumerate(pages) if _FELONY_HEADER.search(p)]
    if not felony:
        raise SgmParseError("no felony list pages ('... Felony List–Current Through ...') found")
    fl = (felony[0], felony[-1])
    if felony != list(range(fl[0], fl[1] + 1)):
        warnings.append(f"felony list pages are not contiguous: {felony}")
    if fl[1] != len(pages):
        warnings.append(f"felony list ends on page {fl[1]} but the PDF has {len(pages)} pages")
    for p in felony:
        for fm in _FELONY_HEADER.finditer(pages[p - 1]):
            if _iso(int(fm.group(1)), int(fm.group(2)), int(fm.group(3))) != current:
                warnings.append(f"felony list page {p} says current through {fm.group(0)!r}, cover says {current}")

    grid_hits: dict[str, list[int]] = {}
    for i, p in enumerate(pages[: fl[0] - 1]):
        for gm in _GRID_TITLE.finditer(p):
            grid_hits.setdefault(gm.group(1), []).append(i + 1)
    if not grid_hits:
        raise SgmParseError("no 'Sentencing Grid for Class ...' pages found")
    expected = ["M2", "A", "B", "C", "D", "E", "F", "G", "H"]
    for g in expected:
        if len(grid_hits.get(g, [])) != 1:
            warnings.append(f"sentencing grid {g} found on pages {grid_hits.get(g, [])} (expected exactly one)")
    gp = (min(min(v) for v in grid_hits.values()), fl[0] - 1)
    return current, gp, fl


# --------------------------------------------------------------------------------------------------
# pdfplumber: words -> lines


@dataclass
class _Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    bold: bool
    sup: bool = False


@dataclass
class _Line:
    page: int
    top: float
    words: list[_Word]
    size: float
    bold: bool
    shaded: bool = False

    @property
    def x0(self) -> float:
        return min(w.x0 for w in self.words)

    @property
    def main(self) -> list[_Word]:
        return [w for w in self.words if not w.sup]

    def plain(self) -> str:
        """Text without superscript marks."""
        return _join(self.main, None)

    def render(self, refs: list[int] | None = None) -> str:
        """Text with superscripts as "[Note n]"; appends their numbers to ``refs``."""
        return _join(self.words, refs)


def _join(words: list[_Word], refs: list[int] | None) -> str:
    out = ""
    prev: _Word | None = None
    for w in words:
        if w.sup:
            if refs is not None:
                refs.append(_sup_number(w.text))
                out += f"[Note {_sup_number(w.text)}]"
            prev = w
            continue
        if prev is not None and out:
            gap = w.x0 - prev.x1
            if gap > 0.12 * min(w.size, prev.size if not prev.sup else w.size):
                out += " "
        out += w.text
        prev = w
    return re.sub(r"\s+", " ", out).strip()


_SUP_TEXT = re.compile(r"^\[?\d{1,2}\]?$")  # footnote marks: "4", once "[1]"


def _sup_number(text: str) -> int:
    return int(text.strip("[]"))


def _is_light_fill(rect: dict) -> bool:
    c = rect.get("non_stroking_color")
    if not rect.get("fill") or not isinstance(c, (tuple, list)) or len(c) != 3:
        return False
    return all(0.8 < float(v) < 0.99 for v in c)


def _page_lines(page, pno: int) -> tuple[list[_Line], list[tuple[float, float, float, float]]]:
    p = page.filter(lambda o: o.get("object_type") != "char" or o.get("upright", True))
    raw = p.extract_words(extra_attrs=["size", "fontname"], keep_blank_chars=False, use_text_flow=False)
    words = [_Word(w["text"], w["x0"], w["x1"], w["top"], w["bottom"], round(float(w["size"]), 1),
                   "Bold" in w["fontname"]) for w in raw]
    words.sort(key=lambda w: (w.top, w.x0))
    clusters: list[list[_Word]] = []
    for w in words:
        if clusters and w.top - clusters[-1][0].top <= 4.5:
            clusters[-1].append(w)
        else:
            clusters.append([w])
    lines = []
    for ws in clusters:
        ws.sort(key=lambda w: w.x0)
        weight: dict[float, int] = {}
        for w in ws:
            weight[w.size] = weight.get(w.size, 0) + len(w.text)
        size = max(weight, key=lambda s: (weight[s], s))
        base_bottom = max(w.bottom for w in ws if w.size == size)
        for w in ws:
            w.sup = (bool(_SUP_TEXT.match(w.text)) and w.size < 0.85 * size and w.bottom < base_bottom - 1.0)
        main = [w for w in ws if not w.sup]
        bold = bool(main) and all(w.bold for w in main)
        tops = sorted(w.top for w in main) or [ws[0].top]
        lines.append(_Line(pno, tops[len(tops) // 2], ws, size, bold))  # median: robust to raised marks
    shades = [(r["x0"], r["top"], r["x1"], r["bottom"]) for r in p.rects if _is_light_fill(r)]
    for ln in lines:
        mid = sum((w.top + w.bottom) / 2 for w in ln.main) / max(1, len(ln.main)) if ln.main else ln.top
        ln.shaded = any(t <= mid <= b and x0 <= ln.x0 + 1 and x1 > ln.x0 + 10 for x0, t, x1, b in shades)
    return lines, shades


# --------------------------------------------------------------------------------------------------
# structure


_FURNITURE_TOP = 60.0
_KNOWN_FURNITURE = re.compile(
    r"^(General Information and Instructions|Definitions|PRVs|OVs - .+|"
    r"Score each (prior record|offense) variable .*|attributable to the one that has the highest number of points.*)$"
)
_STEP = re.compile(r"^STEP\s+([IVX]+)\.\s+(.+)$", re.I)
_UNIT = re.compile(r"^(PRV|OV)\s+(\d{1,2})$")
_MCL_LINE = re.compile(r"^MCL\s+777\.\d+[a-z]?$")
_INSTRUCTIONS = re.compile(r"^Instructions(?: \(((?:PRV|OV) \d{1,2}) continued\))?$")
_FN_LABEL = re.compile(r"^(\d{1,2})\.(\S*)$")  # "3." -- or glued to its first word, "10.See"
_OV_SECTION = re.compile(r"^Offense Variables\s*[—–-]\s*(.+)$")
_ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"]


def _approx(a: float, b: float, tol: float = 0.35) -> bool:
    return abs(a - b) <= tol


@dataclass
class _Para:
    text: str
    last_top: float
    page: int
    size: float


@dataclass
class _Unit:
    citation: str
    page: int
    group: str | None = None  # OV group
    title: list[str] = field(default_factory=list)
    title_refs: list[int] = field(default_factory=list)
    title_rendered: list[str] = field(default_factory=list)
    mcl: str = ""
    have_mcl: bool = False
    blocks: list[str] = field(default_factory=list)  # finished paragraphs / table rows, in order
    para: _Para | None = None
    in_table: bool = False
    rows: list[list] = field(default_factory=list)  # [points, text, shaded, last_top, page]
    refs: list[int] = field(default_factory=list)
    notes: dict[int, str] = field(default_factory=dict)
    shaded_rows: int = 0

    def flush_para(self) -> None:
        if self.para is not None:
            self.blocks.append(self.para.text)
            self.para = None

    def flush_table(self) -> None:
        for pts, txt, shaded, _, _ in self.rows:
            self.blocks.append(f"{pts} — {txt}" + (" [shaded row]" if shaded else ""))
        self.rows = []
        self.in_table = False


def _continue_line(prev: str, nxt: str) -> str:
    if prev.endswith(("-", "/")) and not prev.endswith(" -"):
        return prev + nxt
    return prev + " " + nxt


_PARA_START = re.compile(r"^(•|[A-Z]\.\s)")


class _Parser:
    def __init__(self, url: str, warnings: list[str]):
        self.url = url
        self.warnings = warnings
        self.section: str | None = None  # general | definitions | prv | ov | None
        self.group: str | None = None
        self.groups: list[str] = []
        self.unit: _Unit | None = None
        self.units: list[_Unit] = []
        self.pending_heading: list[_Line] = []  # multi-line 20/22 pt headings
        # footnote references not yet matched to a footnote: [page, number, unit or None]
        self.pending: list[list] = []
        self.last_note: list | None = None  # [number, text, unit]  (text grows with continuation lines)
        self.notes: list[list] = []
        self.steps_seen: list[str] = []

    # -- units -------------------------------------------------------------------------------------
    def close_unit(self) -> None:
        u = self.unit
        if u is None:
            return
        u.flush_para()
        u.flush_table()
        self.units.append(u)
        self.unit = None

    def open_unit(self, citation: str, page: int) -> None:
        self.close_unit()
        self.unit = _Unit(citation, page, self.group)

    def note_refs(self, refs: list[int], page: int) -> None:
        for n in refs:
            self.pending.append([page, n, self.unit])
            if self.unit is not None:
                self.unit.refs.append(n)

    def _claim(self, page: int, n: int, take: bool) -> list | None:
        """The earliest unmatched reference numbered ``n`` on this page or the page before."""
        for i, r in enumerate(self.pending):
            if r[1] == n and r[0] in (page, page - 1):
                return self.pending.pop(i) if take else r
        return None

    # -- headings ----------------------------------------------------------------------------------
    def section_heading(self, text: str) -> None:
        self.close_unit()
        if text.startswith("General Information and Instructions"):
            self.section = "general"
        elif text == "Definitions":
            self.section = "definitions"
        elif text == "Prior Record Variables":
            self.section = "prv"
        elif (m := _OV_SECTION.match(text)):
            self.section = "ov"
            self.group = m.group(1).strip()
            self.groups.append(self.group)
        else:
            raise SgmParseError(f"unexpected section heading {text!r}")

    def sub_heading(self, text: str, page: int) -> None:
        self.close_unit()
        if self.section != "general":
            self.warnings.append(f"page {page}: unexpected 20 pt heading {text!r} outside the instructions")
            return
        m = _STEP.match(text)
        if m:
            step = m.group(1).upper()
            expect = _ROMAN[len(self.steps_seen)] if len(self.steps_seen) < len(_ROMAN) else None
            if step != expect:
                self.warnings.append(f"page {page}: Step {step} found where Step {expect} was expected")
            self.steps_seen.append(step)
            self.open_unit(f"SGM Instructions Step {step}", page)
            self.unit.title = [m.group(2).strip()]
        elif text not in ("General Information", "Legislative History"):
            self.warnings.append(f"page {page}: unrecognized heading {text!r} in the instructions; its text is not stored")

    def flush_heading(self) -> None:
        if not self.pending_heading:
            return
        lines = self.pending_heading
        self.pending_heading = []
        text = " ".join(ln.plain() for ln in lines)
        refs: list[int] = []
        for ln in lines:
            ln.render(refs)
        if refs:
            self.warnings.append(f"page {lines[0].page}: footnote reference on heading {text!r}")
        if lines[0].size >= 21.5:
            self.section_heading(text)
        else:
            self.sub_heading(text, lines[0].page)

    # -- body --------------------------------------------------------------------------------------
    def body_line(self, ln: _Line) -> None:
        u = self.unit
        refs: list[int] = []
        text = ln.render(refs)
        self.note_refs(refs, ln.page)
        if u is None:
            if ln.shaded and self.section in ("prv", "ov", "general"):
                self.warnings.append(f"page {ln.page}: shaded text outside any unit: {text[:60]!r}")
            return
        plain = ln.plain()
        if not u.citation.startswith("SGM Instructions"):
            if ln.bold and plain == "Points":
                u.flush_para()
                u.flush_table()
                u.in_table = True
                u.blocks.append(text)  # "Points", or "Points[Note 2]" when a footnote hangs on the header
                return
            im = _INSTRUCTIONS.match(plain)
            if ln.bold and im:
                u.flush_para()
                u.flush_table()
                if im.group(1) and im.group(1) != u.citation.removeprefix("SGM "):
                    self.warnings.append(f"{u.citation}: page {ln.page} heading says {plain!r}")
                if not (im.group(1) and "Instructions" in u.blocks):
                    u.blocks.append("Instructions")
                return
            if u.in_table:
                self.table_line(u, ln, text)
                return
        if ln.shaded:
            self.warnings.append(f"{u.citation}: shaded text outside the points table: {text[:60]!r}")
        self.para_line(u, ln, text)

    def table_line(self, u: _Unit, ln: _Line, text: str) -> None:
        first = ln.main[0] if ln.main else None
        if first is not None and first.bold and first.text.isdigit() and first.x0 < 70:
            rest = _join([w for w in ln.words if w is not first], [])
            u.rows.append([first.text, rest, ln.shaded, ln.top, ln.page])
            if ln.shaded:
                u.shaded_rows += 1
            return
        if not u.rows:
            self.warnings.append(f"{u.citation}: text in the points table before any points value: {text[:60]!r}")
            u.blocks.append(text)
            return
        row = u.rows[-1]
        if ln.page != row[4] or ln.top - row[3] > 1.6 * ln.size:
            self.warnings.append(f"{u.citation}: points-table line without a points value: {text[:60]!r}")
        if ln.shaded != row[2]:
            self.warnings.append(f"{u.citation}: shading differs within the row for {row[0]} points")
        row[1] = _continue_line(row[1], text)
        row[3] = ln.top
        row[4] = ln.page

    def para_line(self, u: _Unit, ln: _Line, text: str) -> None:
        p = u.para
        new = p is None or bool(_PARA_START.match(text))
        if p is not None and not new:
            if ln.page == p.page:
                new = (ln.top - p.last_top > 1.65 * max(ln.size, p.size) or not _approx(ln.size, p.size, 1.1)) \
                    and not text[:1].islower()
            else:
                new = bool(re.search(r"[.:;!?”\"]$", p.text)) and text[:1].isupper()
        if new:
            u.flush_para()
            u.para = _Para(text, ln.top, ln.page, ln.size)
        else:
            p.text = _continue_line(p.text, text)
            p.last_top, p.page, p.size = ln.top, ln.page, ln.size

    # -- unit headings (PRV / OV) ------------------------------------------------------------------
    def unit_heading(self, ln: _Line) -> None:
        text = ln.plain()
        m = _UNIT.match(text)
        kind = "PRV" if self.section == "prv" else "OV"
        if not m or m.group(1) != kind:
            raise SgmParseError(f"page {ln.page}: unexpected 16 pt heading {text!r} in the {self.section} section")
        self.open_unit(f"SGM {kind} {int(m.group(2))}", ln.page)

    def title_line(self, ln: _Line) -> None:
        u = self.unit
        refs: list[int] = []
        rendered = ln.render(refs)
        self.note_refs(refs, ln.page)
        plain = ln.plain()
        if _MCL_LINE.match(plain):
            if u.have_mcl:
                self.warnings.append(f"{u.citation}: second MCL line {plain!r}")
            u.mcl, u.have_mcl = rendered, True
            return
        if u.have_mcl:
            self.warnings.append(f"{u.citation}: 14 pt line after the MCL line: {plain!r}")
            self.para_line(u, ln, rendered)
            return
        u.title.append(plain)
        u.title_rendered.append(rendered)
        u.title_refs.extend(refs)

    # -- footnotes ---------------------------------------------------------------------------------
    def footnote_line(self, ln: _Line) -> None:
        """Footnotes are matched to references in reading order: a numbered footnote belongs to the
        earliest still-unmatched reference with that number on the same page (or, for a reference at the
        very bottom of a page, the page before). A "N." that matches no such reference is treated as
        ordinary footnote text (e.g. a continuation line starting "2009.")."""
        words = ln.words
        label: int | None = None
        body = words
        if words[0].sup:
            label, body = _sup_number(words[0].text), words[1:]
            if self._claim(ln.page, label, False) is None:
                self.warnings.append(f"page {ln.page}: footnote {label} has no unmatched reference: {_join(body, None)[:60]!r}")
                self.last_note = None
                return
        else:
            m = _FN_LABEL.match(words[0].text)
            if m and self._claim(ln.page, int(m.group(1)), False) is not None:
                label = int(m.group(1))
                glued = m.group(2)
                body = ([_Word(glued, words[0].x1, words[0].x1, words[0].top, words[0].bottom, words[0].size,
                               words[0].bold)] if glued else []) + words[1:]
        text = _join(body, None)
        if label is not None:
            ref = self._claim(ln.page, label, True)
            note = [label, text, ref[2]]
            self.notes.append(note)
            self.last_note = note
            return
        if self.last_note is None:
            self.warnings.append(f"page {ln.page}: footnote text with no footnote number: {text[:60]!r}")
            return
        self.last_note[1] = _continue_line(self.last_note[1], text)

    # -- driver ------------------------------------------------------------------------------------
    def feed_page(self, lines: list[_Line], pno: int, page_height: float) -> None:
        for ln in lines:
            plain = ln.plain()
            if not ln.main:
                self.warnings.append(f"page {pno}: superscript with no text line near top={ln.top:.0f}")
                continue
            if ln.top < _FURNITURE_TOP and _approx(ln.size, 10.0):
                if not _KNOWN_FURNITURE.match(plain) and self.section is not None:
                    self.warnings.append(f"page {pno}: unrecognized page-header text dropped: {plain!r}")
                continue
            if ln.bold and plain == "Continued on next page":
                continue
            if re.fullmatch(r"\d{1,3}", plain) and ln.top > page_height - 60:
                continue  # page number
            if re.fullmatch(r"\d{1,2}", plain) and ln.size < 9.5:
                self.warnings.append(f"page {pno}: orphan superscript-sized number {plain!r} at top={ln.top:.0f}")
                continue
            # headings (>= 23 pt: the cover title and the Definitions alphabet letters -- not structure)
            if ln.size >= 23:
                self.flush_heading()
                continue
            if ln.bold and ln.size >= 19.5:
                if self.pending_heading and not _approx(self.pending_heading[-1].size, ln.size, 0.5):
                    self.flush_heading()
                self.pending_heading.append(ln)
                continue
            self.flush_heading()
            if self.section is None:
                continue  # cover, disclaimer, acknowledgments
            if _approx(ln.size, 9.0, 0.4):
                # footnotes are 9 pt; they sit at the page bottom or, when a new PRV/OV starts lower on
                # the page, right after the previous unit's text
                self.footnote_line(ln)
                continue
            if self.section in ("prv", "ov") and ln.bold and _approx(ln.size, 16.0):
                self.unit_heading(ln)
                continue
            if self.section in ("prv", "ov") and self.unit is not None and ln.bold and _approx(ln.size, 14.0) \
                    and not self.unit.blocks and self.unit.para is None:
                self.title_line(ln)
                continue
            if self.section == "definitions":
                refs: list[int] = []
                ln.render(refs)
                self.note_refs(refs, ln.page)  # owner None: their footnotes are known, not stored
                continue
            self.body_line(ln)
        self.flush_heading()

    def finish(self) -> None:
        self.flush_heading()
        self.close_unit()
        for n, text, owner in self.notes:
            if owner is None:
                continue  # a note in a part of the manual that is not stored (e.g. Definitions)
            if n in owner.notes and owner.notes[n] != text:
                self.warnings.append(f"{owner.citation}: two different footnotes numbered {n}")
            owner.notes[n] = text
        for page, n, owner in self.pending:
            if owner is not None:
                self.warnings.append(f"{owner.citation}: footnote reference {n} on page {page} has no footnote text")


def _unit_body(u: _Unit) -> str:
    out: list[str] = []
    if u.mcl:
        out.append(u.mcl)
    if u.title_refs:
        out.append("Title: " + " ".join(u.title_rendered))
    out.extend(u.blocks)
    for n in sorted(u.notes):
        out.append(f"Note {n}: {u.notes[n]}")
    return "\n".join(out)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def parse_manual(path: str | Path, url: str = DEFAULT_URL, edition_label: str | None = None) -> SgmManual:
    """Parse the manual PDF at ``path`` (``url`` is recorded on each unit, with ``#page=N``)."""
    import pdfplumber

    warnings: list[str] = []
    pages_text = _text_pages(pdf_to_text(path))
    current, grid_pages, felony_pages = _locate(pages_text, warnings)

    parser = _Parser(url, warnings)
    with pdfplumber.open(str(path)) as pdf:
        if len(pdf.pages) != len(pages_text):
            raise SgmParseError(f"pdfplumber sees {len(pdf.pages)} pages, pdftotext {len(pages_text)}")
        for i in range(grid_pages[0] - 1):
            page = pdf.pages[i]
            lines, _ = _page_lines(page, i + 1)
            parser.feed_page(lines, i + 1, float(page.height))
            page.close()
    parser.finish()

    # PRVs and Steps: exactly one each
    docs: list[ParsedDoc] = []
    by_cit: dict[str, list[_Unit]] = {}
    for u in parser.units:
        by_cit.setdefault(u.citation, []).append(u)

    def mkdoc(u: _Unit, text: str) -> ParsedDoc:
        d = ParsedDoc(source="sgm", citation=u.citation, text=text, catchline=" ".join(u.title),
                      kind="guidelines", url=f"{url}#page={u.page}")
        c = normalize_citation(d.citation)
        if c is None or c.canonical != d.citation or c.source != "sgm":
            raise SgmParseError(f"produced non-canonical citation {d.citation!r}")
        return d

    for u in parser.units:
        if u.citation.startswith("SGM Instructions"):
            docs.append(mkdoc(u, _unit_body(u)))
    if len(parser.steps_seen) < 2:
        warnings.append(f"only {len(parser.steps_seen)} instruction Steps found")

    for n in range(1, N_PRV + 1):
        us = by_cit.get(f"SGM PRV {n}", [])
        if len(us) != 1:
            warnings.append(f"SGM PRV {n} found {len(us)} times (expected once)")
        if us:
            docs.append(mkdoc(us[0], _unit_body(us[0])))

    ov_groups: dict[int, list[str]] = {}
    ov_variants: dict[int, dict[str, str]] = {}
    for n in range(1, N_OV + 1):
        us = by_cit.get(f"SGM OV {n}", [])
        if not us:
            warnings.append(f"SGM OV {n} not found in any offense group")
            continue
        groups = [u.group or "?" for u in us]
        if len(set(groups)) != len(groups):
            warnings.append(f"SGM OV {n} printed more than once in one group: {groups}")
        ov_groups[n] = groups
        bodies = [_unit_body(u) for u in us]
        titles = [" ".join(u.title) for u in us]
        scored_for = "Scored for: " + "; ".join(groups)
        if len({_norm(t) for t in titles}) != 1:
            warnings.append(f"SGM OV {n}: title differs between groups: "
                            + "; ".join(f"{g}: {t!r}" for g, t in zip(groups, titles)))
        if len({_norm(b) for b in bodies}) == 1:
            text = scored_for + "\n" + bodies[0]
        else:
            # The manual prints different text for this OV under different crime groups (2026: OV 3, 12,
            # 16, 18). No copy is picked: every distinct version is stored, labeled with its groups, in
            # manual order (by the first group printing it).
            ov_variants[n] = {g: (t + "\n" + b) for g, t, b in zip(groups, titles, bodies)}
            versions: dict[str, tuple[list[str], str]] = {}
            for g, b in zip(groups, bodies):
                versions.setdefault(_norm(b), ([], b))[0].append(g)
            sections = [f"== Version for: {'; '.join(gs)} ==\n{b}" for gs, b in versions.values()]
            text = scored_for + "\n\n" + "\n\n".join(sections)
        docs.append(mkdoc(us[0], text))

    for u in parser.units:
        if u.shaded_rows and "shaded" not in _unit_body(u).lower():
            warnings.append(f"{u.citation} ({u.group or ''}): shaded rows but no explanation of shading")
        if not u.shaded_rows and re.search(r"\bshaded (area|areas)\b", _unit_body(u), re.I):
            warnings.append(f"{u.citation} ({u.group or ''}): text mentions shading but no shaded row was found")
        if not u.citation.startswith("SGM Instructions"):
            if not u.have_mcl:
                warnings.append(f"{u.citation} ({u.group or ''}): no 'MCL 777.xx' line under the heading")
            if not any(b.startswith("Points") and b.split("[")[0] == "Points" for b in u.blocks):
                warnings.append(f"{u.citation} ({u.group or ''}): no points table")
            if not u.title:
                warnings.append(f"{u.citation} ({u.group or ''}): no title")

    return SgmManual(docs=docs, current_through=current, edition_label=edition_label, grid_pages=grid_pages,
                     felony_list_pages=felony_pages, warnings=warnings, ov_groups=ov_groups,
                     ov_variants=ov_variants)


# --------------------------------------------------------------------------------------------------
# sources


def _currency(m: SgmManual) -> str:
    year = None
    if m.edition_label:
        ym = re.search(r"\b(19|20)\d{2}\b", m.edition_label)
        year = ym.group(0) if ym else None
    return f"SGM {year or m.current_through[:4]} (current through {m.current_through})"


class _SgmSourceBase:
    covered_chapters = None
    source_kinds = frozenset({"sgm"})
    complete = True
    currency: str | None = None
    manual: SgmManual | None = None

    def _load(self) -> SgmManual:  # pragma: no cover
        raise NotImplementedError

    def _ensure(self) -> SgmManual:
        if self.manual is None:
            m = self._load()
            self.manual = m
            self.warnings = list(m.warnings)
            self.complete = not m.warnings
            self.currency = _currency(m)
        return self.manual

    def iter_documents(self) -> Iterator[ParsedDoc]:
        yield from self._ensure().docs

    def iter_public_acts(self) -> Iterable[ParsedPublicAct]:
        return []

    def iter_ranges(self) -> Iterable[ParsedRange]:
        return []


class SgmFilesSource(_SgmSourceBase):
    """A saved copy of the manual PDF. Parsed on construction (local, a few seconds), so ``complete``
    and ``currency`` are known before the pipeline starts the run."""

    name = "sgm-files"

    def __init__(self, pdf_path: str | Path, url: str = DEFAULT_URL, edition_label: str | None = None):
        self.pdf_path = Path(pdf_path)
        if not self.pdf_path.is_file():
            raise ValueError(f"no such PDF: {self.pdf_path}")
        self.url = url
        self.edition_label = edition_label
        self.warnings: list[str] = []
        self._ensure()

    def _load(self) -> SgmManual:
        return parse_manual(self.pdf_path, self.url, self.edition_label)


class SgmWebSource(_SgmSourceBase):
    """Finds the current manual on the MJI "Sentencing Guidelines Manuals" page (courts.michigan.gov
    content API, link under the "Current Version" heading), downloads it through the injected client
    (default ``PoliteClient``, which honors robots.txt and fails closed), and parses it like the files
    source. The download happens on the first ``iter_documents`` call."""

    name = "sgm-web"

    def __init__(self, client=None):
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
        self.warnings: list[str] = []
        self.pdf_url: str | None = None
        self.edition_label: str | None = None

    def _load(self) -> SgmManual:
        from . import courts_api

        page = courts_api.content(LISTING_PATH, self.client)
        href, label = courts_api.link_under_heading(courts_api.page_body(page), CURRENT_HEADING)
        if not href.lower().endswith(".pdf"):
            raise SgmParseError(f"'{CURRENT_HEADING}' link is not a PDF: {href!r}")
        if href.startswith("/"):
            href = "https://www.courts.michigan.gov" + href
        self.pdf_url, self.edition_label = href, label or None
        data = self.client.get(href).content
        if not data.startswith(b"%PDF"):
            raise SgmParseError(f"{href} did not return a PDF")
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "sgm.pdf"
            f.write_bytes(data)
            return parse_manual(f, href, self.edition_label)
