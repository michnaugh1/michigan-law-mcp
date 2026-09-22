import httpx
import pytest

from mlaw.web import PoliteClient, RobotsDisallowed

ROBOTS = "User-agent: *\nDisallow: /private/\nCrawl-delay: 2\n"


def make(handler, **kw):
    sleeps = []
    t = {"now": 0.0}

    def sleep(s):
        sleeps.append(s)
        t["now"] += s

    c = PoliteClient(transport=httpx.MockTransport(handler), sleep=sleep, clock=lambda: t["now"],
                     user_agent="test-agent/1.0", **kw)
    return c, sleeps


def test_respects_disallow_and_crawl_delay():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        return httpx.Response(200, text="ok", headers={"ETag": "abc"})

    c, sleeps = make(handler)
    with pytest.raises(RobotsDisallowed):
        c.get("https://x.test/private/a")
    assert c.get("https://x.test/a").etag == "abc"
    c.get("https://x.test/b")
    assert sleeps and sleeps[-1] == pytest.approx(2.0)  # crawl-delay beats 1s default
    assert calls.count("/robots.txt") == 1  # cached


def test_fails_closed_when_robots_unreadable():
    def handler(req):
        if req.url.path == "/robots.txt":
            raise httpx.ConnectError("SSL: CERTIFICATE_VERIFY_FAILED")
        return httpx.Response(200, text="ok")

    c, _ = make(handler)
    with pytest.raises(RobotsDisallowed, match="could not read robots.txt"):
        c.get("https://x.test/a")


def test_missing_robots_allows_and_conditional_get():
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        if req.headers.get("If-None-Match") == "abc":
            return httpx.Response(304)
        return httpx.Response(200, text="body", headers={"ETag": "abc"})

    c, _ = make(handler)
    assert c.get("https://x.test/a").text == "body"
    r = c.get("https://x.test/a", etag="abc")
    assert r.not_modified and r.status == 304


def test_retries_on_503_then_succeeds():
    n = {"i": 0}

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        n["i"] += 1
        return httpx.Response(503) if n["i"] < 3 else httpx.Response(200, text="ok")

    c, sleeps = make(handler)
    assert c.get("https://x.test/a").text == "ok" and n["i"] == 3


def test_tls_verification_is_never_disabled(monkeypatch):
    from mlaw.web import tls_verify_setting

    monkeypatch.delenv("MLAW_CA_BUNDLE", raising=False)
    monkeypatch.delenv("MLAW_USE_SYSTEM_TRUST", raising=False)
    assert tls_verify_setting() is True
    monkeypatch.setenv("MLAW_CA_BUNDLE", "/tmp/bundle.pem")
    assert tls_verify_setting() == "/tmp/bundle.pem"


def test_robots_5xx_means_do_not_crawl_yet():
    # RFC 9309: if robots.txt is unreachable because of a server error, assume complete disallow.
    # www.legislature.mi.gov's robots.txt returned 502 when checked by hand on 2026-09-21.
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(502, text="Bad gateway") if req.url.path == "/robots.txt" else httpx.Response(200, text="ok")

    c, _ = make(handler)
    with pytest.raises(RobotsDisallowed, match="502"):
        c.get("https://x.test/Laws/MCL?objectName=mcl-750-83")
    assert calls == ["/robots.txt"]  # the page itself was never requested


def test_robots_probe_uses_its_own_short_timeout_not_the_main_client_timeout():
    # Real finding, 2026-09-21: legislature.mi.gov's robots.txt doesn't error, it hangs -- confirmed live
    # with curl, not just this client. Waiting out the full crawl timeout (minutes) for that on every run
    # against a new host would be wasteful, especially for the hourly feed-refresh job -- so the robots.txt
    # probe gets its own short timeout, distinct from (and much shorter than) the client's main one.
    seen = {}

    def handler(req):
        if req.url.path == "/robots.txt":
            seen["timeout"] = req.extensions.get("timeout")
            return httpx.Response(404)
        return httpx.Response(200, text="ok")

    c, _ = make(handler, timeout=180.0)  # main timeout stays large (real chapter fetches can be slow)
    c.get("https://x.test/a")
    assert seen["timeout"] == {"connect": 15.0, "read": 15.0, "write": 15.0, "pool": 15.0}  # default, not 180

    c2, _ = make(handler, timeout=180.0, robots_timeout=5.0)
    c2.get("https://x.test/b")
    assert seen["timeout"] == {"connect": 5.0, "read": 5.0, "write": 5.0, "pool": 5.0}  # explicit override


def test_robots_error_override_is_opt_in_and_still_honors_a_readable_file():
    def broken(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(502, text="Bad gateway")
        return httpx.Response(200, text="ok")

    c, _ = make(broken)  # default: fail closed
    with pytest.raises(RobotsDisallowed):
        c.get("https://x.test/a")

    c2, _ = make(broken, robots_error_ok=True)
    assert c2.get("https://x.test/a").text == "ok"
    assert "502" in c2.robots_notes[0]

    def readable(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        return httpx.Response(200, text="ok")

    c3, _ = make(readable, robots_error_ok=True)
    with pytest.raises(RobotsDisallowed):
        c3.get("https://x.test/private/x")  # a real robots.txt still wins
