"""The smallest boxd eval: one sample, one fresh machine, deleted when the sample ends.

inspect eval examples/quickstart.py --model anthropic/claude-haiku-5-5
"""

from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import includes
from inspect_ai.solver import generate, use_tools
from inspect_ai.tool import bash


@task
def kernel() -> Task:
    return Task(
        dataset=[
            Sample(
                input="Run `uname -s` with the bash tool and tell me its output.",
                target="Linux",
            )
        ],
        solver=[use_tools(bash(timeout=60)), generate()],
        scorer=includes(),
        sandbox="boxd",
    )
