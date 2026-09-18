"""The documentation site is a Jongo app, so it is tested like one."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from conftest import use_test_database
from jongo import db
from jongo.testing import TestClient

SITE = Path(__file__).parent.parent / "docs" / "docs_site.py"


@pytest.fixture(scope="module")
def site():
    use_test_database()
    spec = importlib.util.spec_from_file_location("jongo_docs_site", SITE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    db.close_connections()


def test_the_guide_renders(site):
    response = TestClient(site.app).get("/")
    assert response.status == 200
    assert "The Jongo guide" in response.text
    assert "Server functions" in response.text          # a section heading
    assert "@app.page" in response.text                  # a code block survived


def test_the_pattern_index_and_a_pattern_render(site):
    client = TestClient(site.app)
    index = client.get("/patterns")
    assert index.status == 200 and "01 crud" in index.text
    detail = client.get("/patterns/01_crud")
    assert detail.status == 200 and "create_note" in detail.text


def test_an_unknown_pattern_is_handled(site):
    assert "No such pattern" in TestClient(site.app).get("/patterns/nope").text


def test_a_traversal_attempt_reads_nothing(site):
    """The route only matches one path segment, and the handler re-checks the parent."""
    client = TestClient(site.app)
    for attempt in ("/patterns/../secrets", "/patterns/..%2F..%2Fpyproject.toml",
                    "/patterns/....//x", "/patterns/..%2F..%2FREADME.md"):
        response = client.get(attempt)
        assert response.status in (200, 404)
        # nothing outside examples/patterns may ever be served
        assert "[build-system]" not in response.text
        assert "Four building blocks" not in response.text


def test_the_search_component_compiles_to_javascript(site):
    bundle = TestClient(site.app).get("/_jongo/app.js")
    assert bundle.status == 200
    assert "Search" in bundle.text


def test_the_markdown_renderer_handles_the_guide(site):
    blocks = site.parse(site.GUIDE.read_text())
    kinds = {block["kind"] for block in blocks}
    assert {"h1", "h2", "p", "code", "list", "table"} <= kinds
    index = site.build_index(blocks)
    assert len(index) > 15
    assert all(entry["anchor"] and entry["title"] for entry in index)
