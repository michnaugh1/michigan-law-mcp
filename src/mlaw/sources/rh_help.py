"""Shared helpers for Adobe RoboHelp responsive HTML5 help systems.

Used by the Michigan Court Rules (MCR) and Model Criminal Jury Instructions (M Crim JI) sites on
courts.michigan.gov (the Rules of Evidence are published only as a PDF).
"""

from __future__ import annotations

import html
import json
import re
import urllib.parse
from dataclasses import dataclass


@dataclass(frozen=True)
class TocEntry:
    """A table of contents entry from a RoboHelp TOC file.

    Attributes:
        name: The display name of the entry.
        type: Either "book" (has children in a separate TOC file) or "item" (leaf node).
        url: The URL relative to the help site root, may include a fragment identifier.
        key: For "book" entries, the key naming the child TOC file (e.g. "toc1" -> "whxdata/toc1.new.js").
    """

    name: str
    type: str
    url: str
    key: str | None = None

    @property
    def path(self) -> str:
        """The URL without the fragment identifier."""
        if "#" in self.url:
            return self.url.split("#", 1)[0]
        return self.url

    @property
    def anchor(self) -> str | None:
        """The fragment identifier, if present."""
        if "#" in self.url:
            return self.url.split("#", 1)[1]
        return None


def parse_toc(js_text: str) -> list[TocEntry]:
    """Parse a RoboHelp TOC file (JavaScript) to extract the table of contents.

    Args:
        js_text: The contents of a TOC file like whxdata/toc.new.js.

    Returns:
        A list of TocEntry objects.

    Raises:
        ValueError: If the TOC data cannot be found or parsed.
    """
    # Extract the JSON array assigned to `var toc =`
    match = re.search(r'var\s+toc\s*=\s*(\[.*?\]);', js_text, re.DOTALL)
    if not match:
        raise ValueError("Could not find TOC data: expected 'var toc = [...]'")

    json_text = match.group(1)
    try:
        toc_data = json.loads(json_text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Could not parse TOC JSON: {e}") from e

    entries = []
    for item in toc_data:
        entry = TocEntry(
            name=item["name"],
            type=item["type"],
            url=item["url"],
            key=item.get("key"),
        )
        entries.append(entry)

    return entries


def child_toc_path(entry: TocEntry) -> str:
    """Get the path to the child TOC file for a "book" entry.

    Args:
        entry: A TocEntry with type "book".

    Returns:
        The path to the child TOC file (e.g. "whxdata/toc1.new.js").

    Raises:
        ValueError: If the entry has no key.
    """
    if entry.key is None:
        raise ValueError(f"Entry '{entry.name}' has no key; not a book entry")
    return f"whxdata/{entry.key}.new.js"


def normalize_text(fragment: str) -> str:
    """Normalize text from RoboHelp HTML: decode entities, remove formatting, clean whitespace.

    Handles:
    - HTML entities (numeric and named), with or without semicolons.
    - HTML tags (stripped).
    - Unicode zero-width characters (U+200B, U+FEFF).
    - Non-breaking spaces (U+00A0) converted to regular spaces.
    - Multiple spaces collapsed to single space.

    Args:
        fragment: Text possibly containing HTML tags and entities.

    Returns:
        Normalized plain text.
    """
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", "", fragment)

    # Decode HTML entities (handles both with and without semicolons)
    text = html.unescape(text)

    # Remove zero-width characters (U+200B and U+FEFF)
    text = text.replace("\u200b", "").replace("\ufeff", "")

    # Replace non-breaking spaces (U+00A0) with regular spaces
    text = text.replace("\u00a0", " ")

    # Collapse multiple whitespace to single space
    text = re.sub(r"\s+", " ", text)

    # Strip leading/trailing whitespace
    text = text.strip()

    return text


def quote_path(path: str) -> str:
    """Quote a URL path component, avoiding double-encoding.

    Encodes non-ASCII and special characters using percent-encoding, while
    preserving "/" and "#" which are structural in URLs. If the path is already
    encoded (contains %XX sequences), returns it unchanged (idempotent).

    Args:
        path: A path component, possibly containing non-ASCII characters.

    Returns:
        The percent-encoded path, idempotent.
    """
    # Decode first, then encode: idempotent, and a path mixing "%XX" with raw non-ASCII is still fully encoded.
    return urllib.parse.quote(urllib.parse.unquote(path), safe="/#")


def join_url(base: str, path: str) -> str:
    """Join a base URL with a path component, with exactly one "/" between them.

    Args:
        base: The base URL (e.g. "https://example.com/help/").
        path: The path component (e.g. "/page.htm" or "page.htm").

    Returns:
        The joined URL with the path quoted and exactly one "/" between base and path.
    """
    quoted = quote_path(path)
    # Ensure exactly one "/" between base and quoted path
    base_trimmed = base.rstrip("/")
    quoted_trimmed = quoted.lstrip("/")
    return base_trimmed + "/" + quoted_trimmed
