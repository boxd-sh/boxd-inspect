from __future__ import annotations

from collections.abc import Iterator

import pytest

from boxd_inspect import _environment
from tests.fakes import FakeClient, FakeMachines


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeMachines]:
    """The client every region gets, for one test."""
    machines = FakeMachines()
    client = FakeClient(machines)
    monkeypatch.setattr(_environment, "_make_client", lambda region: client)
    token = _environment._unscoped.set(None)
    yield machines
    _environment._unscoped.reset(token)
