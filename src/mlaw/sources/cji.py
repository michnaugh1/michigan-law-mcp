"""Michigan Model Criminal Jury Instructions (M Crim JI), from the Court's published PDF.

* ``CjiFilesSource(pdf_path)`` parses a saved copy of the PDF (no network).

Text comes from ``pdftotext -layout`` (poppler), one form-feed-delimited string per PDF page. Layout:

* front matter (cover with "UPDATED <Month D, YYYY>" = currency, committee list) and a "Table of Contents" of
  ``CHAPTER n Title p-p`` and ``MCrim JI n.n Title p-p`` lines (titles and page refs wrap over lines);
* the body: a page-1 chapter title page per chapter (``CHAPTER 5 Witnesses`` + that chapter's own list of
  instructions with page refs), then each instruction starting on a new page with a left-margin heading
  (``M Crim JI 5.1 Title`` or ``MCrim JI 5.2 Title``), body text, then left-margin sections "Use Note(s)",
  "History", "Reference Guide"/"Reference", occasionally "Statute", "Staff Comment";
* every page carries a header ("Chapter 5: Witnesses") and a footer ("Michigan Model Criminal Jury Instructions
  Page 5-2"); both are removed;
* Chapter 44 is an index (``INDEX-n`` footers) mapping MCL sections to instruction numbers.

Heading rule (the body has many *cross-reference* lines that begin "M Crim JI ..."; the chapter title pages
and their continuation pages also list every instruction): a page is an instruction page only if its first
content line reads ``[M ]Crim JI <number> <title>`` where ``<number>`` is in the front-matter TOC and the title
(re-assembled over as many lines as it wraps) equals the TOC's title for that number, and the heading does not
end in a "n-n" page reference (that is a chapter-contents list, not a heading). A page that starts with such a
line but disagrees with the TOC produces a warning. Pages after a heading page continue that instruction until
the next chapter title page ends it.

``complete`` is True only if the set of instructions parsed equals the set in the TOC; otherwise False plus a
warning (so the pipeline's removal detection stays off).

Chapter 44's statute index is attached to each instruction as a final compilers_notes line
``Statutes (index): MCL 768.21a, MCL 768.36`` (the pipeline extracts cross-references from text, history and
compilers_notes, so this is what makes "MCL 768.21a cited by M Crim JI 7.11" work).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from ..citations import normalize_citation
from ..models import ParsedDoc, ParsedPublicAct, ParsedRange

PDF_URL = (
    "https://www.courts.michigan.gov/4a5756/siteassets/rules-instructions-administrative-orders/"
    "jury-instructions/criminal/current/criminal-jury-instructions.pdf"
)

_NUM = r"\d{1,2}\.\d{1,3}[a-z]?"
# "M Crim JI", "MCrim JI" -- and, in the source PDF's own typo (M Crim JI 16.13), bare "Crim JI"
_HEAD = re.compile(rf"^(?:M ?)?Crim JI ({_NUM})\s+(\S.*)$")
_PAGE_REF = re.compile(r"\s*\b\d{1,2}-\s*\d*\s*$")  # "1-2", "20- 99", "36-" (number continues on the next line)
_FOOTER = re.compile(r"^\s*Michigan Model Criminal Jury Instructions\s+(?:Page \d+-\d+|INDEX-\d+)\s*$")
_CHAPTER_HEADER = re.compile(r"^Chapter \d+: \S")
_INDEX_HEADER = "INDEX: Statutes to Criminal Jury Instructions"
_UPDATED = re.compile(r"UPDATED\s+([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})")
_MONTHS = {m: i for i, m in enumerate(
    "January February March April May June July August September October November December".split(), 1)}

_SECTION = re.compile(r"^(Use Notes?|History|Reference Guide|Reference|Statutes?|Staff Comments?)\s*$", re.I)
_SUBHEAD = re.compile(r"^\s{0,3}(Court Rules?|Case ?Law|Statutes?|Cases|Staff Comments?|Part [AB])\s*$", re.I)
# "(a) ", "(1) ", "(iv) " -- or a footnote marker alone on its line ("1")
_ITEM = re.compile(r"^\s*(?:\((?:[a-z]|[A-Z]|\d{1,2}|[ivx]{1,4})\)\s|\d{1,2}\s*$)")
_STATUS_DELETED = re.compile(r"\[\s*deleted\s*\]", re.I)
_STATUS_RENUMBERED = re.compile(
    rf"\[\s*(?:amended and )?renumbered(?:\s+to)?\s+M ?Crim JI\s+({_NUM})[^\]]*\]", re.I)
_STATUS_COMBINED = re.compile(rf"\[\s*combined with\s+M ?Crim JI\s+({_NUM})[^\]]*\]", re.I)
_STATUS_ANY = re.compile(r"\[[^\]]*(?:delet|repeal|renumber|combin|amend|supersed)[^\]]*\]", re.I)


@dataclass
class CjiResult:
    docs: list[ParsedDoc]
    currency_date: str | None
    warnings: list[str] = field(default_factory=list)
    toc_count: int = 0
    statute_index: dict[str, list[str]] = field(default_factory=dict)  # "7.11" -> ["MCL 768.21a", ...]


def pdf_to_text(path: str | Path) -> str:
    try:
        r = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, check=True, timeout=300,
        )
    except FileNotFoundError as e:
        raise RuntimeError("pdftotext (poppler-utils) is required to read the jury instructions PDF but was not found") from e
    return r.stdout


def _norm(s: str) -> str:
    return " ".join(s.split())


def _wrap_join(a: str, b: str) -> str:
    """Join a wrapped title: a line ending in a hyphen ('Gamma-' / 'butyrolactone') rejoins without a space."""
    return f"{a}{b.strip()}" if a.endswith("-") else _norm(f"{a} {b}")


def _key(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


# ----------------------------------------------------------------------------------------- table of contents
def _parse_toc(lines: list[str]) -> dict[str, str]:
    """number -> full title (including any bracketed status/qualifier note), from the front-matter TOC."""
    toc: dict[str, str] = {}
    i = 0
    while i < len(lines):
        m = _HEAD.match(lines[i].strip())
        if not m:
            i += 1
            continue
        num, title = m.group(1), m.group(2)
        while not _PAGE_REF.search(title) and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if not nxt or _HEAD.match(nxt) or nxt.startswith(("CHAPTER", "TOC-", "Model Criminal")):
                break
            i += 1
            title = _wrap_join(title, nxt)
        if re.search(r"\d-\s*$", title) and i + 1 < len(lines) and re.fullmatch(r"\s*\d+\s*", lines[i + 1]):
            i += 1  # page ref split "36-" / "14"
            title = f"{title} {lines[i].strip()}"
        title = _norm(_PAGE_REF.sub("", title))
        if num in toc:
            toc[num + "#dup"] = title  # surfaced by caller as a mismatch
        toc[num] = title
        i += 1
    return toc


# ----------------------------------------------------------------------------------------- page handling
def _clean_page(page: str) -> list[str]:
    """Page lines minus header/footer furniture, trailing whitespace, and outer blank lines."""
    lines = [ln.rstrip() for ln in page.split("\n")]
    first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first is not None and (_CHAPTER_HEADER.match(lines[first]) or lines[first].strip() == _INDEX_HEADER):
        lines[first] = ""
    lines = ["" if _FOOTER.match(ln) else ln for ln in lines]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _match_heading(lines: list[str], toc: dict[str, str]) -> tuple[str, str, int] | None:
    """(number, title, lines_consumed) if the page's first lines are a body heading that agrees with the TOC."""
    if not lines:
        return None
    m = _HEAD.match(lines[0].strip())
    if not m or m.group(1) not in toc:
        return None
    num, want = m.group(1), toc[m.group(1)]
    title = _norm(m.group(2))
    used = 1
    while _key(title) != _key(want) and _key(want).startswith(_key(title)) and used < len(lines):
        title = _wrap_join(title, lines[used])
        used += 1
    return (num, title, used) if _key(title) == _key(want) else None


