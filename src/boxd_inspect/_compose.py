"""A compose project to run with Docker inside the machine.

The compose file (or a ``Dockerfile``, wrapped in the same compose file Inspect's
Docker sandbox makes for one) goes to the machine with its directory, so build
contexts next to it work as they do locally.
"""

from __future__ import annotations

import io
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path

import yaml
from inspect_ai.util import ComposeConfig, is_dockerfile, parse_compose_yaml

GENERATED_FILE = ".boxd-inspect-compose.yaml"
"""The compose file this package writes next to a ``Dockerfile``."""

MAX_CONTEXT_BYTES = 512 * 1024 * 1024
"""The largest directory (packed) sent to each machine."""

DOCKERFILE_COMPOSE = """\
services:
  default:
    build:
      context: "."
      dockerfile: "{dockerfile}"
    command: "tail -f /dev/null"
    init: true
    network_mode: none
    stop_grace_period: 1s
"""
"""What Inspect's Docker sandbox runs for a lone ``Dockerfile``."""


@dataclass(frozen=True)
class ComposeSource:
    """What goes to the machine: a packed directory and the compose file in it."""

    archive: bytes
    file: str
    services: list[str]
    default_service: str


_cache: dict[tuple[str, float], ComposeSource] = {}


def compose_source(compose: str | ComposeConfig) -> ComposeSource:
    """Pack the compose project once per file version; samples reuse the result."""
    if isinstance(compose, ComposeConfig):
        return _from_config(compose)
    path = Path(compose).resolve()
    key = (str(path), _directory_mtime(path.parent))
    cached = _cache.get(key)
    if cached is None:
        cached = _from_file(path)
        _cache[key] = cached
    return cached


def _from_file(path: Path) -> ComposeSource:
    if not path.is_file():
        raise FileNotFoundError(f"{path} does not exist")
    extra: dict[str, bytes] = {}
    if is_dockerfile(str(path)):
        text = DOCKERFILE_COMPOSE.format(dockerfile=path.name)
        extra[GENERATED_FILE] = text.encode()
        config = ComposeConfig.model_validate(yaml.safe_load(text))
        file = GENERATED_FILE
    else:
        config = parse_compose_yaml(str(path), multiple_services=True)
        file = path.name
    services = list(config.services)
    return ComposeSource(
        archive=_pack(path.parent, extra),
        file=file,
        services=services,
        default_service=default_service(config),
    )


def _from_config(config: ComposeConfig) -> ComposeSource:
    text = yaml.safe_dump(
        config.model_dump(mode="json", by_alias=True, exclude_none=True), sort_keys=False
    )
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        _add_bytes(tar, GENERATED_FILE, text.encode())
    return ComposeSource(
        archive=buffer.getvalue(),
        file=GENERATED_FILE,
        services=list(config.services),
        default_service=default_service(config),
    )


def default_service(config: ComposeConfig) -> str:
    """Pick the service Inspect calls ``default``: ``x-default: true``, else the one named so."""
    for name, service in config.services.items():
        extra = service.model_extra or {}
        if getattr(service, "x_default", None) is True or extra.get("x-default") is True:
            return name
    if "default" in config.services:
        return "default"
    raise ValueError(
        "No 'default' service in the compose file. Name a service 'default' "
        "or add 'x-default: true' to one."
    )


def _pack(directory: Path, extra: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(directory, arcname=".", filter=_portable)
        for name, data in extra.items():
            _add_bytes(tar, name, data)
    data = buffer.getvalue()
    if len(data) > MAX_CONTEXT_BYTES:
        raise ValueError(
            f"{directory} packs to {len(data) // (1024 * 1024)} MiB, more than the "
            f"{MAX_CONTEXT_BYTES // (1024 * 1024)} MiB sent to each machine. Move the "
            "compose file to a smaller directory, or start from a snapshot."
        )
    return data


def _portable(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """Own every entry by the machine's user; skip devices and version-control data."""
    if info.isdev() or "/.git/" in f"/{info.name}/":
        return None
    info.uid = info.gid = 1000
    info.uname = info.gname = "boxd"
    return info


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


def _directory_mtime(directory: Path) -> float:
    """Find the newest change in a directory tree, so an edited build context is re-packed."""
    newest = directory.stat().st_mtime
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            try:
                newest = max(newest, os.stat(os.path.join(root, name)).st_mtime)
            except OSError:
                continue
    return newest
