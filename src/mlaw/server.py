"""MCP server: thin tool wrappers over ``service``. Each call opens the SQLite file read-only,
so the daily job can atomically swap in a fresh database without restarting the server."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from . import service
from .config import allowed_hosts, bearer_token, db_path
from .db import connect

INSTRUCTIONS = """\
Michigan statutes and rules, stored locally and refreshed by a scheduled job.

Workflow: use get_document for a known citation (e.g. "MCL 750.83", "MCR 6.110", "MRE 404",
"Const 1963, art 1, § 17"); use search for topics or phrases; use get_cross_references to follow
citations; use list_changes to see what changed recently. Check data_status when freshness matters.

Always tell the user the citation, and that the text is an unofficial copy to verify against the
official source. get_document(as_of=...) returns the text this server observed on that date, which
is not necessarily the legally effective text; do not present it as an authoritative offense-date
version without checking the amending Public Act.
"""


def _read(fn, *args, **kwargs) -> Any:
    conn = connect(db_path(), readonly=True)
    try:
        return fn(conn, *args, **kwargs)
    finally:
        conn.close()


def build_server(host: str = "127.0.0.1", port: int = 8000) -> FastMCP:
    hosts = allowed_hosts()
    security = None
    if hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=[f"https://{h}" for h in hosts] + [f"http://{h}" for h in hosts],
        )
    elif host not in ("127.0.0.1", "localhost", "::1"):
        # Serving on a public interface: rely on the reverse proxy / bearer token instead of the
        # SDK's localhost-only host check.
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

    mcp = FastMCP(
        "Michigan Law",
        instructions=INSTRUCTIONS,
        host=host,
        port=port,
        transport_security=security,
    )

    @mcp.tool()
    def get_document(citation: str, as_of: str | None = None) -> dict[str, Any]:
        """Get the full text of one Michigan legal provision by citation.

        citation: "MCL 750.83", "MCR 6.110", "MRE 404", or "Const 1963, art 1, § 17".
        Subdivisions like (1)(a) are ignored; the whole section is returned.
        as_of: optional YYYY-MM-DD. Returns the version this server had observed by that date.
        This is observation history, not a legal effective date, and only goes back to this
        server's first ingest.
        Returns text, catchline, history note, compiler's notes (including Admin Rule and
        Constitutionality notes when the source has them), status, and provenance (source URL,
        last confirmed, and the source's own "complete through PA n of year" stamp).
        If the source lists another form of the same citation (for example an amended version that
        takes effect on a future date) it appears under other_forms with its effective date; that
        text is NOT the law currently in force unless its notice says so.
        A citation that falls inside a repealed range gets an explanatory error naming the range.
        """
        return _read(service.get_document, citation, as_of)

    @mcp.tool()
    def search(
        query: str,
        source: Literal["mcl", "mcr", "mre", "const"] | None = None,
        chapter: str | None = None,
        include_repealed: bool = False,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Full-text search across stored provisions (stemmed keywords; "quoted phrases";
        uppercase AND / OR / NOT). Returns citations, catchlines and snippets; call get_document
        for full text. Optionally restrict by source or MCL chapter number (e.g. "750").
        """
        return _read(service.search, query, source, chapter, include_repealed, limit)

    @mcp.tool()
    def get_chapter_outline(chapter: str, source: str = "mcl", limit: int = 200, offset: int = 0) -> dict[str, Any]:
        """List the sections in a chapter (e.g. chapter "750" for the Michigan Penal Code) in
        citation order, with catchlines. Paged with limit/offset.
        The Michigan Constitution is filed as chapter "1" but is NOT an MCL chapter -- pass
        source="const" to list it (chapter "1" with the default source="mcl" returns nothing,
        since there is no ordinary MCL chapter 1)."""
        return _read(service.get_chapter_outline, chapter, source, limit, offset)

    @mcp.tool()
    def get_cross_references(
        citation: str, direction: Literal["cites", "cited_by"] = "cites", limit: int = 100
    ) -> dict[str, Any]:
        """Follow citations. direction="cites": provisions this one refers to. direction="cited_by":
        provisions whose text refers to this one. Extracted automatically from explicit citations;
        not exhaustive."""
        return _read(service.get_cross_references, citation, direction, limit)

    @mcp.tool()
    def list_changes(
        since: str,
        source: str | None = None,
        change_type: Literal["new", "modified", "removed", "restored"] | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """List provisions added, modified, removed or restored since a date (YYYY-MM-DD),
        newest first, as detected by this server's ingest runs."""
        return _read(service.list_changes, since, source, change_type, limit)

    @mcp.tool()
    def lookup_public_act(year: int, number: int) -> dict[str, Any]:
        """Look up a Public Act (e.g. year 1931, number 328): title, effective date, and the
        citations it affects, when known."""
        return _read(service.lookup_public_act, year, number)

    @mcp.tool()
    def data_status() -> dict[str, Any]:
        """Report, per source, how many provisions are stored, when the last successful update and
        last full crawl ran, and the source's own currency stamp (e.g. "Complete Through PA 91 of 2026").
        Call this when the user asks whether the data is current."""
        return _read(service.data_status)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> PlainTextResponse:
        try:
            _read(service.data_status)
        except Exception as e:  # noqa: BLE001
            return PlainTextResponse(f"db error: {e}", status_code=503)
        return PlainTextResponse("ok")

    return mcp


class BearerAuthMiddleware:
    """Minimal shared-secret gate for the HTTP transport. /healthz stays open.

    This is a stopgap: confirm which auth schemes Claude's custom-connector flow supports for
    remote servers (OAuth vs. static headers) before rollout, and swap this out if needed.
    """

    def __init__(self, app, token: str):
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] != "/healthz":
            headers = dict(scope.get("headers") or [])
            import hmac

            given = headers.get(b"authorization", b"")
            if not hmac.compare_digest(given, self._expected):
                resp = JSONResponse({"error": "unauthorized"}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_http_app(host: str = "0.0.0.0", port: int = 8000):
    """ASGI app for uvicorn: streamable HTTP at /mcp, optional bearer auth, /healthz."""
    mcp = build_server(host=host, port=port)
    app = mcp.streamable_http_app()
    token = bearer_token()
    if token:
        app = BearerAuthMiddleware(app, token)
    return app
