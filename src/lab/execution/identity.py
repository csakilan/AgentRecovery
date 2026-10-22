"""Stable payment identity derived only from trusted run context."""

import json
from uuid import UUID, uuid5

from lab.agent.client import TrustedContext

OPERATION_NAMESPACE = UUID("e0a30ae5-f624-4f2f-805c-ee7805ef5fa9")


def operation_id(ctx: TrustedContext) -> UUID:
    name = json.dumps(
        [ctx.tenant, ctx.run_id, ctx.task_id], ensure_ascii=True, separators=(",", ":")
    )
    return uuid5(OPERATION_NAMESPACE, name)


def effective_key(ctx: TrustedContext) -> str:
    return f"arl-{operation_id(ctx)}"
