"""The ``boxd`` sandbox environment: one boxd machine per sample."""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import posixpath
import re
import shlex
import sys
import time
import uuid
import weakref
from contextvars import ContextVar
from typing import Any, Literal, overload

import anyio
from boxd import AsyncBoxd, BoxdError, ConflictError, NotFoundError
from inspect_ai.util import (
    ComposeConfig,
    ExecResult,
    OutputLimitExceededError,
    SandboxConnection,
    SandboxEnvironment,
    SandboxEnvironmentConfigType,
    SandboxEnvironmentLimits,
    SandboxUnavailableError,
    SandboxUserUnsupportedError,
    sandboxenv,
    trace_message,
)
from pydantic import BaseModel
from typing_extensions import override

from ._commands import (
    PWD_SCRIPT,
    READ_SCRIPT,
    TIMEOUT_KILL_AFTER,
    WRITE_SCRIPT,
    Compose,
    exec_argv,
    privileged_argv,
    script_line,
    shell_line,
)
from ._compose import compose_source
from ._config import BoxdSandboxEnvironmentConfig, resolve_config

logger = logging.getLogger(__name__)

SANDBOX_LABEL = "inspect-sandbox"
"""Every machine this package makes has ``inspect-sandbox=boxd``."""

MACHINE_HOME = "/home/boxd"
TEMP_DIR = f"{MACHINE_HOME}/.boxd-inspect"
"""Stdin, scripts and archives on their way in: owned by ``boxd``, mode 0700,
made before the first command, so no other user can read or swap them."""

FILE_TIMEOUT = 600
"""Seconds ``read_file`` and ``write_file`` may take, as in Inspect's Docker sandbox."""
COMPOSE_DIR = f"{MACHINE_HOME}/inspect-compose"
COMPOSE_PROJECT = "inspect"

MAX_COMMAND_LINE = 64 * 1024
"""Longer exec lines go to the machine as a script file: one argument to the
machine's shell is capped at 128 KiB."""

EXEC_SLACK = 15
"""Seconds the API call waits beyond an exec's own timeout and its KILL grace."""

_now = time.monotonic

_run_id: ContextVar[str | None] = ContextVar("boxd_run_id", default=None)
_clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str | None, AsyncBoxd]] = (
    weakref.WeakKeyDictionary()
)
"""Clients per event loop and region: a client cannot outlive the loop it was made on."""
_machines: ContextVar[dict[str, str | None] | None] = ContextVar("boxd_machines", default=None)
"""Machines this task made and has not deleted yet (id to region). Set in
``task_init``, so two evals in one process keep their own lists."""
_untracked: dict[str, str | None] = {}
"""The same, for samples started without ``task_init``."""


def _tracked() -> dict[str, str | None]:
    machines = _machines.get()
    return machines if machines is not None else _untracked


class _Output:
    """What a command printed, its exit code, and whether it went past the limit."""

    def __init__(self) -> None:
        self.exit_code = -1
        self.over_limit = False
        self._out = bytearray()
        self._err = bytearray()

    @property
    def stdout(self) -> bytes:
        return bytes(self._out)

    @property
    def stderr(self) -> bytes:
        return bytes(self._err)

    def add(self, data: bytes, is_stderr: bool, limit: int) -> bool:
        """Keep a chunk; True once a stream is past ``limit`` (the rest is not read)."""
        buffer = self._err if is_stderr else self._out
        buffer += data
        if len(buffer) > limit:
            self.over_limit = True
        return self.over_limit


