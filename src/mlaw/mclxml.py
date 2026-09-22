"""Parser for the Legislature's whole-chapter machine-readable XML
(``https://legislature.mi.gov/documents/mcl/Chapter N.xml``), confirmed by the Legislature's own reply
(``docs/lsb-reply-2026-09-21.md``) as the canonical machine-readable source -- one chapter per file, UTF-16
encoded. Produces the same :class:`~mlaw.chapterdoc.ChapterDocument` shape as the whole-chapter HTML parser
so the rest of the pipeline (a :class:`~mlaw.models.Source`, the ingest pipeline, FTS/xref maintenance)
does not need to know which source produced a document, and so the two can be cross-checked against each
other directly.

Structure (real files: Chapter 37.xml, Chapter 2.xml, Chapter 131.xml, Chapter 760.xml, Chapter 333.xml,
Chapter 750.xml, Chapter 1.xml)::

    MCLChapterInfo                      root
      Name                              "37"
      Title                             "CIVIL RIGHTS"
      Repealed / MultiChapter           MultiChapter=true means the file's <Name> ("760") is only the
                                         chapter the Legislature filed it under -- the file's own sections
                                         can span many chapters (confirmed: Chapter 760.xml, "CODE OF
                                         CRIMINAL PROCEDURE", is MultiChapter=true and actually holds
                                         sections for chapters 760-777 plus 767A and 771A, 672 sections in
                                         one file). Each section's citation is authoritative for its own
                                         chapter regardless of this flag (see _to_doc in chapterdoc.py,
                                         which derives every ParsedDoc's chapter from its own citation, not
                                         from the file-level chapter); the flag only controls whether
                                         parse_chapter_xml's per-section "appears inside chapter X" sanity
                                         check applies (it would otherwise fire on every legitimately
                                         out-of-chapter section in a file like this one).
      MCLDocumentInfoCollection
        MCLStatuteInfo *                one per act / E.R.O. / resolution -- ALSO one per pending-law
                                         variant, see below
          Name                          "Act 453 of 1976" | "E.R.O. No. 1997-1" | "J.R. 10 of 1897"
          Heading, LongTitle, ShortTitle, StyleClause, Repealed
          Commentary -> CommentaryInfo* (Type, Text, EffectiveDate) -- act-level site notice
          MCLDocumentInfoCollection
            MCLSectionInfo *            a leaf section, or
            MCLDivisionInfo *           an article/division grouping (Name "37.2101.D10", DivisionNumber,
                                         DivisionType) whose own MCLDocumentInfoCollection holds more
                                         MCLSectionInfo (no example of a division nesting a further
                                         division was found in the three chapters sampled; handled
                                         recursively below in case one exists elsewhere)

    MCLSectionInfo
      MCLNumber / SectRef               "37.2102" | "2.13.new"  -- the variant suffix lives on the number
                                         itself, not on a separate flag
      Label                             bare "Sec." number, e.g. "102" (redundant with BodyText's own
                                         Section-Number line; not used here)
      CatchLine, Repealed
      HistoryText                       one or more <HistoryData> elements, each one compiler-history
                                         entry without the ";--" separator HTML uses between them
      History -> HistoryInfo*           EffectiveDate, Action, IsImmediateEffect,
                                         Legislation{Type,Number,Year,SessionType,...} -- structured
                                         history, not yet consumed here (deferred with the rest of
                                         historical/as_of backfill; current text comes from HistoryText
                                         via the same history.py grammar the HTML source uses)
      EditorsNotes -> EditorsNoteInfo*  (Type, Text) -- Text holds the same embedded pseudo-XML as BodyText
      Commentary -> CommentaryInfo*     section-level site notice for a variant section (mirrors the
                                         HTML source's ``.commentary`` banner div byte-for-byte, including
                                         the "***** ... IS EFFECTIVE ... *****" wording)
      BodyText                          embedded pseudo-XML, sometimes wrapped in <Section-Body>, sometimes
                                         a bare <Section-Number>/<Paragraph> sequence with no wrapper (seen
                                         in the same file): Section-Number, then Paragraph > P (> Emph for
                                         italics), or Paragraph > Paragraph-Number + P (no space of its own
                                         between the two -- one is inserted here to match the HTML source's
                                         own rendering). A <P> can itself hold a literal HTML <table> instead
                                         of running text (seen in Chapter 2.xml, MCL 2.201/2.202, an
                                         interstate boundary compact); rendered the same way the HTML
                                         source's own tables are.

Variant sections (a law passed but not yet in effect -- see the Legislature's own written explanation in
``docs/lsb-reply-2026-09-21.md``) are represented as an entirely separate ``MCLStatuteInfo`` -- the
amending/enacting act itself (e.g. "Act 7 of 2026") -- sitting alongside the real acts in the chapter's
top-level collection, holding one ``MCLSectionInfo`` whose ``MCLNumber``/``SectRef`` carries the
``.new``/``.amended``/``.added`` suffix (confirmed for ``.new`` against Chapter 2.xml's "Section 2.13.new";
``.amended``/``.added`` are not yet seen in XML and are handled by the same generic suffix regex used
elsewhere, untested against a real file).

The bracket suffix (``[1]``, ``[a]``, ...) is a different thing entirely, despite sharing the ``variant``
field/mechanism: not a not-yet-effective amendment, but the Compiler's own disambiguator for two wholly
separate, both-ordinarily-effective sections that would otherwise collide on the same MCL number -- e.g.
two different acts each enacting their own "Section 3" and having it compiled to the same base citation.
Confirmed live 2026-09-22 against the real Chapter 752.xml: MCL 752.863 (Act 45 of 1952, "Section
repealed.") and MCL 752.863[a] (Act 14 of 1955, an entirely different, ordinary, currently-effective
section), each its own normal ``MCLSectionInfo`` inside its own act's own ordinary ``MCLStatuteInfo`` --
neither is a "variant section" in the not-yet-effective sense above. The disambiguator itself is confirmed
letter-valued here (the section's own Compiler's Note explains it was "compiled as MCL 752.863[a] to
distinguish it from another section 3"), alongside the already-known digit-valued form ("333.5474c[1]",
feed.py) and one seen only in Compiler-note prose, not a real MCLNumber, for the Constitution ("compiled
as § 36[1] to distinguish it from another section 36", Chapter 1.xml) -- so the suffix regex accepts any
letters-or-digits inside the brackets, not just digits.

This is the same shape the HTML source uses (a variant section is its own ``sectionWrapper`` under its own
``statuteWrapper``), so both sources produce ``ParsedDoc.variant`` identically and neither needs
source-specific variant logic in the pipeline.

Chapter 1.xml is the Michigan Constitution (confirmed by Mike, 2026-09-22), delivered through this exact
same mechanism -- same file shape, one ``MCLStatuteInfo`` ("CONSTITUTION OF MICHIGAN OF 1963"), 13
``MCLDivisionInfo`` (Articles I-XII, plus a trailing "Schedule" division -- ``DivisionNumber`` literally
"Schedule", not a roman numeral), 281 ``MCLSectionInfo``, no nested divisions. Several things about it are
genuinely different from an ordinary chapter, all confirmed against the real file:

- ``MCLNumber`` is not a dotted MCL number at all -- it's "Article I § 3" or "Schedule § 1". This is the
  section's real citation (normalized by :func:`~mlaw.citations.normalize_const_mclnumber` to "Const 1963,
  art 1, § 3" / "Const 1963, Schedule, § 1"), NOT the "Const. 1963, Art. I, § 3" form that lives in
  ``HistoryText`` -- that form is what a *reference to* the Constitution from other chapters' text looks
  like (:data:`~mlaw.citations._CONST_RE`), not this section's own stored identity.
- ``SectRef`` is always just the bare "§ N" form (never a comma-list of extra citations the way a handful
  of old, fully-repealed ordinary MCL entries use it) -- except for one real entry, a ceremonial
  signature/vote-record block ("Schedule SigBlock" in MCLNumber) whose ``SectRef`` ("§ 0") is the ONLY
  place its real "section number" appears; ``normalize_const_mclnumber`` falls back to it for exactly this
  case so the document isn't silently dropped.
- BodyText's own internal ``Sec. N.`` numbering restarts at 1 in every Article -- it is never the real
  citation (``MCLNumber``/``SectRef`` always is), so this doesn't need special handling, but it means
  BodyText's "Sec. N." is NOT unique across the whole chapter the way it effectively always is for an
  ordinary single-statute MCL chapter.
- A defunct provision isn't signaled by a CatchLine prefix ("Repealed."/"Expired.") the way ordinary MCL
  sections are -- the catchline stays a plain description, BodyText is empty, and the status word
  ("Abrogated.") lives in HistoryText instead. Confirmed against the only two real examples (Article IV
  §§ 4-5, both removed by the 2018 initiated law): ``_section_content`` falls back to checking
  Repealed+empty-body when the catchline itself gives no status word, and stores ``status="abrogated"`` --
  its own status, not folded into "repealed" (a statutory term) or "expired".
- New ``EditorsNoteInfo`` ``Type`` values not seen in any ordinary chapter: ``FormerConst`` (mapped to
  "Former Constitution", confirmed to match the site's own wording from Mike's real-page sample) plus
  ``CON``, ``Constitutionality``, ``EffectiveDate``, ``HJR``, ``IL``, ``SJR``, ``TransferOfPowers`` (left
  unmapped -- no HTML rendering of Chapter 1 is on hand yet to confirm the site's own wording for these, so
  they fall through to the generic "use the raw Type" label rather than risk a guessed one).
- No per-section URL pattern is confirmed yet (unlike ordinary sections, whose ``objectName`` slug comes
  straight from the dotted citation) -- every const document's ``url`` points at the whole-chapter
  rendering (``Laws/MCL?objectName=MCL-CHAP1``) instead of guessing a slug that might be wrong.
- ``history.py``'s ``parse_history`` grammar ("1927, Act 175, Eff. Sept. 5, 1927") doesn't recognize the
  Constitution's own amendment grammar ("Am. S.J.R. G, approved Nov. 3, 2020, Eff. Dec. 19, 2020", "Am.
  Initiated Law, approved ...", "Abrogated. Initiated Law, approved ..."), so ``effective_date`` stays
  unset for const documents even when the raw ``history_note`` has a real date in it. Not blocking for
  current-text ingestion (the project's own current priority), but will need its own grammar before any
  Constitution-specific historical/``as_of`` backfill.

``StatuteBlock.kind`` is ``"const"`` for this one statute (see ``_act_info``'s "CONSTITUTION OF MICHIGAN OF
1963" branch in chapterdoc.py) and ``ParsedDoc.source`` is ``"const"`` for every one of its documents (see
``SectionContent.source`` in mclsite.py) -- both threaded through from
:func:`~mlaw.citations.normalize_const_mclnumber`'s ``Citation.source``, not hardcoded, so an ordinary MCL
section parsed in the very same file walk stays ``"mcl"`` unaffected.

Not covered, because no sample has shown one: a division nesting a further division (handled recursively
in case it exists elsewhere, but untested); anything inside BodyText/EditorsNoteInfo text other than
Section-Number, Paragraph, Paragraph-Number, P, Emph and table (falls through to the generic "unhandled
element" path below, which keeps the text and flags a warning rather than silently dropping or misparsing
it). The structured ``History``/``HistoryInfo``/``Legislation`` data is left unused for now -- current text
first, per the project's own decision to defer archive-based historical/``as_of`` backfill.

Unlike the HTML chapter rendering, these XML files carry no "Complete Through PA ## of ####" currency
stamp anywhere in the document -- :func:`parse_chapter_xml` always returns ``currency=None`` and
``through_act=None``. Freshness for this source has to come from elsewhere (the directory-listing manifest
in :mod:`mlaw.manifest`, or a cross-check against the HTML source), not from the file's own content.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup, NavigableString

from .chapterdoc import ChapterDocument, SectionRecord, StatuteBlock, _act_info, _stub_ranges, _to_doc
from .citations import MCL_TOKEN, extract_narrative_ranges, normalize_citation, normalize_const_mclnumber
from .history import parse_history
from .mclsite import SectionContent, _clean, _iso_from_banner, _table_text
from .models import ParsedRange

# The bracket alternative was originally digit-only ("[1]"); broadened to letters too after MCL 752.863[a]
# (confirmed live 2026-09-22, real Chapter 752.xml -- see the module docstring's "bracket suffix" section).
_NUM_VARIANT = re.compile(r"^(?P<cite>\d{1,3}[A-Za-z]?\.\d{1,5}[A-Za-z]{0,3})(?P<variant>\.[a-z]+|\[[A-Za-z0-9]+\])?$")
_NOTE_LABELS = {
    "compiler": "Compiler's Notes", "adminrule": "Admin Rule", "popularname": "Popular Name",
    # Confirmed against the real Chapter 1.xml (Const 1963): matches the wording in the site's own
    # rendering, e.g. "Former Constitution: See Const. 1908, Art. II, § 2." Other Constitution-only note
    # types seen in the real file (CON, Constitutionality, EffectiveDate, HJR, IL, SJR, TransferOfPowers)
    # are NOT mapped here yet -- no HTML rendering of Chapter 1 is on hand to confirm the site's own
    # wording for those, so they fall through to the generic "use the raw Type" path below rather than
    # risk a guessed label that's wrong.
    "formerconst": "Former Constitution",
}


def _strip_namespaces(root: ET.Element) -> None:
    """Drop any ``{uri}`` prefix ElementTree attached to every tag, in place.

    Most Chapter N.xml files have no XML namespace at all -- plain ``<Name>115</Name>``. Some (confirmed
    live 2026-09-22: Chapter 115.xml, "FOURTH CLASS CITIES") declare a default namespace on the root,
    ``xmlns="http://localhost/MCLWebService/MCLSearchService"`` -- an artifact of the file being served
    through the underlying MCLWebService rather than as a static export, evidently inconsistent chapter to
    chapter. XML namespace rules mean that default namespace applies to every unprefixed descendant element
    whether or not the file repeats the ``xmlns=`` attribute on each one, so ElementTree resolves every tag
    in such a file to ``{http://localhost/...}Name`` etc. -- every bare ``el.find("Name")``-style lookup
    throughout this module then silently finds nothing, which is what actually produced "chapter XML file
    has no <Name>" for a file that, read raw, plainly has one. Stripped once here, immediately after
    parsing, so every existing bare-tag lookup keeps working unchanged regardless of which shape the
    Legislature's server happened to hand back for a given chapter."""
    for el in root.iter():
        if isinstance(el.tag, str) and el.tag.startswith("{"):
            el.tag = el.tag.split("}", 1)[1]


def _text(el, tag: str) -> str:
    child = el.find(tag)
    return child.text or "" if child is not None else ""


def _bool(el, tag: str) -> bool:
    return _text(el, tag).strip().lower() == "true"


def _mixed_text(node) -> list[str]:
    """Walk ``node``'s own contents in document order, splitting out any ``<table>`` (rendered via
    ``_table_text``) from the running text immediately around it -- however deep the table is nested and
    however much text comes before or after it inside the same node. Needed because a ``<P>`` can hold a
    ``<table>`` followed, still inside the same ``<P>``, by ordinary text introducing the next item -- MCL
    600.9901's repeal table (confirmed live 2026-09-22, Chapter 600.xml): "...</table>(2) Public Acts,\\n
    </P>". A naive ``child.find("table")`` (the previous approach) finds that table and then discards
    everything else in the ``<P>``, silently dropping "(2) Public Acts," -- this walks contents instead so
    nothing next to a table is lost.
    """
    parts: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        t = _clean("".join(buf))
        buf.clear()
        if t:
            parts.append(t)

    for child in node.contents:
        if isinstance(child, NavigableString):
            buf.append(str(child))
        elif child.name == "table":
            flush()
            parts.append(_table_text(child))
        elif child.find("table") is not None:
            flush()
            parts.extend(_mixed_text(child))
        else:
            buf.append(child.get_text())
    flush()
    return parts


def _paragraph_text(para) -> str:
    """A ``<Paragraph>`` is either a bare ``<P>`` (subsection marker, if any, already inside the text --
    "(1) The opportunity...") or a ``<Paragraph-Number>`` sibling ("(a)", or "(<Emph>i</Emph>)" for a
    roman-numeral marker) followed by one or more ``<P>`` with no space of its own before the text -- the
    site's own HTML rendering puts visible whitespace there, so a space is inserted here to match. A
    ``<P>`` can itself hold a full ``<table>`` (seen in Chapter 2.xml, MCL 2.201/2.202, an interstate
    boundary compact) instead of running text, sometimes with ordinary text immediately before or after the
    table inside that same ``<P>`` (MCL 600.9901's repeal table, Chapter 600.xml -- see ``_mixed_text``); a
    ``<table>`` can also appear as a direct child of ``<Paragraph>`` with no ``<P>`` wrapper at all (seen in
    Chapter 333.xml, MCL 333.26252/26329/26330 -- E.R.O.-derived sections with embedded definition tables).
    Both render the same as the HTML source's own tables; direct children are walked in document order so a
    table interleaved with running-text ``<P>`` siblings still comes out in the right place.
    """
    num = para.find("Paragraph-Number", recursive=False)
    num_text = _clean(num.get_text()) if num is not None else ""
    parts: list[str] = []
    for child in para.find_all(["P", "table"], recursive=False):
        if child.name == "table":
            parts.append(_table_text(child))
            continue
        if child.find("table") is not None:
            parts.extend(_mixed_text(child))
            continue
        t = _clean(child.get_text())
        if t:
            parts.append(t)
    if num_text:
        parts = [f"{num_text} {parts[0]}"] + parts[1:] if parts else [num_text]
    return "\n".join(parts)


def _parse_embedded(raw: str | None) -> tuple[str, list[str]]:
    """Parse a BodyText / EditorsNoteInfo.Text embedded pseudo-XML fragment into plain-text lines.

    Handles every form seen in real files: wrapped in a single ``<Section-Body>`` root; a bare sequence
    of ``<Section-Number>``/``<Paragraph>`` elements with no common root (not valid XML on its own, so it
    is always wrapped in a private root before parsing); and, for many ``EditorsNoteInfo`` notes (a
    ``PopularName`` such as "Act 368" or "Lisa's Law" -- 1,883 of them in Chapter 333.xml alone), plain
    text with no markup at all, which is a bare string child rather than a child element and so needs its
    own branch below (``container.contents``, not ``find_all``, so it is seen). An element this function
    does not recognize is kept as text -- never dropped -- and flagged with a warning.
    """
    warnings: list[str] = []
    if not raw or not raw.strip():
        return "", warnings
    try:
        soup = BeautifulSoup(f"<_root_>{raw}</_root_>", "xml")
    except Exception as e:  # pragma: no cover - defensive; no malformed sample seen yet
        warnings.append(f"embedded markup did not parse as XML: {e}")
        return _clean(re.sub(r"<[^>]+>", " ", raw)), warnings
    top = soup.find("_root_")
    container = top.find("Section-Body", recursive=False) or top

    lines: list[str] = []
    for child in container.contents:
        if isinstance(child, NavigableString):
            t = _clean(str(child))
            if t:
                lines.append(t)
            continue
        name = child.name
        if name == "Section-Number":
            t = _clean(child.get_text())
            if t:
                lines.append(t)
        elif name == "Paragraph":
            t = _paragraph_text(child)
            if t:
                lines.append(t)
        else:
            t = _clean(child.get_text())
            if t:
                warnings.append(f"unhandled <{name}> element in embedded markup kept as text")
                lines.append(t)

    strip = lambda t: re.sub(r"[\s|]+", "", t)  # noqa: E731
    if strip(container.get_text()) != strip("".join(lines)):
        warnings.append("INTEGRITY: extracted body differs from the text inside the embedded markup")
    return "\n".join(lines), warnings


def _history_note(history_text: str | None) -> str:
    """Reassemble ``<HistoryData>`` fragments into the ``";--"``-joined string ``history.py`` parses
    (the same grammar the HTML source's ``History:`` line uses), so both sources agree byte-for-byte."""
    if not history_text:
        return ""
    parts = [ET.fromstring(f"<r>{history_text}</r>")]
    pieces = [_clean(d.text or "") for d in parts[0].findall("HistoryData")]
    return " ;-- ".join(p for p in pieces if p)


def _editors_notes(sec) -> tuple[str, list[str]]:
    warnings: list[str] = []
    notes_el = sec.find("EditorsNotes")
    if notes_el is None:
        return "", warnings
    out: list[str] = []
    for eni in notes_el.findall("EditorsNoteInfo"):
        label = _NOTE_LABELS.get(_text(eni, "Type").strip().lower(), _text(eni, "Type").strip() or "Note")
        text, w = _parse_embedded(_text(eni, "Text"))
        warnings.extend(w)
        # HTML's own rendering runs consecutive notes of one label together with no separator (a quirk
        # of the source site, seen verbatim in real pages -- e.g. "...1966-8a.Act 109 of 1939...");
        # matched here rather than "fixed" so the two sources agree byte-for-byte.
        text = text.replace("\n", "")
        if text:
            out.append(f"{label}: {text}")
    return "\n".join(out), warnings


def _division_label(div) -> str:
    """The HTML rendering's ``.divisionHeading`` is always "{DivisionType} {DivisionNumber}" (e.g.
    "Article 1") in every sample seen, with ``DivisionTitle`` empty; matched here for parity. Falls back
    to ``Name`` (e.g. "37.2101.D10") if the type/number fields are ever missing, and appends a real
    ``DivisionTitle`` if one is ever present, since the HTML source would presumably show it too."""
    dtype, dnum = _text(div, "DivisionType").strip(), _text(div, "DivisionNumber").strip()
    label = f"{dtype} {dnum}".strip() if (dtype or dnum) else _clean(_text(div, "Name"))
    # DivisionTitle is plain text (not embedded pseudo-XML like BodyText), but Chapter 1.xml's own
    # "Schedule" division carries a literal escaped "<br/>" inside it anyway ("SCHEDULE AND TEMPORARY
    # PROVISIONS<br/>To insure the orderly transition...") -- confirmed in the raw file. Not currently
    # exposed anywhere (division_path is parse-time-only metadata, not stored or served), but cleaned up
    # here rather than left to leak into the raw text if that ever changes.
    title = _clean(_text(div, "DivisionTitle")).replace("<br/>", " ")
    title = " ".join(title.split())
    return f"{label} {title}".strip() if title else label


def _commentary_banner(el) -> str:
    comm = el.find("Commentary")
    if comm is None:
        return ""
    bits = [_clean(_text(ci, "Text")) for ci in comm.findall("CommentaryInfo")]
    return " ".join(b for b in bits if b)


def _section_content(sec, act_name: str, act_label: str) -> tuple[SectionContent, list[str]]:
    """Returns the section content plus any *extra* citations named only in ``SectRef``.

    ``MCLNumber`` is this section's own identity and is always used for the citation. ``SectRef``
    usually repeats it verbatim, but a handful of old, fully-repealed entries give a comma-separated
    list there instead (seen in Chapter 2.xml: MCLNumber "2.104", SectRef "2.104, 2.105") -- one compiled
    entry standing in for a short run of consecutive repealed sections. The extras are returned
    separately so the caller can record them as covered-range stubs, the same way a whole-act repeal
    range is recorded, rather than silently discarding citations the file does mention.
    """
    warnings: list[str] = []
    number = _text(sec, "MCLNumber").strip()
    sect_ref = _text(sec, "SectRef").strip()
    # The Constitution (Chapter 1.xml) uses an entirely different MCLNumber shape -- "Article I § 3" or
    # "Schedule § 1" -- instead of the ordinary dotted MCL number. Tried first since it's a disjoint shape
    # (never matches _NUM_VARIANT and vice versa); confirmed against the real file. sect_ref is passed as a
    # fallback for the one real entry (a ceremonial signature/vote-record block, "Schedule SigBlock") whose
    # MCLNumber has no "§ N" of its own -- see normalize_const_mclnumber.
    cite = normalize_const_mclnumber(number, sect_ref)
    if cite is not None:
        variant = ""  # no .new/.amended/[1] variant-suffix shape has been seen for the Constitution
    else:
        m = _NUM_VARIANT.match(number)
        if not m:
            raise ValueError(f"unrecognized MCLNumber: {number!r}")
        cite = normalize_citation(m.group("cite"))
        if cite is None:
            raise ValueError(f"bad citation in MCLNumber: {number!r}")
        variant = (m.group("variant") or "").lstrip(".")

    extra_citations: list[str] = []
    # For the Constitution, SectRef is always just the bare "§ N" form of the same section (confirmed
    # against all 281 sections in the real file) -- never a genuine comma-list of extra citations the way
    # a handful of old, fully-repealed ordinary MCL entries use it (see the docstring above). Comparing it
    # to the full "Article I § 3"-style MCLNumber would always look different and misfire a warning on
    # every single section, so this whole block is skipped for a recognized const citation.
    if cite.source != "const" and sect_ref and sect_ref != number:
        for tok in sect_ref.split(","):
            tok = tok.strip()
            if not tok or tok == number:
                continue
            em = _NUM_VARIANT.match(tok)
            ecite = normalize_citation(em.group("cite")) if em else None
            if ecite is None:
                warnings.append(f"SectRef names {tok!r} alongside {number!r}, not understood as a citation")
                continue
            extra_citations.append(ecite.canonical)

    history = _history_note(_text(sec, "HistoryText"))

    catchline = _clean(_text(sec, "CatchLine"))
    status, effective = "active", None
    # The status word must be immediately followed by either a period ("Repealed." / "Expired.") or
    # whitespace leading straight into a year ("Repealed 2026, Act 25, Imd. Eff. July 21, 2026." -- no
    # period at all, confirmed live 2026-09-22, MCL 388.1623g/Chapter 388.xml -- its own sibling section
    # 388.1623h *does* have the period, so both conventions coexist in the same chapter) to count as a
    # status prefix. A plain \b boundary is not enough on its own: real catchlines use these same words as
    # ordinary title text, e.g. MCL 89.4's CatchLine is "Repealed ordinances; re-enactment." (Repealed=
    # false, an active section about the re-enactment of repealed ordinances) -- confirmed live 2026-09-22
    # against the real Chapter 81.xml, which is what first surfaced this false positive. The distinguishing
    # signal is what follows the word: history-citation text always starts with a period or a year, never
    # another word the way ordinary title prose does ("ordinances").
    #
    # Some sections' CatchLine echoes the section's own number before the status word instead of leading
    # straight with it -- MCL 324.32724's real CatchLine is "324.32724 Repealed. 2008, Act 181, Imd. Eff.
    # July 9, 2008." (confirmed live 2026-09-22, Chapter 324.xml/NREPA), not "Repealed. ..." like every
    # other chapter seen so far. Tolerated as an optional leading citation-shaped token so the status word
    # right after it is still recognized; the stored catchline itself is left untouched either way.
    first_word = re.match(rf"^(?:{MCL_TOKEN}\s+)?([A-Za-z]+)(?:\.|\s+(?=\d))", catchline)
    status_words = {
        "repealed": "repealed", "expired": "expired", "reserved": "reserved",
        # A genuine compiler typo, not a parsing edge case: MCL 205.96a's real CatchLine reads "Reepaled.
        # 2006, Act 673, Eff. Jan. 1, 2011." (confirmed live 2026-09-22, Chapter 205.xml) -- misspelled on
        # the Legislature's own site. Repealed=true and its EditorsNotes ("The repealed section pertained
        # to qualified athletic event.") both corroborate it's actually repealed, so this is recognized as
        # an alias rather than left to abort the chapter's ingest. Kept narrow (this exact misspelling)
        # rather than fuzzy-matching "repealed", since a real *different* mismatch should still abort loud.
        "reepaled": "repealed",
        # An Executive (Reorganization) Order provision undone by a later E.O., not by the Legislature --
        # MCL 388.996's real CatchLine is "Rescinded. 2005, E.O. No. 2005-4, Eff. Feb. 15, 2005." (confirmed
        # live 2026-09-22, Chapter 388.xml). Kept as its own status rather than folded into "repealed" --
        # same reasoning as "abrogated" (mclxml.py) and "rejected" (chapterdoc.py) -- since it is not a
        # statutory repeal. feed.py's _STATUS_HINT already anticipated this word (and "Superseded", not yet
        # confirmed against a real section -- add it here the same way, with a real citation, if it turns up).
        "rescinded": "rescinded",
    }
    if first_word and first_word.group(1).lower() in status_words:
        status = status_words[first_word.group(1).lower()]
        entries = parse_history(re.sub(rf"^(?:{MCL_TOKEN}\s+)?[A-Za-z]+\.?\s*", "", catchline))
        if entries and entries[0].effective_iso:
            effective = entries[0].effective_iso
    elif cite.source == "const" and _bool(sec, "Repealed"):
        # The Constitution doesn't signal a defunct provision by prefixing its CatchLine the way ordinary
        # MCL sections do -- the catchline stays a plain description ("Annexation or merger with a city.")
        # and the status word ("Abrogated.") lives in HistoryText instead, alongside an empty BodyText.
        # Confirmed against the real Chapter 1.xml: exactly 2 sections (Article IV § 4, § 5), both removed
        # by the same 2018 initiated law. "abrogated" is the term Michigan's own convention uses here, kept
        # as its own status rather than folded into "repealed" (a statutory term) or "expired".
        status = "abrogated"
        entries = parse_history(history)
        if entries and entries[0].effective_iso:
            effective = entries[0].effective_iso
    # The Repealed flag tracks "no longer current text", not literally "repealed": a section whose
    # catchline reads "Expired." consistently carries Repealed=true too (seen throughout Chapters 333,
    # 750, e.g. MCL 750.409a) -- only a genuine "active" catchline should ever pair with Repealed=false.
    # "Reserved" has not been seen in any sample; included here on the same reasoning, not yet confirmed.
    if _bool(sec, "Repealed") != (status in ("repealed", "expired", "reserved", "abrogated", "rescinded")):
        warnings.append(f"INTEGRITY: Repealed flag ({_bool(sec, 'Repealed')}) disagrees with catchline status ({status!r})")

    text, w = _parse_embedded(_text(sec, "BodyText"))
    warnings.extend(w)
    notes, w = _editors_notes(sec)
    warnings.extend(w)

    banner = _commentary_banner(sec)
    if banner and not variant:
        warnings.append("section carries a commentary notice but no variant suffix in its number")
    if banner and variant:
        effective = _iso_from_banner(banner) or effective
        notes = "\n".join(n for n in (notes, f"Site notice: {banner}") if n)

    return SectionContent(
        citation=cite.canonical, variant=variant, catchline=catchline, status=status, effective_date=effective,
        text=text, history_note=history, compilers_notes=notes, banner=banner, act_name=act_name,
        act_label=act_label, warnings=warnings, source=cite.source,
    ), extra_citations


def parse_chapter_xml(xml_data: bytes | str) -> ChapterDocument:
    """Parse a whole-chapter XML file. Pass the raw ``bytes`` read from disk so the file's own
    ``encoding="utf-16"`` declaration is honored; a pre-decoded ``str`` (e.g. an HTTP client that already
    decoded the response body using the server's Content-Type charset) works too -- ElementTree parses a
    string's characters directly and does not re-interpret the encoding declaration in that case."""
    root = ET.fromstring(xml_data)
    _strip_namespaces(root)
    if root.tag != "MCLChapterInfo":
        raise ValueError(f"not a chapter XML file: root element is <{root.tag}>, not <MCLChapterInfo>")

    chapter = (_text(root, "Name") or "").strip().upper()
    if not chapter:
        raise ValueError("chapter XML file has no <Name>")
    name = _clean(_text(root, "Title"))
    # A file whose <Name> declares one chapter can legitimately hold sections spanning many chapters when
    # <MultiChapter>true</MultiChapter> -- confirmed against the real Chapter 760.xml (CODE OF CRIMINAL
    # PROCEDURE), which is named "760" but actually covers 760-777 plus 767A and 771A in one file. Each
    # section's own citation is still authoritative for which chapter it belongs to (see _to_doc in
    # chapterdoc.py); the per-section "appears inside chapter X" check below is only meaningful -- and
    # only fires -- for a file that does NOT declare itself multi-chapter.
    multi_chapter = _bool(root, "MultiChapter")

    warnings: list[str] = []
    statutes: list[StatuteBlock] = []
    seen_keys: set[tuple[str, str]] = set()
    total_sections = 0
    consumed_sections = 0

    top_coll = root.find("MCLDocumentInfoCollection")
    for st in (top_coll.findall("MCLStatuteInfo") if top_coll is not None else []):
        sname = _clean(_text(st, "Heading")) or _clean(_text(st, "Name"))
        label = _clean(_text(st, "Name"))
        act_citation, kind = _act_info(label)
        if kind == "other":
            warnings.append(f"statute label not recognized: {label!r}")
        long_title = _clean(_text(st, "LongTitle"))
        st_notes, w = _editors_notes(st)
        warnings.extend(w)
        block = StatuteBlock(label, sname, act_citation, kind, preamble="",
                              history=_history_note(_text(st, "HistoryText")), notes=st_notes)
        rng = _stub_ranges(long_title, chapter, act_citation, sname)
        if rng:
            block.ranges.append(rng)
        else:
            block.preamble = long_title

        def walk(node, path: tuple[str, ...]) -> None:
            nonlocal consumed_sections, total_sections
            coll = node.find("MCLDocumentInfoCollection")
            if coll is None:
                return
            for child in coll:
                if child.tag == "MCLDivisionInfo":
                    walk(child, path + (_division_label(child),))
                elif child.tag == "MCLSectionInfo":
                    total_sections += 1
                    try:
                        c, extras = _section_content(child, sname, label)
                    except ValueError as e:
                        warnings.append(f"INTEGRITY: {label}: unparseable section: {e}")
                        continue
                    # This check assumes an "MCL N.N" shaped citation (chapter is its first dotted token).
                    # A const citation ("Const 1963, art 1, § 3") doesn't have that shape at all -- and
                    # doesn't need the check anyway, since the Constitution's own chapter/section split
                    # already comes from a disjoint parsing path (normalize_const_mclnumber) that can't
                    # silently drift the way a shared MCL-token regex could.
                    if not multi_chapter and c.source == "mcl" and c.citation.split()[1].split(".")[0].upper() != chapter:
                        warnings.append(f"INTEGRITY: {c.citation} appears inside chapter {chapter}")
                    key = (c.citation, c.variant)
                    if key in seen_keys:
                        warnings.append(f"INTEGRITY: duplicate section {c.citation} {c.variant!r} in one chapter file")
                        continue
                    seen_keys.add(key)
                    consumed_sections += 1
                    block.sections.append(SectionRecord(_to_doc(c, chapter, act_citation, sname), path, c.warnings))
                    for extra in extras:
                        block.ranges.append(ParsedRange(
                            source="mcl", start_citation=extra, end_citation=extra, chapter=chapter,
                            act_citation=act_citation, act_name=sname, status=c.status,
                            note=f"Compiled with {c.citation} ({c.catchline})",
                        ))
                    block.ranges.extend(extract_narrative_ranges(
                        c.text, c.compilers_notes, act_citation=act_citation, act_name=sname))

        walk(st, ())
        statutes.append(block)

    if total_sections != consumed_sections:
        warnings.append(f"INTEGRITY: {total_sections} sections seen in file but {consumed_sections} kept")

    return ChapterDocument(chapter, name, currency=None, through_act=None, rendered_on=None,
                           statutes=statutes, warnings=warnings)
