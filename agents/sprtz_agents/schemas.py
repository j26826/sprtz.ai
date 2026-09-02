"""Structured payloads exchanged between the analysis tools, the agents and Firestore."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Platform = Literal["tiktok", "instagram", "youtube"]


class SegmentPlan(BaseModel):
    """One analysis window over the source video."""

    index: int = Field(description="Zero-based segment number, in play order.")
    start_sec: float = Field(description="Absolute start offset in the source video.")
    end_sec: float = Field(description="Absolute end offset in the source video.")
    overlap_lead_sec: float = Field(
        default=0.0,
        description=(
            "How much of this segment overlaps the previous one. Detections that start "
            "inside the overlap are candidates for cross-boundary de-duplication."
        ),
    )

    @property
    def duration_sec(self) -> float:
        return max(0.0, self.end_sec - self.start_sec)


def parse_timecode(value: str) -> float | None:
    """Parse MM:SS or H:MM:SS into seconds. Returns None if unparseable.

    The model is asked for timecodes rather than float seconds because floats
    invite it to emit a sequential counter instead of reading the clip position.
    """
    if value is None:
        return None
    text = str(value).strip().replace(",", ".")
    if not text:
        return None
    try:
        parts = [float(p) for p in text.split(":")]
    except ValueError:
        return None
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return None


def format_timecode(seconds: float) -> str:
    """Seconds to MM:SS, or H:MM:SS past the hour.

    The inverse of :func:`parse_timecode`. A match runs past 60 minutes, so
    minutes are not truncated into an hour field unless there is one.
    """
    total = max(0, round(seconds or 0))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


class DetectedMoment(BaseModel):
    """A single moment as Gemini reports it, timed relative to its segment.

    Field names and descriptions are shown to the model, so this class is both
    the response schema and part of the prompt.
    """

    moment_type: str = Field(
        description="One of the allowed moment type codes. Use the code exactly as given."
    )
    start_tc: str = Field(description="MM:SS within this clip where the build-up begins.")
    peak_tc: str = Field(description="MM:SS within this clip of the single decisive frame.")
    end_tc: str = Field(description="MM:SS within this clip where the action resolves.")
    confidence: float = Field(
        ge=0.0, le=1.0, description="How certain you are that this moment type is correct."
    )
    excitement: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "How exciting this instance is compared with a typical instance of the same "
            "moment type. A routine wing goal is 0.4; one that decides the match is 1.0."
        ),
    )
    description: str = Field(
        description=(
            "One or two sentences describing what actually happens, in the present tense, "
            "specific enough to caption from. Name shirt colours or numbers when legible."
        )
    )
    evidence: list[str] = Field(
        default_factory=list,
        description=(
            "The concrete visual or audio cues you actually observed that justify this "
            "classification. Do not restate the definition."
        ),
    )
    scoreboard: str | None = Field(
        default=None,
        description="Score bug text if legible, e.g. 'SWE 24-23 DEN 58:41'. Null if not readable.",
    )
    action_result: str = Field(
        default="",
        description=(
            "How the action ends, in one or two words: Goal, Save, Miss, Block, Foul, "
            "Turnover, Card, Timeout, Penalty. Empty if it does not resolve on camera."
        ),
    )
    participant: str = Field(
        default="",
        description=(
            "Who performs the action, and only if you can actually read it: a shirt "
            "number, or a name shown on screen or said by the commentary. Write "
            "'#7 red' or 'unknown' — never guess a name from context."
        ),
    )
    participant_role: str = Field(
        default="",
        description=(
            "That participant's role in this action: Attacker, Defender, Goalkeeper, "
            "Pivot, Wing, Back, Referee or Coach."
        ),
    )
    team1: str = Field(
        default="",
        description=(
            "Home team, as printed on the score bug — the first or left-hand side. "
            "Copy what is shown, abbreviation and all. Empty if no bug is legible; "
            "never infer it from the competition or the kit."
        ),
    )
    team2: str = Field(
        default="",
        description="Away team, the second or right-hand side of the score bug. Same rule.",
    )
    score_team1: int | None = Field(
        default=None,
        description=(
            "Home team's score at this moment, as shown on the bug. Null if not "
            "readable — 0 is a real score and means nil, not unknown."
        ),
    )
    score_team2: int | None = Field(
        default=None, description="Away team's score at this moment. Null if not readable."
    )
    action_team: str = Field(
        default="",
        description=(
            "Which side this action belongs to, named as on the bug so it matches "
            "team1 or team2. Use the shirt colour if the bug is not legible, and "
            "leave empty for a neutral action such as a referee decision."
        ),
    )
    is_replay: bool = Field(
        default=False, description="True if this is a replay of an earlier live action."
    )
    is_goal: bool = Field(default=False, description="True if the action results in a goal.")

    @field_validator("moment_type")
    @classmethod
    def _normalise(cls, v: str) -> str:
        return v.strip().lower().replace(" ", "_").replace("-", "_")

    # --- Parsed accessors -----------------------------------------------------

    @property
    def start_sec(self) -> float | None:
        return parse_timecode(self.start_tc)

    @property
    def peak_sec(self) -> float | None:
        return parse_timecode(self.peak_tc)

    @property
    def end_sec(self) -> float | None:
        return parse_timecode(self.end_tc)

    def resolve(self, segment_duration_sec: float) -> tuple[float, float, float] | None:
        """Return (start, peak, end) in seconds, or None if unusable.

        On real footage the model sometimes reports past the end of the clip, so
        anything outside the window is rejected rather than silently clamped to
        a timestamp nobody observed.
        """
        start, peak, end = self.start_sec, self.peak_sec, self.end_sec
        if peak is None:
            return None
        # A peak beyond the clip means the model lost track; the whole detection
        # is untrustworthy, not just that one field.
        if peak < 0 or peak > segment_duration_sec + 1.0:
            return None

        if start is None or start < 0 or start > segment_duration_sec:
            start = peak
        if end is None or end > segment_duration_sec + 1.0 or end < start:
            end = min(peak + 4.0, segment_duration_sec)

        start = min(start, peak)
        end = max(end, peak)
        return start, peak, min(end, segment_duration_sec)


class SegmentAnalysis(BaseModel):
    """Gemini's full response for one segment."""

    moments: list[DetectedMoment] = Field(default_factory=list)
    segment_summary: str = Field(
        default="",
        description="Two sentences on what happened in this segment overall.",
    )
    scoreboard_readable: bool = Field(
        default=False, description="Whether an on-screen score bug was legible in this segment."
    )


