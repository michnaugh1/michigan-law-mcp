"""Parse Michigan compilers' history strings such as
    "1927, Act 175, Eff. Sept. 5, 1927 ;-- Am. 1980, Act 506, Imd. Eff. Jan. 22, 1981"
into structured entries. Only the grammar seen so far is understood (from an act-level history);
anything else is kept verbatim in ``raw`` so nothing is lost. Effective dates are ISO only when the
text is a plain calendar date."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_ENTRY = re.compile(
    r"^(?:(?P<op>Am|Add|Rep|Comp|Sub)\.?\s+)?(?P<year>\d{4}),\s*Act\s+(?P<act>\d+)"
    r"(?:,\s*(?P<imd>Imd\.\s*)?Eff\.\s*(?P<eff>.+?))?\s*$",
    re.I,
)
_OPS = {"am": "amended", "add": "added", "rep": "repealed", "comp": "compiled", "sub": "substituted"}


@dataclass(frozen=True)
class HistoryEntry:
    kind: str  # enacted | amended | added | repealed | ... | other
    raw: str
    act_citation: str | None = None  # "1980 PA 506"
    immediate_effect: bool = False
    effective_raw: str | None = None
    effective_iso: str | None = None


def _iso(text: str | None) -> str | None:
    if not text:
        return None
    m = re.match(r"^([A-Za-z]+)\.?\s+(\d{1,2}),\s*(\d{4})\.?$", text.strip())
    if not m:
        return None
    mon = _MONTHS.get(m.group(1)[:3].lower())
    if not mon:
        return None
    try:
        return date(int(m.group(3)), mon, int(m.group(2))).isoformat()
    except ValueError:
        return None


def parse_history(text: str) -> list[HistoryEntry]:
    parts = [p.strip() for p in re.split(r"\s*;\s*--\s*|\s*;--\s*", " ".join(text.split())) if p.strip()]
    out: list[HistoryEntry] = []
    for i, part in enumerate(parts):
        m = _ENTRY.match(part)
        if not m:
            out.append(HistoryEntry("other", part))
            continue
        op = m.group("op")
        kind = _OPS.get(op.lower(), "other") if op else ("enacted" if i == 0 else "other")
        eff = m.group("eff")
        out.append(HistoryEntry(
            kind=kind, raw=part, act_citation=f"{m.group('year')} PA {int(m.group('act'))}",
            immediate_effect=bool(m.group("imd")), effective_raw=eff.strip() if eff else None,
            effective_iso=_iso(eff),
        ))
    return out
