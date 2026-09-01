"""Sport profile registry."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MomentType:
    """One recognisable, clippable event in a sport.

    ``code`` is the stable identifier written to Firestore and used by the UI
    filter chips. ``base_score`` is the prior on how well this moment type
    performs as short-form content before any per-instance evidence is
    considered; ``moment_agent`` adjusts it with what the analysis actually saw.
    """

    code: str
    category: str
    label: str
    description: str
    base_score: float
    # Typical duration of the clippable action itself, excluding lead-in and
    # celebration. Used by clip_agent to pick in/out points.
    typical_action_seconds: float
    lead_in_seconds: float = 2.5
    follow_through_seconds: float = 3.0
    cues: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SportProfile:
    sport: str
    display_name: str
    moment_types: tuple[MomentType, ...]
    # Free text appended to the analysis prompt: rules, court layout, and the
    # broadcast conventions a model needs to read the picture correctly.
    context: str
    # What the model should ignore outright.
    exclusions: tuple[str, ...] = field(default_factory=tuple)

    def by_code(self, code: str) -> MomentType | None:
        return next((m for m in self.moment_types if m.code == code), None)

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(m.code for m in self.moment_types)

    @property
    def categories(self) -> tuple[str, ...]:
        seen: list[str] = []
        for m in self.moment_types:
            if m.category not in seen:
                seen.append(m.category)
        return tuple(seen)


_REGISTRY: dict[str, SportProfile] = {}


def register_profile(profile: SportProfile) -> SportProfile:
    _REGISTRY[profile.sport] = profile
    return profile


def get_profile(sport: str) -> SportProfile:
    key = (sport or "").strip().lower()
    if key not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY)) or "none"
        raise KeyError(f"No sport profile registered for {sport!r}. Registered: {known}.")
    return _REGISTRY[key]


def list_sports() -> list[str]:
    return sorted(_REGISTRY)
