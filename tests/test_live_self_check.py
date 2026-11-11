"""Inspect's portable sandbox checks, run against real boxd machines.

Gated: ``BOXD_API_KEY=... pytest -m live tests/test_live_self_check.py``.
``BOXD_INSPECT_TEST_REGION`` (default ``nyc``) and ``BOXD_INSPECT_TEST_PREFIX``
(default ``t-inspect``) set where the machines go and how they are named.
Each configuration makes one machine, shared by its checks, and deletes it.
Every machine also has a one-hour ``delete_after``, in case a run is killed.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import pytest
import pytest_asyncio
from inspect_ai.util import SandboxEnvironment
from inspect_ai.util._sandbox.self_check import *  # noqa: F403

from boxd_inspect import BoxdSandboxEnvironment, BoxdSandboxEnvironmentConfig

pytestmark = [
    pytest.mark.live,
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.skipif(
        not (os.environ.get("BOXD_API_KEY") or os.environ.get("BOXD_TOKEN")),
        reason="needs BOXD_API_KEY",
    ),
]


@dataclass(frozen=True)
class Case:
    id: str
    compose_yaml: str | None
    compose_file: str | None = None
    xfails: dict[str, str] = field(default_factory=dict)


class CaseAndEnv(NamedTuple):
    case: Case
    env: SandboxEnvironment


CASES = [
    Case(id="machine", compose_yaml=None),
    Case(
        id="compose",
        compose_yaml=(
            "services:\n"
            "  default:\n"
            "    image: python:3.12-bookworm\n"
            "    command: tail -f /dev/null\n"
            "    init: true\n"
        ),
        # As in Inspect's own Docker runs: the container's user is root.
        xfails={
            "test_write_text_file_without_permissions": "root can overwrite a read-only file",
            "test_write_binary_file_without_permissions": "root can overwrite a read-only file",
            "test_read_file_not_allowed": "root can read files without read bits",
        },
    ),
    # Inspect's own non-root fixtures, built from a Dockerfile in the machine.
    Case(id="compose-nonroot", compose_yaml=None, compose_file="compose/nonroot/compose.yaml"),
    Case(
        id="compose-nonroot-alpine",
        compose_yaml=None,
        compose_file="compose/nonroot-alpine/compose.yaml",
    ),
]


@pytest_asyncio.fixture(scope="module", loop_scope="module", params=CASES, ids=lambda c: c.id)
async def _case_and_env(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[CaseAndEnv]:
    case: Case = request.param
    compose = None
    if case.compose_file is not None:
        compose = str(Path(__file__).parent / case.compose_file)
    elif case.compose_yaml is not None:
        compose_file = tmp_path_factory.mktemp(case.id) / "compose.yaml"
        compose_file.write_text(case.compose_yaml)
        compose = str(compose_file)
    config = BoxdSandboxEnvironmentConfig(
        compose=compose,
        region=os.environ.get("BOXD_INSPECT_TEST_REGION", "nyc"),
        name_prefix=os.environ.get("BOXD_INSPECT_TEST_PREFIX", "t-inspect"),
        labels={"boxd-inspect": "live-test"},
        delete_after=3600,
    )
    task_name = f"self-check-{case.id}"
    await BoxdSandboxEnvironment.task_init(task_name, config)
    envs = await BoxdSandboxEnvironment.sample_init(task_name, config, {})
    try:
        yield CaseAndEnv(case, envs["default"])
    finally:
        await BoxdSandboxEnvironment.sample_cleanup(task_name, config, envs, False)
        await BoxdSandboxEnvironment.task_cleanup(task_name, config, cleanup=True)


@pytest.fixture
def sandbox_env(request: pytest.FixtureRequest, _case_and_env: CaseAndEnv) -> SandboxEnvironment:
    reason = _case_and_env.case.xfails.get(request.node.originalname)
    if reason is not None:
        request.node.add_marker(pytest.mark.xfail(reason=reason, strict=True))
    return _case_and_env.env
