"""End-to-end through the MCP protocol (in-memory client <-> server)."""

import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from mlaw.server import build_server


@pytest.fixture
def server(tmp_path, monkeypatch, loaded):
    import shutil

    path = tmp_path / "served.sqlite3"
    loaded.commit()
    src_path = loaded.execute("PRAGMA database_list").fetchone()["file"]
    shutil.copy(src_path, path)
    monkeypatch.setenv("MLAW_DB", str(path))
    return build_server()


def payload(result):
    if result.structuredContent is not None:
        sc = result.structuredContent
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(result.content[0].text)


async def test_tools_listed(server):
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names == {
        "get_document", "search", "get_chapter_outline", "get_cross_references",
        "list_changes", "lookup_public_act", "data_status",
    }


async def test_get_document_over_protocol(server):
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        r = await client.call_tool("get_document", {"citation": "MCL 750.83"})
        assert not r.isError
        assert "AMENDED" in payload(r)["text"]

        r = await client.call_tool("get_document", {"citation": "MCL 999.9"})
        assert r.isError and "not in this database" in r.content[0].text

        r = await client.call_tool("search", {"query": "speed", "source": "mcl"})
        assert payload(r)["results"][0]["citation"] == "MCL 257.627a"

        r = await client.call_tool("search", {"query": "speed", "source": "bogus"})
        assert r.isError  # enum-validated


async def test_read_only_connection(server):
    from mlaw.config import db_path
    from mlaw.db import connect
    import sqlite3

    c = connect(db_path(), readonly=True)
    with pytest.raises(sqlite3.OperationalError):
        c.execute("DELETE FROM documents")
    c.close()