@sandboxenv(name="boxd")
class BoxdSandboxEnvironment(SandboxEnvironment):
    """A sample's sandbox: a boxd machine, deleted when the sample ends.

    Commands run on the machine (Ubuntu, user ``boxd`` with passwordless
    ``sudo``) or, with a compose file or ``Dockerfile``, in a service Docker runs
    inside the machine. Several services in one compose file share the machine.
    """

    def __init__(
        self,
        client: AsyncBoxd,
        machine_id: str,
        machine_name: str,
        region: str | None,
        working_dir: str,
        compose: Compose | None = None,
    ) -> None:
        super().__init__()
        self._client = client
        self._machine_id = machine_id
        self._machine_name = machine_name
        self._region = region
        self._working_dir = working_dir
        self._compose = compose

    @property
    def machine_id(self) -> str:
        """The boxd machine's id."""
        return self._machine_id

    @property
    def machine_name(self) -> str:
        """The boxd machine's name."""
        return self._machine_name

    # --- the sandbox ---

    @override
    async def exec(
        self,
        cmd: list[str],
        input: str | bytes | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        user: str | None = None,
        timeout: int | None = None,
        timeout_retry: bool = True,
        concurrency: bool = True,
    ) -> ExecResult[str]:
        _check_user(user)
        argv = exec_argv(
            cmd,
            cwd=self._path(cwd) if cwd is not None else self._working_dir,
            env=env,
            user=user,
            timeout=timeout,
            compose=self._compose,
        )
        stdin = input.encode() if isinstance(input, str) else input
        started = _now()
        limit = SandboxEnvironmentLimits.MAX_EXEC_OUTPUT_SIZE
        result = await self._run(argv, stdin=stdin, timeout=timeout, limit=limit)
        elapsed = _now() - started
        stdout = _text(result.stdout)
        stderr = _text(result.stderr)

        # `timeout` ends a command with 124 (TERM) or 137 (KILL). A command
        # can exit so by itself, but not before the time is up.
        if timeout is not None and result.exit_code in (124, 137, 143) and elapsed >= timeout:
            error = TimeoutError(f"Command timed out after {timeout} seconds")
            if stdout or stderr:
                error.truncated_output = stdout + stderr  # type: ignore[attr-defined]
            raise error
        # 126 is "found but cannot run". Docker reports it on stdout.
        if result.exit_code == 126 and "permission denied" in (stderr + stdout).casefold():
            message = (stderr or stdout).strip()
            raise PermissionError(errno.EACCES, f"Permission denied: {message}", cmd[0])
        if result.over_limit:
            raise OutputLimitExceededError(
                SandboxEnvironmentLimits.MAX_EXEC_OUTPUT_SIZE_STR, _text(result.stdout[-4096:])
            )
        return ExecResult(
            success=result.exit_code == 0,
            returncode=result.exit_code,
            stdout=stdout,
            stderr=stderr,
        )

    @override
    async def write_file(
        self, file: str, contents: str | bytes, *, user: str | None = None
    ) -> None:
        _check_user(user)
        path = self._path(file)
        data = contents.encode() if isinstance(contents, str) else contents
        argv = privileged_argv(WRITE_SCRIPT, path, user=user, compose=self._compose)
        result = await self._run(
            argv,
            stdin=data,
            timeout=FILE_TIMEOUT,
            limit=SandboxEnvironmentLimits.MAX_EXEC_OUTPUT_SIZE,
        )
        if result.exit_code != 0:
            _raise_file_error(_text(result.stderr), result.exit_code, file, user)

    @overload
    async def read_file(
        self, file: str, text: Literal[True] = True, *, user: str | None = None
    ) -> str: ...

    @overload
    async def read_file(
        self, file: str, text: Literal[False], *, user: str | None = None
    ) -> bytes: ...

    @override
    async def read_file(
        self, file: str, text: bool = True, *, user: str | None = None
    ) -> str | bytes:
        _check_user(user)
        limit = SandboxEnvironmentLimits.MAX_READ_FILE_SIZE
        argv = privileged_argv(
            READ_SCRIPT, self._path(file), str(limit), user=user, compose=self._compose
        )
        result = await self._run(argv, stdin=None, timeout=FILE_TIMEOUT, limit=limit)
        if result.exit_code == 27 or result.over_limit:
            raise OutputLimitExceededError(SandboxEnvironmentLimits.MAX_READ_FILE_SIZE_STR, None)
        if result.exit_code != 0:
            _raise_file_error(_text(result.stderr), result.exit_code, file, user)
        data: bytes = result.stdout
        return data.decode("utf-8") if text else data

    @override
    async def connection(self, *, user: str | None = None) -> SandboxConnection:
        """``boxd connect`` opens a shell on the machine; ``container`` names the service."""
        return SandboxConnection(
            type="boxd",
            command=f"boxd connect {self._machine_name}",
            container=self._compose.service if self._compose is not None else None,
        )

    def _path(self, path: str) -> str:
        """Relative paths resolve against the working directory, as Inspect says."""
        return posixpath.join(self._working_dir, path)

    async def _run(
        self, argv: list[str], *, stdin: bytes | None, timeout: int | None, limit: int
    ) -> _Output:
        """Run ``argv`` on the machine; ``stdin`` goes up as a temp file first.

        Output streams back and stops at ``limit`` bytes per stream: past it the
        call is cancelled, which stops the command on the machine.
        """
        base = f"{TEMP_DIR}/{uuid.uuid4().hex}"
        temps: list[str] = []
        stdin_path = None
        if stdin == b"":
            stdin_path = "/dev/null"
        elif stdin is not None:
            stdin_path = f"{base}.in"
            await self._upload(stdin_path, stdin)
            temps.append(stdin_path)
        line = shell_line(argv, stdin=stdin_path, remove=temps)
        if len(line) > MAX_COMMAND_LINE:
            script = f"{base}.sh"
            await self._upload(script, line)
            line = script_line(script, remove=[script])
        call_timeout = None if timeout is None else timeout + TIMEOUT_KILL_AFTER + EXEC_SLACK
        output = _Output()
        try:
            with anyio.fail_after(call_timeout):
                stream = self._client.machines.stream_exec(
                    self._machine_id, command=line, close_stdin=True, region=self._region
                )
                async with stream:
                    async for chunk in stream.iter_chunks():  # type: ignore[no-untyped-call]
                        if output.add(chunk.data, chunk.is_stderr, limit):
                            break
                if not output.over_limit:
                    output.exit_code = stream.exit_code if stream.exit_code is not None else -1
        except TimeoutError as error:
            raise TimeoutError(f"Command timed out after {timeout} seconds") from error
        except (NotFoundError, ConflictError) as error:
            raise SandboxUnavailableError(
                f"boxd machine {self._machine_name} is not available: {error}"
            ) from error
        return output

    async def _upload(self, path: str, data: bytes | str) -> None:
        try:
            await self._client.machines.files.upload(
                self._machine_id, path, data, region=self._region
            )
        except (NotFoundError, ConflictError) as error:
            raise SandboxUnavailableError(
                f"boxd machine {self._machine_name} is not available: {error}"
            ) from error

    # --- the lifecycle ---

    @override
    @classmethod
    def config_files(cls) -> list[str]:
        return [
            "compose.yaml",
            "compose.yml",
            "docker-compose.yaml",
            "docker-compose.yml",
            "Dockerfile",
        ]

    @override
    @classmethod
    def is_docker_compatible(cls) -> bool:
        return True

    @override
    @classmethod
    def config_deserialize(cls, config: dict[str, Any]) -> BaseModel:
        return BoxdSandboxEnvironmentConfig.model_validate(config)

    @override
    @classmethod
    async def task_init(cls, task_name: str, config: SandboxEnvironmentConfigType | None) -> None:
        """Check the configuration and the API key before the first sample."""
        resolved, compose = resolve_config(config)
        if compose is not None:
            await anyio.to_thread.run_sync(compose_source, compose)
        _run_id.set(uuid.uuid4().hex[:12])
        _machines.set({})
        client = _client(resolved.region)
        await client.machines.list(
            org=_org(resolved),
            labels={SANDBOX_LABEL: "boxd", "inspect-run": _current_run_id()},
            region=resolved.region,
        )

    @override
    @classmethod
    async def sample_init(
        cls,
        task_name: str,
        config: SandboxEnvironmentConfigType | None,
        metadata: dict[str, str],
    ) -> dict[str, SandboxEnvironment]:
        resolved, compose = resolve_config(config)
        client = _client(resolved.region)
        name = machine_name(resolved.name_prefix, task_name, metadata)
        labels = {
            **resolved.labels,
            SANDBOX_LABEL: "boxd",
            "inspect-run": _current_run_id(),
            "inspect-task": _slug(task_name, 200) or "task",
        }
        # Shielded: a machine created while the sample is cancelled is still
        # recorded, and so deleted.
        with anyio.CancelScope(shield=True):
            machine = await _create(client, resolved, name, labels)
        trace_message(logger, "boxd", f"Created machine {machine.name} ({machine.id})")
        try:
            await client.machines.wait_until_ready(
                machine.id, timeout=resolved.ready_timeout, region=resolved.region
            )
            await _prepare(client, machine, resolved, compose is None)
            if compose is None:
                working_dir = resolved.working_dir or MACHINE_HOME
                return {
                    "default": BoxdSandboxEnvironment(
                        client, machine.id, machine.name, resolved.region, working_dir
                    )
                }
            return await _compose_environments(client, machine, resolved, compose)
        except BaseException:
            await _delete(client, machine.id, resolved.region)
            raise

    @override
    @classmethod
    async def sample_cleanup(
        cls,
        task_name: str,
        config: SandboxEnvironmentConfigType | None,
        environments: dict[str, SandboxEnvironment],
        interrupted: bool,
    ) -> None:
        done: set[str] = set()
        for environment in environments.values():
            if not isinstance(environment, BoxdSandboxEnvironment):
                continue
            if environment._machine_id in done:
                continue
            done.add(environment._machine_id)
            await _delete(environment._client, environment._machine_id, environment._region)

    @override
    @classmethod
    async def task_cleanup(
        cls, task_name: str, config: SandboxEnvironmentConfigType | None, cleanup: bool
    ) -> None:
        """Delete the machines whose samples did not clean up (by id, never by search)."""
        machines = _tracked()
        if not machines:
            return
        if not cleanup:
            ids = ", ".join(sorted(machines))
            print(f"boxd machines left running for inspection: {ids}", file=sys.stderr)
            print("Delete them with: inspect sandbox cleanup boxd <id>", file=sys.stderr)
            machines.clear()
            return
        for machine_id, region in list(machines.items()):
            await _delete(_client(region), machine_id, region)

    @override
    @classmethod
    async def cli_cleanup(cls, id: str | None) -> None:
        """``inspect sandbox cleanup boxd [id]``: one machine, or all made by this package."""
        client = _client(os.environ.get("BOXD_REGION") or None)
        if id is not None:
            try:
                machine = await client.machines.get(id)
            except NotFoundError:
                print(f"No boxd machine {id}.")
                sys.exit(1)
            if machine.labels.get(SANDBOX_LABEL) != "boxd":
                print(f"{id} was not made by boxd-inspect (no {SANDBOX_LABEL}=boxd label): kept.")
                sys.exit(1)
            await client.machines.delete(id)
            print(f"Deleted boxd machine {id}")
            return
        machines = await client.machines.list(
            org=os.environ.get("BOXD_ORG") or None, labels={SANDBOX_LABEL: "boxd"}
        )
        if not machines:
            print("No boxd machines made by boxd-inspect.")
            return
        for machine in machines:
            print(f"{machine.id}  {machine.name}  {machine.status}")
        interactive = sys.stdin.isatty() and "CI" not in os.environ
        if interactive:
            answer = input(f"Delete these {len(machines)} machine(s)? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                print("Cancelled.")
                return
        failed = 0
        for machine in machines:
            try:
                await client.machines.delete(machine.id)
            except BoxdError as error:
                failed += 1
                print(f"Could not delete {machine.id}: {error}")
        print(f"Deleted {len(machines) - failed} machine(s).")
        if failed:
            sys.exit(1)


async def _create(
    client: AsyncBoxd, config: BoxdSandboxEnvironmentConfig, name: str, labels: dict[str, str]
) -> Any:
    machine = await client.machines.create(
        name,
        org=_org(config),
        isolated=config.isolated,
        labels=labels,
        vcpu=config.vcpu,
        memory=config.machine_memory(),
        from_snapshot=config.from_snapshot,
        auto_suspend_timeout=0,
        delete_after=config.delete_after,
        region=config.region,
    )
    _tracked()[machine.id] = config.region
    return machine


async def _prepare(
    client: AsyncBoxd, machine: Any, config: BoxdSandboxEnvironmentConfig, on_machine: bool
) -> None:
    """Make the private temp directory and, on the machine, the working directory.

    Other users (``exec(user=...)``) start in the working directory too, so they
    need to pass through the home directory, as in a container's.
    """
    setup = f"/usr/bin/install -d -m 700 {TEMP_DIR}"
    working_dir = config.working_dir or MACHINE_HOME
    if on_machine:
        setup += f" && /bin/chmod o+x {MACHINE_HOME}"
        if working_dir != MACHINE_HOME:
            quoted = shlex.quote(working_dir)
            setup += (
                f" && /usr/bin/sudo -n /bin/mkdir -p -- {quoted}"
                f" && /usr/bin/sudo -n /bin/chown boxd:boxd -- {quoted}"
            )
    result = await client.machines.exec(machine.id, setup, region=config.region)
    if result.exit_code != 0:
        raise RuntimeError(
            f"Could not prepare boxd machine {machine.name}: {_text(result.stderr).strip()}"
        )


async def _compose_environments(
    client: AsyncBoxd,
    machine: Any,
    config: BoxdSandboxEnvironmentConfig,
    compose: str | ComposeConfig,
) -> dict[str, SandboxEnvironment]:
    """Unpack the compose project in the machine, start it, and wrap each service."""
    source = await anyio.to_thread.run_sync(compose_source, compose)
    archive = f"{TEMP_DIR}/{uuid.uuid4().hex}.tar.gz"
    await client.machines.files.upload(machine.id, archive, source.archive, region=config.region)
    project = Compose(COMPOSE_PROJECT, COMPOSE_DIR, f"{COMPOSE_DIR}/{source.file}", "")
    base = shlex.join(project.base())

    unpack = (
        f"/bin/mkdir -p {COMPOSE_DIR} && /bin/tar -xzf {archive} -C {COMPOSE_DIR}"
        f" && /bin/rm -f {archive}"
    )
    up_wait = int(config.compose_timeout)
    up = f"{base} up --detach --wait --wait-timeout {up_wait} --quiet-pull 2>&1"
    started = await client.machines.exec(
        machine.id,
        f"{unpack} && {{ {up}; }}",
        timeout=config.compose_timeout + 60,
        region=config.region,
    )
    # `up --wait` also fails when a service exits on purpose, so the running
    # services decide, as in Inspect's Docker sandbox.
    running = await client.machines.exec(
        machine.id,
        f"{base} ps --status running --format '{{{{.Service}}}}'",
        timeout=120,
        region=config.region,
    )
    running_services = set(_text(running.stdout).split())
    if source.default_service not in running_services:
        output = _text(started.stdout)[-4000:] + _text(started.stderr)[-2000:]
        raise RuntimeError(
            f"The compose service '{source.default_service}' did not start on boxd "
            f"machine {machine.name}.\n{output}"
        )

    environments: dict[str, SandboxEnvironment] = {}
    ordered = [source.default_service] + [
        s for s in source.services if s != source.default_service and s in running_services
    ]
    for service in ordered:
        service_compose = Compose(project.project, project.directory, project.file, service)
        working_dir: str | None = config.working_dir
        if working_dir is None:
            pwd = await client.machines.exec(
                machine.id,
                shell_line(privileged_argv(PWD_SCRIPT, user=None, compose=service_compose)),
                timeout=120,
                region=config.region,
            )
            printed = _text(pwd.stdout).strip()
            working_dir = printed if pwd.exit_code == 0 and printed else "/"
        environments[service] = BoxdSandboxEnvironment(
            client, machine.id, machine.name, config.region, working_dir, service_compose
        )
    return environments


async def _delete(client: AsyncBoxd, machine_id: str, region: str | None) -> None:
    """Delete a machine this process made; a failure only warns. Not cancellable."""
    with anyio.CancelScope(shield=True):
        try:
            await client.machines.delete(machine_id, region=region)
            trace_message(logger, "boxd", f"Deleted machine {machine_id}")
        except NotFoundError:
            pass
        except Exception as error:
            logger.warning(f"Could not delete boxd machine {machine_id}: {error}")
            return
    _tracked().pop(machine_id, None)


def _client(region: str | None) -> AsyncBoxd:
    """One client per event loop and region. The key comes from ``BOXD_API_KEY``."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        raise RuntimeError("The boxd sandbox needs Inspect's asyncio backend.") from None
    clients = _clients.setdefault(loop, {})
    client = clients.get(region)
    if client is None:
        client = _make_client(region)
        clients[region] = client
    return client


def _make_client(region: str | None) -> AsyncBoxd:
    return AsyncBoxd(region=region)


def _org(config: BoxdSandboxEnvironmentConfig) -> str | None:
    return config.org or os.environ.get("BOXD_ORG") or None


def _current_run_id() -> str:
    run_id = _run_id.get()
    if run_id is None:
        run_id = uuid.uuid4().hex[:12]
        _run_id.set(run_id)
    return run_id


_SLUG = re.compile(r"[^a-z0-9]+")
_NAME_MAX = 63


def _slug(value: object, length: int) -> str:
    return _SLUG.sub("-", str(value).lower()).strip("-")[:length].strip("-")


def machine_name(prefix: str, task_name: str | None, metadata: dict[str, Any]) -> str:
    """``<prefix>-<task>-<sample>-<random>``, at most 63 lowercase letters, digits and '-'."""
    suffix = uuid.uuid4().hex[:8]
    room = _NAME_MAX - len(prefix) - len(suffix) - 2
    parts = [
        p for p in (_slug(task_name or "", 30), _slug(metadata.get("__sample_id__", ""), 20)) if p
    ]
    middle = "-".join(parts)[:room].strip("-")
    return "-".join(p for p in (prefix, middle, suffix) if p)


def _check_user(user: str | None) -> None:
    if user == "":
        raise SandboxUserUnsupportedError("The user must not be empty.")


def _text(data: bytes | str) -> str:
    return data if isinstance(data, str) else data.decode("utf-8", errors="replace")


def _raise_file_error(stderr: str, exit_code: int, file: str, user: str | None) -> None:
    message = stderr.casefold()
    if exit_code == 21 or "is a directory" in message:
        raise IsADirectoryError(errno.EISDIR, "Is a directory", file)
    if "no such file" in message or "not a directory" in message:
        raise FileNotFoundError(errno.ENOENT, "No such file or directory", file)
    if "permission denied" in message or "operation not permitted" in message:
        raise PermissionError(errno.EACCES, "Permission denied", file)
    if user is not None and ("unknown user" in message or "unable to find user" in message):
        raise SandboxUserUnsupportedError(f"No user {user!r} in the sandbox: {stderr.strip()}")
    raise RuntimeError(f"File operation on {file} failed (exit {exit_code}): {stderr.strip()}")
