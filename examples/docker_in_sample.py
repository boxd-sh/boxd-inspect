"""Samples that need a whole machine: the agent runs Docker itself, with sudo.

    inspect eval examples/docker_in_sample.py --model anthropic/claude-haiku-5-5

Each sample is a full Ubuntu VM with its own kernel and Docker daemon, so the
agent can build images, start containers and install packages. A container
sandbox can't do this without giving the agent the host's Docker socket. The
three samples run in parallel, each on its own machine, and every machine is
deleted when its sample ends. ``delete_after`` is a safety net: boxd deletes a
machine an hour after it was made even if this process is killed.
"""

from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import includes
from inspect_ai.solver import generate, use_tools
from inspect_ai.tool import bash
from inspect_ai.util import SandboxEnvironmentSpec

from boxd_inspect import BoxdSandboxEnvironmentConfig

PORTS = [8081, 8082, 8083]


@task
def docker_in_sample() -> Task:
    return Task(
        dataset=[
            Sample(
                id=f"port-{port}",
                input=(
                    f"Write an index.html that says 'hello from port {port}', build a "
                    f"Docker image that serves it with nginx, run it on port {port}, and "
                    f"fetch http://localhost:{port} with curl. Answer with what curl printed."
                ),
                target=f"hello from port {port}",
            )
            for port in PORTS
        ],
        solver=[use_tools(bash(timeout=300)), generate()],
        scorer=includes(),
        sandbox=SandboxEnvironmentSpec(
            "boxd",
            BoxdSandboxEnvironmentConfig(
                vcpu=2,
                delete_after=3600,
                labels={"example": "docker-in-sample"},
            ),
        ),
        message_limit=30,
    )