def _split_status(title: str) -> tuple[str, str, str | None, str]:
    """(catchline, status, renumbered_target, status_note) from a heading title with optional status brackets."""
    status, target, note = "active", None, ""
    m = _STATUS_RENUMBERED.search(title)
    if m:
        status, target, note = "renumbered", m.group(1).lower(), _norm(m.group(0))
        title = title.replace(m.group(0), "")
    m = _STATUS_COMBINED.search(title)
    if m:
        status, target, note = "repealed", m.group(1).lower(), _norm(m.group(0))
        title = title.replace(m.group(0), "")
    m = _STATUS_DELETED.search(title)
    if m:
        status, note = ("repealed", note or _norm(m.group(0)))
        title = title.replace(m.group(0), "")
    return _norm(title), status, target, note


# ----------------------------------------------------------------------------------------- text shaping
def _logical(lines: list[str], keep_indent: bool) -> list[str]:
    """Join hard-wrapped lines into logical lines. Blank line = paragraph break (kept as ""); a line starting
    "(a) " / "(2) " starts a new logical line. ``keep_indent`` keeps the first physical line's indent."""
    out: list[str] = []
    cur: str | None = None
    for ln in lines:
        if not ln.strip():
            if cur is not None:
                out.append(cur)
                cur = None
            if out and out[-1] != "":
                out.append("")
            continue
        body = ln.strip()
        if cur is not None and not _ITEM.match(ln):
            cur = f"{cur}{body}" if cur.endswith("-") and not cur.endswith(" -") else f"{cur} {body}"
            continue
        if cur is not None:
            out.append(cur)
        indent = (len(ln) - len(ln.lstrip())) if keep_indent else 0
        cur = " " * indent + body
    if cur is not None:
        out.append(cur)
    while out and out[-1] == "":
        out.pop()
    return out


