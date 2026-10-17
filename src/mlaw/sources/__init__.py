"""Source registry."""

from __future__ import annotations

from ..models import Source


def get_source(kind: str, path: str | None = None) -> Source:
    if kind == "fixture":
        from .fixture import FixtureSource

        if not path:
            raise ValueError("fixture source needs --path")
        return FixtureSource(path)
    if kind == "mcl-files":
        from .mcl_web import MclFilesSource

        if not path:
            raise ValueError("mcl-files source needs --path (a directory of saved chapter renderings)")
        return MclFilesSource(path)
    if kind == "mcl-web":
        from .mcl_web import MclWebSource

        chapters = [c for c in (path or "").replace(",", " ").split() if c] or None  # --path "37 38" limits the crawl
        return MclWebSource(chapters=chapters)
    if kind == "mcl-xml-files":
        from .mcl_web import MclXmlFilesSource

        if not path:
            raise ValueError("mcl-xml-files source needs --path (a directory of saved chapter XML files)")
        return MclXmlFilesSource(path)
    if kind == "mcl-xml-web":
        from .mcl_web import MclXmlWebSource

        chapters = [c for c in (path or "").replace(",", " ").split() if c] or None  # --path "37 38" limits the crawl
        return MclXmlWebSource(chapters=chapters)
    if kind == "mcr-files":
        from .mcr import McrFilesSource

        if not path:
            raise ValueError("mcr-files source needs --path (a directory holding toc.new.js and the chapter files)")
        return McrFilesSource(path)
    if kind == "mcr-web":
        from .mcr import McrWebSource

        return McrWebSource()
    if kind == "mre-files":
        from .mre import MreFilesSource

        if not path:
            raise ValueError("mre-files source needs --path (the saved Michigan Rules of Evidence PDF)")
        return MreFilesSource(path)
    if kind == "mre-web":
        from .mre import MreWebSource

        return MreWebSource(_courts_client())
    if kind == "cji-files":
        from .cji import CjiFilesSource

        if not path:
            raise ValueError("cji-files source needs --path (the saved M Crim JI PDF; robots.txt bars crawling it)")
        return CjiFilesSource(path)
    if kind == "sgm-files":
        from .sgm import SgmFilesSource

        if not path:
            raise ValueError("sgm-files source needs --path (the saved Sentencing Guidelines Manual PDF)")
        return SgmFilesSource(path)
    if kind == "sgm-web":
        from .sgm import SgmWebSource

        return SgmWebSource()
    raise ValueError(f"unknown source {kind!r} (choose: {', '.join(SOURCE_KINDS)})")


SOURCE_KINDS = ("fixture", "mcl-files", "mcl-web", "mcl-xml-files", "mcl-xml-web", "mcr-files", "mcr-web",
                "mre-files", "mre-web", "cji-files", "sgm-files", "sgm-web")


def _courts_client():
    """Default ``PoliteClient`` for courts.michigan.gov, built the way ``McrWebSource`` / ``SgmWebSource``
    build their own (``MreWebSource`` takes its client as a required argument)."""
    import os

    from ..config import USER_AGENT
    from ..web import PoliteClient

    if "set MLAW_USER_AGENT" in USER_AGENT:
        raise RuntimeError("Set MLAW_USER_AGENT to something that identifies you with a real contact address "
                           "before crawling.")
    return PoliteClient(min_interval=max(2.0, float(os.environ.get("MLAW_MIN_INTERVAL", "2"))), timeout=180.0)
