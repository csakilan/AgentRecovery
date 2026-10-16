"""Keep LangSmith from uploading episode state.

`langsmith` is installed as a dependency of LangGraph, and it uploads run data to
api.smith.langchain.com when one of these variables is "true" in the environment. An episode's
state holds the whole conversation and every tool result, so a stray shell setting would send
the experiment out of the machine. Importing `lab.runner` calls `force_tracing_off()`, and the
CLI calls it again, so no entry point depends on the caller's shell.
"""

from __future__ import annotations

import os
import sys
from collections.abc import MutableMapping

# Every spelling langsmith.utils.tracing_is_enabled() reads (langsmith 0.14: LANGSMITH_ then
# LANGCHAIN_ prefixes, TRACING_V2 then TRACING).
TRACING_VARIABLES = (
    "LANGSMITH_TRACING",
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING",
    "LANGCHAIN_TRACING_V2",
)


def force_tracing_off(environ: MutableMapping[str, str] | None = None) -> None:
    """Set every tracing variable to "false", over whatever the shell had."""
    env = os.environ if environ is None else environ
    for name in TRACING_VARIABLES:
        env[name] = "false"
    # langsmith memoises its environment lookups; drop a value cached before this call.
    # This is coupled to the pinned langsmith version (0.14.7). It relies on
    # langsmith.utils.get_env_var being a memoised function with cache_clear(). If langsmith
    # is upgraded, revisit this, since a later version may memoise through a different accessor.
    utils = sys.modules.get("langsmith.utils")
    cache_clear = getattr(getattr(utils, "get_env_var", None), "cache_clear", None)
    if cache_clear is not None:
        cache_clear()
