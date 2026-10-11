from dataclasses import asdict

import psycopg
import pytest

from lab.oracle.ledger import read_ledger
from lab.oracle.score import legacy_score
from lab.protocol.faults import World, world_fault_plan
from lab.protocol.render import render_charge, render_status
from lab.protocol.task import Profile
from lab.simulator import service
from tests.helpers import CHARGE, charges
from tests.reference.retryledger_original import RetryLedgerEpisode


def k(key, amount=49.99):
    return {"amount": amount, "idempotency_key": key}


SEQUENCES = {
    "give_up": [("charge", k("k1"))],
    "retry_new_key": [("charge", k("k1")), ("charge", k("k2"))],
    "retry_same_key": [("charge", k("k1")), ("charge", k("k1"))],
    "check_then_same_key": [("charge", k("k1")), ("status", k("k1")), ("charge", k("k1"))],
    "check_then_new_key": [("charge", k("k1")), ("status", k("k1")), ("charge", k("k2"))],
    "three_new_keys": [("charge", k("k1")), ("charge", k("k2")), ("charge", k("k3"))],
    "wrong_amount": [("charge", k("k1", 4999)), ("charge", k("k1", 4999))],
}


@pytest.mark.parametrize("world", ["lost_ack", "true_fail"])
@pytest.mark.parametrize("name", sorted(SEQUENCES))
def test_compat_profile_reproduces_the_original_episode(payments_db, conn, world, name):
    original = RetryLedgerEpisode(world, verify=True)
    ns = f"parity-{world}-{name}"
    service.configure_namespace(conn, ns, Profile.COMPAT, world_fault_plan(World(world)))
    for tool, args in SEQUENCES[name]:
        if tool == "charge":
            expected, _ = original.handle("charge", dict(args))
            r = service.charge(conn, ns, dict(args))
            assert render_charge(r.http_status, r.body) == expected
        else:
            key = args["idempotency_key"]
            expected, _ = original.handle("get_payment_status", {"idempotency_key": key})
            r = service.status(conn, ns, key)
            assert render_status(r.http_status, r.body) == expected
    # Read through the read-only role the evaluator actually uses, not the agent-side one.
    with psycopg.connect(payments_db["reader"], autocommit=True) as reader:
        found, calls = read_ledger(reader, ns)
    assert asdict(legacy_score(found, calls)) == original.score()


def test_controlled_first_error_is_identical_while_ledgers_differ(conn):
    observed = {}
    for world in (World.LOST_ACK, World.TRUE_FAIL):
        ns = f"controlled-{world.value}"
        service.configure_namespace(conn, ns, Profile.CONTROLLED, world_fault_plan(world))
        r = service.charge(conn, ns, dict(CHARGE))
        observed[world] = (
            r.http_status,
            r.body,
            render_charge(r.http_status, r.body),
            len(charges(conn, ns)),
        )
    lost, fail = observed[World.LOST_ACK], observed[World.TRUE_FAIL]
    assert lost[:3] == fail[:3]
    assert (lost[3], fail[3]) == (1, 0)
