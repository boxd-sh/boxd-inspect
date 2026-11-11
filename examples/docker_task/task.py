"""A task written for Inspect's Docker sandbox, run unchanged on boxd.

    inspect eval examples/docker_task/task.py --model anthropic/claude-haiku-5-5 --sandbox boxd

Without ``--sandbox boxd`` it runs in Docker on your computer, as written.
With it, each sample gets its own boxd machine, and Docker inside that machine
builds the ``Dockerfile`` next to this file. Nothing runs on your computer.
"""

from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import includes
from inspect_ai.solver import generate, use_tools
from inspect_ai.tool import bash, python


@task
def codeword() -> Task:
    return Task(
        dataset=[
            Sample(
                id=f"s{i}",
                input=(
                    "A file in the current directory holds a code word. "
                    "Find it with the tools and answer with the code word only."
                ),
                target="aubergine",
            )
            for i in range(3)
        ],
        solver=[use_tools(bash(timeout=60), python(timeout=60)), generate()],
        scorer=includes(),
        sandbox="docker",
    )
