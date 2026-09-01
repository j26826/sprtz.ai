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
    segment_indexes: list[int] = Field(
        default_factory=list,
        description="Segments this moment was seen in. More than one means it was merged.",
    )

    @property
    def duration_sec(self) -> float:
        return max(0.0, self.end_sec - self.start_sec)


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
