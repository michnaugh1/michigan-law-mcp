"""courts.michigan.gov content API: locate current publication files.

The site's pages are rendered client-side, but its Optimizely content API returns page JSON. Asset
URLs carry a prefix that changes on re-upload (``/49ddcf/siteassets/...``), so sources look the
current file up here on every run instead of hardcoding it.

Always pass a ``PoliteClient``: it enforces robots.txt, and courts.michigan.gov's robots.txt
currently disallows all automated access (see docs/msc-inquiry.md), so live calls fail closed until
the Court permits them. Tests use fakes.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any

from bs4 import BeautifulSoup


API_BASE = "https://www.courts.michigan.gov/api/episerver/v3.0/content/"
_JSON = {"Accept": "application/json"}


class CourtsApiError(RuntimeError):
    """Error fetching or parsing courts.michigan.gov API responses."""

    pass


def content(path: str, client: Any) -> dict:
    """Fetch a page from courts.michigan.gov API.

    Args:
        path: Site path to fetch (e.g., "/publications/felony-sentencing-resources/").
        client: An object with a .get(url) method that returns an object with .text.
                Must send Accept: application/json header.

    Returns:
        The first element of the JSON response (the page).

    Raises:
        CourtsApiError: If the API returns an empty list.
    """
    # "expand=*" literal: the form verified against the live API on 2026-09-29.
    url = f"{API_BASE}?contentUrl={urllib.parse.quote(path, safe='')}&expand=*"
    response = client.get(url, headers=_JSON)
    try:
        data = json.loads(response.text)
    except json.JSONDecodeError as e:
        raise CourtsApiError(f"failed to parse JSON response: {e}") from e

    if not data:
        raise CourtsApiError(f"empty response for path {path!r}")

    return data[0]


def children(content_id: int, client: Any) -> list[dict]:
    """Fetch child pages of a content item.

    Args:
        content_id: The content ID from a page's contentLink.
        client: An object with a .get(url) method that returns an object with .text.
                Must send Accept: application/json header.

    Returns:
        List of child page objects.

    Raises:
        CourtsApiError: If the API returns invalid JSON.
    """
    url = f"{API_BASE}{content_id}/children?top=100"
    response = client.get(url, headers=_JSON)
    try:
        return json.loads(response.text)
    except json.JSONDecodeError as e:
        raise CourtsApiError(f"failed to parse JSON response: {e}") from e


def page_body(page: dict) -> str:
    """Extract the HTML body from a page object.

    Args:
        page: A page object (from content()).

    Returns:
        The mainBody.value HTML string.

    Raises:
        CourtsApiError: If mainBody or value is missing.
    """
    try:
        return page["mainBody"]["value"]
    except KeyError as e:
        raise CourtsApiError(f"page missing mainBody.value: {e}") from e


def child_links(children_json: list[dict]) -> dict[str, str]:
    """Map child page names to their URLs.

    Args:
        children_json: List of child page objects (from children()).

    Returns:
        Dictionary mapping child name to contentLink.url.
    """
    return {child["name"]: child["contentLink"]["url"] for child in children_json}


def link_under_heading(html: str, heading: str) -> tuple[str, str]:
    """The single link under ``heading`` (an h1-h6, matched case-insensitively), up to the next heading
    of the same or higher level. Returns (href, link text).

    Raises CourtsApiError unless exactly one link is found, so a redesigned page fails loudly instead
    of silently picking the wrong file (e.g. a "Former Versions" manual instead of the current one)."""
    soup = BeautifulSoup(html, "lxml")
    target = " ".join(heading.split()).lower()
    level = None
    links: list[tuple[str, str]] = []
    for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "a"]):  # document order
        if el.name == "a":
            if level is not None and el.get("href"):
                links.append((el["href"], el.get_text(strip=True)))
            continue
        el_level = int(el.name[1])
        if level is None:
            if " ".join(el.get_text().split()).lower() == target:
                level = el_level
        elif el_level <= level:
            break
    if level is None:
        raise CourtsApiError(f"heading not found: {heading!r}")
    if len(links) != 1:
        raise CourtsApiError(f"expected exactly one link under heading {heading!r}, found {len(links)}")
    return links[0]
