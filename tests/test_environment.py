from __future__ import annotations

import asyncio
import errno
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest
from boxd import BoxdError, NotFoundError
from inspect_ai.util import (
    OutputLimitExceededError,
    SandboxEnvironmentLimits,
    SandboxUnavailableError,
    SandboxUserUnsupportedError,
)

from boxd_inspect import BoxdSandboxEnvironment, BoxdSandboxEnvironmentConfig, _environment
from boxd_inspect._environment import machine_name
from tests.fakes import FakeMachines, Result, existing_machine

CONFIG = BoxdSandboxEnvironmentConfig(region="nyc", name_prefix="t-test")


async def make(fake: FakeMachines, config: object = CONFIG) -> BoxdSandboxEnvironment:
    await BoxdSandboxEnvironment.task_init("my_task", config)  # type: ignore[arg-type]
    envs = await BoxdSandboxEnvironment.sample_init(
        "my_task",
        config,  # type: ignore[arg-type]
        {"__sample_id__": "7"},
    )
    return envs["default"].as_type(BoxdSandboxEnvironment)


# --- lifecycle ---


async def test_each_sample_gets_an_isolated_machine_that_does_not_sleep(fake: FakeMachines) -> None:
    env = await make(fake)
    (created,) = fake.created
    assert created["isolated"] is True
    assert created["auto_suspend_timeout"] == 0
    assert created["region"] == "nyc"
    assert created["name"].startswith("t-test-my-task-7-")
    assert created["labels"]["inspect-sandbox"] == "boxd"
    assert created["labels"]["inspect-task"] == "my-task"
    assert len(created["labels"]["inspect-run"]) == 12
    assert env.machine_id == "id-1"


