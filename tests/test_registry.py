from __future__ import annotations

from importlib.metadata import entry_points

from inspect_ai.util._sandbox.registry import registry_find_sandboxenv

from boxd_inspect import BoxdSandboxEnvironment


def test_the_entry_point_registers_boxd() -> None:
    names = [e.value for e in entry_points(group="inspect_ai")]
    assert "boxd_inspect._registry" in names
    assert registry_find_sandboxenv("boxd") is BoxdSandboxEnvironment


def test_docker_compatible_config_files() -> None:
    assert BoxdSandboxEnvironment.is_docker_compatible()
    assert "compose.yaml" in BoxdSandboxEnvironment.config_files()
    assert "Dockerfile" in BoxdSandboxEnvironment.config_files()
