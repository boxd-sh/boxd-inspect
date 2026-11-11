# Security

Please don't report security problems in public issues.

Report them privately through
[GitHub's vulnerability reporting](https://github.com/boxd-sh/boxd-inspect/security/advisories/new),
or email [contact@boxd.sh](mailto:contact@boxd.sh). Include what you found, how
to reproduce it, and the version of `boxd-inspect` you use. We'll confirm we
received it and keep you posted while we fix it.

## Supported versions

Fixes go into the latest release.

## Scope

This repo holds the `boxd-inspect` package and its examples. Issues in boxd
itself, such as the API or the machines, can go to the same addresses.

## Running evals safely

An eval's model can run any command in its sandbox. With this package that
sandbox is a full VM with its own kernel, so a command can't reach your own
computer, but it can reach the internet and anything the machine can.

- Keep `isolated` on (the default). An isolated machine has no peers on your
  org's private network and none of your boxd credentials or connected
  integrations.
- Pass the API key in `BOXD_API_KEY`, never in the configuration: the
  configuration is written to the eval log.
- Set `delete_after` so machines from an eval that was killed delete
  themselves, and use `inspect sandbox cleanup boxd` to find any that are
  left.
- `inspect sandbox cleanup boxd` without an id sees every machine in the org
  with the label `inspect-sandbox=boxd`, also those of other people's evals.
  It keeps machines that are running or starting. Set
  `BOXD_INSPECT_CLEANUP_RUNNING=1` only when no eval is running in the org.
