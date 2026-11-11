"""The configuration of a ``boxd`` sandbox."""

from __future__ import annotations

import json
import re
from pathlib import Path

from inspect_ai.util import ComposeConfig, is_compose_yaml, is_dockerfile
from pydantic import BaseModel, ConfigDict, Field

REGIONS = ("fra", "nyc")
SIZES = {1: "4G", 2: "8G", 4: "16G", 8: "32G"}

_PREFIX = re.compile(r"[a-z][a-z0-9-]{0,19}")
_LABEL_KEY = re.compile(r"[a-z0-9]([a-z0-9._/-]{0,61}[a-z0-9])?")


class BoxdSandboxEnvironmentConfig(BaseModel):
    """How each sample's boxd machine is made.

    The API key is never part of the configuration (it would end up in the eval
    log). Set ``BOXD_API_KEY``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    compose: str | None = None
    """A ``compose.yaml`` or ``Dockerfile`` to run with Docker inside the machine.

    Without it, commands run on the machine itself (Ubuntu 24.04, user ``boxd``,
    passwordless ``sudo``, Docker, Python and Node preinstalled).
    """

    region: str | None = None
    """``fra`` or ``nyc``. Default: ``BOXD_REGION``, then your account's region."""

    org: str | None = None
    """The org to create machines in. Default: ``BOXD_ORG``, then the key's org."""

    vcpu: int | None = None
    """1, 2, 4 or 8. Memory follows (4, 8, 16 or 32 GiB) unless ``memory`` is set."""

    memory: str | None = None
    """``4G``, ``8G``, ``16G`` or ``32G``. Default: the org's machine size."""

    from_snapshot: str | None = None
    """Start each sample from this snapshot (for example one with images already pulled)."""

    isolated: bool = True
    """Run the eval's untrusted commands on an isolated machine: no peers on your
    org's private network, and none of the in-machine boxd credentials, CLI or
    connected integrations a non-isolated machine of yours gets. Outbound
    internet still works. Turn it off only for code you trust."""

    working_dir: str | None = None
    """Where commands run and relative paths resolve. Default: ``/home/boxd`` on
    the machine, the container's working directory with ``compose``."""

    name_prefix: str = "inspect"
    """Machine names start with this, then the task, the sample and a random suffix."""

    labels: dict[str, str] = Field(default_factory=dict)
    """Extra labels on every machine, next to ``inspect-run`` and ``inspect-task``."""

    delete_after: int | None = None
    """Seconds after its creation at which boxd deletes a machine, whatever it is
    doing: a safety net for machines an eval that was killed never cleaned up.
    Keep it above your longest sample."""

    ready_timeout: float = 180
    """Seconds to wait for a new machine to answer."""

    compose_timeout: float = 900
    """Seconds ``docker compose up`` may take (pulls and builds included)."""

    def __hash__(self) -> int:
        # Inspect keeps configurations in dicts; the dict field is not hashable.
        return hash(self.model_dump_json())

    def check(self) -> None:
        """Raise ``ValueError`` for a value boxd would refuse, before any request."""
        if self.region is not None and self.region not in REGIONS:
            raise ValueError(f"region is one of {', '.join(REGIONS)}, not {self.region!r}")
        if self.vcpu is not None and self.vcpu not in SIZES:
            raise ValueError(f"vcpu is one of 1, 2, 4 or 8, not {self.vcpu}")
        if self.memory is not None and self.memory not in SIZES.values():
            raise ValueError(f"memory is one of 4G, 8G, 16G or 32G, not {self.memory!r}")
        if not _PREFIX.fullmatch(self.name_prefix):
            raise ValueError(
                "name_prefix is 1 to 20 lowercase letters, digits or '-', starting with a letter"
            )
        if self.working_dir is not None and not self.working_dir.startswith("/"):
            raise ValueError("working_dir is an absolute path")
        for key, value in self.labels.items():
            if not _LABEL_KEY.fullmatch(key) or key.startswith("boxd/"):
                raise ValueError(
                    f"label key {key!r} is 1 to 63 of a-z, 0-9, '.', '_', '/', '-', "
                    "starting and ending with a letter or digit, and not under 'boxd/'"
                )
            if len(value) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError(
                    f"label {key} has a value over 256 characters or a control character"
                )
        if self.compose is not None and not (
            is_compose_yaml(self.compose) or is_dockerfile(self.compose)
        ):
            raise ValueError(f"compose is a compose.yaml or a Dockerfile, not {self.compose!r}")

    def machine_memory(self) -> str | None:
        """``memory``, or the size that goes with ``vcpu``."""
        if self.memory is not None:
            return self.memory
        return SIZES[self.vcpu] if self.vcpu is not None else None


Resolved = tuple[BoxdSandboxEnvironmentConfig, "str | ComposeConfig | None"]


def resolve_config(config: object) -> Resolved:
    """Turn what Inspect passes as ``config`` into our settings and a compose source.

    Inspect passes nothing, our object, a ``ComposeConfig``, or a path: a
    ``compose.yaml`` or ``Dockerfile`` (run with Docker in the machine) or a
    JSON file holding our settings.
    """
    compose: str | ComposeConfig | None = None
    if config is None:
        resolved = BoxdSandboxEnvironmentConfig()
    elif isinstance(config, BoxdSandboxEnvironmentConfig):
        resolved = config
    elif isinstance(config, ComposeConfig) or (
        isinstance(config, str) and (is_compose_yaml(config) or is_dockerfile(config))
    ):
        resolved = BoxdSandboxEnvironmentConfig()
        compose = config
    elif isinstance(config, str) and config.endswith(".json"):
        resolved = BoxdSandboxEnvironmentConfig.model_validate(json.loads(Path(config).read_text()))
    else:
        raise TypeError(
            "The boxd sandbox takes a BoxdSandboxEnvironmentConfig, a ComposeConfig, "
            "or the path of a compose.yaml, a Dockerfile or a JSON file, "
            f"not {config!r}"
        )
    resolved.check()
    if compose is None and resolved.compose is not None:
        compose = resolved.compose
    return resolved, compose
