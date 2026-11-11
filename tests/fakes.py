"""A fake boxd client, recording calls instead of making machines."""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from boxd import NotFoundError
from boxd.models import RegionalList


@dataclass
class Result:
    stdout: bytes = b""
    stderr: bytes = b""
    exit_code: int = 0


Handler = Callable[[str], Result]


@dataclass
class FakeFiles:
    uploads: dict[str, bytes] = field(default_factory=dict)
    on_upload: Callable[[str, bytes], None] | None = None

    async def upload(self, machine_id: str, path: str, data: bytes | str, **_: Any) -> int:
        raw = data.encode() if isinstance(data, str) else data
        if not raw:
            raise AssertionError("an empty upload is refused by the API")
        self.uploads[path] = raw
        if self.on_upload is not None:
            self.on_upload(path, raw)
        return len(raw)


@dataclass
class FakeMachines:
    handler: Handler = lambda line: Result()
    created: list[dict[str, Any]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    exec_kwargs: list[dict[str, Any]] = field(default_factory=list)
    listed: list[dict[str, Any]] = field(default_factory=list)
    ready_error: BaseException | None = None
    exec_error: BaseException | None = None
    files: FakeFiles = field(default_factory=FakeFiles)
    labels: dict[str, dict[str, str]] = field(default_factory=dict)
    chunk_size: int = 1024 * 1024
    chunks_read: int = 0
    exec_delay: float = 0.0
    existing: list[Any] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    deleted_in: dict[str, str | None] = field(default_factory=dict)

    async def create(self, name: str, **kwargs: Any) -> Any:
        machine = SimpleNamespace(id=f"id-{len(self.created) + 1}", name=name, status="pending")
        self.created.append({"name": name, **kwargs})
        return machine

    async def wait_until_ready(self, machine_id: str, **_: Any) -> Any:
        if self.ready_error is not None:
            raise self.ready_error
        return SimpleNamespace(id=machine_id, status="running")

    async def exec(self, machine_id: str, line: str, **kwargs: Any) -> Any:
        self.lines.append(line)
        self.exec_kwargs.append(kwargs)
        if self.exec_error is not None:
            raise self.exec_error
        result = self.handler(line)
        if kwargs.get("encoding") == "buffer":
            return result
        return SimpleNamespace(
            stdout=result.stdout.decode(), stderr=result.stderr.decode(), exit_code=result.exit_code
        )

    def stream_exec(self, machine_id: str, *, command: str, **kwargs: Any) -> FakeStream:
        self.lines.append(command)
        self.exec_kwargs.append({"stream": True, **kwargs})
        return FakeStream(self, command)

    async def get(self, machine_id: str, **_: Any) -> Any:
        if machine_id not in self.labels:
            raise NotFoundError("VM not found")  # type: ignore[no-untyped-call]
        return SimpleNamespace(id=machine_id, labels=self.labels.get(machine_id, {}), region=None)

    async def delete(self, machine_id: str, *, region: str | None = None, **_: Any) -> None:
        self.deleted.append(machine_id)
        self.deleted_in[machine_id] = region

    async def list(self, **kwargs: Any) -> RegionalList[Any]:
        """``existing``, filtered as the API does: by labels, and by region unless it is "all"."""
        self.listed.append(kwargs)
        labels = kwargs.get("labels") or {}
        region = kwargs.get("region")
        found = [
            machine
            for machine in self.existing
            if all(machine.labels.get(key) == value for key, value in labels.items())
            and (region == "all" or machine.region == region)
        ]
        return RegionalList(found, unavailable=self.unavailable if region == "all" else [])


def existing_machine(
    machine_id: str, status: str, region: str = "nyc", made_by_us: bool = True
) -> Any:
    """A machine the fake's ``list`` returns."""
    labels = {"inspect-sandbox": "boxd"} if made_by_us else {}
    return SimpleNamespace(
        id=machine_id, name=f"inspect-{machine_id}", status=status, region=region, labels=labels
    )


class FakeStream:
    """The fake's ``stream_exec``: the handler's output in chunks, stdout first."""

    def __init__(self, machines: FakeMachines, line: str) -> None:
        self._machines = machines
        self._line = line
        self.exit_code: int | None = None
        self.closed = False

    async def __aenter__(self) -> FakeStream:
        if self._machines.exec_error is not None:
            raise self._machines.exec_error
        return self

    async def __aexit__(self, *_: object) -> None:
        self.closed = True

    async def iter_chunks(self) -> Any:
        if self._machines.exec_delay:
            await asyncio.sleep(self._machines.exec_delay)
        result = self._machines.handler(self._line)
        size = self._machines.chunk_size
        for data, is_stderr in ((result.stdout, False), (result.stderr, True)):
            for start in range(0, len(data), size):
                self._machines.chunks_read += 1
                yield SimpleNamespace(data=data[start : start + size], is_stderr=is_stderr)
        self.exit_code = result.exit_code


class FakeClient:
    def __init__(self, machines: FakeMachines | None = None) -> None:
        self.machines = machines or FakeMachines()


def local_machine(home: Path) -> FakeMachines:
    """A fake whose exec runs the line in a local shell, with ``/home/boxd`` at ``home``.

    Commands that switch user are refused, as on a machine without ``sudo``.
    """
    machines = FakeMachines()

    def rewrite(text: str) -> str:
        return text.replace("/home/boxd", str(home))

    def write(path: str, data: bytes) -> None:
        target = Path(rewrite(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def run(line: str) -> Result:
        if "/usr/bin/sudo" in line:
            return Result(stderr=b"sudo: a password is required\n", exit_code=1)
        completed = subprocess.run(
            ["/bin/sh", "-c", rewrite(line)],
            capture_output=True,
            env={**os.environ, "HOME": str(home)},
            check=False,
        )
        return Result(completed.stdout, completed.stderr, completed.returncode)

    machines.handler = run
    machines.files.on_upload = write
    return machines
