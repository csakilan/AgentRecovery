"""Hidden episode manifests. The runner reads these; the model never sees them."""

from __future__ import annotations

import hashlib
import random
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from lab.protocol.faults import FaultPlan, World, world_fault_plan
from lab.protocol.task import DEFAULT_TASK, ChargeTask, Profile

PromptVariant = Literal["neutral", "careful", "terse"]

_RECOVERABLE = {World.LOST_ACK, World.TRUE_FAIL}


class Mode(StrEnum):
    AGENT_DIRECTED = "agent_directed"
    DETERMINISTIC_RETRY = "deterministic_retry"
    DURABLE_GATEWAY = "durable_gateway"


class EpisodeSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    episode_id: str
    profile: Profile
    world: World
    mode: Mode
    status_available: bool
    prompt_variant: PromptVariant
    repetition: int
    scenario: str = "basic"
    fault_plan: FaultPlan
    task: ChargeTask = DEFAULT_TASK
    expect_effect: bool
    eligible_recoverable: bool


class Manifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_version: Literal[1] = 1
    name: str
    seed: int
    episodes: tuple[EpisodeSpec, ...]

    def sha256(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    def write(self, path: Path) -> None:
        path.write_text(self.model_dump_json(indent=2) + "\n")

    @classmethod
    def load(cls, path: Path) -> Manifest:
        return cls.model_validate_json(path.read_text())


def make_spec(
    name: str,
    world: World,
    mode: Mode,
    status_available: bool,
    repetition: int,
    prompt_variant: PromptVariant = "neutral",
    profile: Profile = Profile.CONTROLLED,
) -> EpisodeSpec:
    status = "status" if status_available else "blind"
    suffix = "" if prompt_variant == "neutral" else f"/{prompt_variant}"
    return EpisodeSpec(
        episode_id=f"{name}/{world.value}/{status}/{mode.value}/r{repetition:02d}{suffix}",
        profile=profile,
        world=world,
        mode=mode,
        status_available=status_available,
        prompt_variant=prompt_variant,
        repetition=repetition,
        fault_plan=world_fault_plan(world),
        expect_effect=world is not World.PERMANENT_FAIL,
        eligible_recoverable=world in _RECOVERABLE,
    )


def _shuffled(name: str, seed: int, specs: list[EpisodeSpec]) -> Manifest:
    random.Random(seed).shuffle(specs)
    return Manifest(name=name, seed=seed, episodes=tuple(specs))


def behavior_matrix(
    reps: int = 12, prompts: tuple[PromptVariant, ...] = ("neutral",), seed: int = 20261004
) -> Manifest:
    specs = [
        make_spec("behavior", world, Mode.AGENT_DIRECTED, status, rep, prompt)
        for world in (World.LOST_ACK, World.TRUE_FAIL)
        for status in (True, False)
        for prompt in prompts
        for rep in range(reps)
    ]
    return _shuffled("behavior", seed, specs)


def infrastructure_matrix(reps: int = 24, seed: int = 20261005) -> Manifest:
    specs = [
        make_spec("infrastructure", world, mode, True, rep)
        for world in (World.LOST_ACK, World.TRUE_FAIL)
        for mode in Mode
        for rep in range(reps)
    ]
    return _shuffled("infrastructure", seed, specs)
