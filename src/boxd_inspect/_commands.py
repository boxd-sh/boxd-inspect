"""Shell lines the machine runs for each sandbox operation.

Every exec on a boxd machine runs one ``/bin/sh`` line as the machine's user
``boxd``. These functions build those lines; they do no I/O, so the unit tests
check them directly.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

SHELL_PATH = "/bin/sh"
SYSTEM_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"

SUDO = "/usr/bin/sudo"
TIMEOUT = "/usr/bin/timeout"
ENV = "/usr/bin/env"
TIMEOUT_KILL_AFTER = 5
"""Seconds between the TERM and the KILL a timed-out command gets."""

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# cd to $0, then run "$@": the directory and the argv never pass through a
# shell's word splitting.
_CD_AND_RUN = 'cd -- "$0" && exec "$@"'


def check_env(env: dict[str, str] | None) -> None:
    """Raise ``ValueError`` for a variable name a shell cannot assign."""
    for name in env or {}:
        if not _ENV_NAME.fullmatch(name):
            raise ValueError(f"{name!r} is not an environment variable name")


@dataclass(frozen=True)
class Compose:
    """A compose project inside the machine, and one of its services."""

    project: str
    directory: str
    file: str
    service: str

    def base(self) -> list[str]:
        return [
            "docker",
            "compose",
            "--project-name",
            self.project,
            "--project-directory",
            self.directory,
            "--file",
            self.file,
        ]


def exec_argv(
    cmd: list[str],
    *,
    cwd: str,
    env: dict[str, str] | None,
    user: str | None,
    timeout: int | None,
    compose: Compose | None,
) -> list[str]:
    """Build the argv, run by the machine's shell, that runs ``cmd`` as Inspect asks.

    On the machine: ``timeout`` and ``sudo`` by absolute path, then a shell that
    changes to ``cwd`` (as ``user``, so a directory only they can enter works)
    and ``exec``s ``cmd``. ``env`` goes through ``/usr/bin/env`` so it survives
    ``sudo``. In a compose service: ``docker compose exec`` with ``--workdir``,
    ``--user`` and ``--env``, and the same ``timeout`` inside the container so
    the command's whole process tree is stopped when it runs out.
    """
    if not cmd:
        raise ValueError("cmd must not be empty")
    check_env(env)
    limit = [TIMEOUT, "-k", f"{TIMEOUT_KILL_AFTER}s", f"{timeout}s"] if timeout is not None else []
    if compose is not None:
        argv = [*compose.base(), "exec", "-T", "--workdir", cwd]
        if user is not None:
            argv += ["--user", user]
        for name, value in (env or {}).items():
            argv += ["--env", f"{name}={value}"]
        return [*argv, compose.service, *limit, *cmd]

    run = [SHELL_PATH, "-c", _CD_AND_RUN, cwd]
    if env:
        run += [ENV, *(f"{name}={value}" for name, value in env.items())]
    run += cmd
    if user is not None:
        return [SUDO, "-n", "-H", "-u", _sudo_user(user), "--", *limit, *run]
    return [*limit, *run]


def privileged_argv(
    script: str,
    *args: str,
    user: str | None,
    compose: Compose | None,
) -> list[str]:
    """Run a POSIX ``script`` (``$1``... are ``args``) as ``user`` with a pinned PATH.

    For the file operations: their helper programs come from the system
    directories, never from a PATH the sandbox's user can change.
    """
    inner = pinned_shell_command(script, *args)
    if compose is not None:
        argv = [*compose.base(), "exec", "-T"]
        if user is not None:
            argv += ["--user", user]
        return [*argv, compose.service, *inner]
    if user is not None:
        return [SUDO, "-n", "-H", "-u", _sudo_user(user), "--", *inner]
    return inner


def shell_line(
    argv: list[str],
    *,
    stdin: str | None = None,
    stdout: str | None = None,
    remove: list[str] | None = None,
) -> str:
    """One ``/bin/sh`` line: ``argv`` with redirections, then remove temp files.

    The exit status is ``argv``'s own. ``remove`` lists temp files the line
    deletes after the command, whatever its result.
    """
    line = shlex.join(argv)
    if stdin is not None:
        line += f" < {shlex.quote(stdin)}"
    if stdout is not None:
        line += f" > {shlex.quote(stdout)}"
    if remove:
        line += f"; rc=$?; /bin/rm -f -- {shlex.join(remove)}; exit $rc"
    return line


def script_line(script_path: str, *, remove: list[str] | None = None) -> str:
    """Run an uploaded script (for commands too long for one argument)."""
    line = f"{SHELL_PATH} {shlex.quote(script_path)}"
    if remove:
        line += f"; rc=$?; /bin/rm -f -- {shlex.join(remove)}; exit $rc"
    return line


def pinned_shell_command(script: str, *args: str) -> list[str]:
    """``/bin/sh`` running ``script`` with PATH set to the system directories.

    The same pin Inspect puts on its own privileged commands: what the script
    runs never comes from a directory the sandbox's user can write to.
    """
    pin = f"PATH={SYSTEM_PATH}\nexport PATH\nunset CDPATH\n"
    return [SHELL_PATH, "-c", pin + script, "sh", *args]


def _sudo_user(user: str) -> str:
    """``sudo -u`` takes a uid as ``#<uid>``."""
    return f"#{user}" if user.isdigit() else user


READ_SCRIPT = """\
if [ -d "$1" ]; then echo "$1: Is a directory" >&2; exit 21; fi
if [ -e "$1" ] && [ -r "$1" ]; then
  size=$(stat -L -c %s -- "$1") || exit 1
  if [ "$size" -gt "$2" ]; then echo "$1: larger than $2 bytes" >&2; exit 27; fi
fi
exec cat -- "$1"
"""
"""Print a file (``$1``) of at most ``$2`` bytes. A directory fails with
``EISDIR`` (21), a larger file with ``EFBIG`` (27), before anything is sent."""

WRITE_SCRIPT = """\
parent=$(dirname -- "$1") || exit 1
mkdir -p -- "$parent" || exit 1
if [ -d "$1" ]; then echo "$1: Is a directory" >&2; exit 21; fi
exec cat > "$1"
"""
"""Write stdin to a file, making its directory first. Writes through the file
(and a symlink to it), so its owner and mode stay and its permissions apply."""

PWD_SCRIPT = "exec pwd"

__all__ = [
    "ENV",
    "PWD_SCRIPT",
    "READ_SCRIPT",
    "SHELL_PATH",
    "SUDO",
    "SYSTEM_PATH",
    "TIMEOUT",
    "WRITE_SCRIPT",
    "Compose",
    "check_env",
    "exec_argv",
    "pinned_shell_command",
    "privileged_argv",
    "script_line",
    "shell_line",
]