def _join_pages(pages: list[list[str]]) -> list[str]:
    """Concatenate cleaned page fragments; keep a paragraph break at a page break only when the previous
    page's last line looks like the end of a paragraph (short, terminal punctuation)."""
    out: list[str] = []
    for frag in pages:
        if out and frag:
            last = out[-1]
            if last.strip() and len(last.rstrip()) < 80 and re.search(r"[.?!:\"”)\]]$", last.rstrip()):
                out.append("")
        out.extend(frag)
    return out


def _section_text(name: str, lines: list[str]) -> str:
    """One section as 'Heading: text'. Reference-type sections are flattened onto one line with their
    sub-headings ('Court Rules: ... Case Law: ...'); other sections keep their paragraphs on separate lines."""
    flat = name.lower().startswith(("reference", "statute"))
    parts: list[str] = []
    buf: list[str] = []

    def flush():
        if buf:
            parts.extend(x for x in _logical(buf, keep_indent=False) if x)
            buf.clear()

    for ln in lines:
        m = _SUBHEAD.match(ln)
        if m:
            flush()
            parts.append(f"{_norm(m.group(1))}:")
        else:
            buf.append(ln)
    flush()
    if flat:
        body = " ".join(parts)
    else:
        body = "\n".join(parts)
    return f"{name}: {body}".rstrip()


def _canon_section_name(raw: str) -> str:
    r = _norm(raw)
    key = r.lower()
    return {
        "use note": "Use Note", "use notes": "Use Notes", "history": "History",
        "reference guide": "Reference Guide", "reference": "Reference",
        "statute": "Statute", "statutes": "Statutes",
        "staff comment": "Staff Comment", "staff comments": "Staff Comments",
    }[key]


def _split_sections(lines: list[str]) -> tuple[list[str], list[tuple[str, list[str]]]]:
    body: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for ln in lines:
        m = _SECTION.match(ln)
        if m:
            sections.append((_canon_section_name(m.group(1)), []))
        elif sections:
            sections[-1][1].append(ln)
        else:
            body.append(ln)
    return body, sections


# ----------------------------------------------------------------------------------------- Chapter 44 index
_IDX_ROW = re.compile(r"^(\s*(?:w/\s+)?\d.*?)\s{2,}(\S.*)$")
_IDX_INSTR = re.compile(rf"^({_NUM})(?:\(\w+\))*$")
_IDX_INSTR_W = re.compile(rf"^({_NUM})(?:\(\w+\))*\s+w/\s+\S.*$")  # "20.1 w/ 20.3 to 20.11"


