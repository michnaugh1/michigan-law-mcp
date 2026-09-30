# Court-rules test data, captured 2026-09-29

Source: courts.michigan.gov. **Its robots.txt disallows automated access** (`User-agent: * / Disallow: /`),
so nothing in this project fetches from it automatically until the Court answers `docs/msc-inquiry.md`.
These files were retrieved by hand during research on 2026-09-29.

| Path | What | Notes |
|---|---|---|
| `mcr/toc.new.js` | MCR responsive-HTML5 TOC | transcribed verbatim from the server response |
| `mcr/Court_Rules_Chapter_{1..9}.htm` | MCR chapter pages | as served |
| `mre/michigan-rules-of-evidence.pdf` | MRE, footer "effective on 1/28/2026" | sha256 995b4ac4…6df85 |
| `cji/toc.new.js` | M Crim JI top-level TOC (45 entries) | as served |
| `cji/M_Crim_JI_5.1_…htm` | one instruction page | only sample so far; more needed (see plan) |
| `sgm/sentencing-guidelines-manuals.api.excerpt.json` | content-API JSON for the manuals page | **excerpt**: mainBody trimmed to the preamble, Current Version, and first two Former Versions |
| `sgm/sgm.pdf` | Sentencing Guidelines Manual 2026, current through 5/20/2026 | **not committed** (8.7 MB); see `SHA256SUMS`. Tests that need it skip when it is absent. Put the file here by hand. |
