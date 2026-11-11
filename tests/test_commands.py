from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from boxd_inspect._commands import (
    READ_SCRIPT,
    WRITE_SCRIPT,
    Compose,
    exec_argv,
    pinned_shell_command,
    privileged_argv,
    script_line,
    shell_line,
)

COMPOSE = Compose(
    "inspect", "/home/boxd/inspect-compose", "/home/boxd/inspect-compose/c.yaml", "web"
)


def sh(line: str, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["/bin/sh", "-c", line], input=stdin, capture_output=True, check=False)


def test_machine_exec_changes_directory_and_execs_the_argv() -> None:
    argv = exec_argv(["ls", "-l"], cwd="/tmp", env=None, user=None, timeout=None, compose=None)
    assert argv == ["/bin/sh", "-c", 'cd -- "$0" && exec "$@"', "/tmp", "ls", "-l"]


def test_machine_exec_runs_the_argv_verbatim(tmp_path: Path) -> None:
    weird = ["printf", "%s|", "a b", "c'd", "$HOME", "`id`", '"q"', "back\\slash"]
    argv = exec_argv(weird, cwd=str(tmp_path), env=None, user=None, timeout=None, compose=None)
    result = sh(shell_line(argv))
    assert result.stdout.decode() == 'a b|c\'d|$HOME|`id`|"q"|back\\slash|'


def test_env_reaches_the_command_and_survives_sudo() -> None:
    argv = exec_argv(
        ["sh", "-c", "echo $A"], cwd="/", env={"A": "x y"}, user=None, timeout=None, compose=None
    )
    assert argv[4:6] == ["/usr/bin/env", "A=x y"]
    assert sh(shell_line(argv)).stdout == b"x y\n"


def test_another_user_goes_through_sudo_by_absolute_path() -> None:
    argv = exec_argv(["whoami"], cwd="/", env=None, user="alice", timeout=None, compose=None)
    assert argv[:7] == ["/usr/bin/sudo", "-n", "-H", "-u", "alice", "--", "/bin/sh"]


def test_a_uid_is_given_to_sudo_as_hash_uid() -> None:
    argv = exec_argv(["id"], cwd="/", env=None, user="1111", timeout=None, compose=None)
    assert argv[4] == "#1111"


def test_timeout_runs_inside_sudo_so_the_users_processes_are_stopped() -> None:
    argv = exec_argv(["sleep", "9"], cwd="/", env=None, user="root", timeout=3, compose=None)
    assert argv[6:10] == ["/usr/bin/timeout", "-k", "5s", "3s"]


def test_timeout_without_a_user() -> None:
    argv = exec_argv(["sleep", "9"], cwd="/", env=None, user=None, timeout=3, compose=None)
    assert argv[:4] == ["/usr/bin/timeout", "-k", "5s", "3s"]
    assert (
        sh(
            shell_line(
                exec_argv(["sleep", "5"], cwd="/", env=None, user=None, timeout=1, compose=None)
            )
        ).returncode
        == 124
    )


def test_compose_exec_uses_docker_flags_and_an_in_container_timeout() -> None:
    argv = exec_argv(
        ["python", "-V"], cwd="/app", env={"K": "v"}, user="root", timeout=10, compose=COMPOSE
    )
    assert argv[:9] == [
        "docker", "compose", "--project-name", "inspect",
        "--project-directory", "/home/boxd/inspect-compose",
        "--file", "/home/boxd/inspect-compose/c.yaml", "exec",
    ]  # fmt: skip
    assert argv[9:] == [
        "-T", "--workdir", "/app", "--user", "root", "--env", "K=v", "web",
        "/usr/bin/timeout", "-k", "5s", "10s", "python", "-V",
    ]  # fmt: skip


@pytest.mark.parametrize("name", ["1A", "A-B", "A B", ""])
def test_bad_env_names_are_refused(name: str) -> None:
    with pytest.raises(ValueError, match="environment variable"):
        exec_argv(["true"], cwd="/", env={name: "x"}, user=None, timeout=None, compose=None)


def test_empty_cmd_is_refused() -> None:
    with pytest.raises(ValueError, match="empty"):
        exec_argv([], cwd="/", env=None, user=None, timeout=None, compose=None)


def test_shell_line_redirects_and_keeps_the_exit_status(tmp_path: Path) -> None:
    stdin = tmp_path / "in"
    stdin.write_bytes(b"\x00\xffdata")
    out = tmp_path / "out"
    line = shell_line(
        ["sh", "-c", "cat; exit 7"], stdin=str(stdin), stdout=str(out), remove=[str(stdin)]
    )
    result = sh(line)
    assert result.returncode == 7
    assert out.read_bytes() == b"\x00\xffdata"
    assert not stdin.exists()


def test_script_line_runs_the_script_and_removes_it(tmp_path: Path) -> None:
    script = tmp_path / "s.sh"
    script.write_text("echo hi; exit 3\n")
    result = sh(script_line(str(script), remove=[str(script)]))
    assert (result.returncode, result.stdout) == (3, b"hi\n")
    assert not script.exists()


def test_pinned_shell_ignores_the_callers_path(tmp_path: Path) -> None:
    fake_cat = tmp_path / "cat"
    fake_cat.write_text("#!/bin/sh\necho fake\n")
    fake_cat.chmod(0o755)
    argv = pinned_shell_command('exec cat -- "$1"', "/etc/hostname")
    result = subprocess.run(argv, capture_output=True, env={"PATH": str(tmp_path)}, check=False)
    assert b"fake" not in result.stdout


def test_privileged_argv_wraps_for_user_and_compose() -> None:
    assert privileged_argv("true", user=None, compose=None)[0] == "/bin/sh"
    assert privileged_argv("true", user="bob", compose=None)[:6] == [
        "/usr/bin/sudo", "-n", "-H", "-u", "bob", "--",
    ]  # fmt: skip
    in_service = privileged_argv("true", user="bob", compose=COMPOSE)
    assert in_service[8:13] == ["exec", "-T", "--user", "bob", "web"]


def run_script(
    script: str, *args: str, stdin: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        pinned_shell_command(script, *args), input=stdin, capture_output=True, check=False
    )


def test_read_script(tmp_path: Path) -> None:
    file = tmp_path / "f"
    file.write_bytes(b"a\r\nb")
    assert run_script(READ_SCRIPT, str(file), "100").stdout == b"a\r\nb"
    assert run_script(READ_SCRIPT, str(tmp_path), "100").returncode == 21
    assert run_script(READ_SCRIPT, str(file), "2").returncode == 27
    missing = run_script(READ_SCRIPT, str(tmp_path / "nope"), "100")
    assert missing.returncode != 0
    assert b"No such file" in missing.stderr


def test_write_script_makes_directories_and_writes_through(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "f"
    assert run_script(WRITE_SCRIPT, str(target), stdin=b"one").returncode == 0
    assert target.read_bytes() == b"one"
    link = tmp_path / "link"
    link.symlink_to(target)
    assert run_script(WRITE_SCRIPT, str(link), stdin=b"two").returncode == 0
    assert link.is_symlink()
    assert target.read_bytes() == b"two"
    assert run_script(WRITE_SCRIPT, str(tmp_path / "a"), stdin=b"x").returncode == 21
