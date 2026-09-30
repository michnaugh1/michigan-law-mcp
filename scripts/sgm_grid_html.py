"""Checkpoint B aid: redraw every sentencing grid next to an image of its PDF page.

    .venv/bin/python scripts/sgm_grid_html.py [out_dir] [path/to/sgm.pdf]

Writes out_dir/index.html (default ./sgm_grid_check) plus one PNG per grid page (rendered with
pdftoppm). Open index.html in a browser: each grid is shown twice, as extracted (left) and as printed
(right), in the same layout -- OV rows, PRV columns, the minimum, then the base/HO2/HO3/HO4 maxima,
with asterisks kept and shaded cells gray -- so every cell can be compared by eye. Local only; nothing
is uploaded.
"""

from __future__ import annotations

import html
import subprocess
import sys
from pathlib import Path

import pdfplumber

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mlaw.sources.sgm import GRID_STATUSES, parse_grids  # noqa: E402

DEFAULT_PDF = Path(__file__).resolve().parents[1] / "tests/data/court_rules/2026-09-29/sgm/sgm.pdf"
GRID_PAGES = (125, 133)

CSS = """
body { font-family: system-ui, sans-serif; margin: 16px; color: #111; background: #fff; }
h2 { margin-top: 40px; border-top: 2px solid #999; padding-top: 12px; }
.pair { display: flex; gap: 24px; align-items: flex-start; flex-wrap: wrap; }
table.g { border-collapse: collapse; font: 13px/1.25 "Times New Roman", serif; }
table.g th, table.g td { border: 1px solid #333; padding: 2px 6px; text-align: center; }
table.g td.min { font-size: 20px; min-width: 34px; }
table.g td.max { min-width: 34px; }
.shaded { background: #d0d0d0; }
.status { font-size: 11px; color: #333; }
img { border: 1px solid #999; max-width: 720px; width: 100%; }
.note { color: #555; font-size: 13px; max-width: 1400px; }
"""


def grid_table(g) -> str:
    out = ['<table class="g"><tr><th rowspan="2">OV Level</th>']
    for letter, _ in g.prv_levels:
        out.append(f'<th colspan="2">{html.escape(letter)}</th>')
    out.append('<th rowspan="2">Offender<br>Status</th></tr><tr>')
    for _, pts in g.prv_levels:
        out.append(f'<th colspan="2" class="status">{html.escape(pts)}</th>')
    out.append("</tr>")
    for ov, ov_pts in g.ov_levels:
        for i, status in enumerate(GRID_STATUSES):
            out.append("<tr>")
            if i == 0:
                out.append(f'<th rowspan="4">{html.escape(ov)}<br><span class="status">'
                           f"{html.escape(ov_pts)}</span></th>")
            for prv, _ in g.prv_levels:
                cell = g.cells[(ov, prv, status)]
                if i == 0:
                    min_cls = "min shaded" if g.min_shaded.get((ov, prv)) else "min"
                    out.append(f'<td rowspan="4" class="{min_cls}">{html.escape(cell.min)}</td>')
                cls = "max shaded" if cell.shaded else "max"
                out.append(f'<td class="{cls}">{html.escape(cell.max)}</td>')
            label = "" if status == "base" else status
            out.append(f'<td class="status">{label}</td></tr>')
    out.append("</table>")
    return "".join(out)


def main() -> int:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "sgm_grid_check")
    pdf_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PDF
    out_dir.mkdir(parents=True, exist_ok=True)
    with pdfplumber.open(pdf_path) as pdf:
        grids = parse_grids(pdf, GRID_PAGES)
    parts = [f"<!doctype html><meta charset='utf-8'><title>SGM grid check</title><style>{CSS}</style>",
             "<h1>Sentencing grids: extracted (left) vs. the PDF (right)</h1>",
             f"<p class='note'>Source: {html.escape(str(pdf_path))}. Page numbers are PDF page numbers. "
             "Gray = shaded (straddle) cell; * = intermediate sanction cell. Compare every cell; "
             "note any difference by class, OV level, PRV level, and offender status.</p>"]
    for g in grids:
        page = g.pages[0]
        png_base = out_dir / f"p{page}"
        subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-r", "100", "-png", "-singlefile",
                        str(pdf_path), str(png_base)], check=True, timeout=120)
        problems = "".join(f"<li>{html.escape(p)}</li>" for p in (g.structural + g.review))
        parts.append(f"<h2>Class {html.escape(g.klass)} &mdash; PDF page {page}</h2>"
                     f"<p class='note'>{html.escape(g.title)}</p>"
                     + (f"<ul class='note'>{problems}</ul>" if problems else "")
                     + f"<div class='pair'><div>{grid_table(g)}</div>"
                     f"<div><img src='p{page}.png' alt='PDF page {page}'></div></div>")
    (out_dir / "index.html").write_text("\n".join(parts), encoding="utf-8")
    print(f"wrote {out_dir / 'index.html'} ({len(grids)} grids)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
