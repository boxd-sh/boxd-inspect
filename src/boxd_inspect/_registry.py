# Inspect imports this module at startup (the `inspect_ai` entry point), which
# registers the "boxd" sandbox type.
from ._environment import BoxdSandboxEnvironment

__all__ = ["BoxdSandboxEnvironment"]
