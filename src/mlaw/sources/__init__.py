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
    raise ValueError(f"unknown source {kind!r} (choose: fixture, mcl-files, mcl-web, mcl-xml-files, mcl-xml-web)")
