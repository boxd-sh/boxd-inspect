# Changelog

## 0.1.0 (2026-11-11)

First release.

- The `boxd` sandbox type for Inspect AI, registered through the `inspect_ai` entry point:
  one boxd machine per sample, deleted when the sample ends.
- Tasks with a `compose.yaml` or `Dockerfile` run unchanged with `--sandbox boxd`: Docker
  runs them inside the machine, and several services share one machine.
- `BoxdSandboxEnvironmentConfig`: `region`, `org`, `vcpu`, `memory`, `from_snapshot`,
  `isolated` (on by default), `working_dir`, `name_prefix`, `labels`, `delete_after`,
  `ready_timeout` and `compose_timeout`. Also accepted as a JSON file.
- `exec` with `input`, `cwd`, `env`, `user` and `timeout`; `read_file` and `write_file`
  with `user`; `connection()`; `inspect sandbox cleanup boxd [id]`.
- `inspect sandbox cleanup boxd` covers all regions of the org and keeps machines that are
  in use, unless `BOXD_INSPECT_CLEANUP_RUNNING=1`.
- Each eval keeps its own record of the machines it made, so two evals in one process
  only delete their own.
- Passes Inspect's portable sandbox checks on the machine and in Debian and Alpine
  compose services.
- Four examples: a quick start, a Docker task run unchanged, Docker inside each sample,
  and samples started from a snapshot.
- Ships `py.typed`.
