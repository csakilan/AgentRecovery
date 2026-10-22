def test_transport_uncertainty_text_and_metadata_are_fixed():
    from lab.agent.executor import ExecutionEvidence
    from lab.execution.outcomes import TRANSPORT_UNCERTAIN

    evidence = ExecutionEvidence(source_kind="transport_uncertainty", request_count=3)
    assert evidence.model_dump()["source_http_status"] is None
    assert not evidence.cached
    assert TRANSPORT_UNCERTAIN == "Payment outcome is unknown because no response was received."
