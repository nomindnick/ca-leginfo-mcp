"""MCP-layer tests: the seven tools registered and callable through an
in-memory client session (mcp 2.0's Client connects directly to an
MCPServer instance)."""

import anyio
import pytest
from mcp import Client

from ingest.build import build_current_db
from server import app
from server.db import Databases

EXPECTED_TOOLS = {
    "get_section", "search_sections", "bills_affecting_section",
    "get_bill", "get_bill_analyses", "get_legislative_history",
    "chapter_to_bill",
}


@pytest.fixture(scope="module")
def configured(mini_zip, tmp_path_factory):
    d = tmp_path_factory.mktemp("mcp_dbs")
    build_current_db(mini_zip, d / "current.db")
    app.configure(Databases(d / "current.db", None))
    return app.mcp


def _run(coro_fn):
    return anyio.run(coro_fn)


def test_all_tools_registered(configured):
    async def go():
        async with Client(configured) as client:
            return await client.list_tools()

    tools = _run(go)
    assert {t.name for t in tools.tools} == EXPECTED_TOOLS
    for t in tools.tools:
        assert t.description, f"{t.name} has no description"


def test_call_get_section_roundtrip(configured):
    async def go():
        async with Client(configured) as client:
            return await client.call_tool(
                "get_section",
                {"code": "Ed. Code", "section": "44955"})

    result = _run(go)
    assert not result.is_error
    data = result.structured_content
    assert data["code"] == "EDC"
    assert data["law_extract_date"]
    assert "certificated employees" in data["versions"][0]["text"]


def test_call_search_with_defaults(configured):
    async def go():
        async with Client(configured) as client:
            return await client.call_tool(
                "search_sections", {"query": "certificated employees"})

    result = _run(go)
    assert not result.is_error
    assert result.structured_content["results"]


def test_error_payloads_are_structured_not_exceptions(configured):
    async def go():
        async with Client(configured) as client:
            return await client.call_tool(
                "get_section", {"code": "Klingon Code", "section": "1"})

    result = _run(go)
    assert not result.is_error  # tool-level errors are data, not protocol errors
    assert "error" in result.structured_content
