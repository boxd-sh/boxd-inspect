"""The machine lifecycle against real boxd: what Inspect's portable checks do not cover.

Gated: ``BOXD_API_KEY=... pytest -m live tests/test_live_lifecycle.py``. Uses the
same ``BOXD_INSPECT_TEST_REGION`` and ``BOXD_INSPECT_TEST_PREFIX`` as the
conformance run. ``BOXD_INSPECT_TEST_SNAPSHOT`` names a snapshot to start from,
for the ``from_snapshot`` check. ``BOXD_INSPECT_TEST_OTHER_REGION`` names a
second region, for the cleanup-by-id check across regions.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest
from boxd import AsyncBoxd, NotFoundError
from inspect_ai.util import SandboxUnavailableError

from boxd_inspect import BoxdSandboxEnvironment, BoxdSandboxEnvironmentConfig

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (os.environ.get("BOXD_API_KEY") or os.environ.get("BOXD_TOKEN")),
        reason="needs BOXD_API_KEY",
    ),
]

REGION = os.environ.get("BOXD_INSPECT_TEST_REGION", "nyc")
PREFIX = os.environ.get("BOXD_INSPECT_TEST_PREFIX", "t-inspect")


def config(**fields: object) -> BoxdSandboxEnvironmentConfig:
    return BoxdSandboxEnvironmentConfig.model_validate(
        {
            "region": REGION,
            "name_prefix": PREFIX,
            "labels": {"boxd-inspect": "live-test"},
            "delete_after": 3600,
            **fields,
        }
    )


async def start(settings: BoxdSandboxEnvironmentConfig) -> BoxdSandboxEnvironment:
    await BoxdSandboxEnvironment.task_init("lifecycle", settings)
    began = time.monotonic()
    envs = await BoxdSandboxEnvironment.sample_init("lifecycle", settings, {})
    env = envs["default"].as_type(BoxdSandboxEnvironment)
    print(f"machine {env.machine_name} ({env.machine_id}) ready in {time.monotonic() - began:.2f}s")
    return env


async def stop(env: BoxdSandboxEnvironment, settings: BoxdSandboxEnvironmentConfig) -> None:
    await BoxdSandboxEnvironment.sample_cleanup("lifecycle", settings, {"default": env}, False)
    await BoxdSandboxEnvironment.task_cleanup("lifecycle", settings, cleanup=True)


async def test_machine_lifecycle() -> None:
    settings = config()
    env = await start(settings)
    machine_id = env.machine_id
    client = AsyncBoxd(region=REGION)
    try:
        machine = await client.machines.get(machine_id, region=REGION)
        assert machine.networking.isolated is True
        assert machine.idle.suspend_after == 0
        assert machine.labels["inspect-sandbox"] == "boxd"

        began = time.monotonic()
        first = await env.exec(["uname", "-s"])
        print(f"first exec {time.monotonic() - began:.3f}s")
        assert first.stdout == "Linux\n"

        payload = os.urandom(5 * 1024 * 1024)
        began = time.monotonic()
        await env.write_file("blob.bin", payload)
        back = await env.read_file("blob.bin", text=False)
        print(f"5 MiB write + read {time.monotonic() - began:.2f}s")
        assert hashlib.sha256(back).hexdigest() == hashlib.sha256(payload).hexdigest()
        remote = await env.exec(["sha256sum", "blob.bin"])
        assert remote.stdout.split()[0] == hashlib.sha256(payload).hexdigest()

        started = await env.exec(
            ["sh", "-c", "nohup sleep 300 > /dev/null 2>&1 & echo $!"], timeout=10
        )
        pid = started.stdout.strip()
        assert (await env.exec(["kill", "-0", pid])).success, "background process died"

        split = await env.exec(["sh", "-c", "echo out; echo err >&2; exit 4"], env={"X": "1"})
        assert (split.stdout, split.stderr, split.returncode) == ("out\n", "err\n", 4)
    finally:
        await stop(env, settings)

    with pytest.raises(NotFoundError):
        await client.machines.get(machine_id, region=REGION)
    with pytest.raises(SandboxUnavailableError):
        await env.exec(["true"])


async def test_compose_services_share_one_machine(tmp_path: Path) -> None:
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text(
        "services:\n"
        "  default:\n"
        "    image: alpine:3.20\n"
        "    command: tail -f /dev/null\n"
        "    init: true\n"
        "  helper:\n"
        "    image: alpine:3.20\n"
        "    command: sh -c 'echo ready > /tmp/state; tail -f /dev/null'\n"
        "    init: true\n"
    )
    settings = config(compose=str(compose_file))
    await BoxdSandboxEnvironment.task_init("lifecycle", settings)
    began = time.monotonic()
    envs = await BoxdSandboxEnvironment.sample_init("lifecycle", settings, {})
    print(f"two-service compose ready in {time.monotonic() - began:.2f}s")
    try:
        assert list(envs) == ["default", "helper"]
        default = envs["default"].as_type(BoxdSandboxEnvironment)
        helper = envs["helper"].as_type(BoxdSandboxEnvironment)
        assert default.machine_id == helper.machine_id
        assert (await helper.read_file("/tmp/state")) == "ready\n"
        reach = await default.exec(["ping", "-c", "1", "-W", "5", "helper"])
        assert reach.success, reach.stderr
    finally:
        await BoxdSandboxEnvironment.sample_cleanup("lifecycle", settings, envs, False)
        await BoxdSandboxEnvironment.task_cleanup("lifecycle", settings, cleanup=True)


async def test_from_snapshot() -> None:
    snapshot = os.environ.get("BOXD_INSPECT_TEST_SNAPSHOT")
    if not snapshot:
        pytest.skip("set BOXD_INSPECT_TEST_SNAPSHOT")
    settings = config(from_snapshot=snapshot)
    env = await start(settings)
    try:
        marker = await env.exec(["cat", "/home/boxd/snapshot-marker"])
        assert marker.success, marker.stderr
    finally:
        await stop(env, settings)


async def test_cleanup_by_id_finds_a_machine_in_another_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = os.environ.get("BOXD_INSPECT_TEST_OTHER_REGION")
    if not other:
        pytest.skip("set BOXD_INSPECT_TEST_OTHER_REGION")
    monkeypatch.setenv("BOXD_REGION", REGION)
    settings = config(region=other)
    env = await start(settings)
    try:
        await BoxdSandboxEnvironment.cli_cleanup(env.machine_id)
        with pytest.raises(NotFoundError):
            await AsyncBoxd(region=other).machines.get(env.machine_id, region=other)
    finally:
        await stop(env, settings)


@pytest.mark.parametrize("variable", ["BOXD_API_KEY", "BOXD_TOKEN"])
async def test_a_wrong_key_fails_task_init(monkeypatch: pytest.MonkeyPatch, variable: str) -> None:
    for name in ("BOXD_API_KEY", "BOXD_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    fake_key = "bxd_" + "0" * 40
    monkeypatch.setenv(variable, fake_key if variable == "BOXD_API_KEY" else "x.y.z")
    with pytest.raises(Exception, match=r"(?i)auth|key|token|credential|unauthenticated"):
        await BoxdSandboxEnvironment.task_init("lifecycle", config())


async def test_a_missing_key_fails_task_init(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("BOXD_API_KEY", "BOXD_TOKEN", "BOXD_VM_ID", "BOXD_VM_NAME", "BOXD_PROXY_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(Exception, match="BOXD_API_KEY"):
        await BoxdSandboxEnvironment.task_init("lifecycle", config())
