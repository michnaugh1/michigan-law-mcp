"""Parser for the Legislature's "Daily Bill Status Report" page (Home/DailyBillStatus?dateFrom=…).

This is *pending legislation*, not law: bills are listed under the stage they reached in the date range
(Introduced / Passed by Chamber / Enrolled / Adopted, and probably others such as signed or vetoed —
only these four were seen in the saved sample). Each bill's one-line description usually says which
statute it changes ("Amends sec. 9 of 1972 PA 348 (MCL 554.609)."), which is what lets the server answer
"is there pending legislation touching MCL 554.609?" — always labelled as pending and never merged into
statute text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from .citations import MCL_TOKEN, _mcl_key, extract_xrefs
from .mclsite import object_name

_BILL_OBJECT = re.compile(r"^(?P<year>\d{4})-(?P<type>[A-Z]+)-(?P<num>\d{4})$")
_ET_SEQ = re.compile(rf"MCL\s+({MCL_TOKEN})\s+et\s+seq", re.I)


@dataclass
class BillRow:
    stage: str  # heading the row appeared under, e.g. "Enrolled"
    object_name: str  # "2025-SB-0022"
    label: str  # "SB 0022 of 2025"
    year: int
    type: str  # HB | SB | HR | SR | HCR | SCR | HJR | SJR …
    number: int
    description: str
    amends_mcl: list[str] = field(default_factory=list)  # individual sections named, canonical "MCL x.y"
    # Whole-act spans: "(MCL 460.1 - 460.11)" -> ("MCL 460.1", "MCL 460.11"); "(MCL 722.622 et seq.)" -> (start, None).
    # These say which act is amended, NOT that every section inside the span is — the bill text says which.
    amends_ranges: list[tuple[str, str | None]] = field(default_factory=list)
    amends_acts: list[str] = field(default_factory=list)  # canonical "1972 PA 348"
    creates_new_act: bool = False

    @property
    def is_bill(self) -> bool:
        return self.type in ("HB", "SB")  # resolutions never change the MCL

    @property
    def url(self) -> str:
        return f"https://www.legislature.mi.gov/Home/GetObject?objectName={self.object_name}"


@dataclass
class BillStatusReport:
    date_from: str | None
    date_to: str | None
    rows: list[BillRow]

    def stages(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rows:
            out[r.stage] = out.get(r.stage, 0) + 1
        return out


def parse_bill_status_report(html: str | bytes) -> BillStatusReport:
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main") or soup
    dates = {}
    form = main.find("form")
    if form:
        for inp in form.find_all("input"):
            if inp.get("name") in ("dateFrom", "dateTo"):
                dates[inp["name"]] = inp.get("value") or None
    rows: list[BillRow] = []
    for h3 in main.find_all("h3"):
        stage = " ".join(h3.get_text().split())
        table = h3.find_next_sibling("table")
        if table is None:
            continue
        for tr in table.select("tbody tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            a = tds[0].find("a")
            name = object_name(a.get("href")) if a else None
            m = _BILL_OBJECT.match(name or "")
            if not m:
                continue
            desc = " ".join(tds[1].get_text().split())
            xr = extract_xrefs(desc)
            spans: list[tuple[str, str | None]] = []
            range_keys = [x.key for x in xr if x.kind == "mcl" and x.in_range]
            for i in range(0, len(range_keys) - 1, 2):
                spans.append((range_keys[i], range_keys[i + 1]))
            for em in _ET_SEQ.finditer(desc):
                spans.append((_mcl_key(em.group(1)), None))
            in_span = {k for pair in spans for k in pair if k}
            rows.append(
                BillRow(
                    stage=stage,
                    object_name=name,
                    label=" ".join(a.get_text().split()),
                    year=int(m["year"]),
                    type=m["type"],
                    number=int(m["num"]),
                    description=desc,
                    amends_mcl=sorted({x.key for x in xr if x.kind == "mcl"} - in_span),
                    amends_ranges=spans,
                    amends_acts=sorted({x.key for x in xr if x.kind == "public_act"}),
                    creates_new_act=bool(re.search(r"\bCreates new act\b", desc, re.I)),
                )
            )
    return BillStatusReport(dates.get("dateFrom"), dates.get("dateTo"), rows)
