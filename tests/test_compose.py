from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest
import yaml
from inspect_ai.util import ComposeConfig, ComposeService

from boxd_inspect._compose import GENERATED_FILE, compose_source, default_service


def names(archive: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        return {
            m.name.removeprefix("./"): tar.extractfile(m).read()  # type: ignore[union-attr]
            for m in tar.getmembers()
            if m.isfile()
        }


def test_a_compose_file_goes_with_its_build_context(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text(
        "services:\n  default:\n    build: .\n  db:\n    image: x\n"
    )
    (tmp_path / "Dockerfile").write_text("FROM alpine\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref")
    source = compose_source(str(tmp_path / "compose.yaml"))
    files = names(source.archive)
    assert source.file == "compose.yaml"
    assert set(files) == {"compose.yaml", "Dockerfile"}
    assert source.services == ["default", "db"]
    assert source.default_service == "default"


def test_a_dockerfile_gets_the_compose_file_inspect_makes_for_one(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM alpine\n")
    source = compose_source(str(tmp_path / "Dockerfile"))
    generated = yaml.safe_load(names(source.archive)[GENERATED_FILE])
    service = generated["services"]["default"]
    assert service["build"] == {"context": ".", "dockerfile": "Dockerfile"}
    assert service["command"] == "tail -f /dev/null"
    assert service["network_mode"] == "none"
    assert source.file == GENERATED_FILE


def test_an_edited_context_is_packed_again(tmp_path: Path) -> None:
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text("services:\n  default:\n    image: a\n")
    first = compose_source(str(compose_file))
    assert compose_source(str(compose_file)) is first
    extra = tmp_path / "data.txt"
    extra.write_text("new")
    import os

    os.utime(extra, (first_mtime := compose_file.stat().st_mtime + 10, first_mtime))
    assert compose_source(str(compose_file)) is not first


def test_a_compose_config_object_is_written_out() -> None:
    config = ComposeConfig(
        services={"default": ComposeService(image="python:3.12", command="sleep infinity")}
    )
    source = compose_source(config)
    written = yaml.safe_load(names(source.archive)[GENERATED_FILE])
    assert written == {
        "services": {"default": {"image": "python:3.12", "command": "sleep infinity"}}
    }


def test_x_default_picks_the_default_service(tmp_path: Path) -> None:
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text(
        "services:\n  web:\n    image: a\n  agent:\n    image: b\n    x-default: true\n"
    )
    assert compose_source(str(compose_file)).default_service == "agent"


def test_no_default_service_is_an_error() -> None:
    config = ComposeConfig(services={"web": ComposeService(image="a")})
    with pytest.raises(ValueError, match="x-default"):
        default_service(config)


def test_missing_compose_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        compose_source(str(tmp_path / "compose.yaml"))