def _parse_index(lines: list[str], known: set[str], warnings: list[str]) -> dict[str, list[str]]:
    """Chapter 44: 'MCL section(s)   instruction numbers' rows -> {instruction number: [MCL citations]}."""
    out: dict[str, list[str]] = {}
    for ln in lines:
        s = ln.rstrip()
        if not s.strip() or s.strip() == _INDEX_HEADER or _FOOTER.match(s):
            continue
        if re.match(r"^(?:[IVX]+\.|[A-Z]\.)", s.strip()) or s.strip().startswith(("CHAPTER", "Instructions", "Statutes ")):
            continue  # outline headings ("II.Defenses", "A.   Insanity"), chapter title, column header
        m = _IDX_ROW.match(s)
        if not m:
            warnings.append(f"index: unparsed line {s.strip()!r}")
            continue
        lhs = re.sub(r"\[[^\]]*\]", "", m.group(1))
        if re.search(r"\d\.\s+\d", lhs):
            warnings.append(f"index: stray space inside statute number, read as no space: {s.strip()!r}")
            lhs = re.sub(r"(\d\.)\s+(\d)", r"\1\2", lhs)  # "[28.729(1)]" = related penalty section; not attributed
        lhs = re.sub(r"^\s*w/\s+", "", lhs)
        sections: list[str] = []
        prefix = ""
        for tok in (t.strip() for t in lhs.split(",")):
            if not tok or tok.startswith("("):
                continue  # bare subsection ("(4)") of the previous section
            t = re.match(r"^(\d{1,4}[A-Z]?)\.(\d+[a-z]{0,2})(?=$|[(\s])", tok)
            u = re.match(r"^\.(\d+[a-z]{0,2})(?=$|[(\s])", tok)
            if t:
                prefix = t.group(1)
                sec = f"{t.group(1)}.{t.group(2)}"
            elif u and prefix:
                sec = f"{prefix}.{u.group(1)}"
            else:
                warnings.append(f"index: cannot read statute {tok!r} in line {s.strip()!r}")
                continue
            c = normalize_citation(f"MCL {sec}")
            if c is None or c.source != "mcl":
                warnings.append(f"index: {sec!r} is not a recognizable MCL section (line {s.strip()!r})")
                continue
            sections.append(c.canonical)
        rhs = m.group(2)
        if "[*" in rhs or "*]" in rhs:
            warnings.append(f"index: asterisk-bracketed instruction group (meaning not explained in the index); "
                            f"all listed instructions attached: {s.strip()!r}")
            rhs = rhs.replace("[*", ",").replace("*]", "")
        if re.search(r"[0-9][a-z]?\.\s+\d", rhs):
            warnings.append(f"index: period between instruction numbers read as a comma: {s.strip()!r}")
            rhs = re.sub(r"([0-9][a-z]?)\.\s+(\d)", r"\1, \2", rhs)
        for tok in (t.strip() for t in rhs.split(",")):
            if not tok:
                continue  # stray comma (", ,7.24")
            if _IDX_INSTR_W.match(tok):
                warnings.append(f"index: {tok!r} attached to its first instruction only (the 'w/' companions are "
                                f"not expanded): {s.strip()!r}")
                tok = tok.split()[0]
            im = _IDX_INSTR.match(tok)
            if not im:
                warnings.append(f"index: cannot read instruction {tok!r} in line {s.strip()!r}")
                continue
            num = im.group(1).lower()
            if num not in known:
                warnings.append(f"index: M Crim JI {num} is not an instruction in the PDF (line {s.strip()!r})")
                continue
            lst = out.setdefault(num, [])
            lst.extend(c for c in sections if c not in lst)
    return out


