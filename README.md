# michigan-law-mcp

An MCP server that gives Claude and other agents access to Michigan statutes and rules from a
local SQLite store refreshed by a daily job.

MIT-licensed (see `LICENSE`). This repository is the code only: no copy of the Michigan Compiled
Laws is included or distributed. **If you're standing up your own instance**, read `NOTICE` and
`docs/lsb-reply-2026-09-21.md` first -- they cover what the Michigan Legislature has said about
automated access, rate limits, and reuse, as of 2026-09-21. That understanding was reached for
this project specifically; running your own instance means keeping to it yourself (a real
contact address in your crawler's user agent, the requested rate limit, the standing NOTICE) and
confirming nothing has changed since. None of this is legal advice.

**Status: two independent parsers agree on real law; XML is primary, HTML is the cross-check.** The server, schema,
full-text search, version history, change tracking, cross-reference graph, polite crawler and ingest pipeline are
tested (139 tests). Two parsers now read the same chapters -- the Legislature's whole-chapter HTML
(`Home/RenderDoc?objectName=mcl-chapNN`, `chapterdoc.py`) and its own machine-readable XML
(`documents/mcl/Chapter N.xml`, confirmed as the canonical source in `docs/lsb-reply-2026-09-21.md`; `mclxml.py`) --
and Chapter 37 (17 acts and E.R.O.s, 142 sections) parses identically from both, field for field (text, catchlines,
history, compiler's notes, division structure, the repealed range), with zero warnings either way. Chapter 2 exercises
the XML source's edge cases: a `.new` variant section (a law passed but not yet in force, represented as its own
statute entry alongside the real acts), a table embedded inside a paragraph, and two old repealed entries whose
`SectRef` names an extra citation beyond their own `MCLNumber` (kept as a range stub, not dropped). Three more real
chapters (760, 333, 750, 43 MB combined) now parse with zero warnings and zero integrity problems too, confirmed with
a full end-to-end ingest (3,777 documents, 22 chapters, one run); see "Real chapters parsed so far" below for what
each one exercised, including `Chapter 760.xml`'s `MultiChapter` case (one file legitimately spanning 20 chapters).
`MclWebSource` and `MclXmlWebSource` fetch chapters through the polite client (the Legislature has permitted automated
access, see below); `MclXmlWebSource` discovers chapters from the directory listing (`manifest.py`). `mlaw ingest
--source mcl-files|mcl-xml-files` loads chapter files saved by hand. Fixture text is prefixed `[FIXTURE]` and is not
real law.

## Quick start (fixtures)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
export MLAW_DB=data/mlaw.sqlite3
mlaw ingest --source fixture --path fixtures/day1     # day 1
mlaw ingest --source fixture --path fixtures/day2     # day 2: one modified, one added, one removed
mlaw status
pytest
mlaw serve                                            # stdio, for Claude Desktop
```

Real chapter files (no network): save a chapter's HTML from its Download page (the HTML icon) into a folder and run

```bash
mlaw ingest --source mcl-files --path ~/Desktop/MCL/chapters     # every *.html holding a chapter is loaded
mlaw status                                                        # shows the site's "Complete Through PA n of year" stamp
```

Or the Legislature's own XML (save `Chapter N.xml` files, e.g. via the site's "Download Chapter" link or the
directory listing):

```bash
mlaw ingest --source mcl-xml-files --path ~/Desktop/MCL     # every Chapter *.xml is loaded
```

A run over some chapters only touches those chapters: sections missing from a loaded chapter are marked removed,
everything else in the database is left alone. Live crawl (permitted by the Legislature; set a real
contact in the user agent and `MLAW_ROBOTS_ERROR_OK=1`, since robots.txt returns an error): `MLAW_ROBOTS_ERROR_OK=1 MLAW_USER_AGENT="north-coast-legal-mlaw/0.1 (mike@thenorthcoastlegal.com)" mlaw
ingest --source mcl-xml-web` (all chapters, discovered from the directory listing, 2 s between requests; `--source
mcl-web` is the HTML equivalent) or `--path "37 38"` for specific chapters.

Claude Desktop (stdio) config:

```json
{ "mcpServers": { "michigan-law": {
    "command": "/path/to/.venv/bin/mlaw", "args": ["serve"],
    "env": { "MLAW_DB": "/path/to/data/mlaw.sqlite3" } } } }
```

Remote (team) mode: `mlaw serve --transport http --host 127.0.0.1 --port 8000` behind TLS
(`deploy/Caddyfile.example`), with `MLAW_BEARER_TOKEN` set. Endpoint `/mcp`, health check `/healthz`.
The bearer token is a stopgap; check which auth schemes Claude's custom-connector flow accepts for
remote servers before rollout. Updates: `deploy/mlaw-feed-refresh.{service,timer}` polls the real
`MCLupdate.xml` feed hourly and re-fetches live, via `MclXmlWebSource`, only the chapters it flags;
`deploy/mlaw-ingest.{service,timer}` runs underneath it as a nightly full-crawl safety net (unrestricted
`mcl-xml-web`, 04:30 America/Detroit). See "Feed-driven refresh" below.

## What the source site looks like (from saved pages and the full updates feed)

* Hierarchy: chapter (or chapter group, e.g. `mcl-chapters-760-777`) -> act or E.R.O.
  (`mcl-Act-175-of-1927`) -> division (`mcl-175-1927-II`, description carries its citation range
  "(762.1...762.16)") -> section (`Home/GetObject?objectName=mcl-750-83`). Initiated laws also exist.
* Every page shows a currency banner, "Michigan Compiled Laws Complete Through PA 91 of 2026". That is
  the site's own freshness stamp; `mclsite.parse_currency` reads it.
* Every level has a "Download ..." link to `Home/Document?objectName=...`. For a section that page offers an
  HTML rendering (`Home/RenderDoc?objectName=...`) and a static PDF at a predictable path
  (`documents/mcl/pdf/MCL-750-145P.pdf`, upper-case). The **chapter**-level download page
  (`Home/Document?objectName=mcl-chap37`) offers the same pair for a whole chapter
  (`Home/RenderDoc?objectName=mcl-chap37`, `documents/mcl/pdf/MCL-CHAP37.pdf`) and has Previous/Next Document
  links, so the chapters can be walked without an index. If the chapter-level HTML holds every section with
  usable structure, the full MCL is a few hundred requests instead of tens of thousands. **The RenderDoc HTML
  for a chapter has not been seen yet**; the statute-level download page is still unseen too.
* Daily Bill Status Report (`Home/DailyBillStatus?dateFrom=…&dateTo=…`): bills grouped by stage (Introduced,
  Passed by Chamber, Enrolled, Adopted; others may exist) with one-line descriptions such as "Amends sec. 9 of
  1972 PA 348 (MCL 554.609)". `billstatus.py` parses it into rows with the statutes each bill touches. This is
  *pending legislation*, not law; the plan is a clearly-labelled `pending_legislation` tool, never mixed into text.
* Section pages: catchline heading (`750.145p Caregiver, ...`), body paragraphs beginning "Sec. 145p.", then
  labeled notes ("History:", "Compiler's Notes:"). A repealed section keeps its status in the heading
  ("Repealed. 2010, Act 96, Imd. Eff. June 22, 2010.") and may have no body. Division pages list their
  sections with catchlines. Statutes cite each other as "being section 400.11b of the Michigan Compiled Laws",
  which the cross-reference extractor understands.
* The footer links the three RSS feeds (bills, meetings, laws) and an Acceptable Use Policy at
  `/Home/AcceptableUse`. Its four guidelines: respect copyright/licence in the data; do not use the Services in a way
  that "precludes or significantly hinders use by others"; do not violate laws; respect others' privacy. It does not
  mention automated access either way, so we crawl slowly, identify ourselves, and ask (see `docs/lsb-inquiry.md`).
* Variant nodes. The Legislature confirmed that `.amended`, `.added` and `.new` sections display changes made by laws that
  have passed but are not yet in effect (for example Section 2.13.new, added by PA 7 of 2026, takes effect 91 days after
  adjournment). `mcl-333-16335-amended` (saved page) is the same citation with a red notice, "THIS AMENDED SECTION IS
  EFFECTIVE JANUARY 22, 2028": future text next to the ordinary node that holds today's text. The bracket form
  (`mcl-333-5474c[1]`) was not addressed in the reply and stays unconfirmed. Note that `.new` sections have no ordinary
  node yet, so `get_document` must not say "not found" for them (to do). We store variants as separate rows, never in the
  search index, and `get_document` returns them under `other_forms` with the effective date and a caution.
  Whether chapter-level RenderDoc files include variants is not yet known (Chapter 37 has none).
* Tables sit inline inside a section (`<table><tr><td>` between paragraphs) and the next subsection can be a
  loose text node after `</table>`. An earlier version of the parser silently dropped that subsection; the
  section parser now visits every node and compares its output with all the text in the section, and a
  chapter whose sections fail that check aborts the run instead of storing truncated law.
* Chapter files also contain range lines for wholly repealed acts ("37.1-37.9 Repealed. 1976, Act 453, ...");
  these are stored as ranges so `get_document("MCL 37.5")` can explain instead of saying "not found".
* Notes after the History line ("Compiler's Notes", "Admin Rule", "Constitutionality") are kept verbatim;
  the Constitutionality note (e.g. a provision held unconstitutional) is exactly what a lawyer needs to see.
* Act and section histories use a compact grammar ("Am. 1980, Act 506, Imd. Eff. Jan. 22, 1981") that
  `history.py` parses, including effective dates.

## Update feed

`mlaw feed --file saved.xml [--record]` (or `--url ...`) parses the "MCL updates" RSS feed and can log its
events to `feed_events`. Findings from the full 2026-09-02 build (2,410 items, saved as a regression sample):
all one re-load of the Public Health Code (chapter 333): 2,209 section events, 137 heading nodes, 29 act-level
deletions, 27 E.R.O. deletions, plus initiated laws and 26 variant nodes. Items carry only a catchline (no text,
no effective date) and share one pubDate per build, so it is a batch id, not a change date. Volume is not a
measure of legal change. The feed tells the daily job which sections and acts to re-fetch; it never edits stored
text itself. Unknown title shapes are kept as `unrecognized`. Per the Legislature: items stay in the feed for one day
(a missed day is a gap, so keep a periodic full refresh), "deleted" means only "removed from the website" (not repealed),
and a modified document currently shows up as deleted then added instead of "updated" (they have logged a fix).

## The XML directory (primary source)

`https://legislature.mi.gov/documents/mcl/` is an IIS directory listing of one `Chapter N.xml` per chapter (241 files,
480 MB in total on 2026-09-21; largest Chapter 324 at 38 MB, Chapter 333 at 24 MB), plus `pdf/`, `xml/` (2005) and
`archive/` (monthly snapshots, one directory per year 2013-2026). `manifest.py` parses the listing: one request tells
the daily job which chapters exist and which changed (size or time differs from the last parsed copy), and vanished
files are reported, never turned into repeals. There is no `Chapter 764.xml` to `Chapter 777.xml`; confirmed against
a fetched copy that `Chapter 760.xml` (11.7 MB) holds them, via its own `<MultiChapter>true</MultiChapter>` flag --
one file declared "760" actually spans 760, 761, 762, 763, 764, 765, 766, 767, 767A, 768, 769, 770, 771, 771A, 772,
773, 774, 775, 776, and 777 (672 sections). Listing times have no time zone. Caution: file dates are per chapter and
can be old (the saved Chapter 37.xml is dated 2026-02-06 while the site says it is complete through PA 91 of 2026)
-- **the files carry no currency stamp of their own**, unlike the HTML rendering's "Complete Through PA ## of ####"
banner, so `MclXmlFilesSource`/`MclXmlWebSource` always report `currency=None`. Rely on the directory listing's own
modified time, or an occasional cross-check against the HTML rendering, for freshness.

