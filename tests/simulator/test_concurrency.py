import threading
from concurrent.futures import ThreadPoolExecutor

import psycopg
from psycopg.rows import dict_row

from lab.protocol.faults import FaultPlan, World, world_fault_plan
from lab.protocol.task import Profile
from lab.simulator import service
from tests.helpers import CHARGE, charges

WORKERS = 16
ROUNDS = 10


def _race(svc_url, namespace, bodies):
    start = threading.Barrier(len(bodies))

    def call(body):
        with psycopg.connect(svc_url, autocommit=True, row_factory=dict_row) as c:
            start.wait()
            return service.charge(c, namespace, body)

    with ThreadPoolExecutor(len(bodies)) as pool:
        return list(pool.map(call, bodies))


def test_concurrent_same_key_requests_commit_exactly_one_effect(svc_url, conn):
    for round_ in range(ROUNDS):
        ns = f"race-{round_}"
        service.configure_namespace(conn, ns, Profile.CONTROLLED, FaultPlan())
        results = _race(svc_url, ns, [dict(CHARGE) for _ in range(WORKERS)])
        fresh = [r for r in results if r.http_status == 200 and not r.body["replayed"]]
        replays = [r for r in results if r.http_status == 200 and r.body["replayed"]]
        assert len(fresh) == 1, f"round {round_}"
        assert len(replays) == WORKERS - 1, f"round {round_}"
        assert len(charges(conn, ns)) == 1, f"round {round_}"


def test_concurrent_same_key_in_lost_ack_world_still_one_effect(svc_url, conn):
    for round_ in range(ROUNDS):
        ns = f"lost-{round_}"
        service.configure_namespace(conn, ns, Profile.CONTROLLED, world_fault_plan(World.LOST_ACK))
        results = _race(svc_url, ns, [dict(CHARGE) for _ in range(WORKERS)])
        assert sum(r.http_status == 504 for r in results) == 1
        assert len(charges(conn, ns)) == 1


def test_concurrent_distinct_keys_each_commit(svc_url, conn):
    ns = "distinct"
    service.configure_namespace(conn, ns, Profile.CONTROLLED, FaultPlan())
    bodies = [{**CHARGE, "idempotency_key": f"k{i}"} for i in range(WORKERS)]
    results = _race(svc_url, ns, bodies)
    assert all(r.http_status == 200 for r in results)
    assert len(charges(conn, ns)) == WORKERS
