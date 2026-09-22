"""Command line: init-db, ingest, status, serve."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .config import db_path
from .db import connect, init_db
from .pipeline import run_ingest


def _cmd_init(args) -> int:
    conn = connect(args.db)
    init_db(conn)
    conn.close()
    print(f"initialized {args.db}")
    return 0


def _record_failure(live: Path, source_name: str, err: BaseException) -> None:
    from .pipeline import utcnow

    conn = connect(live)
    now = utcnow()
    conn.execute(
        "INSERT INTO runs (source, started_at, finished_at, status, error) VALUES (?,?,?,?,?)",
        (source_name, now, now, "failed", f"{type(err).__name__}: {err}"),
    )
    conn.commit()
    conn.close()


def _safe_ingest(db: str | Path, source, removal_guard: float):
    """Copy the live DB, ingest ``source`` into the copy, then atomically swap it in. The running server
    keeps reading the old file until the swap, and a failed run leaves the live DB untouched. Shared by
    ``mlaw ingest`` and ``mlaw feed --refresh-from`` (feed-driven refresh, see refresh.py) -- a refresh
    writes fewer documents than a full crawl, but it's still an ingest, and a partial write there would be
    just as visible to a concurrently-serving MCP server as a partial full crawl."""
    live = Path(db)
    work = live.with_suffix(live.suffix + ".new")
    if work.exists():
        work.unlink()
    live.parent.mkdir(parents=True, exist_ok=True)

    if live.exists():
        src = connect(live, readonly=True)
        dst = connect(work)
        src.backup(dst)
        src.close()
    else:
        dst = connect(work)
    init_db(dst)

    try:
        report = run_ingest(dst, source, removal_guard=removal_guard)
    except BaseException as e:
        dst.close()
        # Leave the live DB's data untouched, keep the working copy for inspection, and note the
        # failure in the live DB so data_status() shows it.
        os.replace(work, live.with_suffix(live.suffix + ".failed"))
        if live.exists():
            _record_failure(live, source.name, e)
        raise
    dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    dst.close()
    os.replace(work, live)
    return report


def _client_from_env(**kwargs):
    """Build a ``PoliteClient`` honoring this process's environment the same way ``MclXmlWebSource``'s own
    default client does (``MLAW_MIN_INTERVAL``, ``MLAW_ROBOTS_ERROR_OK`` -- see ``sources/mcl_web.py``).

    Needed because ``_cmd_feed`` builds a client of its own to fetch the feed URL itself, separately from
    any client a source builds -- without this, ``MLAW_ROBOTS_ERROR_OK=1`` would silently have no effect on
    that fetch (real bug, found 2026-09-21: legislature.mi.gov's ``robots.txt`` doesn't error, it hangs --
    confirmed independently with curl -- so a feed fetch with the env var unread just times out and then
    fails closed, exactly as if the flag had never been passed)."""
    from .web import PoliteClient

    kwargs.setdefault("min_interval", max(2.0, float(os.environ.get("MLAW_MIN_INTERVAL", "2"))))
    kwargs.setdefault("timeout", 180.0)
    kwargs.setdefault("robots_error_ok", os.environ.get("MLAW_ROBOTS_ERROR_OK") == "1")
    return PoliteClient(**kwargs)


def _cmd_ingest(args) -> int:
    from .sources import get_source

    source = get_source(args.source, args.path)
    report = _safe_ingest(args.db, source, args.removal_guard)
    print(json.dumps(report.__dict__, indent=2))
    return 0


def _cmd_feed(args) -> int:
    """Parse the MCL updates feed from a saved file or URL and print a summary; with --record,
    append its events to the database's feed_events log and resolve them to the chapters that need
    re-fetching (see refresh.py); with --refresh-from too, actually ingest just those chapters from a
    directory of saved chapter XML, or with --refresh-web, live from legislature.mi.gov itself via
    MclXmlWebSource -- both go through the same atomic copy-and-swap ``mlaw ingest`` uses (see
    refresh.py's refresh_from_files/refresh_from_web, which this mirrors through _safe_ingest instead of
    running the ingest directly).

    When --url and --refresh-web are both given, the feed fetch and the chapter refresh share one
    PoliteClient, so the whole poll -> refresh cycle stays under a single rate limit and robots.txt check
    instead of opening a second connection just to fetch the flagged chapters."""
    from . import feed as feedmod

    if bool(args.file) == bool(args.url):
        raise SystemExit("give exactly one of --file or --url")
    if args.refresh_from and args.refresh_web:
        raise SystemExit("give at most one of --refresh-from or --refresh-web")

    client = None
    try:
        if args.file:
            xml = Path(args.file).read_bytes()
        else:
            client = _client_from_env()
            xml = client.get(args.url).content
        fd = feedmod.parse_feed(xml)
        out = {"summary": feedmod.summarize(fd)}
        if args.record or args.refresh_from or args.refresh_web:
            from .pipeline import utcnow

            conn = connect(args.db)
            init_db(conn)
            recorded = feedmod.record_feed(conn, fd, utcnow())
            conn.close()
            out["recorded"] = recorded
            if args.refresh_from:
                if recorded["refresh_chapters"]:
                    from .sources.mcl_web import MclXmlFilesSource

                    source = MclXmlFilesSource(args.refresh_from, chapters=recorded["refresh_chapters"])
                    out["refresh_ingest"] = _safe_ingest(args.db, source, args.removal_guard).__dict__
                else:
                    out["refresh_ingest"] = "nothing to refresh"
            elif args.refresh_web:
                if recorded["refresh_chapters"]:
                    from .sources.mcl_web import MclXmlWebSource

                    if client is None:  # --file was given, so there's no feed-fetch client to reuse
                        client = _client_from_env()
                    source = MclXmlWebSource(client=client, chapters=recorded["refresh_chapters"])
                    out["refresh_ingest"] = _safe_ingest(args.db, source, args.removal_guard).__dict__
                else:
                    out["refresh_ingest"] = "nothing to refresh"
    finally:
        if client is not None:
            client.close()
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def _cmd_status(args) -> int:
    from . import service

    conn = connect(args.db, readonly=True)
    print(json.dumps(service.data_status(conn), indent=2))
    return 0


def _cmd_serve(args) -> int:
    from .server import build_server, create_http_app

    if args.transport == "stdio":
        build_server().run(transport="stdio")
        return 0
    import uvicorn

    uvicorn.run(create_http_app(args.host, args.port), host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mlaw", description="Michigan law MCP server")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--db", default=db_path())
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db").set_defaults(fn=_cmd_init)

    ing = sub.add_parser("ingest", help="run one ingest into a copy of the DB, then swap it in")
    ing.add_argument("--source", required=True,
                     help="fixture | mcl-files (saved chapter HTML) | mcl-web | mcl-xml-files (saved chapter XML) | mcl-xml-web")
    ing.add_argument("--path", help="fixture dir, or directory of saved chapter HTML/XML; for mcl-web/mcl-xml-web an optional list of chapters, e.g. \"37 38\"")
    ing.add_argument("--removal-guard", type=float, default=0.9)
    ing.set_defaults(fn=_cmd_ingest)

    fd = sub.add_parser("feed", help="parse the MCL updates RSS feed (file or URL)")
    fd.add_argument("--file")
    fd.add_argument("--url")
    fd.add_argument("--record", action="store_true", help="append events to the feed_events log")
    fd.add_argument("--refresh-from", help="directory of saved chapter XML to ingest the affected chapters"
                    " from (implies --record); see refresh.py")
    fd.add_argument("--refresh-web", action="store_true", help="fetch the affected chapters live from"
                    " legislature.mi.gov instead of a saved directory (implies --record); mutually"
                    " exclusive with --refresh-from; see refresh.py")
    fd.add_argument("--removal-guard", type=float, default=0.9)
    fd.set_defaults(fn=_cmd_feed)

    sub.add_parser("status").set_defaults(fn=_cmd_status)

    srv = sub.add_parser("serve")
    srv.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8000)
    srv.set_defaults(fn=_cmd_serve)

    args = p.parse_args(argv)
    os.environ["MLAW_DB"] = args.db  # server reads config from the environment
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
