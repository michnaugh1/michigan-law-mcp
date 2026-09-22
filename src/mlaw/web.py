"""Polite HTTP client for source sites: honors robots.txt, identifies itself, rate-limits,
retries with backoff, and caches ETag / Last-Modified so unchanged pages cost one cheap
conditional request.

Nothing here has been run against a live site. Read each site's robots.txt and terms of use
first, and set MLAW_USER_AGENT to something with a real contact address.
"""

from __future__ import annotations

import os
import ssl
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .config import USER_AGENT


def tls_verify_setting():
    """How to verify server certificates. Verification is never disabled.

    * MLAW_CA_BUNDLE=/path/to/bundle.pem: use this bundle (e.g. certifi's plus a missing intermediate).
    * MLAW_USE_SYSTEM_TRUST=1: use the operating system trust store via the optional ``truststore``
      package. Helps when a server omits its intermediate certificate, because the OS can fetch it.
    * otherwise: httpx's default (certifi).
    """
    bundle = os.environ.get("MLAW_CA_BUNDLE")
    if bundle:
        return bundle
    if os.environ.get("MLAW_USE_SYSTEM_TRUST") == "1":
        import truststore  # pip install truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return True


class RobotsDisallowed(PermissionError):
    pass


@dataclass
class Fetched:
    url: str
    status: int
    text: str
    not_modified: bool = False
    etag: str | None = None
    last_modified: str | None = None
    content: bytes = b""  # raw response bytes; use this (not .text) for anything not UTF-8, e.g. the
    # Legislature's UTF-16 chapter XML -- httpx's charset auto-detection for .text is a heuristic, and
    # the file's own encoding declaration is only honored when a parser is handed the actual bytes.


class PoliteClient:
    def __init__(
        self,
        *,
        min_interval: float = 1.0,
        max_retries: int = 4,
        timeout: float = 30.0,
        robots_timeout: float = 15.0,
        robots_error_ok: bool = False,
        user_agent: str = USER_AGENT,
        transport: httpx.BaseTransport | None = None,
        verify=None,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        self._client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
            transport=transport,
            **({} if transport is not None else {"verify": tls_verify_setting() if verify is None else verify}),
        )
        self._ua = user_agent
        # Only for a site whose operator has said in writing that automated access is permitted and that it does
        # not serve robots.txt (legislature.mi.gov, 2026-09-21; see docs/lsb-reply-2026-09-21.md). A robots.txt
        # that IS readable is still honored; this only covers the file being unreachable or erroring.
        self._robots_error_ok = robots_error_ok
        # A short, separate timeout for the robots.txt probe specifically. Confirmed live 2026-09-21 (curl,
        # not just this client): legislature.mi.gov's robots.txt doesn't error, it hangs -- 0 bytes, no
        # response at all, for as long as you'll wait. There's no reason to burn the full crawl timeout
        # (potentially minutes) finding that out on every single run against a new host, especially one
        # polled hourly -- a real robots.txt is a small file and should answer in well under this.
        self._robots_timeout = robots_timeout
        self.robots_notes: list[str] = []
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._sleep = sleep
        self._clock = clock
        self._last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}

    def close(self) -> None:
        self._client.close()

    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        rp = self._robots.get(origin)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self._client.get(f"{origin}/robots.txt", timeout=self._robots_timeout)
            except httpx.HTTPError as e:
                if not self._robots_error_ok:
                    # Fail closed: if we cannot read robots.txt we do not crawl.
                    raise RobotsDisallowed(f"could not read robots.txt for {origin}: {e}") from e
                self.robots_notes.append(f"robots.txt for {origin} unreachable ({type(e).__name__}); proceeding by operator override")
                rp.parse([])
                self._robots[origin] = rp
                return rp
            if r.status_code == 404:
                rp.parse([])  # no robots.txt: nothing disallowed
            elif r.status_code >= 400:
                if not self._robots_error_ok:
                    raise RobotsDisallowed(f"robots.txt for {origin} returned {r.status_code}")
                self.robots_notes.append(f"robots.txt for {origin} returned {r.status_code}; proceeding by operator override")
                rp.parse([])
            else:
                rp.parse(r.text.splitlines())
            self._robots[origin] = rp
        return rp

    def _crawl_delay(self, rp: urllib.robotparser.RobotFileParser) -> float:
        d = rp.crawl_delay(self._ua)
        return max(self._min_interval, float(d)) if d else self._min_interval

    def get(self, url: str, *, etag: str | None = None, last_modified: str | None = None) -> Fetched:
        rp = self._robots_for(url)
        if not rp.can_fetch(self._ua, url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        host = urlsplit(url).netloc
        delay = self._crawl_delay(rp)

        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        for attempt in range(self._max_retries + 1):
            wait = self._last.get(host, -1e9) + delay - self._clock()
            if wait > 0:
                self._sleep(wait)
            self._last[host] = self._clock()
            try:
                r = self._client.get(url, headers=headers)
            except httpx.TransportError:
                if attempt == self._max_retries:
                    raise
                self._sleep(2 ** attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < self._max_retries:
                ra = r.headers.get("Retry-After")
                self._sleep(float(ra) if ra and ra.isdigit() else 2 ** attempt)
                continue
            if r.status_code == 304:
                return Fetched(url, 304, "", True, etag, last_modified)
            r.raise_for_status()
            return Fetched(
                url, r.status_code, r.text, False, r.headers.get("ETag"), r.headers.get("Last-Modified"),
                r.content,
            )
        raise RuntimeError("unreachable")
