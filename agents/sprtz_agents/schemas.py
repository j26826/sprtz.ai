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
    summary: str = Field(
        default="",
        description=(
            "One sentence naming who did what and how it ended, in the order a "
            "commentator would say it: '#12 blue saves the seven-metre and turns "
            "the rebound over the bar.' Not a shorter copy of the description — "
            "that says what the picture shows, this says what happened."
        ),
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
    competition: str = Field(
        default="",
        description=(
            "Competition or league, only if a caption, graphic or the commentary "
            "actually names it. Empty otherwise — never infer it from the teams."
        ),
    )
    venue: str = Field(
        default="",
        description=(
            "Arena or ground, only if named on screen or by the commentary. Empty "
            "otherwise — never infer it from the home team."
        ),
    )


class EquestrianMoment(DetectedMoment):
    """A moment in a sport judged on how a movement was performed.

    Two fields more than the general shape, and a separate model rather than
    optional fields on it: what a response schema asks for is part of the
    prompt, so adding these to every sport would have a handball analysis
    writing paragraphs about a jump shot's balance for nobody to read.
    """

    execution_details: str = Field(
        default="",
        description=(
            "What the bodies are actually doing: form, balance, trajectory, the "
            "line taken, where the weight is. Describe the horse as much as the "
            "human — the horse is the athlete here too."
        ),
    )
    harmony_index: str = Field(
        default="",
        description=(
            "One clause on the visible communication between horse and human: how "
            "fluid it looked, whether the aids were invisible or obvious, whether "
            "the horse was with the rider or against them. A note, not a number."
        ),
    )


class NotConfirmed(BaseModel):
    """A movement looked for in this segment and not found.

    An absence is a finding. Without somewhere to record one, "no pirouette in
    this ride" and "nobody checked" are the same empty result — and they lead to
    opposite decisions, because one is an answer and the other is a gap.

    It matters most for the movements that are easy to half-see. A collected
    canter through a corner looks like the beginning of a pirouette from the
    wrong angle, and a model with nowhere to put "I looked at this and it was
    not one" will either drop it silently or report the thing it half-saw.
    """

    moment_type: str = Field(
        description=(
            "The moment type code you looked for and could not confirm. Use the "
            "code exactly as given in the catalogue."
        )
    )
    note: str = Field(
        description=(
            "What you saw instead, and where. Name the timecode of the strongest "
            "candidate you rejected and say what ruled it out — 'two candidates "
            "near 03:12 and 04:40, both collected canter through a corner rather "
            "than a turn on the haunches'. A bare 'not seen' is not useful; the "
            "point of this field is that somebody can go and check."
        )
    )


class ObservedRide(BaseModel):
    """One competitor's turn, as a single segment saw it.

    A dressage competition day is many rounds in sequence rather than one
    contest, so the unit that matters sits between the recording and the
    moments: a *ride*. This is what one fifteen-minute window can say about the
    ones it contains.

    It is reported per segment rather than derived from the moments afterwards
    because the moments are sparse. A ride with nothing clippable in it produces
    no moments at all, and reconstructing boundaries from detections alone would
    make such a ride disappear — which is exactly the ride somebody searching
    for a competitor wants to be told about.

    Names are transcribed, never corrected. The lower third is the only thing
    that knows how they are spelt, and a model tidying "Woodcroft Royal Charter"
    into something more plausible has invented a horse.
    """

    rider: str = Field(
        default="",
        description=(
            "Rider's name exactly as the lower third prints it. Empty if no "
            "graphic named them — do not infer a name from commentary alone."
        ),
    )
    horse: str = Field(
        default="",
        description="Horse's name exactly as printed. Empty if not shown.",
    )
    start_tc: str = Field(
        description=(
            "MM:SS within this clip where this combination's round begins — the "
            "entry or the opening halt, not the first thing they do well."
        )
    )
    end_tc: str = Field(
        description=(
            "MM:SS where the round ends: the final halt and salute, or where "
            "they leave the arena. Use the end of the clip if they are still "
            "going when it stops."
        )
    )
    test_type: str = Field(
        default="",
        description=(
            "'freestyle' if the round is ridden to music, 'straight' for a "
            "conventional test, empty if the footage does not say. Music alone "
            "is not enough — arenas play music between rounds too."
        ),
    )
    scoreboard_text: str = Field(
        default="",
        description=(
            "The results graphic verbatim, if one was displayed for this "
            "combination. Copy it as printed including the row labels, because "
            "the label is what says whether a number is a technical mark, an "
            "artistic mark or a total."
        ),
    )
    judge_marks: list[float] = Field(
        default_factory=list,
        description=(
            "Each judge's percentage in the order shown, left to right. Empty "
            "unless a graphic actually displayed them. Do not compute these."
        ),
    )
    total_pct: float | None = Field(
        default=None,
        description=(
            "The overall percentage as displayed. Null if no graphic showed one "
            "— never averaged from the judges' marks yourself, because a "
            "computed total is one nobody put on screen."
        ),
    )
    rank: int | None = Field(
        default=None, description="Placing as displayed at that moment, or null."
    )


class EquestrianSegmentAnalysis(SegmentAnalysis):
    """A segment of equestrian footage, which also says what it is.

    The discipline is read off the footage rather than declared at upload,
    because the tack, the obstacles and the movement are what say so and the
    person uploading may not know. Every segment answers, and the readings are
    consensused across the job the same way team names are — a discipline does
    not change halfway through a video, but one segment's view of it can be
    wrong.
    """

    moments: list[EquestrianMoment] = Field(default_factory=list)
    discipline: str = Field(
        default="",
        description=(
            "Which discipline this footage shows, using the code exactly as given "
            "in the catalogue. Empty if the footage genuinely does not settle it."
        ),
    )
    rides: list[ObservedRide] = Field(
        default_factory=list,
        description=(
            "Every competitor whose round appears in this clip, in the order "
            "they ride. A round running past the end of the clip still counts, "
            "and so does one already under way when it starts — the windows "
            "overlap and the halves are stitched back together afterwards."
        ),
    )
    not_confirmed: list[NotConfirmed] = Field(
        default_factory=list,
        description=(
            "Movements you actively looked for in this segment and could not "
            "confirm. Report the ones a viewer would reasonably expect at this "
            "level and the ones you saw a candidate for and rejected. Leave it "
            "empty only if you genuinely checked for nothing beyond what you "
            "reported — an empty list is a claim about your own search, not a "
            "claim about the footage."
        ),
    )
    discipline_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "How sure you are of the discipline. Low is a useful answer; a wrong "
            "discipline stated confidently filters out everything that follows."
        ),
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
    # Copied from the moment type rather than decided per moment, so the gate
    # cannot be reasoned away by a confident-sounding description. See
    # MomentType.requires_human_review: this is the welfare gate, and it travels
    # with the record to whatever eventually publishes it.
    requires_human_review: bool = False
    confidence: float
    excitement: float
    highlight_score: float = Field(
        description="Final ranking score combining the type prior, confidence and excitement."
    )
    description: str
    evidence: list[str] = Field(default_factory=list)
    scoreboard: str | None = None
    is_goal: bool = False
    summary: str = ""
    action_result: str = ""
    participant: str = ""
    participant_role: str = ""
    team1: str = ""
    team2: str = ""
    score_team1: int | None = None
    score_team2: int | None = None
    action_team: str = ""
    # Who was in the arena when this happened, for a sport where the recording
    # is a day of rounds. Joined from the ride windows in code rather than asked
    # of the model per moment, and identity_source says how the ride itself was
    # named: "observed" from a graphic, "schedule" inferred from the published
    # start list. A caption must never present the second as the first.
    rider: str = ""
    horse: str = ""
    start_number: str = ""
    ride_order: int | None = None
    identity_source: str = ""
    # Judgements about form. Equestrian asks for them because the discipline is
    # judged on how a movement was performed rather than on whether it scored;
    # handball leaves them empty, which is why they default rather than being
    # required.
    execution_details: str = ""
    harmony_index: str = ""
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
            "summary": self.summary,
            "description": self.description,
            "executionDetails": self.execution_details,
            "harmonyIndex": self.harmony_index,
            "confidenceScore": round(self.confidence * 100),
        }


