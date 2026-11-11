![boxd sandboxes for Inspect AI](https://raw.githubusercontent.com/boxd-sh/boxd-inspect/main/assets/banner.png)

# boxd-inspect

A [boxd](https://boxd.sh/?ref=boxd-inspect) sandbox for [Inspect AI](https://inspect.aisi.org.uk/).
Each sample runs on its own boxd machine, a full Ubuntu 24.04 VM, and the machine is
deleted when the sample ends. Tasks written for Inspect's Docker sandbox run unchanged:
their `compose.yaml` or `Dockerfile` runs with Docker inside the machine.

A boxd machine is a KVM microVM with its own kernel, `sudo`, Docker, Python, Node and Go.
It boots in about a second, so you can run many samples in parallel without a Docker host of
your own. Because each sample is a whole machine, the agent can build images, start
containers and install system packages, and you can start every sample from a snapshot
that already has your tools and images in it.

## Install

```sh
pip install boxd-inspect
```

You'll need a boxd API key ([sign up](https://boxd.sh/?ref=boxd-inspect), then create a key
in the console or with `boxd auth keys create inspect`):

```sh
export BOXD_API_KEY=bxd_...
```

When Inspect itself runs on a boxd machine you can skip the key: the machine's own
credential works. Python 3.11 or later.

Installing the package registers the sandbox type `boxd` with Inspect. There is nothing to
import in your task.

## Quick start

```python
# kernel.py
from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import includes
from inspect_ai.solver import generate, use_tools
from inspect_ai.tool import bash


@task
def kernel():
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
```

```sh
pip install anthropic
export ANTHROPIC_API_KEY=...
inspect eval kernel.py --model anthropic/claude-haiku-5-5
```

Any model Inspect supports works.

## Run an existing Docker task on boxd

Override the sandbox type, and the task's own `compose.yaml` or `Dockerfile` is used:

```sh
inspect eval my_task.py --sandbox boxd --model anthropic/claude-haiku-5-5
```

Or name the file in the task: `sandbox=("boxd", "compose.yaml")`. The compose file's
directory (all of it except `.git`) goes to the machine, so `build:` contexts next to it work.
Services named `default` or marked `x-default: true` are the default sandbox, as in Docker.
Several services share one machine.

Without a compose file, commands run on the machine itself: Ubuntu 24.04, user `boxd` with
passwordless `sudo`, Docker, Python, Node, Go and git preinstalled. The working directory is
`/home/boxd`.

## Configuration

Pass a `BoxdSandboxEnvironmentConfig`, or a JSON file with the same fields:
`sandbox=("boxd", "boxd.json")`.

```python
from inspect_ai.util import SandboxEnvironmentSpec

from boxd_inspect import BoxdSandboxEnvironmentConfig

sandbox = SandboxEnvironmentSpec(
    "boxd",
    BoxdSandboxEnvironmentConfig(region="fra", vcpu=4, delete_after=3600),
)
```

| Field | Default | Means |
| --- | --- | --- |
| `compose` | none | A `compose.yaml` or `Dockerfile` to run with Docker inside the machine. |
| `region` | `BOXD_REGION`, then your account's | `fra` or `nyc`. |
| `org` | `BOXD_ORG`, then the key's org | The org the machines belong to. |
| `vcpu` | the org's machine size | 1, 2, 4 or 8. Memory follows: 4, 8, 16 or 32 GiB. |
| `memory` | follows `vcpu` | `4G`, `8G`, `16G` or `32G`. |
| `from_snapshot` | none | Start each sample from this snapshot, for example one with images already pulled. |
| `isolated` | `true` | The agent's commands get no peers on your org's private network and none of the in-machine boxd credentials, CLI or connected integrations your other machines have. Outbound internet still works. Turn it off only for code you trust. |
| `working_dir` | `/home/boxd`, or the container's | Where commands run and relative paths resolve. |
| `name_prefix` | `inspect` | Machine names are `<prefix>-<task>-<sample>-<random>`. |
| `labels` | none | Extra labels on every machine. Each machine also has `inspect-sandbox=boxd`, `inspect-run` and `inspect-task`. |
| `delete_after` | none | Seconds after its creation at which boxd deletes a machine, whatever it is doing. A safety net for evals that are killed. Keep it above your longest sample. |
| `ready_timeout` | `180` | Seconds to wait for a new machine. |
| `compose_timeout` | `900` | Seconds `docker compose up` may take, pulls and builds included. |

The API key is read from `BOXD_API_KEY` (or `BOXD_TOKEN`) only. A key in the configuration
would end up in the eval log.

| Environment variable | Means |
| --- | --- |
| `BOXD_API_KEY` | The API key. |
| `BOXD_REGION`, `BOXD_ORG` | The defaults for `region` and `org`. `inspect sandbox cleanup boxd` lists the machines of `BOXD_ORG`. |
| `BOXD_INSPECT_CLEANUP_RUNNING` | Set to `1` and `inspect sandbox cleanup boxd` also deletes machines that are in use. |

## What is supported

| Inspect | On boxd |
| --- | --- |
| `exec` with `input`, `cwd`, `env`, `timeout` | Yes. |
| `exec(user=...)`, `read_file(user=...)`, `write_file(user=...)` | Yes. On the machine through `sudo`, in a service through `docker compose exec --user`. |
| `read_file`, `write_file` | Yes. Missing directories are created. Permissions apply. |
| Sample `files` and `setup` | Yes. |
| Several sandboxes per sample | Yes, as services of one compose file in one machine. |
| `connection()` (for example the human agent) | The `boxd connect <machine>` command, and the service as `container`. |
| `inspect sandbox cleanup boxd [id]` | With an id: deletes that machine, if this package made it. Without: lists every machine with `inspect-sandbox=boxd` in the org, in all regions, and deletes the ones that are not in use. See [Clean up](#clean-up). |

Every check in Inspect's portable sandbox suite (`inspect_ai.util._sandbox.self_check`)
passes on the machine and in Debian and Alpine compose services. A container running as root
passes all but the three checks that Inspect's own Docker sandbox also expects root to fail.

`timeout_retry` and `concurrency` have no effect. Limit parallel machines with
`--max-sandboxes`.

## Errors

Configuration and key problems stop the eval before the first sample. Everything else
fails the sample it happens in, and its machine is still deleted.

| What happened | What you get |
| --- | --- |
| A configuration value boxd doesn't accept (region, size, label, prefix, path) | `ValueError` when the task starts, before any machine is made |
| The compose file's directory packs to more than 512 MiB | `ValueError` when the task starts |
| A `config` that is not a `BoxdSandboxEnvironmentConfig`, `ComposeConfig`, compose file, `Dockerfile` or JSON file | `TypeError` when the task starts |
| No API key, or boxd rejected it | the boxd SDK's error (`boxd.AuthenticationError` for a rejected key) when the task starts |
| The machine didn't answer within `ready_timeout` | `TimeoutError`; the sample fails and its machine is deleted |
| The compose project's default service didn't start | `RuntimeError` with the end of the `docker compose up` output; the machine is deleted |
| A command ran past its `timeout` | `TimeoutError`. The command and its child processes are stopped. |
| A command printed more than 10 MiB | `OutputLimitExceededError`, and the command is stopped |
| A command that exists but can't be run | `PermissionError` |
| `read_file` of a file over 100 MiB | `OutputLimitExceededError` |
| `read_file` or `write_file` on a missing file, a directory, or without permission | `FileNotFoundError`, `IsADirectoryError` or `PermissionError` |
| `user=` names a user that doesn't exist | `SandboxUserUnsupportedError` |
| The machine was deleted while the sample ran | `SandboxUnavailableError` |
| Deleting a machine failed | a warning in the log; the machine is tried again when the task ends |
| `inspect sandbox cleanup boxd <id>` with an id that doesn't exist, or of a machine this package didn't make | a message, exit code 1, nothing deleted |
| `inspect sandbox cleanup boxd` couldn't list a region, or a delete failed | a message naming the region or machine, exit code 1; the rest is still cleaned up |

A command that exits non-zero is not an error: `exec` returns its exit code, stdout and
stderr, as in every Inspect sandbox.

## Limits

| | Limit |
| --- | --- |
| Machines | one per sample, shared by all services of its compose file |
| Machine size | 1, 2, 4 or 8 vCPUs with 4 GiB of memory each, up to your organization's quota |
| Machine ready | waits up to 180 seconds (`ready_timeout`); it usually takes about a second |
| `docker compose up` | up to 900 seconds (`compose_timeout`), image pulls and builds included |
| Command output | 10 MiB per stream, Inspect's limit for every sandbox |
| `read_file` | 100 MiB, Inspect's limit for every sandbox |
| `read_file`, `write_file` | 600 seconds each |
| Compose directory | 512 MiB, sent to the machine as one archive |
| Machine name prefix | 1 to 20 lowercase letters, digits or `-`, starting with a letter |
| Regions | `fra` and `nyc` |
| `inspect sandbox cleanup boxd` | one org (`BOXD_ORG`, or the key's), all its regions; machines that are running or starting are kept unless `BOXD_INSPECT_CLEANUP_RUNNING=1` |

## How it works

- Installing the package adds an `inspect_ai` entry point. Inspect loads it at startup, and
  it registers `BoxdSandboxEnvironment` as the sandbox type `boxd`.
- When the task starts, the configuration is checked and one API call checks the key, so a
  mistake shows up before any machine exists.
- For each sample, `sample_init` creates a machine with the boxd Python SDK, waits until it
  takes commands, and makes a private temp directory on it. Auto-suspend is off for these
  machines, so a sample that thinks for a long time keeps its machine running.
- Commands stream through the SDK's `machines.exec`. `read_file` and `write_file` run as
  small shell commands too, as the requested user, so the file's permissions apply. A write
  creates missing directories and writes through the existing file, so its owner and mode
  stay. Stdin and very long command lines go up as files first.
- With a compose file, its directory is uploaded as one archive, and `docker compose up
  --wait` starts it inside the machine. Each running service becomes one sandbox, reached
  with `docker compose exec`.
- When the sample ends, fails or is cancelled, its machine is deleted. Every machine id the
  eval made is recorded, and any left over is deleted by id when the eval ends. The record
  belongs to one eval: two evals in the same process never delete each other's machines.

## Examples

Each one runs against real boxd. They use Claude (`pip install anthropic`,
`ANTHROPIC_API_KEY`); pass any other model with `--model`.

- [`examples/quickstart.py`](examples/quickstart.py): the task above, one sample on one fresh
  machine.
- [`examples/docker_task/`](examples/docker_task/task.py): a task written for Inspect's Docker
  sandbox with its own `Dockerfile`, run unchanged with `--sandbox boxd`.
- [`examples/docker_in_sample.py`](examples/docker_in_sample.py): three samples in parallel,
  each on its own machine, where the agent builds a Docker image, runs it with nginx and
  fetches the page.
- [`examples/from_snapshot.py`](examples/from_snapshot.py): prepare a machine once (install a
  tool, pull an image), save it as a snapshot, and start every sample from it.

```sh
inspect eval examples/quickstart.py --model anthropic/claude-haiku-5-5
inspect eval examples/docker_task/task.py --sandbox boxd --model anthropic/claude-haiku-5-5
inspect eval examples/docker_in_sample.py --model anthropic/claude-haiku-5-5
python examples/from_snapshot.py
```

## Clean up

A sample's machine is deleted when the sample ends, also when it fails or is cancelled.
Machines whose samples did not clean up are deleted, by id, when the task ends. With
`--no-sandbox-cleanup` the machines stay and their ids are printed. If the eval process is
killed, its machines stay. Set `delete_after` so boxd deletes them for you, or delete them
yourself:

```sh
inspect sandbox cleanup boxd <id>                               # one machine
inspect sandbox cleanup boxd                                    # every machine that is not in use
BOXD_INSPECT_CLEANUP_RUNNING=1 inspect sandbox cleanup boxd     # the ones in use too
```

Without an id, the command lists every machine with the label `inspect-sandbox=boxd` in the
org, in all regions. That includes machines of evals other people in the org are running
right now. So it only deletes machines that are stopped, hibernated, in standby or failed,
and keeps the ones that are running or starting. A machine of a killed eval is still
running: delete it by id, or set `BOXD_INSPECT_CLEANUP_RUNNING=1` when you know no eval is
running in the org. In a terminal the command asks before it deletes. In a script or in CI
it doesn't ask.

New machines start fresh, so a compose service's images are pulled for each sample. For big
images, prepare a machine once, save it as a snapshot and pass `from_snapshot`
(see [`examples/from_snapshot.py`](examples/from_snapshot.py)).

## Development

Needs [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pytest                                      # offline, the boxd client is faked
uv run ruff check . && uv run ruff format --check .
uv run mypy src tests examples
BOXD_API_KEY=bxd_... uv run pytest -m live         # real machines
```

The live tests run Inspect's portable sandbox checks on a machine and in three compose
setups, plus lifecycle and error tests. They create machines named `t-inspect-*`, labelled
`boxd-inspect=live-test`, each with a one-hour `delete_after`, and delete them again.
`BOXD_INSPECT_TEST_REGION` (default `nyc`) picks the region. Set
`BOXD_INSPECT_TEST_OTHER_REGION` to a second region to also test cleanup across regions.
They are skipped when `BOXD_API_KEY` is not set. No live test runs the cleanup without an id.

## Resources

- [boxd docs](https://docs.boxd.sh/?ref=boxd-inspect)
- [boxd Python SDK](https://pypi.org/project/boxd/)
- [Inspect sandboxing docs](https://inspect.aisi.org.uk/sandboxing.html)

## License

MIT
