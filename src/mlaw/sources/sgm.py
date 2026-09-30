"""Michigan Sentencing Guidelines Manual (MJI) -- the manual's text units.

One PDF (``sgm.pdf``, ~212 pages) published by the Michigan Judicial Institute on courts.michigan.gov.
This module turns the *text* parts of it into citable units:

* ``SGM Instructions Step I`` .. ``Step V``   "General Information and Instructions", one unit per Step
* ``SGM PRV 1`` .. ``SGM PRV 7``               Prior Record Variables
* ``SGM OV 1``  .. ``SGM OV 20``               Offense Variables (each OV is printed once per offense
                                               group that scores it; the copies must agree -- see below)

It also parses the felony list at the back of the manual (``SgmManual.felony``, see "felony list"
below) into rows for the ``sentencing_offenses`` table -- offense class and category per MCL cite -- which
the pipeline stores through :meth:`_SgmSourceBase.apply_extras`, and the sentencing grids (pp.125-133 in
2026) into ``SGM Grid M2`` .. ``SGM Grid H`` units (see "sentencing grids" below; ``scripts/sgm_grid_dump.py``
writes them as CSV for checking against the PDF).

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

from ..citations import MCL_TOKEN, normalize_citation
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
    felony: FelonyList | None = None  # the parsed felony list (its structural warnings are also in warnings)
    grids: list[Grid] = field(default_factory=list)  # the sentencing grids (structural problems also in warnings)


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
        felony = parse_felony_list(pdf, felony_pages)
        grids = parse_grids(pdf, grid_pages)
    parser.finish()
    warnings.extend(felony.structural)
    for g in grids:
        warnings.extend(g.structural)

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

    # sentencing grids: one unit per class; a grid that failed any structural check is a pointer to its page
    step4 = next((d for d in docs if d.citation == "SGM Instructions Step IV"), None)
    quote = GRID_UNITS_QUOTE if step4 is not None and GRID_UNITS_QUOTE in _norm(step4.text) else None
    if quote is None:
        warnings.append("SGM Instructions Step IV no longer says how grid cells are expressed (months / \"L\"); "
                        "grid documents are stored without that statement")
    for g in sorted(grids, key=lambda g: FELONY_CLASSES.index(g.klass) if g.klass in FELONY_CLASSES else 99):
        if g.klass not in FELONY_CLASSES:
            continue
        d = ParsedDoc(source="sgm", citation=f"SGM Grid {g.klass}", text=grid_text(g, quote),
                      catchline=g.title or f"Sentencing Grid for Class {g.klass}", kind="guidelines",
                      url=f"{url}#page={g.page}" if g.pages else url)
        c = normalize_citation(d.citation)
        if c is None or c.canonical != d.citation or c.source != "sgm":
            raise SgmParseError(f"produced non-canonical citation {d.citation!r}")
        docs.append(d)

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
                     ov_variants=ov_variants, felony=felony, grids=grids)


# --------------------------------------------------------------------------------------------------
# felony list ("MCL # Felony List", "Numerical Felony List")
#
# Layout (2026 manual, verified by rendering the pages): one ruled table per page. Every row, group bar
# and the header row is bounded by full-width horizontal rules, and the header row's cells by vertical
# rules, so the table geometry is read from the drawing instead of guessed from text positions:
#
#   * row boundaries  = the full-width horizontal rules (a wrapped description stays inside its row);
#   * column bounds   = the vertical rules crossing the header row ("Felonies by MCL #" | "Category" |
#                       "Class" | "Description*" | "Stat Max" | "Date Offense Added/ Substantive
#                       Amendments**"), checked against the header words themselves;
#   * group bars      = rows filled mid-gray (0.5) whose only text is "MCL 777.11a" etc. in the narrow
#                       left column -- the MCL 777.1x list the offenses below come from (``listed_in``);
#   * shaded rows     = rows whose cells sit on a light-gray fill (0.847 gray, a single-channel color --
#                       which is why the RGB-only check used for the OV tables does not see it). The page
#                       footnote: "Lightly shaded lines indicate that the statute governing the felony
#                       offense described there is no longer current." Matched by geometry: a fill rect
#                       that covers the row's vertical middle and spans the MCL-cite cell.
#
# Everything above the header row (running header, "Numerical Felony List" title) and below the last rule
# (the footnote block, page number) is outside the table and ignored -- but any *word* inside the table
# box that does not land in a row/column is a structural warning, never silently dropped.

# Offense classes that select a sentencing grid ("SGM Grid <class>").
FELONY_CLASSES = ("M2", "A", "B", "C", "D", "E", "F", "G", "H")
# Other values the 2026 manual prints in the Class column, kept exactly as printed and never mapped to a
# grid: "SPEC" (the MCL 777.18 offenses and MCL 750.462f(3)), "Variable" (MCL 750.213a(3)(a)), and "I"
# (only on two shaded, no-longer-current MIOSHA rows). Anything else is a structural parse failure.
FELONY_OTHER_CLASSES = ("SPEC", "Variable")
FELONY_OLD_CLASSES = ("I",)  # accepted only on shaded (not current) rows
# The Category column's printed values (2026); "Variable" = the computer-crime rows whose category follows
# the underlying offense (MCL 750.145d(2), 752.797(3)). Anything else is a structural parse failure.
FELONY_CATEGORIES = ("Person", "Property", "CS", "Pub ord", "Pub saf", "Pub trst", "Variable")
_FELONY_COLS = ("cite", "category", "class", "description", "stat_max", "amendments")
# A word of the header row that must fall inside each column.
_FELONY_HEADER_WORDS = {"cite": "Felonies", "category": "Category", "class": "Class",
                        "description": "Description*", "stat_max": "Stat", "amendments": "Date"}
_FELONY_CITE = re.compile(rf"^MCL\s+({MCL_TOKEN})((?:\s?\([0-9A-Za-z]{{1,5}}\))*)(\*?)$")
_GROUP_LABEL = re.compile(r"^MCL\s+777\.1\d[a-z]{0,2}$")  # 777.11a .. 777.16bb, 777.18
_NOT_CURRENT_WORDS = re.compile(r"deleted|replaced|repealed", re.I)


@dataclass
class FelonyRow:
    raw_cite: str  # as printed, e.g. "MCL 28.425o(3)(c)"
    mcl_citation: str  # canonical section, e.g. "MCL 28.425o"
    listed_in: str | None  # group bar above the row, e.g. "MCL 777.11b"
    category: str  # as printed, e.g. "Pub saf"
    offense_class: str  # M2 | A .. H
    description: str
    stat_max: str  # as printed: "10", "Life", "Fine", "Variable", ...
    amendments: str
    current: bool  # False = the row is shaded (statute no longer current)
    page: int  # 1-based PDF page


def _is_gray_fill(rect: dict, lo: float, hi: float) -> bool:
    """A filled rect whose gray (single-channel, or equal-RGB) color is within [lo, hi]."""
    if not rect.get("fill"):
        return False
    c = rect.get("non_stroking_color")
    if isinstance(c, (int, float)):
        vals = [float(c)]
    elif isinstance(c, (tuple, list)) and len(c) in (1, 3) and all(isinstance(v, (int, float)) for v in c):
        vals = [float(v) for v in c]
        if max(vals) - min(vals) > 0.02:
            return False
    else:
        return False
    return all(lo <= v <= hi for v in vals)


# A line ending "dam-" followed by a line starting "age" is a word broken at the cell edge (the manual
# hyphenates to fit its narrow cells); the hyphen is dropped. Anything else that ends in "-" or "/" stays
# glued without a space ("12/16/" + "2005"), and all other wrapped lines are joined with one space.
_SOFT_HYPHEN_BREAK = re.compile(r"[a-z]-$")


def _join_wrapped(prev: str, nxt: str) -> str:
    if _SOFT_HYPHEN_BREAK.search(prev) and nxt[:1].islower():
        return prev[:-1] + nxt
    if prev.endswith(("-", "/")) and not prev.endswith(" -"):
        return prev + nxt
    return prev + " " + nxt


def _chars_text(chars: list[dict]) -> str:
    """Text of one table cell from its characters: visual lines top to bottom, characters left to right.
    Word breaks come from the PDF's own space glyphs (or a visible gap), not from pdfplumber's word
    grouping -- which glues neighbouring cells together ("VariableReplaced,") and letter-spaces some
    cells ("R e p e a le d")."""
    glyphs = sorted(chars, key=lambda c: (c["top"], c["x0"]))
    lines: list[list[dict]] = []
    for c in glyphs:
        if lines and abs(c["top"] - lines[-1][0]["top"]) <= 2.0:
            lines[-1].append(c)
        else:
            lines.append([c])
    out = ""
    for ln in lines:
        text, prev, space = "", None, False
        for c in sorted(ln, key=lambda c: c["x0"]):
            if c["text"].isspace():
                space = True
                continue
            if prev is not None and (space or c["x0"] - prev["x1"] > 1.2):
                text += " "
            text += c["text"]
            prev, space = c, False
        if text:
            out = text if not out else _join_wrapped(out, text)
    return re.sub(r"\s+", " ", out).strip()


def _dedupe(vals: list[float], tol: float = 1.0) -> list[float]:
    out: list[float] = []
    for v in sorted(vals):
        if not out or v - out[-1] > tol:
            out.append(v)
    return out


def _felony_page_kind(page) -> str | None:
    head = " ".join(w["text"] for w in page.extract_words() if w["top"] < 60)
    if re.search(r"Alphabetical Felony List", head):
        return "alphabetical"
    if re.search(r"MCL # Felony List", head):
        return "numerical"
    return None


def _felony_page(page, pno: int, listed_in: str | None, structural: list[str]
                 ) -> tuple[list[FelonyRow], str | None]:
    """Parse one felony-list page. Returns its rows and the group label in force at the page end."""
    chars = [c for c in page.chars if c.get("upright", True)]
    words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
    width = float(page.width)

    hlines = [ln for ln in page.lines if abs(ln["top"] - ln["bottom"]) < 1.0
              and ln["x1"] - ln["x0"] > 0.8 * (width - 60)]
    ys = _dedupe([(ln["top"] + ln["bottom"]) / 2 for ln in hlines])
    vlines = [ln for ln in page.lines if abs(ln["x0"] - ln["x1"]) < 1.0 and ln["bottom"] - ln["top"] > 5]

    hdr = [w for w in words if w["text"] in ("Category", "Class", "Description*")]
    if len(hdr) != 3 or len(ys) < 3:
        structural.append(f"felony list page {pno}: no table header / rules found")
        return [], listed_in
    hmid = sum((w["top"] + w["bottom"]) / 2 for w in hdr) / len(hdr)
    hi = next((i for i in range(len(ys) - 1) if ys[i] <= hmid <= ys[i + 1]), None)
    if hi is None:
        structural.append(f"felony list page {pno}: header words not inside a ruled row")
        return [], listed_in
    h_top, h_bot = ys[hi], ys[hi + 1]
    xs = _dedupe([ln["x0"] for ln in vlines if ln["top"] <= h_top + 1 and ln["bottom"] >= h_bot - 1])

    # columns: the ruled intervals holding each header word, which must be six consecutive intervals; a
    # seventh interval to their left (numerical list only) holds the group labels
    col_idx: dict[str, int] = {}
    for name, needle in _FELONY_HEADER_WORDS.items():
        hw = [w for w in words if w["text"] == needle and h_top <= (w["top"] + w["bottom"]) / 2 <= h_bot]
        idx = {j for w in hw for j in range(len(xs) - 1) if xs[j] - 1 <= w["x0"] and w["x1"] <= xs[j + 1] + 1}
        if len(hw) != 1 or len(idx) != 1:
            structural.append(f"felony list page {pno}: header word {needle!r} not inside one ruled column")
            return [], listed_in
        col_idx[name] = idx.pop()
    j0 = col_idx["cite"]
    if [col_idx[n] for n in _FELONY_COLS] != list(range(j0, j0 + 6)) or j0 > 1 or len(xs) != j0 + 7:
        structural.append(f"felony list page {pno}: unexpected column layout {col_idx} with rules "
                          f"{[round(x, 1) for x in xs]}")
        return [], listed_in
    bounds = {name: (xs[j0 + k], xs[j0 + k + 1]) for k, name in enumerate(_FELONY_COLS)}
    label_col = (xs[0], xs[1]) if j0 == 1 else None
    table_left, table_right = xs[0], xs[-1]
    # The last row's bottom rule is sometimes drawn only across the group-label column (p.144 in 2026);
    # the column rules still run to the table's bottom edge, so that edge closes the last row.
    t_bottom = max(ln["bottom"] for ln in vlines if table_left - 1 <= ln["x0"] <= table_right + 1)
    if t_bottom > ys[-1] + 5:
        ys.append(t_bottom)

    # Shaded rows are usually 0.847 gray, but at least one is 0.753 (MCL 445.67 on p.149 of the 2026
    # manual, and its alphabetical-list twin on p.199) -- missed by an 0.8 cutoff and caught only at the
    # attorney's checkpoint review. Accept 0.72-0.95, and refuse any other gray inside the table, so a new
    # shade in a future edition fails loudly instead of silently reading as "current".
    fills = [r for r in page.rects if _is_gray_fill(r, 0.72, 0.95)]
    bars = [r for r in page.rects if _is_gray_fill(r, 0.3, 0.7)]
    for r in page.rects:
        if (_is_gray_fill(r, 0.05, 0.99) and not _is_gray_fill(r, 0.72, 0.95) and not _is_gray_fill(r, 0.3, 0.7)
                and table_left - 1 <= r["x0"] and r["x1"] <= table_right + 1):
            structural.append(f"felony list page {pno}: unrecognized gray fill {r.get('non_stroking_color')!r}"
                              f" at y={r['top']:.0f}; cannot tell whether that row is shaded")

    in_box = [c for c in chars if not c["text"].isspace()
              and table_left - 1 <= (c["x0"] + c["x1"]) / 2 <= table_right + 1
              and h_bot < (c["top"] + c["bottom"]) / 2 < ys[-1]]
    rows: list[FelonyRow] = []
    used: set[int] = set()
    for i in range(hi + 1, len(ys) - 1):
        top, bot = ys[i], ys[i + 1]
        mid = (top + bot) / 2
        row_chars = [c for c in chars if top < (c["top"] + c["bottom"]) / 2 < bot
                     and table_left - 1 <= (c["x0"] + c["x1"]) / 2 <= table_right + 1]
        used.update(id(c) for c in row_chars)
        is_bar = any(r["top"] <= mid <= r["bottom"] and r["x0"] <= table_left + 1 for r in bars)
        if is_bar:
            # the label sits in the narrow left column but may overflow it ("MCL 777.16aa")
            text = _chars_text(row_chars)
            if label_col is None or not _GROUP_LABEL.match(text):
                structural.append(f"felony list page {pno}: unexpected shaded bar at y={mid:.0f}: {text!r}")
                continue
            listed_in = text
            continue
        cell: dict[str, list[dict]] = {name: [] for name in _FELONY_COLS}
        label: list[dict] = []
        for c in row_chars:
            cx = (c["x0"] + c["x1"]) / 2
            if label_col is not None and cx < label_col[1]:
                label.append(c)
                continue
            for name, (x0, x1) in bounds.items():
                if x0 <= cx <= x1:
                    cell[name].append(c)
                    break
        texts = {name: _chars_text(cs) for name, cs in cell.items()}
        if _chars_text(label):
            structural.append(f"felony list page {pno}: text {_chars_text(label)!r} in the group-label column "
                              f"outside a group bar at y={mid:.0f}")
            continue
        if not any(texts.values()):
            structural.append(f"felony list page {pno}: empty table row at y={mid:.0f}")
            continue
        ctx = f"felony list page {pno}, {texts['cite'] or '(no cite)'}"
        m = _FELONY_CITE.match(texts["cite"])
        canon = normalize_citation(m.group(1)) if m else None
        if m is None or canon is None or canon.source != "mcl":
            structural.append(f"{ctx}: unparseable MCL citation {texts['cite']!r}")
            continue
        if not texts["class"]:
            structural.append(f"{ctx}: row has no class")
            continue
        if not texts["category"]:
            structural.append(f"{ctx}: row has no category")
            continue
        c0, c1 = bounds["cite"]
        in_row = [r for r in fills if r["top"] <= mid <= r["bottom"]]
        on_cite = any(r["x0"] <= c0 + 2 and r["x1"] >= c1 - 2 for r in in_row)
        if in_row and not on_cite:
            structural.append(f"{ctx}: light-gray fill in the row but not behind the MCL cell")
        if on_cite:
            covered = sum(min(r["x1"], table_right) - max(r["x0"], c0) for r in in_row if r["x1"] > c0)
            if covered < 0.95 * (table_right - c0):
                structural.append(f"{ctx}: shading covers only part of the row")
        if label_col is not None and listed_in is None:
            structural.append(f"{ctx}: row before any 'MCL 777.1x' group bar")
        rows.append(FelonyRow(
            raw_cite=texts["cite"], mcl_citation=canon.canonical,
            listed_in=listed_in if label_col is not None else None,
            category=texts["category"], offense_class=texts["class"], description=texts["description"],
            stat_max=texts["stat_max"], amendments=texts["amendments"], current=not on_cite, page=pno,
        ))

    # anything printed inside the table box but in no row (e.g. astride a rule) is a parse failure
    stray = [c for c in in_box if id(c) not in used]
    if stray:
        structural.append(f"felony list page {pno}: {len(stray)} characters inside the table are in no row: "
                          f"{''.join(c['text'] for c in stray)[:40]!r}")
    if not rows:
        structural.append(f"felony list page {pno}: no offense rows parsed")
    return rows, listed_in


def _row_key(r: FelonyRow) -> tuple:
    """What must agree between the numerical and the alphabetical list for the same offense."""
    return (re.sub(r"\s+", "", r.raw_cite), r.category, r.offense_class, r.stat_max, r.current,
            re.sub(r"\s+", " ", r.description))


@dataclass
class FelonyList:
    rows: list[FelonyRow]  # the numerical list (the stored data)
    alphabetical: list[FelonyRow]  # the same offenses re-sorted by description (used only to cross-check)
    structural: list[str]  # the parse cannot be trusted (these make the source incomplete)
    review: list[str]  # disagreements for a human to check against the PDF (rows are kept as printed)


def parse_felony_list(pdf, pages: tuple[int, int]) -> FelonyList:
    """Parse the felony list pages of an open pdfplumber document.

    The range holds two printings of the same table: the "MCL # Felony List" (numerical, with the
    "MCL 777.1x" group bars) and the "Alphabetical Felony List" (same rows sorted by description, no
    group bars). The numerical list is the data; the alphabetical list is parsed the same way and every
    row must appear in both with the same cite, category, class, stat max, description and shading.

    Review notes (never resolved automatically): rows present in only one printing or differing between
    them, and rows whose shading disagrees with the amendments column (shaded without "deleted /
    replaced / repealed", or unshaded with one of those words)."""
    fl = FelonyList([], [], [], [])
    listed_in: str | None = None
    kinds: list[str] = []
    for pno in range(pages[0], pages[1] + 1):
        page = pdf.pages[pno - 1]
        kind = _felony_page_kind(page)
        if kind is None:
            fl.structural.append(f"felony list page {pno}: running header names neither list")
            page.close()
            continue
        if kinds and kind != kinds[-1] and kind in kinds:
            fl.structural.append(f"felony list page {pno}: {kind} list resumes after the other list")
        kinds.append(kind)
        got, listed_in = _felony_page(page, pno, listed_in if kind == "numerical" else None, fl.structural)
        page.close()
        (fl.rows if kind == "numerical" else fl.alphabetical).extend(got)
    if not fl.rows:
        fl.structural.append("felony list: no numerical-list rows parsed")
    for r in fl.rows + fl.alphabetical:
        ctx = f"felony list page {r.page}, {r.raw_cite}"
        if r.category not in FELONY_CATEGORIES:
            fl.structural.append(f"{ctx}: unknown category {r.category!r}")
        if r.offense_class in FELONY_OLD_CLASSES:
            if r.current:
                fl.structural.append(f"{ctx}: class {r.offense_class!r} on a row that is not shaded")
        elif r.offense_class not in FELONY_CLASSES + FELONY_OTHER_CLASSES:
            fl.structural.append(f"{ctx}: unknown class {r.offense_class!r}")
    if not fl.alphabetical:
        fl.structural.append("felony list: no alphabetical-list rows parsed (no cross-check possible)")

    for r in fl.rows:  # (the alphabetical printing is compared with these rows below)
        if not r.stat_max:
            fl.review.append(f"page {r.page}, {r.raw_cite} (class {r.offense_class}): the Stat Max cell is blank")
        says = bool(_NOT_CURRENT_WORDS.search(r.amendments))
        if says and r.current:
            fl.review.append(f"page {r.page}, {r.raw_cite} (class {r.offense_class}): not shaded, but the "
                             f"amendments column says {r.amendments!r}")
        elif not says and not r.current:
            fl.review.append(f"page {r.page}, {r.raw_cite} (class {r.offense_class}): shaded (not current), "
                             f"but the amendments column says {r.amendments!r}")

    num: dict[tuple, list[FelonyRow]] = {}
    for r in fl.rows:
        num.setdefault(_row_key(r), []).append(r)
    alpha: dict[tuple, list[FelonyRow]] = {}
    for r in fl.alphabetical:
        alpha.setdefault(_row_key(r), []).append(r)
    for key in sorted(set(num) | set(alpha), key=str):
        a, b = num.get(key, []), alpha.get(key, [])
        if len(a) == len(b):
            continue
        where = "; ".join([f"numerical p.{r.page}" for r in a] + [f"alphabetical p.{r.page}" for r in b])
        fl.review.append(f"{(a or b)[0].raw_cite} (category {key[1]}, class {key[2]}, stat max {key[3]}, "
                         f"{'current' if key[4] else 'shaded'}, {key[5]!r}): printed {len(a)}x in the numerical "
                         f"list, {len(b)}x in the alphabetical list [{where}]")
    return fl


# --------------------------------------------------------------------------------------------------
# sentencing grids ("Sentencing Grid for Class M2 ... H")
#
# Layout (2026 manual, pp.125-133, one grid per page, p.134 is an empty tab page; verified by rendering):
# one ruled table per page, read entirely from the drawing:
#
#   * columns     = the 15 vertical rules: | OV Level | A-min | A-max | ... | F-min | F-max | Offender Status |
#                   (each PRV level is a *pair* of columns; the header cell above the pair holds the letter and
#                   its points, "A" / "0 Points");
#   * OV rows     = the full-width horizontal rules below the header; the OV label cell holds "I 0-9 Points";
#   * statuses    = inside an OV row, the max column of every PRV is split by three short rules into four
#                   sub-rows; the Offender Status column labels them (blank = base, HO2, HO3, HO4);
#   * a cell      = the MINIMUM, printed once (large, centered in the min column, spanning all four statuses),
#                   and one MAXIMUM per status sub-row. The printed maximum is kept as printed: "150", "300/L",
#                   "17*" (the asterisk marks an intermediate sanction cell);
#   * shading     = a 0.82 gray fill behind a max sub-cell (the manual's "straddle cells are shaded"). Shading
#                   varies by status within one cell (D, OV I / PRV D: base 0-17* unshaded, HO2..HO4 shaded).
#                   The *last-painted* fill covering a sub-cell decides (p.128 paints one min cell white).
#
# Every character inside the table box must land in exactly one header / label / min / max cell; every
# cell must hold exactly one value of the expected shape. Any failure is structural: the grid is then
# stored as a pointer to the PDF page, never as numbers.

GRID_STATUSES = ("base", "HO2", "HO3", "HO4")
_GRID_TITLE_LINE = re.compile(r"^Sentencing Grid for Class (M2|[A-H])\b.*[—–-]\s*(MCL 777\.6\d)$")
_GRID_PRV_HEAD = re.compile(r"^([A-F]) (\d+(?:-\d+|\+)? Points)$")
_GRID_OV_LABEL = re.compile(r"^([IVX]+) (\d+(?:-\d+|\+)?) Points$")
_GRID_MIN = re.compile(r"^\d+$")
_GRID_MAX = re.compile(r"^(\d+)(\*|/L)?$|^L$")
_GRID_NOTE_START = "•"
_ROMANS = ("I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X")
# Statement quoted into every grid document, only if SGM Instructions Step IV (as parsed) contains it.
GRID_UNITS_QUOTE = ("The recommended minimum sentence in each cell is expressed by a range of numbers "
                    "(in months) or life imprisonment (“L”).")


@dataclass
class CellRange:
    min: str  # as printed, e.g. "90", "0"
    max: str  # as printed, e.g. "150", "300/L", "17*"
    shaded: bool = False  # the max sub-cell for this status sits on a gray fill


@dataclass
class Grid:
    klass: str  # M2 | A .. H
    title: str  # "Sentencing Grid for Class D Offenses—MCL 777.65"
    subtitle: str  # "Includes Ranges Calculated for Habitual Offenders (MCL 777.21(3)(a)-(c))"
    statute: str  # "MCL 777.65"
    pages: tuple[int, ...]
    prv_levels: list[tuple[str, str]] = field(default_factory=list)  # ("A", "0 Points")
    ov_levels: list[tuple[str, str]] = field(default_factory=list)  # ("I", "0-9 Points")
    cells: dict[tuple[str, str, str], CellRange] = field(default_factory=dict)  # (ov, prv, status)
    min_shaded: dict[tuple[str, str], bool] = field(default_factory=dict)  # (ov, prv) -> min cell shaded
    notes: list[str] = field(default_factory=list)  # bullet notes below the grid, verbatim
    structural: list[str] = field(default_factory=list)  # parse cannot be trusted -> pointer doc
    review: list[str] = field(default_factory=list)  # as printed, but worth a human look

    @property
    def page(self) -> int:
        return self.pages[0]

    @property
    def ok(self) -> bool:
        return not self.structural


def _grid_value(s: str) -> float | None:
    """Numeric part of a printed maximum ("300/L" -> 300, "17*" -> 17); "L" alone -> infinity."""
    if s == "L":
        return float("inf")
    m = re.match(r"^(\d+)", s)
    return float(m.group(1)) if m else None


def _char_lines(chars: list[dict], tol: float = 2.0) -> list[list[dict]]:
    lines: list[list[dict]] = []
    for c in sorted(chars, key=lambda c: (c["top"], c["x0"])):
        if lines and abs(c["top"] - lines[-1][0]["top"]) <= tol:
            lines[-1].append(c)
        else:
            lines.append([c])
    return lines


def _mid(c: dict) -> tuple[float, float]:
    return (c["x0"] + c["x1"]) / 2, (c["top"] + c["bottom"]) / 2


def _last_fill(rects: list[dict], x: float, y: float) -> dict | None:
    """The last-painted filled rect covering (x, y) -- what the reader sees there."""
    hit = None
    for r in rects:
        if r.get("fill") and r["x0"] <= x <= r["x1"] and r["top"] <= y <= r["bottom"]:
            hit = r
    return hit


def _grid_page(page, pno: int) -> Grid | None:
    """Parse one grid page; None if the page holds no grid title (the empty tab page)."""
    chars = [c for c in page.chars if c.get("upright", True) and not c["text"].isspace()]
    lines = [_chars_text(ln) for ln in _char_lines(chars)]
    ti = next((i for i, t in enumerate(lines) if _GRID_TITLE_LINE.match(t)), None)
    if ti is None:
        if any(re.search(r"\d", t) for t in lines):
            return Grid("?", "", "", "", (pno,), structural=[f"page {pno}: numbers on a grid page with no grid title"])
        return None
    tm = _GRID_TITLE_LINE.match(lines[ti])
    g = Grid(tm.group(1), lines[ti], lines[ti + 1] if ti + 1 < len(lines) else "", tm.group(2), (pno,))
    err = g.structural.append
    ctx = f"SGM Grid {g.klass} (page {pno})"
    if not g.subtitle.startswith("Includes Ranges Calculated for Habitual Offenders"):
        err(f"{ctx}: unexpected line under the title: {g.subtitle!r}")

    # --- ruling
    vl = [ln for ln in page.lines if abs(ln["x0"] - ln["x1"]) < 1.0 and ln["bottom"] - ln["top"] > 50]
    xs = _dedupe([ln["x0"] for ln in vl])
    if len(xs) != 15:
        err(f"{ctx}: expected 15 vertical rules (OV | 6 x min,max | status), found {len(xs)}: "
            f"{[round(x, 1) for x in xs]}")
        return g
    left, right = xs[0], xs[-1]
    hl = [ln for ln in page.lines if abs(ln["top"] - ln["bottom"]) < 1.0]
    full = _dedupe([(ln["top"] + ln["bottom"]) / 2 for ln in hl if ln["x0"] <= left + 1 and ln["x1"] >= right - 1])
    t_bottom = max(ln["bottom"] for ln in vl)
    if full and t_bottom > full[-1] + 5:
        full.append(t_bottom)
    if len(full) < 3:
        err(f"{ctx}: too few full-width rules: {full}")
        return g
    top, h_bot, bottom = full[0], full[1], full[-1]
    row_ys = full[1:]
    in_table = [c for c in chars if left - 1 <= _mid(c)[0] <= right + 1 and top < _mid(c)[1] < bottom]
    used: set[int] = set()

    def take(x0: float, x1: float, y0: float, y1: float) -> list[dict]:
        got = [c for c in in_table if x0 <= _mid(c)[0] <= x1 and y0 < _mid(c)[1] < y1]
        for c in got:
            if id(c) in used:
                err(f"{ctx}: character {c['text']!r} at ({_mid(c)[0]:.0f},{_mid(c)[1]:.0f}) falls in two cells")
            used.add(id(c))
        return got

    # --- header
    subs = _dedupe([(ln["top"] + ln["bottom"]) / 2 for ln in hl if top + 1 < ln["top"] < h_bot - 1
                    and abs(ln["x0"] - xs[1]) < 2 and abs(ln["x1"] - xs[-2]) < 2])
    if len(subs) != 1:
        err(f"{ctx}: no single 'PRV Level' underline in the header row ({subs})")
        return g
    y_sub = subs[0]
    for name, (x0, x1, y0, y1), want in (
        ("OV header", (xs[0], xs[1], top, h_bot), "OV Level"),
        ("PRV Level header", (xs[1], xs[-2], top, y_sub), "PRV Level"),
        ("status header", (xs[-2], xs[-1], top, h_bot), "Offender Status"),
    ):
        got = _chars_text(take(x0, x1, y0, y1))
        if got != want:
            err(f"{ctx}: {name} reads {got!r}, expected {want!r}")
    for k in range(6):
        text = _chars_text(take(xs[1 + 2 * k], xs[3 + 2 * k], y_sub, h_bot))
        m = _GRID_PRV_HEAD.match(text)
        if not m or m.group(1) != "ABCDEF"[k]:
            err(f"{ctx}: PRV header {k + 1} reads {text!r}")
            return g
        g.prv_levels.append((m.group(1), m.group(2)))

    # --- rows
    fills = [r for r in page.rects if r.get("fill")]
    for i in range(len(row_ys) - 1):
        y0, y1 = row_ys[i], row_ys[i + 1]
        label = _chars_text(take(xs[0], xs[1], y0, y1))
        m = _GRID_OV_LABEL.match(label)
        if not m or m.group(1) != _ROMANS[i]:
            err(f"{ctx}: OV label of row {i + 1} reads {label!r}")
            return g
        ov = m.group(1)
        g.ov_levels.append((ov, f"{m.group(2)} Points"))

        def bands(x0: float, x1: float) -> list[float] | None:
            cx = (x0 + x1) / 2
            ys = _dedupe([(ln["top"] + ln["bottom"]) / 2 for ln in hl if ln["x0"] <= cx <= ln["x1"]
                          and y0 + 1 < ln["top"] < y1 - 1])
            return [y0, *ys, y1] if len(ys) == 3 else None

        ref: list[float] | None = None
        for k in range(6):
            b = bands(xs[2 + 2 * k], xs[3 + 2 * k])
            if b is None:
                err(f"{ctx}: OV {ov} / PRV {'ABCDEF'[k]}: max column is not split into four status rows")
                return g
            if ref is not None and any(abs(a - c) > 1.5 for a, c in zip(b, ref)):
                err(f"{ctx}: OV {ov}: status rows of PRV {'ABCDEF'[k]} do not line up with PRV A")
            ref = ref or b
        sb = bands(xs[-2], xs[-1]) or ref  # the status column may have no rules of its own
        for s, status in enumerate(GRID_STATUSES):
            got = _chars_text(take(xs[-2], xs[-1], sb[s], sb[s + 1]))
            if got != ("" if status == "base" else status):
                err(f"{ctx}: OV {ov}: offender status row {s + 1} reads {got!r}, expected "
                    f"{'blank' if status == 'base' else status!r}")
        for k, (prv, _) in enumerate(g.prv_levels):
            mx0, mx1 = xs[1 + 2 * k], xs[2 + 2 * k]
            mn = _chars_text(take(mx0, mx1, y0, y1))
            if not _GRID_MIN.match(mn):
                err(f"{ctx}: OV {ov} / PRV {prv}: minimum cell reads {mn!r}")
                continue
            f = _last_fill(fills, (mx0 + mx1) / 2, (y0 + y1) / 2)
            g.min_shaded[(ov, prv)] = _grid_shade(f, ctx, g.structural)
            cx0, cx1 = xs[2 + 2 * k], xs[3 + 2 * k]
            b = bands(cx0, cx1)
            for s, status in enumerate(GRID_STATUSES):
                mx = _chars_text(take(cx0, cx1, b[s], b[s + 1]))
                if not _GRID_MAX.match(mx):
                    err(f"{ctx}: OV {ov} / PRV {prv} / {status}: maximum reads {mx!r}")
                    continue
                f = _last_fill(fills, (cx0 + cx1) / 2, (b[s] + b[s + 1]) / 2)
                g.cells[(ov, prv, status)] = CellRange(mn, mx, _grid_shade(f, ctx, g.structural))

    stray = [c for c in in_table if id(c) not in used]
    if stray:
        err(f"{ctx}: {len(stray)} characters inside the grid are in no cell: "
            f"{''.join(c['text'] for c in stray)[:40]!r}")

    # --- text outside the table: known furniture above, bullet notes below
    above = [_chars_text(ln) for ln in _char_lines([c for c in chars if _mid(c)[1] < top])]
    for t in above:
        if t not in ("Grids", "Sentencing Grids", g.title, g.subtitle):
            err(f"{ctx}: unexpected text above the grid: {t!r}")
    paras: list[list[dict]] = []
    for ln in _char_lines([c for c in chars if _mid(c)[1] > bottom]):
        first = min(ln, key=lambda c: c["x0"])["text"]
        if first == _GRID_NOTE_START:
            paras.append([c for c in ln if c is not min(ln, key=lambda c: c["x0"])])
        elif paras:
            paras[-1].extend(ln)
        else:
            err(f"{ctx}: text below the grid before any bullet: {_chars_text(ln)!r}")
    g.notes = [_chars_text(p) for p in paras]
    return g


def _grid_shade(rect: dict | None, ctx: str, structural: list[str]) -> bool:
    if rect is None or _is_gray_fill(rect, 0.99, 1.0):
        return False
    if _is_gray_fill(rect, 0.72, 0.95):
        return True
    structural.append(f"{ctx}: unrecognized fill {rect.get('non_stroking_color')!r} at "
                      f"({rect['x0']:.0f},{rect['top']:.0f}); cannot tell whether that cell is shaded")
    return False


def _validate_grid(g: Grid) -> None:
    """Completeness (structural) and consistency (review) checks on a parsed grid."""
    ctx = f"SGM Grid {g.klass} (page {g.page})"
    prvs = [p for p, _ in g.prv_levels]
    ovs = [o for o, _ in g.ov_levels]
    missing = [(o, p, s) for o in ovs for p in prvs for s in GRID_STATUSES if (o, p, s) not in g.cells]
    if not ovs or len(prvs) != 6:
        g.structural.append(f"{ctx}: {len(ovs)} OV levels, {len(prvs)} PRV levels")
    if missing:
        g.structural.append(f"{ctx}: {len(missing)} cells missing, e.g. {missing[:3]}")
        return
    legend = any("Intermediate sanction cells are marked by asterisks" in n for n in g.notes)
    straddle = any("straddle cells are shaded" in n for n in g.notes)
    stars = [k for k, c in g.cells.items() if c.max.endswith("*")]
    shaded = [k for k, c in g.cells.items() if c.shaded] + [k for k, v in g.min_shaded.items() if v]
    if stars and not legend:
        g.structural.append(f"{ctx}: asterisked cells but no intermediate-sanction legend below the grid")
    if shaded and not straddle:
        g.structural.append(f"{ctx}: shaded cells but no straddle-cell legend below the grid")

    for o in ovs:
        for p in prvs:
            cs = [g.cells[(o, p, s)] for s in GRID_STATUSES]
            vals = [_grid_value(c.max) for c in cs]
            if any(b < a for a, b in zip(vals, vals[1:])):
                g.structural.append(f"{ctx}: OV {o} / PRV {p}: maxima decrease across offender status "
                                    f"{[c.max for c in cs]}")
            for s, c in zip(GRID_STATUSES, cs):
                if float(c.min) > _grid_value(c.max):
                    g.structural.append(f"{ctx}: OV {o} / PRV {p} / {s}: minimum {c.min} above maximum {c.max}")
            # habitual-offender maxima: MCL 777.21(3)(a)-(c) raise the upper limit by 25/50/100 %, and the
            # manual's own note says the results are rounded down to the whole month
            base = cs[0].max
            bv = _grid_value(base)
            if bv is not None and bv != float("inf"):
                for s, c, pct in zip(GRID_STATUSES[1:], cs[1:], (125, 150, 200)):
                    exp = int(bv) * pct // 100
                    if _grid_value(c.max) != exp or c.max.endswith("/L") != base.endswith("/L"):
                        g.review.append(f"{ctx}: OV {o} / PRV {p} / {s}: printed {c.max}, but base {base} "
                                        f"+{pct - 100}% rounded down is {exp}{'/L' if base.endswith('/L') else ''}")
            # the manual's definitions of the cell kinds (Step IV): intermediate sanction = upper limit 18
            # months or less (asterisk); straddle = lower limit one year or less and upper limit more than
            # 18 months (shaded); prison = the rest (unmarked)
            for s, c in zip(GRID_STATUSES, cs):
                lo, hi = float(c.min), _grid_value(c.max)
                expect = "intermediate" if hi <= 18 else ("straddle" if lo <= 12 else "prison")
                printed = ("intermediate" if c.max.endswith("*") else "straddle" if c.shaded else "prison")
                if c.max.endswith("*") and c.shaded:
                    printed = "asterisked and shaded"
                if printed != expect:
                    g.review.append(f"{ctx}: OV {o} / PRV {p} / {s}: {c.min}-{c.max} is printed as {printed}, "
                                    f"but its numbers make it {expect}")
            if g.min_shaded.get((o, p)) != all(c.shaded for c in cs):
                verb = "is" if g.min_shaded.get((o, p)) else "is not"
                g.review.append(f"{ctx}: OV {o} / PRV {p}: minimum cell {verb}"
                                f" shaded, maxima shaded: {[c.shaded for c in cs]}")
    # monotonic across the grid (review only: the manual is what it is)
    for s in GRID_STATUSES:
        for o in ovs:
            for a, b in zip(prvs, prvs[1:]):
                ca, cb = g.cells[(o, a, s)], g.cells[(o, b, s)]
                if _grid_value(cb.max) < _grid_value(ca.max) or int(cb.min) < int(ca.min):
                    g.review.append(f"{ctx}: {s}, OV {o}: PRV {a} {ca.min}-{ca.max} > PRV {b} {cb.min}-{cb.max}")
        for p in prvs:
            for a, b in zip(ovs, ovs[1:]):
                ca, cb = g.cells[(a, p, s)], g.cells[(b, p, s)]
                if _grid_value(cb.max) < _grid_value(ca.max) or int(cb.min) < int(ca.min):
                    g.review.append(f"{ctx}: {s}, PRV {p}: OV {a} {ca.min}-{ca.max} > OV {b} {cb.min}-{cb.max}")
    # Step IV: "every cell in the sentencing grids for class G and class H offenses is either an intermediate
    # sanction cell or a straddle cell. The sentencing grids for M2 and class A offenses are the only grids
    # that do not contain intermediate sanction cells or straddle cells."
    marked = [k for k, c in g.cells.items() if c.max.endswith("*") or c.shaded]
    if g.klass in ("G", "H") and len(marked) != len(g.cells):
        g.review.append(f"{ctx}: {len(g.cells) - len(marked)} unmarked (prison) cells in a class {g.klass} grid")
    if g.klass in ("M2", "A") and marked:
        g.review.append(f"{ctx}: {len(marked)} marked cells in a class {g.klass} grid")
    if g.klass not in ("M2", "A", "G", "H") and not marked:
        g.review.append(f"{ctx}: no intermediate sanction or straddle cells")


def parse_grids(pdf, pages: tuple[int, int]) -> list[Grid]:
    """Parse the sentencing grids on ``pages`` (1-based, inclusive) of an open pdfplumber document."""
    grids: list[Grid] = []
    for pno in range(pages[0], pages[1] + 1):
        page = pdf.pages[pno - 1]
        g = _grid_page(page, pno)
        page.close()
        if g is None:
            continue
        if g.ok:
            _validate_grid(g)
        grids.append(g)
    seen = [g.klass for g in grids]
    for k in FELONY_CLASSES:
        if seen.count(k) != 1:
            extra = Grid(k, "", "", "", (), structural=[f"SGM Grid {k}: found {seen.count(k)} times on pages "
                                                        f"{[g.page for g in grids if g.klass == k]}"])
            grids.append(extra)
    return grids


def _md_table(g: Grid, status: str) -> str:
    head = "| OV level | " + " | ".join(f"PRV {p} ({pts})" for p, pts in g.prv_levels) + " |"
    sep = "|---" * (len(g.prv_levels) + 1) + "|"
    rows = []
    for o, pts in g.ov_levels:
        cells = []
        for p, _ in g.prv_levels:
            c = g.cells[(o, p, status)]
            cells.append(f"{c.min}–{c.max}" + (" [shaded]" if c.shaded else ""))
        rows.append(f"| {o} ({pts}) | " + " | ".join(cells) + " |")
    return "\n".join([head, sep, *rows])


def grid_text(g: Grid, units_quote: str | None) -> str:
    if not g.ok:
        return f"This grid could not be extracted reliably; see the PDF page {g.page}." if g.pages else \
            "This grid could not be located in the manual."
    out = [g.title, g.subtitle, ""]
    if units_quote:
        out.append(f"The manual (SGM Instructions Step IV): \"{units_quote}\"")
    out.append("Each cell is shown as \"minimum–maximum\": the left number of the grid cell (printed once for all "
               "offender statuses) and the right number printed on that offender status's row, exactly as "
               "printed (\"*\" and \"/L\" included). [shaded] = that part of the cell is shaded in the manual.")
    names = {"base": "Base (no habitual offender status)", "HO2": "HO2", "HO3": "HO3", "HO4": "HO4"}
    for s in GRID_STATUSES:
        out += ["", f"## {names[s]}", "", _md_table(g, s)]
    if g.notes:
        out += ["", "## Notes printed below the grid", ""] + [f"• {n}" for n in g.notes]
    return "\n".join(out)


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

    def apply_extras(self, conn, report) -> None:
        """Pipeline hook (inside the ingest transaction): replace the ``sentencing_offenses`` table
        wholesale with this manual's felony list, stamped with this source's currency string.

        If the felony list did not parse cleanly (any structural warning -- which also makes this source
        incomplete) the table is left exactly as it was: a partial list must never replace a whole one.
        Shading / amendments / numerical-vs-alphabetical disagreements are not failures; the rows are
        stored as printed and each disagreement is appended to ``report.warnings`` for human review."""
        m = self._ensure()
        report.warnings.extend(f"SGM grids, check against the PDF: {x}" for g in m.grids for x in g.review)
        fl = m.felony
        if fl is None or fl.structural:
            report.warnings.append(
                "sentencing_offenses NOT updated: the felony list did not parse cleanly; previous rows kept. "
                + "; ".join((fl.structural if fl else ["no felony list parsed"])[:20]))
            return
        conn.execute("DELETE FROM sentencing_offenses")
        conn.executemany(
            "INSERT INTO sentencing_offenses (mcl_citation, raw_cite, listed_in, category, offense_class,"
            " description, stat_max, amendments, current, page, edition) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(r.mcl_citation, r.raw_cite, r.listed_in, r.category, r.offense_class, r.description, r.stat_max,
              r.amendments, int(r.current), r.page, self.currency) for r in fl.rows],
        )
        report.warnings.extend(f"SGM felony list, check against the PDF: {x}" for x in fl.review)


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