`mclxml.py` parses `MCLChapterInfo` -> `MCLStatuteInfo` (one per act/E.R.O./resolution, and, for a variant section, a
whole extra statute entry standing in for the amending act) -> `MCLSectionInfo` / `MCLDivisionInfo`, and the embedded
pseudo-XML inside `BodyText` (`Section-Number`, `Paragraph`/`Paragraph-Number`/`P`, and an occasional literal `<table>`
either inside a `<P>` or, seen only in Chapter 333.xml, as a direct child of `<Paragraph>` with no `<P>` at all).
Verified byte-for-byte against the HTML parser on Chapter 37 (see Status above); Chapter 2 and the fully-repealed
Chapter 131 exercise variants, tables, and the `SectRef`-names-an-extra-citation edge case; Chapter 760 exercises
`MultiChapter` (one file, many chapters -- each section's own citation decides its stored chapter, not the file's
declared one); Chapter 333 exercises `.amended`/`.added`/`[1]` variants, the direct-`<Paragraph>` table shape, and
1,883 editor's notes that are bare text with no embedded markup at all. Structured `History`/`HistoryInfo`/`Legislation`
data exists in the XML (effective dates, PA numbers, session type) but is not yet consumed -- current text comes
first, per plan; that data is there for the historical/`as_of` backfill later.

