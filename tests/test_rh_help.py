"""Tests for Adobe RoboHelp helper functions."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mlaw.sources.rh_help import (
    TocEntry,
    child_toc_path,
    join_url,
    normalize_text,
    parse_toc,
    quote_path,
)

# Data directory
DATA_DIR = Path(__file__).parent / "data" / "court_rules" / "2026-09-29"


class TestTocEntry:
    """Tests for TocEntry dataclass."""

    def test_properties_with_fragment(self):
        """Test path and anchor properties when URL has a fragment."""
        entry = TocEntry(
            name="Test",
            type="book",
            url="path/to/file.htm#anchor123",
            key="toc1",
        )
        assert entry.path == "path/to/file.htm"
        assert entry.anchor == "anchor123"

    def test_properties_without_fragment(self):
        """Test path and anchor properties when URL has no fragment."""
        entry = TocEntry(
            name="Test",
            type="item",
            url="path/to/file.htm",
        )
        assert entry.path == "path/to/file.htm"
        assert entry.anchor is None


class TestParseToc:
    """Tests for parse_toc function."""

    def test_parse_mcr_toc(self):
        """Test parsing the Michigan Court Rules TOC."""
        toc_file = DATA_DIR / "mcr" / "toc.new.js"
        with open(toc_file, encoding="utf-8") as f:
            content = f.read()

        entries = parse_toc(content)

        # Check total count
        assert len(entries) == 10

        # Check first entry is an item
        first = entries[0]
        assert first.type == "item"
        assert first.name == "Updated with MSC Order(s) Effective on September 2, 2026"
        assert first.key is None

        # Check that entries 1-9 are books with the expected keys
        book_keys = ["toc1", "toc4", "toc12", "toc23", "toc29", "toc40", "toc49", "toc53", "toc57"]
        for i, expected_key in enumerate(book_keys, 1):
            assert entries[i].type == "book"
            assert entries[i].key == expected_key

        # Check Chapter 6 entry (index 6)
        ch6 = entries[6]
        assert ch6.name == "Chapter 6. Criminal Procedure"
        assert ch6.type == "book"
        assert ch6.key == "toc40"
        assert ch6.path == "Michigan_Court_Rules/Court_Rules_Chapter_6/Court_Rules_Chapter_6.htm"
        assert ch6.anchor == "1022597"

    def test_parse_cji_toc(self):
        """Test parsing the Criminal Jury Instructions TOC."""
        toc_file = DATA_DIR / "cji" / "toc.new.js"
        with open(toc_file, encoding="utf-8") as f:
            content = f.read()

        entries = parse_toc(content)

        # Check total count
        assert len(entries) == 45

        # Check first entry
        assert entries[0].type == "item"
        assert entries[0].name == "Model Criminal Jury Instructions"
        assert entries[0].key is None

        # Count books and items
        books = sum(1 for e in entries if e.type == "book")
        items = sum(1 for e in entries if e.type == "item")
        assert books == 43
        assert items == 2

        # Check last entry
        last = entries[-1]
        assert last.type == "item"
        assert last.name.startswith("CHAPTER 44 Statutes to Criminal Jury Instructions")

    def test_parse_toc_raises_on_invalid_input(self):
        """Test that parse_toc raises ValueError on invalid input."""
        with pytest.raises(ValueError, match="Could not find TOC data"):
            parse_toc("var x = 1;")


class TestChildTocPath:
    """Tests for child_toc_path function."""

    def test_generates_correct_path(self):
        """Test that child_toc_path generates the correct path."""
        entry = TocEntry(
            name="Chapter 6",
            type="book",
            url="path/to/file.htm#anchor",
            key="toc40",
        )
        assert child_toc_path(entry) == "whxdata/toc40.new.js"

    def test_raises_on_missing_key(self):
        """Test that child_toc_path raises ValueError when key is None."""
        entry = TocEntry(
            name="Item",
            type="item",
            url="path/to/file.htm",
        )
        with pytest.raises(ValueError, match="has no key"):
            child_toc_path(entry)


class TestNormalizeText:
    """Tests for normalize_text function."""

    def test_hex_entities(self):
        """Test decoding of hex entities."""
        # Rule 6.&#x30;&#x30;1 -> Rule 6.001
        text = '<p class="Rule_Heading">Rule 6.&#x30;&#x30;1 Scope</p>'
        result = normalize_text(text)
        assert result == "Rule 6.001 Scope"

    def test_zero_width_space_without_semicolon(self):
        """Test that zero-width spaces (with or without semicolon) are removed."""
        # (A)<span>&#8203</span>Felony Cases.
        text = '(A)<span style="padding-right:6pt ;">&#8203</span>Felony Cases.'
        result = normalize_text(text)
        assert result == "(A)Felony Cases."

    def test_mixed_entities(self):
        """Test mixed numeric and hex entities."""
        # 6.&#x30;&#x30;&#x30;&#x2D;6.5&#x30;&#x30;
        text = "6.&#x30;&#x30;&#x30;&#x2D;6.5&#x30;&#x30;"
        result = normalize_text(text)
        assert result == "6.000-6.500"

    def test_entity_named(self):
        """Test numeric entity (comma example)."""
        # January 1&#44; 2026
        text = "January 1&#44; 2026"
        result = normalize_text(text)
        assert result == "January 1, 2026"

    def test_chapter_1_body(self):
        """Test normalization of full HTML body from Chapter 1."""
        ch1_file = DATA_DIR / "mcr" / "Court_Rules_Chapter_1.htm"
        with open(ch1_file, encoding="utf-8") as f:
            content = f.read()

        # Extract body
        match = re.search(r"<body[^>]*>(.*?)</body>", content, re.DOTALL)
        assert match
        body_content = match.group(1)

        # Normalize
        result = normalize_text(body_content)

        # Check that the expected phrase is present
        assert "Rule 1.101 Title; Citation" in result

        # Check that no zero-width space remains
        assert "​" not in result


class TestQuotePath:
    """Tests for quote_path function."""

    def test_quotes_non_ascii_characters(self):
        """Test that non-ASCII characters (curly apostrophe) are quoted."""
        # Prosecutor’s_Failure (U+2019 = RIGHT SINGLE QUOTATION MARK)
        path = "Chap_5_Witnesses/MCrim_JI_5.12_Prosecutor’s_Failure.htm"
        result = quote_path(path)
        assert result == "Chap_5_Witnesses/MCrim_JI_5.12_Prosecutor%E2%80%99s_Failure.htm"

    def test_idempotent(self):
        """Test that quote_path is idempotent (encoding twice gives same result)."""
        path = "Chap_5_Witnesses/MCrim_JI_5.12_Prosecutor’s_Failure.htm"
        once = quote_path(path)
        twice = quote_path(once)
        assert once == twice


class TestJoinUrl:
    """Tests for join_url function."""

    def test_joins_with_exactly_one_slash(self):
        """Test that join_url produces exactly one / between base and path."""
        base = "https://h/x.zip/"
        path = "/a b.htm"
        result = join_url(base, path)
        assert result == "https://h/x.zip/a%20b.htm"
        # Check no double slashes
        assert "//" not in result.replace("https://", "")


def test_quote_path_encodes_mixed_input():
    from mlaw.sources.rh_help import quote_path

    assert quote_path("a%20b/Prosecutor\u2019s.htm") == "a%20b/Prosecutor%E2%80%99s.htm"
