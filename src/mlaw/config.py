"""Runtime configuration (environment variables) and shared constants."""

from __future__ import annotations

import os

# Attached to every tool response. The Legislature's site footer says its information "is not intended
# to replace official versions of that information and is subject to revision" and is offered "without
# warranties"; this notice carries that forward. Have counsel confirm the final wording.
NOTICE = (
    "Unofficial copy of information published by the Michigan Legislature (Legislative Service Bureau), "
    "which states that it is not intended to replace official versions, is subject to revision, and is "
    "provided without warranties. Verify against the official source before relying on it, especially "
    "for filings."
)

# Court-published material (spec 2026-09-29: "Wording to confirm"). M Crim JI, MCR and MRE are published by
# the Michigan Supreme Court; the Sentencing Guidelines Manual by the Michigan Judicial Institute.
NOTICE_MSC = (
    "Unofficial copy of material published by the Michigan Supreme Court; verify against the official source "
    "before relying on it, especially for filings."
)
NOTICE_MJI = (
    "Unofficial copy of material published by the Michigan Judicial Institute; verify against the official "
    "source before relying on it, especially for filings."
)
_NOTICES = {"mcr": NOTICE_MSC, "mre": NOTICE_MSC, "cji": NOTICE_MSC, "sgm": NOTICE_MJI}


def notice_for(*sources: str | None) -> str:
    """The notice for documents of these kinds (``documents.source`` or a run's crawler name, e.g.
    "cji-files"): one publisher's notice, or every applicable one joined when the kinds are mixed.
    MCL, the Constitution and anything unknown get the Legislature's ``NOTICE``."""
    seen: list[str] = []
    for s in sources or (None,):
        n = _NOTICES.get((s or "").lower().split("-")[0], NOTICE)
        if n not in seen:
            seen.append(n)
    return " ".join(seen)

USER_AGENT = os.environ.get(
    "MLAW_USER_AGENT",
    "michigan-law-mcp/0.1 (+contact: set MLAW_USER_AGENT with a real contact address)",
)


def db_path() -> str:
    return os.environ.get("MLAW_DB", "data/mlaw.sqlite3")


def bearer_token() -> str | None:
    return os.environ.get("MLAW_BEARER_TOKEN") or None


def allowed_hosts() -> list[str]:
    raw = os.environ.get("MLAW_ALLOWED_HOSTS", "")
    return [h.strip() for h in raw.split(",") if h.strip()]
