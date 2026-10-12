"""One place that turns a caller's profile argument into a `Profile`.

Both `lab.agent.prompts` and `lab.agent.tools` take a profile, and a bad one must fail the
same way from either, so the check lives here rather than being written twice.
"""

from __future__ import annotations

from lab.protocol.task import Profile


def coerce_profile(profile: Profile | str) -> Profile:
    """Return `profile` as a `Profile`, or raise ValueError naming the accepted values.

    `Profile(...)` rejects anything that is not a profile, and accepts the plain strings a
    manifest carries, since `Profile` is a StrEnum.
    """
    try:
        return Profile(profile)
    except ValueError:
        raise ValueError(
            f"unknown profile {profile!r}; expected one of {[p.value for p in Profile]}"
        ) from None
