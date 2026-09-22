"""Parser and event log for the Legislature's "MCL updates" RSS feed.

What the feed is (from a real sample, 2026-09-02 build):
* Items are database events, not summaries of law changes. Title forms seen so far:
      "Section 333.1011 - added"                      (link + guid: .../Home/GetObject?objectName=mcl-333-1011)
      "Section Act.687.of.2002 has been deleted"      (no link; guid "mcl-Act-687-of-2002")
* The description is the section's catchline (plus a status prefix such as "Expired. 1978, Act 368,
  Eff. Sept. 30, 1981.") followed by boilerplate. It carries no statutory text and no effective date.
* Every item in a build shares one pubDate (the build time). It is a batch id, not a change date.
* "added" appears to cover re-loaded sections, and an act-level "deleted" sits beside a run of
  "added" sections of the same act. Treat events as triggers to re-fetch and compare, never as
  authoritative statements that law was enacted or removed.

Full-file findings (2,410 items, one build, all Public Health Code / chapter 333): besides sections,
the feed reports structural nodes and other document types:
      "BASIC HEALTH SERVICES - added"                 heading/division node (object mcl-368-1978-2-23; no "Section")
      "Section 368.1978.5.55 has been deleted"        heading node id: act.year.article.part
      "Section E.R.O.No.1991.23 has been deleted"     Executive Reorganization Order
So a single re-load of one act produced thousands of events. Volume is not a measure of legal change.

Variant nodes: one citation can have several nodes, e.g. "Section 333.16188.added - added"
(object mcl-333-16188-added), "Section 333.16335.amended - added", and "Section 333.5474c[1] - added".
The dotted suffixes (``.added``/``.amended``/``.new``) are the compilers' way of publishing a version
that's enacted but not yet in effect (mclxml.py's module docstring has the confirmed detail). The bracket
suffix (``[1]``, and confirmed letter-valued too as ``[a]`` -- see mclxml.py's module docstring, real MCL
752.863[a], live 2026-09-22) is a different thing: the Compiler's own disambiguator for two separate,
both-ordinarily-effective sections that would otherwise collide on the same MCL number. Either way, the
data model must not collapse them into one text per citation.

Unknown title forms are kept as kind="unrecognized" with the raw title, never dropped or fatal.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree as ET

from .citations import normalize_citation

_BOILERPLATE = re.compile(r"\s*Please see compilers notes or history at the bottom of the section for details\.?\s*$")
_SECTION_TITLE = re.compile(r"^Section\s+(?P<id>\S+)\s+(?:-\s+(?P<verb1>[A-Za-z ]+?)|has been\s+(?P<verb2>[A-Za-z ]+?))\s*$")
_ACT_ID = re.compile(r"^Act\.(\d+)\.of\.(\d{4})$")
_HEADING_TITLE = re.compile(r"^(?P<name>.+?)\s+-\s+(?P<verb>[A-Za-z ]+?)$")
_SECTION_ID = re.compile(r"^(?P<cite>\d{1,3}[A-Za-z]?\.\d{1,5}[A-Za-z]{0,3})(?:\.(?P<dotv>[A-Za-z]+)|\[(?P<brk>[A-Za-z0-9]+)\])?$")
_INITIATED_ID = re.compile(r"^Initiated\.Law\.(\d+)\.of\.(\d{4})$")
_ERO_ID = re.compile(r"^E\.R\.O\.No\.(\d{4})\.(\d+)$")
# act.year.article.part, e.g. 368.1978.5.55 or 175.1927.II. Extra segments are digits or UPPERCASE, so a
# lowercase variant word ("333.2026.amended") is never mistaken for a heading node.
_NODE_ID = re.compile(r"^(\d{1,4})\.(1[89]\d{2}|20\d{2})(?:\.(?:\d+|[A-Z][A-Z-]*))+$")
_NODE_OBJECT = re.compile(r"^mcl-(\d{1,4})-(\d{4})-", re.I)  # mcl-368-1978-2-23, mcl-175-1927-II
_STATUS_HINT = re.compile(r"^(Expired|Repealed|Reserved|Rescinded|Superseded)\b", re.I)


@dataclass(frozen=True)
class FeedEvent:
    raw_title: str
    kind: str  # added | deleted | act_deleted | <other verb> | unrecognized
    citation: str | None  # canonical, e.g. "MCL 333.1011"
    act_citation: str | None  # canonical, e.g. "1978 PA 368" (act-level events)
    object_name: str | None  # e.g. "mcl-333-1011"
    url: str | None
    catchline_hint: str  # catchline as given by the feed; may start with a status word
    status_hint: str | None  # "expired", "repealed", ... when the hint says so
    published: str | None  # ISO UTC build time shared by the batch
    variant: str | None = None  # "added" | "amended" | "[1]" ... for variant section nodes


@dataclass(frozen=True)
class Feed:
    title: str
    description: str
    built_at: str | None
    events: list[FeedEvent]


def _iso(rfc822: str | None) -> str | None:
    if not rfc822 or not rfc822.strip():
        return None
    try:
        dt = parsedate_to_datetime(rfc822.strip())
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(item: ET.Element, tag: str) -> str:
    el = item.find(tag)
    return " ".join((el.text or "").split()) if el is not None else ""


def _parse_item(item: ET.Element) -> FeedEvent:
    title = _text(item, "title")
    link = _text(item, "link") or None
    guid = _text(item, "guid") or None
    desc = _BOILERPLATE.sub("", _text(item, "description")).strip()
    published = _iso(_text(item, "pubDate"))

    object_name = None
    for candidate in (link, guid):
        if candidate and "objectName=" in candidate:
            object_name = parse_qs(urlsplit(candidate).query).get("objectName", [None])[0]
            break
    if object_name is None and guid and guid.startswith("mcl-"):
        object_name = guid

    status = _STATUS_HINT.match(desc)
    status_hint = status.group(1).lower() if status else None

    def ev(kind, citation=None, act=None, variant=None):
        return FeedEvent(title, kind, citation, act, object_name, link, desc, status_hint, published, variant)

    m = _SECTION_TITLE.match(title)
    if not m:
        h = _HEADING_TITLE.match(title)
        if h and object_name:
            nm = _NODE_OBJECT.match(object_name)
            act = f"{nm.group(2)} PA {int(nm.group(1))}" if nm else None
            verb = h.group("verb").strip().lower().replace(" ", "_")
            return FeedEvent(title, f"heading_{verb}", None, act, object_name, link,
                             h.group("name").strip(), None, published)
        return ev("unrecognized")
    ident = m.group("id")
    verb = (m.group("verb1") or m.group("verb2") or "").strip().lower()

    am = _ACT_ID.match(ident)
    if am:
        act = f"{am.group(2)} PA {int(am.group(1))}"
        return ev("act_deleted" if verb == "deleted" else f"act_{verb.replace(' ', '_')}", act=act)
    if _ERO_ID.match(ident):
        return ev(f"ero_{verb.replace(' ', '_')}")
    nm = _NODE_ID.match(ident)
    if nm:
        return ev(f"heading_{verb.replace(' ', '_')}", act=f"{nm.group(2)} PA {int(nm.group(1))}")

    if _INITIATED_ID.match(ident):
        return ev(f"initiated_law_{verb.replace(' ', '_')}")
    sm = _SECTION_ID.match(ident)
    if sm:
        c = normalize_citation(sm.group("cite"))
        if c is not None and c.source == "mcl":
            variant = sm.group("dotv").lower() if sm.group("dotv") else (f"[{sm.group('brk')}]" if sm.group("brk") else None)
            return ev(verb.replace(" ", "_") or "unrecognized", citation=c.canonical, variant=variant)
    return ev("unrecognized")


def parse_feed(xml_text: str | bytes) -> Feed:
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("Not an RSS feed: no <channel>")
    events = [_parse_item(i) for i in channel.findall("item")]
    return Feed(
        title=_text(channel, "title"),
        description=_text(channel, "description"),
        built_at=_iso(_text(channel, "lastBuildDate")),
        events=events,
    )


def summarize(feed: Feed) -> dict:
    by_kind: dict[str, int] = {}
    chapters: dict[str, int] = {}
    for e in feed.events:
        by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
        if e.citation:
            ch = e.citation.split()[1].split(".")[0]
            chapters[ch] = chapters.get(ch, 0) + 1
    return {
        "built_at": feed.built_at,
        "items": len(feed.events),
        "by_kind": by_kind,
        "chapters_touched": dict(sorted(chapters.items(), key=lambda kv: -kv[1])),
        "variant_nodes": sorted({(e.citation, e.variant) for e in feed.events if e.variant}),
        "act_level_events": [e.act_citation for e in feed.events if e.kind.startswith("act_")],
        "acts_touched": sorted({e.act_citation for e in feed.events if e.act_citation}),
        "unrecognized_titles": [e.raw_title for e in feed.events if e.kind == "unrecognized"],
    }


def resolve_refresh_chapters(conn: sqlite3.Connection, events) -> tuple[list[str], list[str]]:
    """Resolve feed events to the actual MCL chapters that need re-fetching -- the piece feed-driven
    refresh needs on top of the raw event log, because a chapter source (``MclXmlFilesSource``,
    ``MclXmlWebSource``) is fetched a whole file at a time, not by citation.

    A section-level event names its own chapter directly (from its citation, exactly like a
    ``ParsedDoc``'s own ``chapter`` -- see ``chapterdoc.py``'s ``_to_doc``). An act-level or heading event
    names only the act; the chapter(s) it currently occupies are looked up from what is already stored --
    checking both ``documents`` (a live act's current sections) and ``ranges`` (a *fully* repealed or
    expired act, which is stored only as a covered-range stub with no live document at all -- confirmed
    against the real feed sample: 5 of its 29 act-level "deleted" events name acts, e.g. "1978 PA 443",
    that exist in Chapter 333.xml's own ``ranges`` but have zero rows in ``documents``, and were wrongly
    falling through to "unmapped" before this checked ``ranges`` too). This also does the right thing for
    a ``MultiChapter`` file: an act spanning many chapters, like Chapter 760.xml's Code of Criminal
    Procedure, resolves to *all* of them, live or repealed. An act found in neither table -- never
    ingested before, or a brand-new act the feed just introduced with no companion section event in the
    same build -- can't be resolved this way and is returned separately rather than guessed at, so the
    caller can fall back to a full crawl for it (see the module docstring's "full re-crawl as a safety
    net")."""
    chapters: set[str] = set()
    unmapped_acts: set[str] = set()
    for e in events:
        if e.citation:
            chapters.add(e.citation.split()[1].split(".")[0].upper())
        elif e.act_citation:
            found = {
                r["chapter"] for r in conn.execute(
                    "SELECT DISTINCT chapter FROM documents WHERE act_citation = ?"
                    " UNION SELECT DISTINCT chapter FROM ranges WHERE act_citation = ?",
                    (e.act_citation, e.act_citation),
                ).fetchall()
            }
            if found:
                chapters.update(found)
            else:
                unmapped_acts.add(e.act_citation)
    return sorted(chapters), sorted(unmapped_acts)


def record_feed(conn: sqlite3.Connection, feed: Feed, now: str) -> dict:
    """Append events not seen before (deduplicated on build time + title + object). Returns the
    newly recorded events and what should be re-fetched. The feed never changes stored text on
    its own; it only tells the ingest what to re-check."""
    new: list[FeedEvent] = []
    for e in feed.events:
        cur = conn.execute(
            "INSERT OR IGNORE INTO feed_events (seen_at, feed_built_at, kind, citation, act_citation,"
            " object_name, url, catchline_hint, status_hint, raw_title, variant) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (now, feed.built_at, e.kind, e.citation, e.act_citation, e.object_name or "", e.url,
             e.catchline_hint, e.status_hint, e.raw_title, e.variant),
        )
        if cur.rowcount:
            new.append(e)
    conn.commit()
    refresh_chapters, unmapped_acts = resolve_refresh_chapters(conn, new)
    return {
        "new_events": len(new),
        "refetch_sections": sorted({e.citation for e in new if e.citation}),
        "refetch_objects": sorted({e.object_name for e in new if e.citation and e.object_name}),
        "recrawl_acts": sorted({e.act_citation for e in new if e.act_citation}),
        "refresh_chapters": refresh_chapters,
        "unmapped_acts": unmapped_acts,
        "unrecognized": [e.raw_title for e in new if e.kind == "unrecognized"],
    }