async def test_size_snapshot_org_and_labels_reach_create(
    fake: FakeMachines, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOXD_ORG", "evals")
    config = BoxdSandboxEnvironmentConfig(
        vcpu=4,
        from_snapshot="warm",
        labels={"team": "safety"},
        isolated=False,
        delete_after=7200,
    )
    await make(fake, config)
    (created,) = fake.created
    assert (created["vcpu"], created["memory"]) == (4, "16G")
    assert created["from_snapshot"] == "warm"
    assert created["org"] == "evals"
    assert created["labels"]["team"] == "safety"
    assert created["isolated"] is False
    assert created["delete_after"] == 7200


async def test_task_init_checks_the_key_with_a_list_call(fake: FakeMachines) -> None:
    await BoxdSandboxEnvironment.task_init("t", CONFIG)
    assert fake.listed[0]["labels"]["inspect-sandbox"] == "boxd"


async def test_a_machine_that_never_gets_ready_is_deleted(fake: FakeMachines) -> None:
    fake.ready_error = TimeoutError("not ready")
    with pytest.raises(TimeoutError):
        await make(fake)
    assert fake.deleted == ["id-1"]
    assert _environment._registry().machines == {}


async def test_sample_cleanup_deletes_by_id(fake: FakeMachines) -> None:
    env = await make(fake)
    await BoxdSandboxEnvironment.sample_cleanup("my_task", CONFIG, {"default": env}, False)
    assert fake.deleted == ["id-1"]
    assert _environment._registry().machines == {}


async def test_task_cleanup_deletes_what_samples_left(fake: FakeMachines) -> None:
    await make(fake)
    await BoxdSandboxEnvironment.task_cleanup("my_task", CONFIG, cleanup=True)
    assert fake.deleted == ["id-1"]


async def test_no_cleanup_leaves_the_machines_and_says_so(
    fake: FakeMachines, capsys: pytest.CaptureFixture[str]
) -> None:
    await make(fake)
    await BoxdSandboxEnvironment.task_cleanup("my_task", CONFIG, cleanup=False)
    assert fake.deleted == []
    assert "id-1" in capsys.readouterr().err


async def test_a_failed_delete_only_warns(fake: FakeMachines) -> None:
    env = await make(fake)

    async def broken(machine_id: str, **_: object) -> None:
        raise BoxdError("unavailable")

    fake.delete = broken  # type: ignore[method-assign]
    await BoxdSandboxEnvironment.sample_cleanup("my_task", CONFIG, {"default": env}, False)
    assert "id-1" in _environment._registry().machines


async def test_the_home_directory_is_passable_for_other_users(fake: FakeMachines) -> None:
    await make(fake)
    assert fake.lines[0] == (
        "/usr/bin/install -d -m 700 /home/boxd/.boxd-inspect && /bin/chmod o+x /home/boxd"
    )


async def test_a_custom_working_dir_is_made(fake: FakeMachines) -> None:
    env = await make(fake, BoxdSandboxEnvironmentConfig(working_dir="/srv/task"))
    assert "/bin/mkdir -p -- /srv/task" in fake.lines[0]
    assert env._working_dir == "/srv/task"


def test_machine_names_are_short_and_unique() -> None:
    name = machine_name("inspect", "A Very Long Task Name " * 5, {"__sample_id__": "Sample/42"})
    assert len(name) <= 63
    assert name.startswith("inspect-a-very-long-task-name")
    assert machine_name("inspect", "t", {}) != machine_name("inspect", "t", {})
    assert all(c.isalnum() or c == "-" for c in name)


# --- exec ---


async def test_exec_runs_one_line_as_the_machine_user(fake: FakeMachines) -> None:
    env = await make(fake)
    fake.handler = lambda line: Result(stdout=b"hi\n", stderr=b"warn\n", exit_code=3)
    result = await env.exec(["echo", "hi"], cwd="sub")
    assert (result.success, result.returncode, result.stdout, result.stderr) == (
        False,
        3,
        "hi\n",
        "warn\n",
    )
    argv = shlex.split(fake.lines[-1])
    assert argv[3] == "/home/boxd/sub"
    assert fake.exec_kwargs[-1]["close_stdin"] is True


async def test_input_goes_up_as_a_file_and_is_removed(fake: FakeMachines) -> None:
    env = await make(fake)
    await env.exec(["cat"], input="hello")
    (path,) = [p for p in fake.files.uploads if p.endswith(".in")]
    assert fake.files.uploads[path] == b"hello"
    assert f"< {path}" in fake.lines[-1]
    assert f"/bin/rm -f -- {path}" in fake.lines[-1]


async def test_empty_input_comes_from_dev_null(fake: FakeMachines) -> None:
    env = await make(fake)
    await env.exec(["cat"], input="")
    assert "< /dev/null" in fake.lines[-1]
    assert not [p for p in fake.files.uploads if p.endswith(".in")]


async def test_a_long_command_goes_up_as_a_script(fake: FakeMachines) -> None:
    env = await make(fake)
    await env.exec(["printf", "%s", "x" * 200_000])
    (script,) = [p for p in fake.files.uploads if p.endswith(".sh")]
    assert fake.lines[-1].startswith(f"/bin/sh {script}")
    assert b"x" * 200_000 in fake.files.uploads[script]


async def test_a_timeout_raises_when_the_time_is_up(
    fake: FakeMachines, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await make(fake)
    clock = iter([100.0, 103.5])
    monkeypatch.setattr(_environment, "_now", lambda: next(clock))
    fake.handler = lambda line: Result(stdout=b"partial", exit_code=124)
    with pytest.raises(TimeoutError) as info:
        await env.exec(["sleep", "9"], timeout=3)
    assert info.value.truncated_output == "partial"  # type: ignore[attr-defined]


async def test_exit_124_before_the_timeout_is_the_commands_own(
    fake: FakeMachines, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await make(fake)
    clock = iter([100.0, 100.2])
    monkeypatch.setattr(_environment, "_now", lambda: next(clock))
    fake.handler = lambda line: Result(exit_code=124)
    assert (await env.exec(["sh", "-c", "exit 124"], timeout=30)).returncode == 124


async def test_a_call_that_outlives_the_timeout_is_a_timeout(
    fake: FakeMachines, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await make(fake)
    monkeypatch.setattr(_environment, "EXEC_SLACK", 0)
    monkeypatch.setattr(_environment, "TIMEOUT_KILL_AFTER", 0)
    fake.exec_delay = 2
    with pytest.raises(TimeoutError, match="1 seconds"):
        await env.exec(["sleep", "99"], timeout=1)


async def test_a_gone_machine_is_unavailable(fake: FakeMachines) -> None:
    env = await make(fake)
    fake.exec_error = NotFoundError("VM not found")  # type: ignore[no-untyped-call]
    with pytest.raises(SandboxUnavailableError):
        await env.exec(["true"])


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_exit_126_permission_denied_is_a_permission_error(
    fake: FakeMachines, stream: str
) -> None:
    env = await make(fake)
    message = b"exec: /etc/passwd: permission denied"
    fake.handler = lambda line: Result(exit_code=126, **{stream: message})
    with pytest.raises(PermissionError):
        await env.exec(["/etc/passwd"])


async def test_output_stops_streaming_past_the_limit(fake: FakeMachines) -> None:
    env = await make(fake)
    limit = SandboxEnvironmentLimits.MAX_EXEC_OUTPUT_SIZE
    fake.handler = lambda line: Result(stdout=b"a" * (limit * 5))
    with pytest.raises(OutputLimitExceededError):
        await env.exec(["cat", "/dev/zero"])
    assert fake.chunks_read * fake.chunk_size <= limit + fake.chunk_size


async def test_an_empty_user_is_refused(fake: FakeMachines) -> None:
    env = await make(fake)
    with pytest.raises(SandboxUserUnsupportedError):
        await env.exec(["id"], user="")


# --- files ---


async def test_write_file_goes_through_cat_as_the_user(fake: FakeMachines) -> None:
    env = await make(fake)
    await env.write_file("notes/a.txt", "text", user="alice")
    line = fake.lines[-1]
    assert line.startswith("/usr/bin/sudo -n -H -u alice -- /bin/sh -c")
    assert "/home/boxd/notes/a.txt" in line
    (path,) = [p for p in fake.files.uploads if p.endswith(".in")]
    assert fake.files.uploads[path] == b"text"


@pytest.mark.parametrize(
    ("stderr", "code", "error"),
    [
        (b"sh: 1: cannot create f: Permission denied", 2, PermissionError),
        (b"f: Is a directory", 21, IsADirectoryError),
        (b"sudo: unknown user bob", 1, SandboxUserUnsupportedError),
        (b"disk full", 1, RuntimeError),
    ],
)
async def test_write_file_errors(
    fake: FakeMachines, stderr: bytes, code: int, error: type[Exception]
) -> None:
    env = await make(fake)
    fake.handler = lambda line: Result(stderr=stderr, exit_code=code)
    with pytest.raises(error):
        await env.write_file("f", "x", user="bob")


async def test_read_file_returns_bytes_or_text(fake: FakeMachines) -> None:
    env = await make(fake)
    fake.handler = lambda line: Result(stdout=b"a\r\nb")
    assert await env.read_file("f") == "a\r\nb"
    assert await env.read_file("f", text=False) == b"a\r\nb"


@pytest.mark.parametrize(
    ("stderr", "code", "error", "number"),
    [
        (b"cat: f: No such file or directory", 1, FileNotFoundError, errno.ENOENT),
        (b"cat: f: Permission denied", 1, PermissionError, errno.EACCES),
        (b"f: Is a directory", 21, IsADirectoryError, errno.EISDIR),
    ],
)
async def test_read_file_errors_name_the_file(
    fake: FakeMachines, stderr: bytes, code: int, error: type[OSError], number: int
) -> None:
    env = await make(fake)
    fake.handler = lambda line: Result(stderr=stderr, exit_code=code)
    with pytest.raises(error) as info:
        await env.read_file("some/file.txt")
    assert info.value.errno == number
    assert "some/file.txt" in str(info.value)


async def test_read_file_over_the_limit(fake: FakeMachines) -> None:
    env = await make(fake)
    fake.handler = lambda line: Result(stderr=b"too large", exit_code=27)
    with pytest.raises(OutputLimitExceededError):
        await env.read_file("big")
    assert str(SandboxEnvironmentLimits.MAX_READ_FILE_SIZE) in fake.lines[-1]


# --- compose ---


async def test_compose_starts_the_project_and_wraps_each_running_service(
    fake: FakeMachines, tmp_path: Path
) -> None:
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text(
        "services:\n  web:\n    image: a\n  agent:\n    image: b\n    x-default: true\n"
        "  job:\n    image: c\n"
    )

    def handler(line: str) -> Result:
        if " ps --status running" in line:
            return Result(stdout=b"agent\nweb\n")
        if "exec pwd" in line:
            return Result(stdout=b"/app\n")
        return Result()

    fake.handler = handler
    await BoxdSandboxEnvironment.task_init("t", str(compose_file))
    envs = await BoxdSandboxEnvironment.sample_init("t", str(compose_file), {})
    assert list(envs) == ["agent", "web"]
    agent = envs["agent"].as_type(BoxdSandboxEnvironment)
    assert agent._working_dir == "/app"
    assert agent._compose is not None
    assert agent._compose.service == "agent"
    assert any(p.endswith(".tar.gz") for p in fake.files.uploads)
    assert any("up --detach --wait" in line for line in fake.lines)

    await BoxdSandboxEnvironment.sample_cleanup("t", str(compose_file), envs, False)
    assert fake.deleted == ["id-1"]


async def test_compose_without_the_default_service_running_fails_and_deletes(
    fake: FakeMachines, tmp_path: Path
) -> None:
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text("services:\n  default:\n    image: nope\n")
    fake.handler = lambda line: Result(stdout=b"pull access denied")
    with pytest.raises(RuntimeError, match="did not start"):
        await BoxdSandboxEnvironment.sample_init("t", str(compose_file), {})
    assert fake.deleted == ["id-1"]


async def test_cleanup_by_id_only_deletes_machines_made_by_this_package(
    fake: FakeMachines,
) -> None:
    fake.labels = {"mine": {"inspect-sandbox": "boxd"}, "other": {}}
    await BoxdSandboxEnvironment.cli_cleanup("mine")
    with pytest.raises(SystemExit):
        await BoxdSandboxEnvironment.cli_cleanup("other")
    with pytest.raises(SystemExit):
        await BoxdSandboxEnvironment.cli_cleanup("gone")
    assert fake.deleted == ["mine"]


async def test_two_tasks_keep_their_own_machines_without_inspects_state(
    fake: FakeMachines, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def task(name: str) -> dict[str, str | None]:
        await BoxdSandboxEnvironment.task_init(name, CONFIG)
        await BoxdSandboxEnvironment.sample_init(name, CONFIG, {})
        return dict(_environment._registry().machines)

    monkeypatch.setattr(_environment, "_scope", lambda: None)
    first, second = await asyncio.gather(task("a"), task("b"))
    assert len(first) == len(second) == 1
    assert first != second


async def test_a_late_task_init_in_another_task_keeps_each_evals_machines_apart(
    fake: FakeMachines,
) -> None:
    """Inspect can run ``task_init`` in a task next to the samples. Two evals in
    one process must still only delete their own machines."""
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    async def one_eval(ready: asyncio.Event, finish: asyncio.Event) -> str:
        with sandbox_lifecycle_scope():
            await asyncio.create_task(BoxdSandboxEnvironment.task_init("startup", CONFIG))
            envs = await asyncio.create_task(BoxdSandboxEnvironment.sample_init("t", CONFIG, {}))
            ready.set()
            await finish.wait()
            await BoxdSandboxEnvironment.task_cleanup("shutdown", CONFIG, cleanup=True)
            return envs["default"].as_type(BoxdSandboxEnvironment).machine_id

    ready = [asyncio.Event(), asyncio.Event()]
    finish = [asyncio.Event(), asyncio.Event()]
    first = asyncio.create_task(one_eval(ready[0], finish[0]))
    second = asyncio.create_task(one_eval(ready[1], finish[1]))
    await ready[0].wait()
    await ready[1].wait()

    # Each eval has one run id, the same in task_init and in its samples.
    listed = [call["labels"]["inspect-run"] for call in fake.listed]
    created = [machine["labels"]["inspect-run"] for machine in fake.created]
    assert sorted(listed) == sorted(created)
    assert created[0] != created[1]

    finish[0].set()
    first_id = await first
    assert fake.deleted == [first_id]
    finish[1].set()
    second_id = await second
    assert fake.deleted == [first_id, second_id]


async def test_a_sample_without_task_init_is_never_tracked_in_shared_state(
    fake: FakeMachines, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without Inspect's state (the import guard's fallback) a sample's machine
    stays in its own context: another eval's cleanup cannot see it."""
    monkeypatch.setattr(_environment, "_scope", lambda: None)
    envs = await asyncio.create_task(BoxdSandboxEnvironment.sample_init("t", CONFIG, {}))
    await BoxdSandboxEnvironment.task_cleanup("shutdown", CONFIG, cleanup=True)
    assert fake.deleted == []
    await BoxdSandboxEnvironment.sample_cleanup("t", CONFIG, envs, False)
    assert fake.deleted == ["id-1"]


def test_an_inspect_without_lifecycle_state_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "inspect_ai.util._sandbox.lifecycle", None)
    assert _environment._scope() is None


# --- inspect sandbox cleanup boxd ---


def leftovers(fake: FakeMachines) -> None:
    fake.existing = [
        existing_machine("resting", "hibernated"),
        existing_machine("busy", "running"),
        existing_machine("booting", "starting"),
        existing_machine("far", "stopped", region="fra"),
        existing_machine("not-ours", "stopped", made_by_us=False),
    ]


async def test_bulk_cleanup_keeps_machines_in_use(
    fake: FakeMachines, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("BOXD_INSPECT_CLEANUP_RUNNING", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    leftovers(fake)
    await BoxdSandboxEnvironment.cli_cleanup(None)
    assert fake.deleted == ["resting", "far"]
    output = capsys.readouterr().out
    assert "Keeping 2 machine(s) in use" in output
    assert "busy" in output
    assert "Deleted 2 machine(s)." in output


async def test_bulk_cleanup_covers_every_region(
    fake: FakeMachines, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOXD_REGION", "nyc")
    leftovers(fake)
    await BoxdSandboxEnvironment.cli_cleanup(None)
    assert fake.listed[-1]["region"] == "all"
    assert fake.deleted_in == {"resting": "nyc", "far": "fra"}


async def test_bulk_cleanup_deletes_machines_in_use_when_told_to(
    fake: FakeMachines, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOXD_INSPECT_CLEANUP_RUNNING", "1")
    leftovers(fake)
    await BoxdSandboxEnvironment.cli_cleanup(None)
    assert fake.deleted == ["resting", "busy", "booting", "far"]


async def test_bulk_cleanup_asks_first_in_a_terminal(
    fake: FakeMachines, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("BOXD_INSPECT_CLEANUP_RUNNING", raising=False)
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    asked: list[str] = []

    def answer(prompt: str) -> str:
        asked.append(prompt)
        return "n"

    monkeypatch.setattr("builtins.input", answer)
    leftovers(fake)
    await BoxdSandboxEnvironment.cli_cleanup(None)
    assert asked == ["Delete 2 machine(s)? [y/N] "]
    assert fake.deleted == []
    assert "Cancelled." in capsys.readouterr().out


async def test_bulk_cleanup_with_only_machines_in_use_deletes_nothing(
    fake: FakeMachines, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("BOXD_INSPECT_CLEANUP_RUNNING", raising=False)
    fake.existing = [existing_machine("busy", "running")]
    await BoxdSandboxEnvironment.cli_cleanup(None)
    assert fake.deleted == []
    assert "Nothing to delete." in capsys.readouterr().out


async def test_bulk_cleanup_says_when_a_region_could_not_be_listed(
    fake: FakeMachines, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("BOXD_INSPECT_CLEANUP_RUNNING", raising=False)
    leftovers(fake)
    fake.unavailable = ["fra"]
    with pytest.raises(SystemExit):
        await BoxdSandboxEnvironment.cli_cleanup(None)
    assert fake.deleted == ["resting", "far"]
    assert "Could not list region(s) fra" in capsys.readouterr().out


async def test_cleanup_by_id_finds_a_machine_in_another_region(fake: FakeMachines) -> None:
    leftovers(fake)
    await BoxdSandboxEnvironment.cli_cleanup("far")
    with pytest.raises(SystemExit):
        await BoxdSandboxEnvironment.cli_cleanup("not-ours")
    assert fake.deleted_in == {"far": "fra"}


async def test_a_cancelled_sample_still_deletes_its_machine(fake: FakeMachines) -> None:
    async def slow_ready(machine_id: str, **_: object) -> None:
        await asyncio.sleep(5)

    fake.wait_until_ready = slow_ready  # type: ignore[method-assign]
    task = asyncio.create_task(make(fake))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.deleted == ["id-1"]


async def test_connection_names_the_machine(fake: FakeMachines) -> None:
    env = await make(fake)
    connection = await env.connection()
    assert connection.type == "boxd"
    assert connection.command == f"boxd connect {env.machine_name}"


def test_each_event_loop_gets_its_own_client(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[str | None] = []

    def make_client(region: str | None) -> object:
        made.append(region)
        return object()

    monkeypatch.setattr(_environment, "_make_client", make_client)

    async def twice() -> tuple[object, object]:
        return _environment._client("nyc"), _environment._client("nyc")

    first = asyncio.run(twice())
    second = asyncio.run(twice())
    assert first[0] is first[1]
    assert second[0] is not first[0]
    assert made == ["nyc", "nyc"]


def test_a_client_outside_an_event_loop_is_refused() -> None:
    with pytest.raises(RuntimeError, match="asyncio"):
        _environment._client(None)
