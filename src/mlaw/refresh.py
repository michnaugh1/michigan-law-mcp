"""Feed-driven refresh: turns the MCL updates feed's event log into a plan of exactly which chapters
need re-fetching, then (optionally) carries that plan out -- instead of a full 241-chapter crawl every
time, poll the feed (the Legislature says it rebuilds every 5 minutes) and re-fetch only what it flagged.

Why this is its own module rather than logic inline in the CLI: resolving events to chapters
(``feed.resolve_refresh_chapters``) is pure and DB-only, so it's cheap to run on every poll; actually
fetching and ingesting a chapter is the expensive, network-touching step, and the two need to stay
separable so a caller can compute and inspect a plan without committing to acting on it (e.g. the CLI's
``mlaw feed`` without ``--refresh-from`` just reports what *would* be refreshed).

Two ways to carry a plan out: ``refresh_from_files`` (offline -- a directory of saved chapter XML,
exactly how this was first validated: the real 2026-09-02 feed sample against the real, on-hand Chapter
333.xml) and ``refresh_from_web`` (live -- ``MclXmlWebSource`` against legislature.mi.gov itself; see that
source's own docstring for how it now resolves a chapter that lives inside a ``MultiChapter`` file, e.g.
the feed flagging "767A" resolving to a fetch of "Chapter 760.xml"). Both run the ingest directly on
whatever connection they're given, which is right for a test or a one-off check; ``mlaw feed
--refresh-from`` / ``--refresh-web`` (the CLI) do the equivalent through ``_safe_ingest`` instead -- the
same copy-the-live-DB-then-atomically-swap-it-in a full ``mlaw ingest`` uses -- because a refresh that
runs on a schedule against a live-serving database needs that same protection, not just this module's own
already-transactional ``run_ingest``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import feed as feedmod
from .pipeline import RunReport, run_ingest, utcnow
from .sources.mcl_web import MclXmlFilesSource, MclXmlWebSource


@dataclass
class RefreshPlan:
    chapters: list[str]  # chapters resolved to need re-fetching (empty: nothing new to do)
    unmapped_acts: list[str]  # act-level/heading events that named an act with no chapter on file yet
    new_events: int  # events this poll actually added (0 on a repeat poll of the same feed build)


def plan_refresh(conn: sqlite3.Connection, feed_xml: bytes | str, now: str | None = None) -> RefreshPlan:
    """Parse and record one feed build (idempotent -- polling the same build twice adds nothing new and
    returns an empty plan the second time, since ``feed.record_feed`` dedupes on build time + title +
    object) and resolve the newly recorded events to the chapters that need re-fetching."""
    parsed = feedmod.parse_feed(feed_xml)
    recorded = feedmod.record_feed(conn, parsed, now or utcnow())
    return RefreshPlan(
        chapters=recorded["refresh_chapters"],
        unmapped_acts=recorded["unmapped_acts"],
        new_events=recorded["new_events"],
    )


def refresh_from_files(
    conn: sqlite3.Connection, plan: RefreshPlan, files_dir: str | Path, *,
    now: str | None = None, removal_guard: float = 0.9,
) -> RunReport | None:
    """Carry out a refresh plan against chapter XML files already saved to ``files_dir``. ``files_dir`` can
    hold every chapter ever saved -- only the ones the plan names (or that share a ``MultiChapter`` file
    with one that's named) are actually read and ingested (``MclXmlFilesSource``'s ``chapters`` filter).

    Returns ``None`` without touching the database if the plan names no chapters -- a poll that saw no new
    events, or one whose events were all unmapped acts, should not run an ingest at all (an ``ingest`` of
    zero chapters would otherwise look like a suspiciously empty *complete* crawl to the removal guard)."""
    if not plan.chapters:
        return None
    source = MclXmlFilesSource(files_dir, chapters=plan.chapters)
    return run_ingest(conn, source, removal_guard=removal_guard, now=now)


def refresh_from_web(
    conn: sqlite3.Connection, plan: RefreshPlan, *, client=None,
    now: str | None = None, removal_guard: float = 0.9,
) -> RunReport | None:
    """Carry out a refresh plan live, against legislature.mi.gov itself, via ``MclXmlWebSource`` instead of
    a saved directory. ``client`` is an already-constructed ``PoliteClient`` (typically the same one used to
    fetch the feed itself, so the whole poll -> refresh cycle shares one rate-limited, robots-checked
    connection) -- pass ``None`` to let the source build its own.

    Same early-return behavior as ``refresh_from_files``: a plan with no chapters touches nothing and
    returns ``None``, so a poll that found nothing new never runs a zero-chapter ingest."""
    if not plan.chapters:
        return None
    source = MclXmlWebSource(client=client, chapters=plan.chapters)
    return run_ingest(conn, source, removal_guard=removal_guard, now=now)
