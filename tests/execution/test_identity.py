import pytest

from lab.agent.client import TrustedContext

CTX = TrustedContext(
    tenant="lab", run_id="run-001", task_id="charge-order-42", namespace="ns", episode_id="ep"
)


def test_identity_literal_vector_and_boundaries():
    from lab.execution.identity import effective_key, operation_id

    assert str(operation_id(CTX)) == "112acdfc-c3cc-539a-9db0-5cc42dfa5e27"
    assert effective_key(CTX) == "arl-112acdfc-c3cc-539a-9db0-5cc42dfa5e27"
    assert operation_id(CTX.model_copy(update={"episode_id": "other"})) == operation_id(CTX)
    assert operation_id(CTX.model_copy(update={"namespace": "other"})) == operation_id(CTX)


@pytest.mark.parametrize("field", ["tenant", "run_id", "task_id"])
def test_each_trusted_identity_component_separates_operations(field):
    from lab.execution.identity import operation_id

    assert operation_id(CTX.model_copy(update={field: "different"})) != operation_id(CTX)


def test_component_boundaries_unicode_and_whitespace_are_not_collapsed():
    from lab.execution.identity import operation_id

    variants = [
        CTX.model_copy(update={"tenant": "a:b", "run_id": "c"}),
        CTX.model_copy(update={"tenant": "a", "run_id": "b:c"}),
        CTX.model_copy(update={"tenant": "Lab"}),
        CTX.model_copy(update={"tenant": "lab "}),
        CTX.model_copy(update={"tenant": "láb"}),
        CTX,
    ]
    assert len({operation_id(ctx) for ctx in variants}) == 6
