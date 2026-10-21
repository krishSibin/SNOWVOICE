import pytest


@pytest.fixture(autouse=True)
def _default_knowledge_source(monkeypatch):
    """Settings also read the developer's .env; a machine set to
    KNOWLEDGE_SOURCE=index must not change what the tests see."""

    monkeypatch.setenv("KNOWLEDGE_SOURCE", "live")
