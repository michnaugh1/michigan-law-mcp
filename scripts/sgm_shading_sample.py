#!/usr/bin/env python3
"""Print a hand-check sample of the Sentencing Guidelines Manual felony list parse.

Shows 10 rows detected as shaded (current = no; "statute no longer current") and 10 unshaded rows, spread
evenly through the numerical list (deterministic: evenly spaced picks, so the same rows every run), with the
PDF page to open for each. Then the totals and every disagreement the parser flagged for review.

Reads the local PDF only; nothing leaves the machine.

    .venv/bin/python scripts/sgm_shading_sample.py [path/to/sgm.pdf]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pdfplumber

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mlaw.sources.sgm import _locate, _text_pages, parse_felony_list, pdf_to_text  # noqa: E402

DEFAULT_PDF = Path(__file__).resolve().parent.parent / "tests/data/court_rules/2026-09-29/sgm/sgm.pdf"


def spread(rows: list, n: int = 10) -> list:
    """n rows at evenly spaced positions (first and last included), so the sample covers the whole list."""
    if len(rows) <= n:
        return rows
    return [rows[round(i * (len(rows) - 1) / (n - 1))] for i in range(n)]


def cut(s: str, width: int) -> str:
    s = s or ""
    return s if len(s) <= width else s[: width - 1] + "…"


def table(rows: list) -> None:
    head = f"{'page':>4}  {'raw cite':<24} {'class':<5} {'category':<8}  {'description':<52}  amendments"
    print(head)
    print("-" * len(head))
    for r in rows:
        print(f"{r.page:>4}  {cut(r.raw_cite, 24):<24} {r.offense_class:<5} {r.category:<8}  "
              f"{cut(r.description, 52):<52}  {cut(r.amendments, 50)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", nargs="?", type=Path, default=DEFAULT_PDF)
    args = ap.parse_args()
    if not args.pdf.is_file():
        print(f"no such PDF: {args.pdf}", file=sys.stderr)
        return 2

    warnings: list[str] = []
    _, _, pages = _locate(_text_pages(pdf_to_text(args.pdf)), warnings)
    with pdfplumber.open(str(args.pdf)) as pdf:
        fl = parse_felony_list(pdf, pages)

    shaded = [r for r in fl.rows if not r.current]
    unshaded = [r for r in fl.rows if r.current]
    print(f"Felony list pages {pages[0]}-{pages[1]} of {args.pdf}")
    print(f"Numerical list rows: {len(fl.rows)}   shaded (current = no): {len(shaded)}   "
          f"unshaded: {len(unshaded)}   alphabetical-list rows (cross-check): {len(fl.alphabetical)}")
    print()
    print("SHADED rows (parser says: statute no longer current) -- each should be light gray in the PDF")
    table(spread(shaded))
    print()
    print("UNSHADED rows (parser says: current) -- each should be white in the PDF")
    table(spread(unshaded))
    print()
    print(f"Structural parse failures: {len(fl.structural)}")
    for s in fl.structural:
        print("  !", s)
    print(f"Disagreements flagged for review: {len(fl.review)}")
    for s in fl.review:
        print("  -", s)
    for w in warnings:
        print("  page-location warning:", w)
    return 1 if fl.structural else 0


if __name__ == "__main__":
    raise SystemExit(main())