class Moment(BaseModel):
    """A merged, absolute-timestamped moment as persisted to Firestore."""

    moment_id: str
    job_id: str
    moment_type: str
    category: str
    label: str
    start_sec: float
    end_sec: float
    peak_sec: float
    confidence: float
    excitement: float
    highlight_score: float = Field(
        description="Final ranking score combining the type prior, confidence and excitement."
    )
    description: str
    evidence: list[str] = Field(default_factory=list)
    scoreboard: str | None = None
    is_goal: bool = False
    action_result: str = ""
    participant: str = ""
    participant_role: str = ""
    team1: str = ""
    team2: str = ""
    score_team1: int | None = None
    score_team2: int | None = None
    action_team: str = ""
    segment_indexes: list[int] = Field(
        default_factory=list,
        description="Segments this moment was seen in. More than one means it was merged.",
    )

    @property
    def duration_sec(self) -> float:
        return max(0.0, self.end_sec - self.start_sec)

    def as_action_play(self) -> dict:
        """The moment in ActionPlay form.

        Timecodes are MM:SS into the match, not into the segment the moment was
        found in — a consumer of this has no idea segments exist. Confidence is
        0-100 here while it stays 0-1 everywhere inside, because this shape asks
        for a score and the internal one is a probability.
        """
        return {
            "type": "ActionPlay",
            "timeOffsetStart": format_timecode(self.start_sec),
            "timeOffsetEnd": format_timecode(self.end_sec),
            "actionCategory": self.category,
            "actionClass": self.label,
            "actionResult": self.action_result,
            "participant": self.participant,
            "participantRole": self.participant_role,
            "team1": self.team1,
            "team2": self.team2,
            "scoreTeam1": self.score_team1,
            "scoreTeam2": self.score_team2,
            "actionTeam": self.action_team,
            "description": self.description,
            "confidenceScore": round(self.confidence * 100),
        }


class ClipSuggestion(BaseModel):
    """A publishable short-form cut derived from a moment."""

    clip_id: str
    job_id: str
    moment_id: str
    start_sec: float
    end_sec: float
    duration_sec: float
    aspect: Literal["9:16", "1:1", "16:9"] = "9:16"
    platforms: list[Platform] = Field(default_factory=lambda: ["tiktok", "instagram", "youtube"])
    hook_text: str = Field(description="Large on-screen text for the first second.")
    title: str
    captions: dict[str, str] = Field(
        default_factory=dict, description="Platform code -> caption copy."
    )
    hashtags: list[str] = Field(default_factory=list)
    score: float
    rationale: str = Field(description="Why this cut was chosen, for the editor to review.")
