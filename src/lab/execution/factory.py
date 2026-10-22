"""The shared executor seam; safe production orchestration is integrated in Task 10."""

from lab.agent.executor import DirectExecutor
from lab.execution.gateway import GatewayExecutor
from lab.execution.reconcile import Reconciler
from lab.execution.retry import RetryExecutor
from lab.protocol.manifest import Mode


def make_executor(mode, client, config, budget, *, clock, guard, journal=None):
    mode = Mode(mode)
    if mode == Mode.AGENT_DIRECTED:
        return DirectExecutor(client.with_bounds(guard, budget.remaining_s))
    if mode == Mode.DETERMINISTIC_RETRY:
        return RetryExecutor(client, config, budget, clock=clock, guard=guard)
    if journal is None:
        raise ValueError("mode C requires a durable journal")
    reconciler = Reconciler(journal, client, config, clock=clock, guard=guard)
    return GatewayExecutor(client, journal, reconciler, config, budget, clock=clock, guard=guard)
