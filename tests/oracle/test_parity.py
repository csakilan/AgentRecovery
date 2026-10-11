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


def drive(conn, original, ns, world, steps):
    """Run the same steps through the original and the compat profile.

    Returns the pairs (original text, our text) for every step; the caller decides whether
    they must agree.
    """
    service.configure_namespace(conn, ns, Profile.COMPAT, world_fault_plan(World(world)))
    texts = []
    for tool, args in steps:
        if tool == "charge":
            expected, _ = original.handle("charge", dict(args))
            r = service.charge(conn, ns, dict(args))
            actual = render_charge(r.http_status, r.body)
        else:
            key = args["idempotency_key"]
            expected, _ = original.handle("get_payment_status", {"idempotency_key": key})
            r = service.status(conn, ns, key)
            actual = render_status(r.http_status, r.body)
        texts.append((expected, actual))
    return texts


def read_through_reader(payments_db, ns):
    # The read-only role the evaluator actually uses, not the agent-side one.
    with psycopg.connect(payments_db["reader"], autocommit=True) as reader:
        return read_ledger(reader, ns)


@pytest.mark.parametrize("world", ["lost_ack", "true_fail"])
@pytest.mark.parametrize("name", sorted(SEQUENCES))
def test_compat_profile_reproduces_the_original_episode(payments_db, conn, world, name):
    original = RetryLedgerEpisode(world, verify=True)
    ns = f"parity-{world}-{name}"
    texts = drive(conn, original, ns, world, SEQUENCES[name])
    found, calls = read_through_reader(payments_db, ns)
    # Parity is claimed for well-formed request sequences only (docs/protocol.md). The
    # evaluator can see when that does not hold, so the comparison is gated on it, before
    # anything is compared: a malformed charge call makes the two episodes drift apart, and
    # comparing them would report a failure of the simulator that is really a known,
    # documented difference.
    assert calls.invalid_charge_calls == 0, "parity is only claimed for well-formed sequences"
    assert all(expected == actual for expected, actual in texts)
    assert asdict(legacy_score(found, calls)) == original.score()


# The seven malformed charge shapes the final review drove through both implementations.
# Each is followed by a well-formed charge and a status check, as an agent that recovers
# from its own mistake would do.
MALFORMED = {
    "missing_key": {"amount": 49.99},
    "missing_amount": {"idempotency_key": "k1"},
    "empty_key": {"amount": 49.99, "idempotency_key": ""},
    "nonnumeric_amount": {"amount": "abc", "idempotency_key": "k1"},
    "bool_amount": {"amount": True, "idempotency_key": "k1"},
    "numeric_key": {"amount": 49.99, "idempotency_key": 5},
    "extra_field": {"amount": 49.99, "idempotency_key": "k1", "note": "hello"},
}
RECOVERY = [("charge", k("k1")), ("status", k("k1"))]

# Where the original coerces the key to a different string, it commits under that string
# and then again under "k1", so a lost_ack episode scores duplicate there and correct here.
COERCED_KEY = {"missing_key", "empty_key", "numeric_key"}


@pytest.mark.parametrize("world", ["lost_ack", "true_fail"])
@pytest.mark.parametrize("shape", sorted(MALFORMED))
def test_a_malformed_charge_call_puts_compat_out_of_phase_with_the_original(
    payments_db, conn, world, shape
):
    """Pins the known limit of the parity claim as behaviour, not only as prose.

    The original counts a malformed call as its first charge call, so it consumes the
    ambiguous-error slot. Ours rejects the call with 400 and claims no slot, so the two are
    out of phase for the rest of the episode: the model-visible text and the legacy score
    differ, and the headline cells can flip. If this test starts failing, the contract in
    docs/protocol.md has changed and must be updated with it.
    """
    original = RetryLedgerEpisode(world, verify=True)
    ns = f"diverge-{world}-{shape}"
    texts = drive(conn, original, ns, world, [("charge", MALFORMED[shape]), *RECOVERY])
    found, calls = read_through_reader(payments_db, ns)
    ours, theirs = asdict(legacy_score(found, calls)), original.score()

    assert calls.invalid_charge_calls == 1  # the evaluator can see why the two differ
    assert any(expected != actual for expected, actual in texts)
    assert ours != theirs
    assert (theirs["n_charge_attempts"], ours["n_charge_attempts"]) == (2, 1)
    if world == "true_fail":
        # The original reaches a committed charge on the retry, ours misses.
        assert (theirs["correct"], theirs["missed"]) == (1, 0)
        assert (ours["correct"], ours["missed"]) == (0, 1)
    elif shape in COERCED_KEY:
        assert (theirs["duplicate"], theirs["correct"], theirs["effect_count"]) == (1, 0, 2)
        assert (ours["duplicate"], ours["correct"], ours["effect_count"]) == (0, 1, 1)


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
