"""Runtime database access.

Deliberately a different database from `payments`, owned by a different role.
The evaluator reads the payment ledger to decide a verdict and must not be able
to reach the agent's own records, so the separation is structural.
"""

from __future__ import annotations

import os


def runtime_url() -> str:
    return os.environ["LAB_RUNTIME_URL"]
