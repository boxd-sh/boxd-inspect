"""A whole Inspect eval through the ``boxd`` sandbox, with a fake client that runs
each command in a local shell."""

from __future__ import annotations

from pathlib import Path

import pytest
from inspect_ai import Task, eval_async
from inspect_ai.dataset import Sample
from inspect_ai.scorer import includes
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.util import SandboxEnvironmentSpec, sandbox

from boxd_inspect import BoxdSandboxEnvironmentConfig, _environment
from tests.fakes import FakeClient, local_machine


@solver
def run_in_sandbox() -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        await sandbox().write_file("answer.txt", "the answer is 42\n")
        result = await sandbox().exec(["sh", "-c", "cat answer.txt notes.txt"])
        state.output.completion = result.stdout
        return state

    return solve


async def test_eval_with_sample_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    machines = local_machine(home)
    client = FakeClient(machines)
    monkeypatch.setattr(_environment, "_make_client", lambda region: client)
    task = Task(
        dataset=[
            Sample(input="go", target="42", files={"notes.txt": "copied in first\n"}),
        ],
        solver=run_in_sandbox(),
        scorer=includes(),
        sandbox=SandboxEnvironmentSpec("boxd", BoxdSandboxEnvironmentConfig(region="nyc")),
    )
    (log,) = await eval_async(task, model="mockllm/model", log_dir=str(tmp_path / "logs"))

    assert log.status == "success", log.error
    assert log.results is not None
    assert log.results.scores[0].metrics["accuracy"].value == 1.0
    assert len(machines.created) == 1
    assert machines.deleted == ["id-1"]
    assert (home / "notes.txt").read_text() == "copied in first\n"
