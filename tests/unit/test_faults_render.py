import pytest
from pydantic import ValidationError

from lab.protocol.faults import FaultPlan, World, world_fault_plan
from lab.protocol.render import render_charge, render_status


def test_world_plans_match_the_original_episode():
    assert world_fault_plan(World.HEALTHY).charge_fault(1) is None
    assert world_fault_plan(World.LOST_ACK).charge_fault(1) == "commit_then_error"
    assert world_fault_plan(World.LOST_ACK).charge_fault(2) is None
    assert world_fault_plan(World.TRUE_FAIL).charge_fault(1) == "error_no_commit"
    assert world_fault_plan(World.TRUE_FAIL).charge_fault(2) is None
    for n in (1, 2, 7):
        assert world_fault_plan(World.PERMANENT_FAIL).charge_fault(n) == "error_no_commit"


def test_fault_plan_round_trips_through_json():
    plan = FaultPlan(
        charge={1: "commit_then_error"}, status_unavailable_calls=2, hold_after_commit=(1,)
    )
    again = FaultPlan.model_validate_json(plan.model_dump_json())
    assert again == plan
    assert again.charge_fault(1) == "commit_then_error"


def test_fault_plan_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        FaultPlan.model_validate({"charge": {}, "surprise": True})


ORIGINAL_LOST_ACK = (
    "HTTP 504 Gateway Timeout: the upstream processor did not confirm. Payment status is UNKNOWN."
)
ORIGINAL_TRUE_FAIL = (
    "HTTP 500 Internal Server Error: the charge was not processed. Payment status is UNKNOWN."
)


def test_render_errors_in_original_format():
    lost = {
        "error": {
            "code": "gateway_timeout",
            "message": "the upstream processor did not confirm. Payment status is UNKNOWN.",
        }
    }
    fail = {
        "error": {
            "code": "internal_error",
            "message": "the charge was not processed. Payment status is UNKNOWN.",
        }
    }
    assert render_charge(504, lost) == ORIGINAL_LOST_ACK
    assert render_charge(500, fail) == ORIGINAL_TRUE_FAIL


def test_render_success_and_replay_use_the_message():
    assert render_charge(200, {"message": "Charge succeeded."}) == "Charge succeeded."


def test_render_status_in_original_format():
    found = {"idempotency_key": "k1", "status": "SUCCEEDED", "charge": {"amount": "49.99"}}
    missing = {"idempotency_key": "k1", "status": "NOT_FOUND", "charge": None}
    assert render_status(200, found) == "Status for 'k1': SUCCEEDED — $49.99 charged."
    assert render_status(200, missing) == "Status for 'k1': NOT_FOUND — no charge recorded."


def test_render_status_outage():
    body = {
        "error": {
            "code": "unavailable",
            "message": "payment status service is temporarily unavailable.",
        }
    }
    assert render_status(503, body) == (
        "HTTP 503 Service Unavailable: payment status service is temporarily unavailable."
    )
