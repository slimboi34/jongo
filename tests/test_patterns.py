"""Every pattern in examples/patterns must import, migrate, render and compile.

These are the examples people copy. A broken example is worse than a missing one, so
each file is loaded in isolation, its schema is created, its first page is rendered on
the server and its components are compiled to JavaScript.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from conftest import use_test_database
from jongo import Jongo, db
from jongo.testing import TestClient

PATTERNS = sorted((Path(__file__).parent.parent / "examples" / "patterns").glob("*.py"))
assert PATTERNS, "no patterns found"


def load(path: Path):
    spec = importlib.util.spec_from_file_location(f"pattern_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = dict(db.models_registry)
    db.models_registry.clear()
    use_test_database()
    yield
    db.close_connections()
    db.models_registry.clear()
    db.models_registry.update(saved)


@pytest.mark.parametrize("path", PATTERNS, ids=lambda p: p.stem)
def test_pattern_runs(path):
    module = load(path)
    app = getattr(module, "app", None)
    if not isinstance(app, Jongo):  # 12_testing.py builds its app inside a fixture
        assert hasattr(module, "build_app"), f"{path.name} has neither app nor build_app"
        return

    db.migrate()
    client = TestClient(app)

    response = client.get("/")
    assert response.status == 200, f"{path.name}: GET / returned {response.status}"

    bundle = client.get("/_jongo/app.js")
    assert bundle.status == 200, f"{path.name}: components did not compile"
    assert len(bundle.text) > 1000


@pytest.mark.parametrize("path", PATTERNS, ids=lambda p: p.stem)
def test_pattern_explains_itself(path):
    """Each pattern opens with a docstring saying what it is and what to notice."""
    text = path.read_text()
    assert text.startswith('"""'), f"{path.name} has no module docstring"
    assert "What to notice" in text, f"{path.name} does not say what to notice"