### Real chapters parsed so far

| Chapter | Size | What it exercised |
|---|---|---|
| 37 (Civil Rights) | 1.2 MB | Baseline cross-check against the HTML parser: byte-for-byte match, 17 statutes, 142 sections. |
| 2 | small | `.new` variant section as its own statute entry; a table nested inside `<P>`; `SectRef` naming an extra citation. |
| 131 (Municipal Finance Act, fully repealed) | small | A chapter with no current sections at all -- just a repealed range. |
| 760 (Code of Criminal Procedure) | 11.7 MB | `MultiChapter=true`: one file spanning 20 chapters (760-777, 767A, 771A), 672 sections. |
| 333 (Public Health Code) | 24 MB | `.amended`/`.added`/`[1]` variants (8/15/2); a table as a direct `<Paragraph>` child; 1,883 bare-text editor's notes. |
| 750 (Michigan Penal Code) | 8.7 MB | A large, structurally ordinary chapter (896 sections) -- clean confirmation before it's used for cross-reference validation. |

## robots.txt and permission

The Legislature replied on 2026-09-21 (`docs/lsb-reply-2026-09-21.md`): automated access is permitted, requests should
be rate-limited to at most 1 per second (may change), storing and serving a copy is fine with no wording requirements, and
they do not believe they serve a robots.txt (the 502 seen by hand is a gateway error). The crawler still fails closed by
default; set `MLAW_ROBOTS_ERROR_OK=1` to proceed when robots.txt errors. A robots.txt that does load is still honored.
Requests are spaced 2 s apart (`MLAW_MIN_INTERVAL`, never below 2). Keep the reply on file; counsel should confirm the wording
if the tool is offered beyond your own office.

