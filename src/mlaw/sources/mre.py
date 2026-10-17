"""Michigan Rules of Evidence, from the Court's published PDF (there is no HTML version).

* ``MreFilesSource(path)``  parses a saved copy of the PDF (no network).
* ``MreWebSource(client)``  finds the current PDF through the courts.michigan.gov content API
  (``courts_api``), downloads it through a ``PoliteClient`` and parses it the same way. That site's
  robots.txt currently disallows automated access, so a live run fails closed with ``RobotsDisallowed``.

Text comes from ``pdftotext -layout`` (poppler). Layout of that text: a front-matter table of contents
(dot leaders, page numbers), then per-page footers ("Michigan Rules of Evidence  N  Updated with MSC
order(s)" / "effective on M/D/YYYY"), article group headings ("RULES 101-106", "RULE 501") and rule headings
("Rule 609 Impeachment by ..."). Headings are validated against the TOC, so a body line that merely begins
"Rule 803 ..." at the margin is not mistaken for a heading, and a heading wrapped over two lines (Rules 105,
403, 804) is reassembled from the TOC's full title.

Subrule indentation is normalized to two spaces per nesting level. The PDF's raw indents are irregular
(first-level "(a)" at 0, "(1)" at 5, "(A)" at 10, "(i)" at 15-16, hanging continuations at marker indent + the
marker's width), so nesting is inferred from marker lines with a stack of their raw indents, and a wrapped
continuation line is put one hanging step (2 spaces) inside its marker.

``complete`` is True only if the number of rules parsed equals the number of distinct rules in the TOC; any
mismatch makes it False (so the pipeline's removal detection stays off) and adds a warning.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from ..models import ParsedDoc, ParsedPublicAct, ParsedRange
from . import courts_api

CATALOG_PATH = "/rules-administrative-orders-and-jury-instructions/current-rules-and-jury-instructions/"
CATALOG_LINK_NAME = "Michigan Rules of Evidence"
# Where the Court publishes the PDF (catalog link, 2026-09-29). A saved copy is attributed to this, not to the
# local file, so provenance never exposes a path on the ingesting machine.
PDF_URL = ("https://www.courts.michigan.gov/492ca5/siteassets/rules-instructions-administrative-orders/"
           "rules-of-evidence/michigan-rules-of-evidence.pdf")

_FOOTER1 = re.compile(r"^Michigan Rules of Evidence\s{2,}\S+\s{2,}Updated with MSC order\(s\)\s*$")
_FOOTER2 = re.compile(r"^\s+effective on (\d{1,2})/(\d{1,2})/(\d{4})\s*$")
_GROUP = re.compile(r"^\s*RULES?\s+\d{3,4}")  # "RULES 101–106", "RULE 501" (body) -- not a rule
_TOC_ENTRY = re.compile(r"^\s+Rule (\d{3,4}[A-Z]?) (\S.*)$")
_BODY_HEADING = re.compile(r"^Rule (\d{3,4}[A-Z]?) (\S.*)$")
_MARKER = re.compile(r"^(\s*)(\([A-Za-z0-9]{1,4}\))(?=\s)")
_RESERVED = re.compile(r"^\[?\s*reserved\.?\s*\]?$", re.I)
_TITLE_LINE = "Michigan Rules of Evidence"


@dataclass
class MreResult:
    docs: list[ParsedDoc]
    currency_date: str | None
    warnings: list[str] = field(default_factory=list)
    toc_rule_count: int = 0


def pdf_to_text(path: str | Path) -> str:
    try:
        r = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, check=True, timeout=120,
        )
    except FileNotFoundError as e:
        raise RuntimeError("pdftotext (poppler-utils) is required to read the Rules of Evidence PDF but was not found") from e
    return r.stdout


def _norm(s: str) -> str:
    return " ".join(s.split())


def _strip_footers(text: str) -> tuple[list[str], str | None]:
    """Lines without page footers, form feeds, and the blank lines that flank a footer."""
    lines = text.replace("\f", "").split("\n")
    out: list[str] = []
    stamp: str | None = None
    i = 0
    while i < len(lines):
        if _FOOTER1.match(lines[i]) and i + 1 < len(lines) and (m := _FOOTER2.match(lines[i + 1])):
            stamp = stamp or f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
            while out and not out[-1].strip():
                out.pop()  # blank lines before the footer
            i += 2
            while i < len(lines) and not lines[i].strip():
                i += 1  # and after it (page top)
            continue
        out.append(lines[i].rstrip())
        i += 1
    return out, stamp


def _parse_toc(lines: list[str]) -> dict[str, str]:
    toc: dict[str, str] = {}
    last: str | None = None
    for ln in lines:
        m = _TOC_ENTRY.match(ln)
        if m:
            title = re.split(r"\s*\.{3,}", m.group(2))[0].strip()
            if not title:  # group line "Rule 501 ....."
                last = None
                continue
            toc[m.group(1)] = title
            last = m.group(1) if not re.search(r"\.{3,}", m.group(2)) else None
        elif last and ln.strip() and not ln.lstrip().startswith("Rules"):
            toc[last] = _norm(toc[last] + " " + re.split(r"\s*\.{3,}", ln.strip())[0])
            last = None
        else:
            last = None
    return toc


def _normalize_body(lines: list[str]) -> str:
    # trim outer blanks, collapse blank runs
    while lines and not lines[0].strip():
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    out: list[str] = []
    stack: list[int] = []  # raw indents of open marker levels
    hang = -1  # raw indent at which the current marker's wrapped text continues
    level = 0
    for ln in lines:
        if not ln.strip():
            if out and out[-1]:
                out.append("")
            continue
        indent = len(ln) - len(ln.lstrip())
        body = ln.strip()
        m = _MARKER.match(ln)
        if m and indent != hang:
            while stack and stack[-1] >= indent:
                stack.pop()
            stack.append(indent)
            level = len(stack) - 1
            hang = indent + len(m.group(2)) + 1
            out.append("  " * level + body)
        elif indent == 0:
            stack, hang, level = [], -1, 0
            out.append(body)
        else:
            out.append("  " * (level + 1) + body if stack else body)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def parse_mre_text(text: str, url: str) -> MreResult:
    lines, stamp = _strip_footers(text)
    warnings: list[str] = []
    if stamp is None:
        warnings.append("no 'effective on M/D/YYYY' currency stamp found")
    lines = [ln for ln in lines if ln.strip() != _TITLE_LINE]

    first = next((i for i, ln in enumerate(lines) if _GROUP.match(ln)), None)
    if first is None:
        return MreResult([], stamp, warnings + ["no body found (no 'RULES nnn' group heading)"])
    toc = _parse_toc(lines[:first])
    body = lines[first:]

    # split the body into rules
    rules: list[tuple[str, str, list[str]]] = []
    excluded: list[str] = []
    cur: tuple[str, str, list[str]] | None = None
    i = 0
    while i < len(body):
        ln = body[i]
        if _GROUP.match(ln):
            excluded.append(ln.strip())
            i += 1
            continue
        m = _BODY_HEADING.match(ln)
        if m and m.group(1) in toc and not re.search(r"\.{4,}", ln) and (
            toc[m.group(1)] == _norm(m.group(2)) or toc[m.group(1)].startswith(_norm(m.group(2)) + " ")
        ):
            n, title = m.group(1), _norm(m.group(2))
            while title != toc[n] and i + 1 < len(body) and toc[n].startswith(title + " "):
                i += 1  # wrapped heading: pull in continuation lines
                title = _norm(title + " " + body[i])
            cur = (n, title, [])
            rules.append(cur)
        elif cur is not None:
            cur[2].append(ln)
        i += 1
    warnings.extend(f"non-rule heading excluded: {h}" for h in dict.fromkeys(excluded))

    docs: list[ParsedDoc] = []
    seen: set[str] = set()
    for n, title, blines in rules:
        if n in seen:
            warnings.append(f"duplicate heading for Rule {n}; keeping the first")
            continue
        seen.add(n)
        text_ = _normalize_body(blines)
        reserved = bool(_RESERVED.match(text_.strip())) or "[reserved" in title.lower()
        docs.append(ParsedDoc(
            source="mre", citation=f"MRE {n}", catchline=title, text=text_, kind="rule",
            chapter=str(int(re.match(r"\d+", n).group()) // 100),
            url=url, status="reserved" if reserved else "active",
        ))
        if not text_.strip():
            warnings.append(f"Rule {n} has no body text")
    if set(toc) != seen:
        warnings.append(
            f"TOC/body mismatch: in TOC only {sorted(set(toc) - seen)}; in body only {sorted(seen - set(toc))}")
    return MreResult(docs, stamp, warnings, toc_rule_count=len(toc))


class MreFilesSource:
    name = "mre"
    source_kinds = frozenset({"mre"})
    covered_chapters = None

    def __init__(self, path: str | Path, url: str = PDF_URL):
        self.path = Path(path)
        self._result: MreResult | None = None
        self.url: str = url

    def _load(self) -> MreResult:
        if self._result is None:
            self._result = self._parse()
        return self._result

    def _parse(self) -> MreResult:
        return parse_mre_text(pdf_to_text(self.path), self.url)

    @property
    def result(self) -> MreResult:
        return self._load()

    @property
    def warnings(self) -> list[str]:
        return self._load().warnings

    @property
    def complete(self) -> bool:
        r = self._load()
        return bool(r.docs) and r.toc_rule_count == len(r.docs) and not any(
            w.startswith("TOC/body mismatch") for w in r.warnings)

    @property
    def currency(self) -> str | None:
        d = self._load().currency_date
        return f"MRE updated with MSC order(s) effective {d}" if d else None

    def iter_documents(self) -> Iterator[ParsedDoc]:
        yield from self._load().docs

    def iter_public_acts(self) -> Iterable[ParsedPublicAct]:
        return []

    def iter_ranges(self) -> Iterable[ParsedRange]:
        return []


class MreWebSource(MreFilesSource):
    """Locate and download the current PDF through ``client`` (a ``PoliteClient``), then parse it.

    TODO: SHA-256 short-circuit via ``manifest.source_files``. The existing API (``plan_fetch`` /
    ``record_parsed``) needs a DB connection and a ``DirEntry``, which a source does not have; wiring it
    belongs in the refresh/CLI layer (hash the downloaded bytes, skip ingest if unchanged).
    """

    def __init__(self, client, catalog_path: str = CATALOG_PATH):
        self.client = client
        self.catalog_path = catalog_path
        self._result = None
        self.url = ""
        self.sha256: str | None = None

    def _parse(self) -> MreResult:
        import hashlib

        page = courts_api.content(self.catalog_path, self.client)
        kids = courts_api.children(page["contentLink"]["id"], self.client)
        links = courts_api.child_links(kids)
        if CATALOG_LINK_NAME not in links:
            raise courts_api.CourtsApiError(f"{CATALOG_LINK_NAME!r} not found among catalog children")
        self.url = links[CATALOG_LINK_NAME]
        data = self.client.get(self.url).content
        self.sha256 = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "mre.pdf"
            p.write_bytes(data)
            return parse_mre_text(pdf_to_text(p), self.url)
