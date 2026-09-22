"""Citation normalization and cross-reference extraction.

Canonical forms
    MCL 750.83                     (source "mcl")
    MCR 6.110                      (source "mcr")
    MRE 404                        (source "mre")
    Const 1963, art 1, § 17        (source "const")
    1931 PA 328                    (cross-reference kind "public_act")

Subdivisions such as "(1)(a)" are dropped: documents are stored at section level.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import ParsedRange

_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}

MCL_TOKEN = r"\d{1,3}[A-Za-z]?\.\d{1,5}[A-Za-z]{0,3}"  # chapter may carry a letter: 767A.1, 771A.1
_MCL_TOKEN_RE = re.compile(MCL_TOKEN + r"(?:\([^)]*\))*")
_MCL_KEY_RE = re.compile(rf"^({MCL_TOKEN})")
# "&" appears in the Legislature's bill summaries ("MCL 3.1041 & 3.1042"); " - " / en dash means a range
# ("MCL 460.1 - 460.11"). A hyphen is only a separator when another MCL token follows (scan enforces that).
_SEP_RE = re.compile(r"\s*,\s*(?:and\s+|or\s+)?|\s*&\s*|\s*[-\u2013\u2014]\s*|\s+(?:and|or|to|through)\s+", re.I)
_MCL_START_RE = re.compile(r"\bMCL\s+", re.I)
# Statutes usually cite this way instead: "being section 400.11b of the Michigan Compiled Laws",
# "being sections 750.520b to 750.520e of the Michigan Compiled Laws".
_BEING_START_RE = re.compile(r"\bbeing\s+sections?\s+", re.I)
_BEING_TAIL_RE = re.compile(r"\s+of\s+the\s+Michigan\s+Compiled\s+Laws", re.I)
# A whole section can BE a narrative repeal notice for a *different* span of sections, e.g. MCL 333.13607:
# "Sections 13601 to 13606 of Act No. 368 ..., being sections 333.13601 to 333.13606 of the Michigan
# Compiled Laws, are repealed effective December 31, 1993." Distinct from the act/chapter-level stub line
# the compilers otherwise use ("37.1-37.9 Repealed. 1976, Act 453..."), which chapterdoc.py's _stub_ranges
# already handles from the LongTitle/preamble -- this is prose inside a live section's own body text.
_NARRATIVE_REPEAL_RE = re.compile(r"\s*,?\s*(?:is|are)\s+(?:hereby\s+)?repealed\b", re.I)
# "Former MCL 750.85, which pertained to ..., was repealed by Act 266 of 1974, Eff. Apr. 1, 1975." or
# "Former MCL 333.20701-333.20773 Expired. 1981, Act 79, ...;--Repealed, 1990, Act 179, ..." -- how
# Compiler's Notes announce that a citation (or range) once existed and no longer has live text. The
# descriptive clause in between ("which pertained to ...") can run long; 400 chars covers every real
# example found (longest: MCL 769.25, ~216 chars of description).
_FORMER_MCL_RE = re.compile(
    rf"\bFormer\s+MCL\s+(?P<a>{MCL_TOKEN})(?:\s*(?:-|–|—|to)\s*(?P<b>{MCL_TOKEN}))?"
    r"(?P<mid>.{0,400}?)\b(?P<status>repealed|expired)\b(?:\s+(?:by|pursuant\s+to)\s+(?P<by>[^.;]{0,80}))?",
    re.I | re.S,
)
_PA_RE = re.compile(r"\b(\d{4})\s+PA\s+(\d{1,4})\b")
# The other common way acts get cited in body/history text: "Act No. 267 of the Public Acts of 1976",
# "Act 380 of the Public Acts of 1965", or the bare "Act 116 of 1954" -- "No." and "the Public Acts of"
# are each independently optional. Confirmed against real Chapters 750/760/333/37/2/131 text: 262 clean
# hits, no false positives sampled.
_PA_RE2 = re.compile(r"\bAct\s+(?:No\.\s+)?(\d{1,4})\s+of\s+(?:the\s+Public\s+Acts\s+of\s+)?(\d{4})\b", re.I)
_MCR_RE = re.compile(r"\bMCR\s+(\d{1,2}\.\d{1,3}[A-Za-z]?)")
_MRE_RE = re.compile(r"\bMRE\s+(\d{3,4})")
_CONST_RE = re.compile(
    r"(?:\bConst\.?\s*1963|\b1963\s*Const\.?),?\s*"
    r"(?:art(?:icle)?\.?\s*(?P<art>[IVXivx]+|\d+)|(?P<sched>Schedule))\s*,?\s*§\s*(?P<sec>\d+[A-Za-z]?)",
    re.I,
)
# The Constitution's OWN MCLNumber field, e.g. "Article I § 3" or "Schedule § 1" -- confirmed against the
# real Chapter 1.xml. This is a different shape from _CONST_RE above (which recognizes "Const. 1963, Art.
# I, § 3"-style prose *references* to the Constitution inside other chapters' text); both normalize to the
# same canonical key. 280 of Chapter 1.xml's 281 sections match this; the one exception is a ceremonial
# signature/vote-record block (MCLNumber "Schedule SigBlock") that isn't a real numbered section and is
# left unrecognized here on purpose.
_CONST_MCLNUMBER_RE = re.compile(
    r"^(?:Article\s+(?P<art>[IVXLCM]+)|(?P<sched>Schedule))\s+§\s*(?P<sec>\d+[A-Za-z]?)$", re.I,
)
# A handful of non-section entries don't carry a "§ N" of their own in MCLNumber -- confirmed exactly one
# in the real Chapter 1.xml, the Schedule's ceremonial signature/vote-record block, MCLNumber "Schedule
# SigBlock" -- but SectRef still reliably has one ("§ 0"), so normalize_const_mclnumber falls back to that
# rather than dropping the document.
_CONST_LEADING_RE = re.compile(r"^(?:Article\s+(?P<art>[IVXLCM]+)|(?P<sched>Schedule))\b", re.I)
_CONST_SECTREF_RE = re.compile(r"^§\s*(\d+[A-Za-z]?)$")


@dataclass(frozen=True)
class Citation:
    source: str
    canonical: str


@dataclass(frozen=True)
class XRef:
    kind: str  # mcl | mcr | mre | const | public_act
    key: str  # canonical citation
    raw_text: str
    in_range: bool = False


def _roman_to_int(s: str) -> int:
    total, prev = 0, 0
    for ch in reversed(s.lower()):
        v = _ROMAN[ch]
        total += v if v >= prev else -v
        prev = max(prev, v)
    return total


def _mcl_key(token: str) -> str:
    m = re.match(r"^(\d{1,3})([A-Za-z]?)\.(\d+)([A-Za-z]*)", token)
    assert m, token
    return f"MCL {m.group(1)}{m.group(2).upper()}.{m.group(3)}{m.group(4).lower()}"


def _has_real_chapter(token: str) -> bool:
    """MCL_TOKEN's shape also matches plain decimals like "0.02" (a blood-alcohol figure, not a
    citation) -- chapter "0" doesn't exist in the MCL, so reject it. Confirmed against the real
    Chapter 750/760/333/37/2/131 corpus: this is the only chapter value among citation-list
    continuations that isn't a genuine cross-chapter range (e.g. "MCL 760.1 to 777.69" is legitimate
    and must still be accepted -- do not tighten this to "same chapter as the first token")."""
    m = re.match(r"^(\d{1,3})", token)
    return bool(m) and int(m.group(1)) != 0


def _const_key(art: str, sec: str) -> str:
    # "Schedule" (the Constitution's own trailing "SCHEDULE AND TEMPORARY PROVISIONS" article, confirmed
    # in the real Chapter 1.xml as DivisionNumber "Schedule" rather than a roman numeral) isn't a numbered
    # article and cites as "Const 1963, Schedule, § N", not "Const 1963, art Schedule, § N".
    if art.strip().lower() == "schedule":
        return f"Const 1963, Schedule, § {sec.lower()}"
    n = int(art) if art.isdigit() else _roman_to_int(art)
    return f"Const 1963, art {n}, § {sec.lower()}"


def normalize_const_mclnumber(number: str, sect_ref: str | None = None) -> Citation | None:
    """Recognize the Constitution's own MCLNumber field ("Article I § 3", "Schedule § 1") and normalize it
    to the same canonical key _CONST_RE produces for a prose reference. See mclxml.py's _section_content,
    which tries this before falling back to the ordinary MCL-dotted-number shape.

    ``sect_ref`` is an optional fallback for the one confirmed real-file case where MCLNumber itself has no
    "§ N" (the Schedule's signature/vote-record block, "Schedule SigBlock") but SectRef still does ("§ 0")
    -- rather than silently dropping a real document from the corpus."""
    m = _CONST_MCLNUMBER_RE.match(number.strip())
    if m:
        return Citation("const", _const_key(m.group("art") or m.group("sched"), m.group("sec")))
    if sect_ref:
        lead = _CONST_LEADING_RE.match(number.strip())
        ref = _CONST_SECTREF_RE.match(sect_ref.strip())
        if lead and ref:
            return Citation("const", _const_key(lead.group("art") or lead.group("sched"), ref.group(1)))
    return None


def normalize_citation(text: str) -> Citation | None:
    """Turn user input like 'mcl 750.520B(1)(a)' into a canonical Citation, or None."""
    s = " ".join(text.strip().split())
    if not s:
        return None

    m = re.fullmatch(rf"(?:MCL\s*)?({MCL_TOKEN})(?:\(.*\))?(?:\s*et seq\.?)?", s, re.I)
    if m and _has_real_chapter(m.group(1)):
        return Citation("mcl", _mcl_key(m.group(1)))

    m = re.fullmatch(r"MCR\s*(\d{1,2}\.\d{1,3}[A-Za-z]?)(?:\(.*\))?", s, re.I)
    if m:
        return Citation("mcr", f"MCR {m.group(1)}")

    m = re.fullmatch(r"MRE\s*(\d{3,4})(?:\(.*\))?", s, re.I)
    if m:
        return Citation("mre", f"MRE {m.group(1)}")

    m = _CONST_RE.fullmatch(s)
    if m:
        return Citation("const", _const_key(m.group("art") or m.group("sched"), m.group("sec")))

    return None


_CONST_SORT_RE = re.compile(r"^Const\s+1963,\s*(?:art\s+(?P<art>\d+)|(?P<sched>Schedule)),\s*§\s*(?P<sec>\d+)(?P<suf>[A-Za-z]?)$", re.I)


def citation_sort_key(citation: str) -> tuple:
    """Natural ordering: MCL 750.9 < 750.10 < 750.10a < 750.83 < 767.96 < 767A.1 < 768.1. Const 1963, art
    1, § 1 < ... < art 12, § N < Schedule, § N (Schedule sorts after every numbered article, matching how
    the Legislature's own site orders Chapter 1 -- confirmed against the real file: Schedule is the last
    division). A dedicated branch, not the generic digit-extraction fallback below: that fallback produces
    a variable-length tuple (a Schedule citation has one fewer digit group than a numbered-article one,
    since "Schedule" itself isn't numeric) and mixing tuple shapes/types in one sort raised a real
    TypeError when Chapter 1 was first ingested end to end, 2026-09-22."""
    m = re.search(r"(\d+)([A-Za-z]?)\.(\d+)([a-z]*)", citation)
    if m:
        return (int(m.group(1)), m.group(2).upper(), int(m.group(3)), m.group(4))
    cm = _CONST_SORT_RE.match(citation)
    if cm:
        art = int(cm.group("art")) if cm.group("art") else 9999
        return (art, "", int(cm.group("sec")), cm.group("suf").lower())
    nums = [int(n) for n in re.findall(r"\d+", citation)]
    return (*nums, "", 0, "")


def _scan_mcl_chain(text: str, pos: int) -> tuple[list[tuple[str, str | None]], int]:
    """From ``pos``, greedily consume a citation-shaped token, then further tokens across list
    separators (comma, &, hyphen/en-dash/em-dash, "and"/"or"/"to"/"through") without requiring "MCL" to
    repeat -- this is what lets "MCL 750.83, 750.84, and 750.85" work. Shared by ``extract_xrefs`` (after
    "MCL " / "being section(s)...") and ``extract_narrative_ranges`` (after "being section(s)...")."""
    toks: list[tuple[str, str | None]] = []  # (raw token, separator before it)
    prev_sep: str | None = None
    m = _MCL_TOKEN_RE.match(text, pos)
    if m and not _has_real_chapter(m.group(0)):
        m = None
    end = pos
    while m:
        toks.append((m.group(0), prev_sep))
        end = m.end()
        sm = _SEP_RE.match(text, m.end())
        if not sm:
            break
        m2 = _MCL_TOKEN_RE.match(text, sm.end())
        if not m2 or not _has_real_chapter(m2.group(0)):
            break
        prev_sep = sm.group(0).strip().lower()
        if prev_sep in ("-", "\u2013", "\u2014"):
            prev_sep = "to"
        m = m2
    return toks, end


def extract_xrefs(text: str, self_citation: str | None = None) -> list[XRef]:
    """Find outgoing citations in ``text``. Duplicates (same key + raw) are collapsed."""
    found: list[XRef] = []

    def emit(toks) -> None:
        for i, (raw, sep) in enumerate(toks):
            nxt = toks[i + 1][1] if i + 1 < len(toks) else None
            in_range = sep in ("to", "through") or nxt in ("to", "through")
            found.append(XRef("mcl", _mcl_key(raw), raw, in_range))

    for start in _MCL_START_RE.finditer(text):
        toks, _ = _scan_mcl_chain(text, start.end())
        emit(toks)
    for start in _BEING_START_RE.finditer(text):
        toks, end = _scan_mcl_chain(text, start.end())
        if toks and _BEING_TAIL_RE.match(text, end):
            emit(toks)

    for m in _PA_RE.finditer(text):
        found.append(XRef("public_act", f"{m.group(1)} PA {int(m.group(2))}", m.group(0)))
    for m in _PA_RE2.finditer(text):
        found.append(XRef("public_act", f"{m.group(2)} PA {int(m.group(1))}", m.group(0)))
    for m in _MCR_RE.finditer(text):
        found.append(XRef("mcr", f"MCR {m.group(1)}", m.group(0)))
    for m in _MRE_RE.finditer(text):
        found.append(XRef("mre", f"MRE {m.group(1)}", m.group(0)))
    for m in _CONST_RE.finditer(text):
        found.append(XRef("const", _const_key(m.group("art") or m.group("sched"), m.group("sec")), m.group(0)))

    seen: set[tuple[str, str, str]] = set()
    out: list[XRef] = []
    for x in found:
        if self_citation and x.key == self_citation:
            continue
        k = (x.kind, x.key, x.raw_text)
        if k in seen:
            continue
        seen.add(k)
        out.append(x)
    return out


def _chapter_of(mcl_citation: str) -> str:
    return mcl_citation.split()[1].split(".")[0].upper()


def extract_narrative_ranges(
    text: str, compilers_notes: str = "", *, act_citation: str | None = None, act_name: str | None = None,
) -> list[ParsedRange]:
    """Find spans of MCL sections that a section's own prose (``text``) or a Compiler's Note
    (``compilers_notes``) declares no longer have live text, beyond the act/chapter-level stub line
    chapterdoc.py's ``_stub_ranges`` already catches from a statute's LongTitle/preamble. ``act_citation``/
    ``act_name`` are the enclosing statute's, same as the caller already passes to ``_to_doc`` -- attached
    here only for display; each stub's ``chapter`` is always derived from its own citation, since a
    "Former MCL" range is often a leftover from a *different*, no-longer-existing act. Confirmed against
    the real Chapter 750/760-777/333/37/2/131 corpus: 2 in-body narrative repeals, 37 Compiler's Notes
    "Former MCL" mentions (35 single citations, 2 ranges), no false positives found."""
    out: list[ParsedRange] = []

    for start in _BEING_START_RE.finditer(text):
        toks, end = _scan_mcl_chain(text, start.end())
        if not toks:
            continue
        tail = _BEING_TAIL_RE.match(text, end)
        if not tail:
            continue
        rm = _NARRATIVE_REPEAL_RE.match(text, tail.end())
        if not rm:
            continue
        note = " ".join(text[start.start():rm.end()].split())
        a = _mcl_key(toks[0][0])
        out.append(ParsedRange(
            source="mcl", start_citation=a, end_citation=_mcl_key(toks[-1][0]), note=note, status="repealed",
            chapter=_chapter_of(a), act_citation=act_citation, act_name=act_name,
        ))

    for m in _FORMER_MCL_RE.finditer(compilers_notes):
        a = _mcl_key(m.group("a"))
        b = _mcl_key(m.group("b")) if m.group("b") else a
        note = " ".join(compilers_notes[m.start():m.end()].split())
        status = "repealed" if m.group("status").lower() == "repealed" else "expired"
        out.append(ParsedRange(
            source="mcl", start_citation=a, end_citation=b, note=note, status=status,
            chapter=_chapter_of(a), act_citation=act_citation, act_name=act_name,
        ))

    return out