**Confirmed live, 2026-09-21** (with `curl`, independent of this project's own code): `robots.txt` doesn't
actually return that 502 (or anything else) to an automated request -- it just hangs, 0 bytes, indefinitely.
So `PoliteClient` gives that one probe its own short `robots_timeout` (15s default, separate from the main
request timeout) rather than waiting out the full timeout to learn the same thing every single run. Both
`deploy/*.service` units set `MLAW_ROBOTS_ERROR_OK=1`; if you're running `mlaw` by hand, you need it too.

## TLS

A cloud fetcher, `wget` on a Mac, and this session's own `WebFetch` tool have all independently rejected
www.legislature.mi.gov's certificate (issuer DigiCert G2 TLS RSA SHA256 2020 CA1: "unable to locally verify
the issuer"), which suggests the server does not send its intermediate certificate. The crawler never
disables verification. Set `MLAW_CA_BUNDLE` (certifi bundle plus the intermediate) or `MLAW_USE_SYSTEM_TRUST=1`.
Confirm the cause with:
`openssl s_client -connect www.legislature.mi.gov:443 -showcerts </dev/null | grep -E '^ *(s|i):'`
(one certificate listed means the chain is incomplete).

**Confirmed live, 2026-09-21**: `MLAW_USE_SYSTEM_TRUST=1` fixes this for a real Python `httpx` client, not
just non-Python fetchers. `truststore` is now a base dependency (`pyproject.toml`), not an optional extra to
remember -- both `deploy/*.service` units set this env var already.

## Tools

| Tool | Purpose |
| --- | --- |
| `get_document` | Full text by citation (`MCL 750.83`, `MCR 6.110`, `MRE 404`, `Const 1963, art 1, § 17`); optional `as_of` date |
| `search` | Full-text (stemmed, phrases, AND/OR/NOT) with source / chapter filters |
| `get_chapter_outline` | Sections in a chapter, citation order, paged |
| `get_cross_references` | What a provision cites, and what cites it |
| `list_changes` | Added / modified / removed since a date |
| `lookup_public_act` | Public Act metadata and affected citations |
| `data_status` | Freshness per source |

## How it stays trustworthy

* Every response carries provenance (source URL, last confirmed, first observed) and an
  unofficial-copy notice. Confirm the notice wording against the source site's own disclaimer.
* Versions are kept whenever text changes. `as_of` answers "what had we observed by this date",
  **not** "what was legally effective"; history begins at the first ingest.
* A complete crawl that sees under 90% of previously known sections aborts and changes nothing, so a
  half-loaded site cannot look like a mass repeal. The daily job ingests into a copy of the database
  and swaps it in atomically; failures are recorded and shown by `data_status`.
* The crawler honors robots.txt (and fails closed if it cannot read it), identifies itself, rate-limits,
  and uses conditional requests. Set `MLAW_USER_AGENT` to include a real contact address.

## To do, in order

1. **Done, and confirmed live.** Feed-driven refresh has a live path: `refresh.refresh_from_web()` and
   `mlaw feed --url ... --refresh-web` poll the real `MCLupdate.xml` URL and fetch flagged chapters live via
   `MclXmlWebSource`, exactly mirroring the offline `--refresh-from` path through the same atomic
   copy-and-swap. Cadence decided and deployed: feed poll hourly, full crawl nightly
   (`deploy/mlaw-feed-refresh.timer`, `deploy/mlaw-ingest.timer`). Proven with mocked HTTP
   (`tests/test_refresh.py`) and, 2026-09-21, against the real live site end to end (real feed parsed and
   resolved correctly, a real chapter fetched and ingested live) -- see "Live-wiring verification" below for
   what was confirmed and the two real bugs (plus one deploy-config gap) that verification caught and fixed.
   Read the equivalents for courts.michigan.gov.
2. **Done.** Cross-reference validation against 6 real chapters (750, 760-777, 333, 37, 2, 131 -- 3,994
   documents, 2,383 MCL xrefs, 1,162 Public Act xrefs). Found and fixed two bugs in `citations.py`: a
   citation-list scan would sweep up an unrelated decimal figure (e.g. "MCL 257.625, 0.02 grams...") as a
   bogus "MCL 0.02" citation, since chapter "0" was never rejected; and Public Act citations written as
   "Act No. 267 of the Public Acts of 1976" or the bare "Act 368 of 1978" (the common form in real body
   text, alongside "1978 PA 368") weren't recognized at all. Both fixed with real-corpus regression tests.
   Zero false negatives found for MCL/MCR/MRE literal citations. Known, accepted limitation (not fixed, too
   rare/ambiguous to be worth it): a "being section 17766a of the Michigan Compiled Laws" clause with no
   chapter-number prefix (1 occurrence in the corpus). Follow-up done: the `ranges`-table gap this
   validation surfaced (item 3 below) is now closed too.
3. **Done.** `ranges`-table coverage gap closed. The compilers sometimes declare a span of sections
   repealed/expired in running prose rather than the act/chapter-level stub line `_stub_ranges` already
   caught from a LongTitle ("37.1-37.9 Repealed. 1976, Act 453..."): a whole section can BE a narrative
   repeal notice for a *different* span (e.g. MCL 333.13607: "Sections 13601 to 13606 ..., being sections
   333.13601 to 333.13606 of the Michigan Compiled Laws, are repealed effective December 31, 1993"), or a
   Compiler's Note on some other, often unrelated, live section can say "Former MCL 750.85, which pertained
   to ..., was repealed by Act 266 of 1974" or "Former MCL 333.20701-333.20773 Expired...". New
   `citations.extract_narrative_ranges()` recognizes both shapes (39 real hits across the 6-chapter corpus:
   2 in-body, 37 Compiler's Notes -- 35 single citations, 2 ranges) and both `chapterdoc.py` and `mclxml.py`
   now call it per section, feeding the same `ranges` table the act-level stubs already do.
   `service.get_cross_references()` now also checks `ranges` for an unresolved MCL reference and, when
   found, attaches a `known_repealed_range` field instead of a bare `"in_database": false` -- so an agent
   asking why a citation didn't resolve gets "the compilers themselves say this was repealed" rather than
   an unexplained miss. The two confirmed typos in the Legislature's own XML (a citation to a
   chapter/section number that never existed) still correctly show no `known_repealed_range` and remain
   genuinely unresolved, as they should -- this doesn't paper over real errors, only recognizes real,
   compiler-stated repeals.
4. Deeper structural verification of the `.amended`/`.added`/`[1]` variants confirmed in Chapter 333.xml, the way
   `.new` was verified against Chapter 2's state duck example.
5. Constitution, then court rules and Rules of Evidence (PDF), then Public Acts.
6. Historical/`as_of` backfill from the monthly archives (2013-2026) and the structured `History`/`HistoryInfo`
   data already sitting in the chapter XML, once current-text ingest has run cleanly for a while.
7. Hosting, auth, and rollout to the team.

### Feed-driven refresh

`MCLupdate.xml` (the Legislature's own feed, rebuilt every 5 minutes per its channel description) reports
database events, not law changes -- see `feed.py`'s module docstring. `feed.resolve_refresh_chapters()`
turns a batch of new events into the actual MCL chapters that need re-fetching: a section-level event names
its own chapter directly; an act-level or heading event is resolved by looking up which chapter(s) that
act's `documents` (or, for a fully repealed/expired act, its `ranges` stub -- both are checked, a real gap
the first version had until it was tested against real data, see Status) currently occupy. Anything that
resolves to nothing is reported as `unmapped_acts` rather than guessed at. `refresh.py` ties this to an
actual re-ingest: `MclXmlFilesSource` gained a `chapters` filter so a refresh can point at one directory
holding every chapter ever saved and only read the ones flagged (a `MultiChapter` file like Chapter 760.xml
is pulled in whole if any one of its chapters is wanted, since there's no cheaper fetch unit). From the CLI,
`mlaw feed --file/--url ... --refresh-from DIR` parses the feed, records new events, resolves the plan, and
-- if it names any chapters -- re-ingests just those, through the same atomic copy-the-live-DB-then-swap-it-in
`mlaw ingest` uses. Proven against the real 2,410-item feed sample and the real Chapter 333.xml: resolves to
exactly `["333"]` with zero unmapped acts, and a repeat poll of the same build is a true no-op.

**Live wiring (2026-09-21):** the same shape now exists against the live site instead of a saved directory --
`refresh.refresh_from_web()` and `mlaw feed --url ... --refresh-web` build (or reuse, when `--url` was also
used to fetch the feed itself) one `PoliteClient` and hand it to `MclXmlWebSource` with exactly the chapters
the plan named. `MclXmlWebSource`'s own former known limitation (a `MultiChapter` chapter requested by an
explicit chapter list, e.g. the feed flagging "767A") is fixed too: `_KNOWN_MULTI_CHAPTER_FILES` resolves the
request to the real file (`Chapter 760.xml`) and declares the file's whole real coverage up front, so this no
longer fails even when the feed happens to flag a chapter living inside that file. Covered by mocked-HTTP
tests (`tests/test_mclxml.py`, `tests/test_refresh.py`); see "Live-wiring verification" just below for what
running this against the real site still needs.

### Live-wiring verification

**Confirmed live end to end, 2026-09-21, by Mike running it for real** (from a session with no real internet
access of its own, everything up to this point had only been proven with mocked HTTP or a browser -- see
below for what that gap looked like and how it closed):

- `MLAW_USE_SYSTEM_TRUST=1` (the `truststore` package, now a base dependency) does fix legislature.mi.gov's
  incomplete certificate chain for a real Python `httpx` client, not just `curl`/`wget`.
- `mlaw feed --url https://legislature.mi.gov/documents/publications/RssFeeds/MCLupdate.xml --record` parses
  the real live feed correctly and resolves it to the right chapter (`refresh_chapters: ["333"]`), matching
  the saved 2026-09-02 sample -- the feed hadn't actually rebuilt with new content since then, which is itself
  a useful data point (it rebuilds on real database changes, not on a fixed clock, despite what its own
  channel description implies).
- `mlaw ingest --source mcl-xml-web --path "333"` fetched the real, live `Chapter 333.xml` (~24 MB) and
  ingested it cleanly: 2,209 new documents, matching the live feed's own count for that chapter exactly.
- `mlaw feed --url ... --refresh-web` correctly reused one client for the feed fetch and the (in this case
  skipped, since no new events) chapter refresh, and correctly reported "nothing to refresh" rather than
  re-fetching a build already recorded -- the offline idempotency guarantee holds against the live feed too.

Two real bugs turned up along the way, both fixed:

- **`robots.txt` doesn't error, it hangs** -- confirmed independently with `curl` (0 bytes, no response, for
  as long as you'll wait), not just this client. `PoliteClient` now gives that one probe its own short
  timeout (`robots_timeout`, 15s default) instead of burning the full request timeout finding this out on
  every run against a new host -- see "robots.txt and permission" below.
- **`mlaw feed --url ...` silently ignored `MLAW_ROBOTS_ERROR_OK`.** The CLI built its own `PoliteClient` for
  fetching the feed URL, separately from the one a source builds, and only the latter ever read that env
  var -- so setting it had no effect on the feed fetch itself, which then failed closed exactly as if it had
  never been passed. Fixed (`_client_from_env()` in `cli.py`); `mlaw feed --url ...`, with or without
  `--refresh-web`, now honors it correctly.

A third issue was caught by inspection once the first one made it obvious to look: neither
`deploy/mlaw-ingest.service` nor `deploy/mlaw-feed-refresh.service` set `MLAW_ROBOTS_ERROR_OK` or
`MLAW_USE_SYSTEM_TRUST` at all, and neither set a `TimeoutStartSec` -- so, unfixed, both scheduled jobs would
have failed closed on `robots.txt` in production, and even once that was fixed, systemd's 90-second default
would have killed the nightly full crawl (8+ minutes minimum) and, before the 15s `robots_timeout` fix, the
hourly job too. Both unit files now set both env vars and a generous `TimeoutStartSec`.

If you haven't yet: `systemctl daemon-reload` and re-enable both timers to pick up these unit file changes
before relying on the schedule.
