"""Prepare a machine once, save it as a snapshot, and start every sample from it.

    python examples/from_snapshot.py

Set the model with INSPECT_EVAL_MODEL (default anthropic/claude-haiku-5-5).

A snapshot holds a machine's disk and memory. Here the setup installs a tool
(``jq``) and pulls a Docker image; every sample then starts with both already in
place instead of repeating the setup. The script deletes the setup machine and,
at the end, the snapshot.
"""

import os
import time

from boxd import Boxd
from inspect_ai import Task, eval
from inspect_ai.dataset import Sample
from inspect_ai.scorer import includes
from inspect_ai.solver import generate, use_tools
from inspect_ai.tool import bash
from inspect_ai.util import SandboxEnvironmentSpec

from boxd_inspect import BoxdSandboxEnvironmentConfig

SNAPSHOT = "inspect-example-base"
SETUP = (
    "sudo apt-get install -y -q jq > /dev/null"
    " && docker pull -q python:3.12-slim"
    " && echo '{\"answer\": 42}' > ~/data.json"
)


def prepare(client: Boxd) -> None:
    """Make a machine, run the setup on it, save it as a snapshot, delete it."""
    machine = client.machines.create("inspect-example-setup", isolated=True, delete_after=3600)
    try:
        client.machines.wait_until_ready(machine.id)
        result = client.machines.exec(machine.id, SETUP, timeout=600)
        if result.exit_code != 0:
            raise RuntimeError(f"setup failed: {result.stderr!r}")
        began = time.monotonic()
        client.snapshots.create(machine.id, SNAPSHOT)
        while (status := client.snapshots.get(SNAPSHOT).status) != "ready":
            if status == "failed" or time.monotonic() - began > 900:
                raise RuntimeError(f"snapshot {SNAPSHOT} is {status}")
            time.sleep(5)
        print(f"snapshot {SNAPSHOT} ready in {time.monotonic() - began:.0f}s")
    finally:
        client.machines.delete(machine.id)


def prepared() -> Task:
    return Task(
        dataset=[
            Sample(
                id=f"s{i}",
                input=(
                    "Use jq to read the field `answer` from data.json in the home directory, then "
                    "list the Docker images on this machine. Answer with the number and "
                    "the image names."
                ),
                target="42",
            )
            for i in range(2)
        ],
        solver=[use_tools(bash(timeout=120)), generate()],
        scorer=includes(),
        sandbox=SandboxEnvironmentSpec(
            "boxd", BoxdSandboxEnvironmentConfig(from_snapshot=SNAPSHOT, delete_after=3600)
        ),
        message_limit=20,
        name="from_snapshot",
    )


def main() -> None:
    client = Boxd()
    prepare(client)
    try:
        model = os.environ.get("INSPECT_EVAL_MODEL", "anthropic/claude-haiku-5-5")
        (log,) = eval(prepared(), model=model)
        print(log.status, log.results.scores[0].metrics if log.results else None)
    finally:
        client.snapshots.delete(SNAPSHOT)


if __name__ == "__main__":
    main()
