#!/usr/bin/env python3
"""Dump the Sentencing Guidelines Manual grids as CSV for checking against the PDF by hand.

One CSV per grid (``grid_M2.csv`` .. ``grid_H.csv``) in the output directory, rows in the order you would
walk the printed page with a finger: OV rows top to bottom, within a row PRV A..F left to right, within a
cell base / HO2 / HO3 / HO4 top to bottom. Columns:

    page, class, status, ov_level, ov_points, prv_level, prv_points, min, max, shaded

``min`` / ``max`` are exactly as printed ("17*", "300/L"); ``shaded`` = the max sub-cell for that status
sits on a gray fill (the manual's straddle-cell marking). Then prints a summary: grids, cells per grid,
structural problems and review notes.

Reads the local PDF only; nothing leaves the machine.

    .venv/bin/python scripts/sgm_grid_dump.py [out_dir (default ./sgm_grids)] [path/to/sgm.pdf]
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pdfplumber

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mlaw.sources.sgm import GRID_STATUSES, _locate, _text_pages, parse_grids, pdf_to_text  # noqa: E402

DEFAULT_PDF = Path(__file__).resolve().parent.parent / "tests/data/court_rules/2026-09-29/sgm/sgm.pdf"
COLUMNS = ["page", "class", "status", "ov_level", "ov_points", "prv_level", "prv_points", "min", "max", "shaded"]


def rows(g) -> list[list[str]]:
    out = []
    for ov, ov_pts in g.ov_levels:
        for prv, prv_pts in g.prv_levels:
            for s in GRID_STATUSES:
                c = g.cells.get((ov, prv, s))
                if c is None:
                    out.append([g.page, g.klass, s, ov, ov_pts, prv, prv_pts, "MISSING", "MISSING", ""])
                    continue
                out.append([g.page, g.klass, s, ov, ov_pts, prv, prv_pts, c.min, c.max, "yes" if c.shaded else ""])
    return out


def main(argv: list[str]) -> int:
    out_dir = Path(argv[1] if len(argv) > 1 else "sgm_grids")
    pdf_path = Path(argv[2]) if len(argv) > 2 else DEFAULT_PDF
    warnings: list[str] = []
    _, grid_pages, _ = _locate(_text_pages(pdf_to_text(pdf_path)), warnings)
    with pdfplumber.open(str(pdf_path)) as pdf:
        grids = parse_grids(pdf, grid_pages)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"{pdf_path}: grid pages {grid_pages[0]}-{grid_pages[1]}")
    for w in warnings:
        print(f"  locate warning: {w}")
    for g in grids:
        if not g.pages:
            print(f"Class {g.klass}: NOT FOUND")
        else:
            f = out_dir / f"grid_{g.klass}.csv"
            with f.open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(COLUMNS)
                w.writerows(rows(g))
            print(f"Class {g.klass:>2}  page {g.page}  {len(g.ov_levels)} OV x {len(g.prv_levels)} PRV x 4 = "
                  f"{len(g.cells)} cells  -> {f}")
        for s in g.structural:
            print(f"    STRUCTURAL (stored as a pointer to the PDF): {s}")
        for r in g.review:
            print(f"    review: {r}")
    bad = [g.klass for g in grids if g.structural]
    notes = sum(len(g.review) for g in grids)
    print(f"{len(grids)} grids, {sum(len(g.cells) for g in grids)} cells; "
          f"structural failures: {bad or 'none'}; review notes: {notes}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