# ----------------------------------------------------------------------------------------- main parse
def parse_cji_text(text: str, url: str = PDF_URL) -> CjiResult:
    warnings: list[str] = []
    pages = text.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()

    m = _UPDATED.search("\n".join(pages[:3]))
    currency = None
    if m and m.group(1) in _MONTHS:
        currency = f"{m.group(3)}-{_MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"
    else:
        warnings.append("no 'UPDATED <Month D, YYYY>' currency stamp found on the cover")

    toc_start = next((i for i, p in enumerate(pages) if "Table of Contents" in p), None)
    body_start = next((i for i, p in enumerate(pages) if re.search(r"Page 1-1\s*$", p.rstrip())), None)
    if toc_start is None or body_start is None or body_start <= toc_start:
        return CjiResult([], currency, warnings + ["no Table of Contents / body found"])
    toc_lines = "\n".join(pages[toc_start:body_start]).split("\n")
    toc = _parse_toc(toc_lines)
    dups = [k[:-4] for k in toc if k.endswith("#dup")]
    toc = {k: v for k, v in toc.items() if not k.endswith("#dup")}
    warnings.extend(f"TOC lists M Crim JI {n} more than once" for n in dups)
    if not toc:
        return CjiResult([], currency, warnings + ["Table of Contents has no instruction entries"])

    # walk the body
    entries: list[dict] = []  # {num, title, page, lines: [fragments]}
    cur: dict | None = None
    index_start = None
    for pi in range(body_start, len(pages)):
        lines = _clean_page(pages[pi])
        if not lines:
            continue
        if re.match(r"^CHAPTER 44\b", lines[0]):
            index_start = pi
            break
        if re.match(r"^CHAPTER \d+\b", lines[0]):
            cur = None  # chapter title page: ends the previous instruction
            continue
        h = _match_heading(lines, toc)
        if h and not _PAGE_REF.search(lines[h[2] - 1]):
            num, title, used = h
            cur = {"num": num, "title": title, "page": pi + 1, "frags": [lines[used:]]}
            entries.append(cur)
        elif h:
            cur = None  # chapter-contents continuation page (heading + page ref)
        elif cur is not None:
            if _HEAD.match(lines[0].strip()) and _PAGE_REF.search(lines[0]):
                cur = None  # chapter-contents continuation page whose first entry is not in the TOC
                warnings.append(f"page {pi + 1}: contents-list line not in TOC: {lines[0].strip()[:80]!r}")
                continue
            if _HEAD.match(lines[0].strip()):
                warnings.append(f"page {pi + 1}: begins like a heading but disagrees with the TOC; "
                                f"treated as continuation of M Crim JI {cur['num']}: {lines[0].strip()[:80]!r}")
            cur["frags"].append(lines)
        elif _HEAD.match(lines[0].strip()) and not _PAGE_REF.search(lines[0]):
            warnings.append(f"page {pi + 1}: begins like a heading but disagrees with the TOC; skipped: "
                            f"{lines[0].strip()[:80]!r}")
    if index_start is None:
        warnings.append("Chapter 44 (statute index) not found")

    # index
    statute_index: dict[str, list[str]] = {}
    if index_start is not None:
        idx_lines: list[str] = []
        for p in pages[index_start:]:
            idx_lines.extend(ln.rstrip() for ln in p.split("\n"))
        statute_index = _parse_index(idx_lines, set(toc), warnings)

    docs: list[ParsedDoc] = []
    seen: set[str] = set()
    for e in entries:
        num = e["num"]
        if num in seen:
            warnings.append(f"duplicate heading for M Crim JI {num}; keeping the first")
            continue
        seen.add(num)
        catchline, status, target, status_note = _split_status(e["title"])
        if status == "active" and _STATUS_ANY.search(e["title"]):
            warnings.append(f"M Crim JI {num}: unrecognized status note in heading {e['title']!r}")
        lines = _join_pages(e["frags"])
        body_lines, sections = _split_sections(lines)
        body = "\n".join(_logical(body_lines, keep_indent=True)).strip()
        history = ""
        notes: list[str] = []
        if status_note:
            notes.append(f"Status: {status_note}")
        for name, sl in sections:
            if name == "History":
                history = _norm(" ".join(x for x in _logical(sl, keep_indent=False) if x))
            else:
                t = _section_text(name, sl)
                if t.split(":", 1)[1].strip():
                    notes.append(t)
        stat = statute_index.get(num)
        if stat:
            notes.append("Statutes (index): " + ", ".join(stat))
        if not body.strip() and status == "active":
            warnings.append(f"M Crim JI {num} has no instruction text")
        docs.append(ParsedDoc(
            source="cji", citation=f"M Crim JI {num}", catchline=catchline, text=body,
            history_note=history, compilers_notes="\n".join(notes), status=status, kind="instruction",
            chapter=str(int(num.split(".")[0])), url=f"{url}#page={e['page']}",
        ))
    if set(toc) != seen:
        warnings.append(
            f"TOC/body mismatch: in TOC only {sorted(set(toc) - seen)}; in body only {sorted(seen - set(toc))}")
    return CjiResult(docs, currency, warnings, toc_count=len(toc), statute_index=statute_index)


class CjiFilesSource:
    name = "cji-files"
    source_kinds = frozenset({"cji"})
    covered_chapters = None

    def __init__(self, pdf_path: str | Path):
        self.path = Path(pdf_path)
        self._result: CjiResult | None = None

    def _load(self) -> CjiResult:
        # lazy: run_ingest reads ``complete`` before iterating
        if self._result is None:
            self._result = parse_cji_text(pdf_to_text(self.path))
        return self._result

    @property
    def result(self) -> CjiResult:
        return self._load()

    @property
    def warnings(self) -> list[str]:
        return self._load().warnings

    @property
    def complete(self) -> bool:
        r = self._load()
        return bool(r.docs) and r.toc_count == len(r.docs) and not any(
            w.startswith("TOC/body mismatch") for w in r.warnings)

    @property
    def currency(self) -> str | None:
        d = self._load().currency_date
        if not d:
            return None
        y, mo, day = (int(x) for x in d.split("-"))
        month = next(k for k, v in _MONTHS.items() if v == mo)
        return f"M Crim JI updated {month} {day}, {y} ({d})"

    def iter_documents(self) -> Iterator[ParsedDoc]:
        yield from self._load().docs

    def iter_public_acts(self) -> Iterable[ParsedPublicAct]:
        return []

    def iter_ranges(self) -> Iterable[ParsedRange]:
        return []
