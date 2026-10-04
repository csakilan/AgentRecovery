from collections import Counter

from lab.protocol.faults import World, world_fault_plan
from lab.protocol.manifest import (
    Manifest,
    Mode,
    behavior_matrix,
    infrastructure_matrix,
    make_spec,
)
from lab.protocol.task import Profile


def test_behavior_matrix_default_is_48_mode_a_episodes():
    m = behavior_matrix()
    assert len(m.episodes) == 48
    assert {e.mode for e in m.episodes} == {Mode.AGENT_DIRECTED}
    assert {e.profile for e in m.episodes} == {Profile.CONTROLLED}
    assert {e.prompt_variant for e in m.episodes} == {"neutral"}
    assert Counter(e.status_available for e in m.episodes) == {True: 24, False: 24}
    assert Counter(e.world for e in m.episodes) == {World.LOST_ACK: 24, World.TRUE_FAIL: 24}


def test_behavior_matrix_can_regenerate_the_original_96():
    m = behavior_matrix(prompts=("neutral", "careful"))
    assert len(m.episodes) == 96


def test_infrastructure_matrix_default_is_144_episodes():
    m = infrastructure_matrix()
    assert len(m.episodes) == 144
    assert Counter(e.mode for e in m.episodes) == {mode: 48 for mode in Mode}
    assert all(e.status_available for e in m.episodes)


def test_episode_ids_are_unique():
    for m in (behavior_matrix(), infrastructure_matrix()):
        ids = [e.episode_id for e in m.episodes]
        assert len(ids) == len(set(ids))


def test_order_is_shuffled_deterministically_by_seed():
    a, b, c = behavior_matrix(seed=1), behavior_matrix(seed=1), behavior_matrix(seed=2)
    assert [e.episode_id for e in a.episodes] == [e.episode_id for e in b.episodes]
    assert a.sha256() == b.sha256()
    assert [e.episode_id for e in a.episodes] != [e.episode_id for e in c.episodes]
    assert {e.episode_id for e in a.episodes} == {e.episode_id for e in c.episodes}


def test_spec_carries_the_hidden_plan_and_expectations():
    spec = make_spec("x", World.LOST_ACK, Mode.DURABLE_GATEWAY, True, 3)
    assert spec.fault_plan == world_fault_plan(World.LOST_ACK)
    assert spec.expect_effect is True
    assert spec.eligible_recoverable is True
    assert spec.episode_id == "x/lost_ack/status/durable_gateway/r03"
    permanent = make_spec("x", World.PERMANENT_FAIL, Mode.AGENT_DIRECTED, False, 0)
    assert permanent.expect_effect is False
    assert permanent.eligible_recoverable is False
    assert permanent.episode_id == "x/permanent_fail/blind/agent_directed/r00"


def test_manifest_round_trips_through_a_file(tmp_path):
    m = infrastructure_matrix(reps=2)
    path = tmp_path / "m.json"
    m.write(path)
    again = Manifest.load(path)
    assert again == m
    assert again.sha256() == m.sha256()
