"""Shared fixtures. Redirects queue state to a tmp path for every test."""
import pytest

from repobot import queues


@pytest.fixture(autouse=True)
def tmp_state(tmp_path, monkeypatch):
    path = tmp_path / "queues.json"
    monkeypatch.setattr(queues, "STATE_PATH", path)
    return path
