"""Sport profile registry."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Discipline:
    """A form of a sport with its own vocabulary of moments.

    Handball has none: a handball match is a handball match. Equestrian is ten
    of them — a dressage test and a showjumping round share a horse and nothing
    else an editor would cut on — and which one a video shows is read off the
    footage rather than declared at upload, because the tack, the obstacles and
    the movement are what say so and the uploader may not know.
    """

    code: str
    label: str
    # What identifies this discipline on screen: tack, attire, obstacles, gait.
    cues: str


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
    # Which discipline this belongs to, for a sport that has them. Empty means
    # it applies to the whole sport.
    discipline: str = ""


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

    # The words this sport's actions resolve into, and who performs them. Both
    # were hard-coded in the prompt when handball was the only sport, and both
    # are wrong for every other one: an equestrian round has no Goal and no
    # Goalkeeper. They are matched on as codes downstream, so they stay in
    # English whatever language the prose is written in.
    action_results: tuple[str, ...] = field(default_factory=tuple)
    participant_roles: tuple[str, ...] = field(default_factory=tuple)

    # How to read this sport's on-screen graphic. A handball score bug carries
    # two teams and a running score; a showjumping board carries one competitor,
    # a fault count and a time, which is not a scoreline at all.
    scoreboard_guidance: str = ""

    # Forms of the sport, if it has them. Empty is the ordinary case.
    disciplines: tuple[Discipline, ...] = field(default_factory=tuple)

    # The response shape this sport asks Gemini for. None means the general one.
    # Held as a plain type rather than imported here, so the registry stays free
    # of the schemas that depend on it.
    segment_schema: type | None = None

    def by_code(self, code: str) -> MomentType | None:
        return next((m for m in self.moment_types if m.code == code), None)

    def discipline_by_code(self, code: str) -> Discipline | None:
        """Accepts the code or the label. What is stored on the game record is
        the label, because that is what is displayed and what someone searches
        by; normalising it back is cheaper than storing both and letting them
        disagree."""
        key = (code or "").strip().lower().replace("-", "_").replace(" ", "_")
        return next((d for d in self.disciplines if d.code == key), None)

    def types_for(self, discipline: str) -> tuple[MomentType, ...]:
        """The moments that belong to one discipline, plus the sport-wide ones.

        An unknown discipline gets everything rather than nothing: a video the
        analysis could not place is still a video, and answering it with an
        empty catalogue would report no moments in it at all.
        """
        found = self.discipline_by_code(discipline)
        if not found:
            return self.moment_types
        return tuple(m for m in self.moment_types if m.discipline in ("", found.code))

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
