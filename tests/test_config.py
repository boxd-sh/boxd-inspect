from __future__ import annotations

import json
from pathlib import Path

import pytest
from inspect_ai.util import ComposeConfig, ComposeService

from boxd_inspect import BoxdSandboxEnvironment, BoxdSandboxEnvironmentConfig
from boxd_inspect._config import resolve_config


def test_defaults_isolate_the_machine() -> None:
    config, compose = resolve_config(None)
    assert config.isolated is True
    assert compose is None


def test_our_object_passes_through() -> None:
    given = BoxdSandboxEnvironmentConfig(region="fra", vcpu=4)
    config, compose = resolve_config(given)
    assert config is given
    assert compose is None
    assert config.machine_memory() == "16G"


def test_memory_overrides_the_size_that_goes_with_vcpu() -> None:
    assert BoxdSandboxEnvironmentConfig(vcpu=2, memory="4G").machine_memory() == "4G"
    assert BoxdSandboxEnvironmentConfig().machine_memory() is None


def test_a_compose_file_runs_in_the_machine(tmp_path: Path) -> None:
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text("services:\n  default:\n    image: alpine\n")
    config, compose = resolve_config(str(compose_file))
    assert compose == str(compose_file)
    assert config == BoxdSandboxEnvironmentConfig()


def test_a_dockerfile_runs_in_the_machine(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM alpine\n")
    _, compose = resolve_config(str(dockerfile))
    assert compose == str(dockerfile)


def test_a_compose_config_object() -> None:
    given = ComposeConfig(services={"default": ComposeService(image="alpine")})
    _, compose = resolve_config(given)
    assert compose is given


def test_compose_field_of_our_object(tmp_path: Path) -> None:
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text("services:\n  default:\n    image: alpine\n")
    _, compose = resolve_config(BoxdSandboxEnvironmentConfig(compose=str(compose_file)))
    assert compose == str(compose_file)


def test_a_json_file(tmp_path: Path) -> None:
    settings = tmp_path / "boxd.json"
    settings.write_text(json.dumps({"region": "nyc", "vcpu": 8}))
    config, _ = resolve_config(str(settings))
    assert config.region == "nyc"
    assert config.machine_memory() == "32G"


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"region": "ams"}, "region"),
        ({"vcpu": 3}, "vcpu"),
        ({"memory": "6G"}, "memory"),
        ({"name_prefix": "Inspect"}, "name_prefix"),
        ({"name_prefix": "a" * 21}, "name_prefix"),
        ({"working_dir": "work"}, "working_dir"),
        ({"labels": {"Team": "x"}}, "label key"),
        ({"labels": {"boxd/x": "y"}}, "label key"),
        ({"labels": {"team": "a\nb"}}, "control character"),
        ({"compose": "settings.toml"}, "compose"),
    ],
)
def test_bad_values_fail_before_any_request(fields: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        resolve_config(BoxdSandboxEnvironmentConfig.model_validate(fields))


def test_unknown_fields_are_refused() -> None:
    with pytest.raises(ValueError, match="api_key"):
        BoxdSandboxEnvironmentConfig.model_validate({"api_key": "secret"})


def test_other_types_are_refused() -> None:
    with pytest.raises(TypeError, match="BoxdSandboxEnvironmentConfig"):
        resolve_config(42)


def test_hashable_and_round_trips_through_the_log() -> None:
    config = BoxdSandboxEnvironmentConfig(labels={"team": "evals"}, region="fra")
    assert hash(config) == hash(
        BoxdSandboxEnvironmentConfig(labels={"team": "evals"}, region="fra")
    )
    restored = BoxdSandboxEnvironment.config_deserialize(config.model_dump())
    assert restored == config
