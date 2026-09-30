"""Tests for courts.michigan.gov API client."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mlaw.sources.courts_api import (
    API_BASE,
    CourtsApiError,
    child_links,
    children,
    content,
    link_under_heading,
    page_body,
)

DATA = Path(__file__).parent / "data" / "court_rules" / "2026-09-29"


class FakeClient:
    """Mock client that returns JSON-like text."""

    def __init__(self, text: str):
        self.text = text

    def get(self, url: str, **kwargs):
        return self


def test_page_body():
    """Extract mainBody.value from a page object."""
    sgm_data = json.loads((DATA / "sgm" / "sentencing-guidelines-manuals.api.excerpt.json").read_text())
    page = sgm_data[0]
    body = page_body(page)
    assert "<h3>Current Version</h3>" in body
    assert "<h3>Former Versions</h3>" in body
    assert "Sentencing Guidelines Manual 2026" in body


def test_page_body_missing_mainbody():
    """page_body raises if mainBody is missing."""
    with pytest.raises(CourtsApiError, match="missing mainBody"):
        page_body({})


def test_page_body_missing_value():
    """page_body raises if value is missing."""
    with pytest.raises(CourtsApiError, match="missing mainBody"):
        page_body({"mainBody": {}})


def test_link_under_heading_current_version():
    """Find the single link under 'Current Version' heading."""
    sgm_data = json.loads((DATA / "sgm" / "sentencing-guidelines-manuals.api.excerpt.json").read_text())
    body = page_body(sgm_data[0])
    href, text = link_under_heading(body, "Current Version")
    assert href == "https://www.courts.michigan.gov/49ddcf/siteassets/offices/mji/felony-sentencing-online-resources/sgm.pdf"
    assert text == "Sentencing Guidelines Manual 2026"


def test_link_under_heading_multiple_links_raises():
    """link_under_heading raises when multiple links are found."""
    sgm_data = json.loads((DATA / "sgm" / "sentencing-guidelines-manuals.api.excerpt.json").read_text())
    body = page_body(sgm_data[0])
    with pytest.raises(CourtsApiError, match="found 2"):
        link_under_heading(body, "Former Versions")


def test_link_under_heading_not_found_raises():
    """link_under_heading raises when heading is not found."""
    sgm_data = json.loads((DATA / "sgm" / "sentencing-guidelines-manuals.api.excerpt.json").read_text())
    body = page_body(sgm_data[0])
    with pytest.raises(CourtsApiError, match="heading not found"):
        link_under_heading(body, "No Such Heading")


def test_link_under_heading_zero_links_raises():
    """link_under_heading raises when heading has zero links."""
    html = "<h1>My Heading</h1><p>Some text with no links.</p>"
    with pytest.raises(CourtsApiError, match="found 0"):
        link_under_heading(html, "My Heading")


def test_link_under_heading_case_insensitive_whitespace_normalized():
    """link_under_heading matches headings case-insensitively with normalized whitespace."""
    html = """
    <h1>My   Heading   Text</h1>
    <p><a href="/url1">Link 1</a></p>
    """
    href, text = link_under_heading(html, "my heading text")
    assert href == "/url1"
    assert text == "Link 1"


def test_link_under_heading_stops_at_same_level_heading():
    """link_under_heading stops collecting at a heading of the same or higher level."""
    html = """
    <h2>Section A</h2>
    <p><a href="/link1">Link 1</a></p>
    <h2>Section B</h2>
    <p><a href="/link2">Link 2</a></p>
    """
    href, text = link_under_heading(html, "Section A")
    assert href == "/link1"


def test_child_links():
    """Map child page names to URLs."""
    children_data = json.loads((DATA / "catalog.children.excerpt.json").read_text())
    result = child_links(children_data)

    # Check specific mappings from the test data
    assert "Michigan Rules of Evidence" in result
    assert result["Michigan Rules of Evidence"].endswith("/michigan-rules-of-evidence.pdf")

    assert "Model Criminal Jury Instructions" in result
    assert "model-criminal-jury-instructions2" in result["Model Criminal Jury Instructions"]


def test_cji_page_body():
    """Extract mainBody from model criminal jury instructions page."""
    cji_data = json.loads((DATA / "cji" / "model-criminal-jury-instructions.api.excerpt.json").read_text())
    body = page_body(cji_data[0])
    assert "responsive-html5.zip/index.html" in body


def test_content_builds_correct_url():
    """content() builds the correct API URL with proper URL encoding."""
    call_log = []

    class TrackingClient:
        def get(self, url: str, **kwargs):
            call_log.append(url)
            return FakeClient("[]")

    client = TrackingClient()
    try:
        content("/publications/x/", client)
    except CourtsApiError:
        pass  # Expected: empty list

    assert len(call_log) == 1
    url = call_log[0]
    assert url.startswith(API_BASE + "?")
    assert "contentUrl=%2Fpublications%2Fx%2F" in url
    assert url == API_BASE + "?contentUrl=%2Fpublications%2Fx%2F&expand=*"


def test_content_empty_response_raises():
    """content() raises CourtsApiError on empty response."""
    client = FakeClient("[]")
    with pytest.raises(CourtsApiError, match="empty response"):
        content("/some/path/", client)


def test_content_invalid_json_raises():
    """content() raises CourtsApiError on invalid JSON."""
    client = FakeClient("not json")
    with pytest.raises(CourtsApiError, match="failed to parse JSON"):
        content("/some/path/", client)


def test_content_returns_first_element():
    """content() returns the first element of the JSON response."""
    response_data = [{"name": "First"}, {"name": "Second"}]
    client = FakeClient(json.dumps(response_data))
    result = content("/some/path/", client)
    assert result["name"] == "First"


def test_children_returns_list():
    """children() returns the full JSON response as a list."""
    response_data = [{"name": "Child1"}, {"name": "Child2"}]
    client = FakeClient(json.dumps(response_data))
    result = children(12345, client)
    assert result == response_data


def test_children_invalid_json_raises():
    """children() raises CourtsApiError on invalid JSON."""
    client = FakeClient("not json")
    with pytest.raises(CourtsApiError, match="failed to parse JSON"):
        children(12345, client)