class GameDetails(BaseModel):
    """The match as a whole, for the game-level index.

    Two kinds of field live here and they are not equally trustworthy. The
    factual ones — teams, score, competition, venue — are only ever copied from
    something on screen or said aloud, and stay empty when nothing said it. The
    interpretive ones — sentiment, mood, outcome — are judgements over what the
    analysis observed, and are allowed to be inferred because inference is what
    they are.
    """

    job_id: str
    sport: str
    # Which form of the sport this footage shows, for a sport that has several.
    # Read off the tack, the obstacles and the movement rather than declared at
    # upload, so it carries how sure the reading was: an equestrian video the
    # analysis could not place is a real outcome, and pretending otherwise
    # filters a whole match into the wrong vocabulary.
    discipline: str = ""
    discipline_confidence: float = 0.0
    title: str = Field(
        default="",
        description=(
            "How this match is named. Composed from what was read rather than "
            "invented, and falls back to the uploaded file's title when nothing "
            "on screen identified the fixture."
        ),
    )
    # What the analysis looked for across this match and did not find, one entry
    # per moment type with the notes that rejected it. Kept beside the moments
    # rather than derived from their absence, because "no pirouette in this
    # test" and "nobody looked" are the same empty list and opposite answers.
    not_confirmed: list[dict] = Field(default_factory=list)
    # The competitors, in running order, for a recording that is a day of rounds
    # rather than one contest. Empty for a sport where the whole video is the
    # unit — a handball match has no rides.
    rides: list[dict] = Field(default_factory=list)
    # What the published record adds to the observed one, kept apart from it.
    # The show's own name, where it was, the panel that judged it, and the whole
    # start list in published order — every combination, seen or not, because
    # the start list is what places an unnamed round in the video by its time.
    show_title: str = ""
    location: str = ""
    equipe_url: str = ""
    judges: list[dict] = Field(default_factory=list)
    start_list: list[dict] = Field(default_factory=list)
    # How the start list was placed against the video: how many named rounds
    # anchored it, and the clock-to-video offset that resulted. Null when it
    # could not be placed, which is a real outcome rather than a gap.
    schedule_anchors: int = 0
    schedule_offset_sec: float | None = None
    home_team: str = Field(default="", description="As printed on the score bug.")
    away_team: str = Field(default="", description="As printed on the score bug.")
    competition: str = Field(default="", description="League or competition, if named on screen.")
    venue: str = Field(default="", description="Arena or ground, if named on screen.")
    final_score: str = Field(
        default="",
        description=(
            "Last legible scoreline, as 'H-A'. Empty when no score bug was ever "
            "readable — not '0-0', which is a real result."
        ),
    )
    event_outcome: str = Field(
        default="",
        description=(
            "Who won, phrased as '<team> win' or 'Draw'. Empty when the final "
            "score was never legible, because the winner is then unknown."
        ),
    )
    sentiment: str = Field(
        default="Neutral",
        description="Overall tone of the match: Positive, Neutral or Negative.",
    )
    mood: str = Field(
        default="",
        description="One word for how the match felt: Intense, End-to-end, Cagey, One-sided.",
    )
    summary: str = Field(
        default="", description="Two or three sentences on how the match went."
    )
    moment_count: int = 0
    highlight_count: int = 0

    # Grounding sits beside the observations rather than merging into them, so
    # it is always possible to tell what a camera showed from what a search
    # suggested. Merging the two silently is how a record ends up asserting a
    # fixture nobody can check.
    grounded: bool = False
    grounded_competition: str = ""
    grounded_venue: str = ""
    grounded_home_team: str = ""
    grounded_away_team: str = ""
    match_date: str = ""
    grounding_sources: list[dict] = Field(default_factory=list)

    def as_game_record(self) -> dict:
        """The shape returned to callers asking about the game rather than its moments."""
        return {
            "type": "GameDetails",
            "jobId": self.job_id,
            "title": self.title,
            "sport": self.sport,
            "homeTeam": self.home_team,
            "awayTeam": self.away_team,
            "competition": self.competition,
            "venue": self.venue,
            "finalScore": self.final_score,
            "eventOutcome": self.event_outcome,
            "sentiment": self.sentiment,
            "mood": self.mood,
            "summary": self.summary,
            "momentCount": self.moment_count,
            # Empty unless a search actually identified the fixture.
            "grounded": self.grounded,
            "groundedCompetition": self.grounded_competition,
            "groundedVenue": self.grounded_venue,
            "groundedHomeTeam": self.grounded_home_team,
            "groundedAwayTeam": self.grounded_away_team,
            "matchDate": self.match_date,
            "groundingSources": self.grounding_sources,
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
