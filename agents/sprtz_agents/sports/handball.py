"""Handball moment taxonomy.

The 18 moment types below are the ones Sprtz looks for in a handball match,
grouped the way a handball editor thinks about a game: what the attack did, what
the defence did, how the ball moved up the court, what the referees decided, and
which situations changed the shape of the match.
"""

from __future__ import annotations

from sprtz_agents.sports.registry import MomentType, SportProfile, register_profile

# --- 1. Offensive milestones & shooting feats --------------------------------

_OFFENSE = (
    MomentType(
        code="jump_shot",
        category="offense",
        label="Jump Shot",
        description=(
            "The signature handball movement: an attacker leaps horizontally over the "
            "6-metre line, suspends themselves in the air to find an opening, and lands "
            "inside the goal area after release."
        ),
        base_score=0.72,
        typical_action_seconds=3.0,
        cues=(
            "take-off from outside the 6-metre line",
            "body fully airborne and travelling forward over the line",
            "release at the apex, before the landing",
            "lands inside the goal area after the ball has gone",
        ),
    ),
    MomentType(
        code="wing_shot",
        category="offense",
        label="Wing Shot",
        description=(
            "A tight-angle shot from a winger who leaps from the far corner of the court "
            "and adjusts their body mid-air to beat the goalkeeper from almost no angle."
        ),
        base_score=0.78,
        typical_action_seconds=3.0,
        cues=(
            "shooter starts at the extreme left or right sideline",
            "leap out toward the goal line, often nearly parallel to the goal",
            "visible mid-air body or arm adjustment around the keeper",
            "keeper narrowing the near post",
        ),
    ),
    MomentType(
        code="pivot_slip_through",
        category="offense",
        label="Pivot Slip-Through",
        description=(
            "A physical battle at the line: the pivot receives a pass with their back to "
            "goal, spins past the defenders holding them, and shoots while falling."
        ),
        base_score=0.80,
        typical_action_seconds=3.5,
        cues=(
            "line player with their back to goal, defenders on both sides",
            "reception under contact",
            "turn or spin that breaks the defensive grip",
            "shot released while off balance or already falling",
        ),
    ),
    MomentType(
        code="kempa_trick",
        category="offense",
        label="In-Flight (Kempa Trick)",
        description=(
            "A spectacular play in which an attacker catches a pass while already airborne "
            "above the goal area and shoots before touching the ground."
        ),
        base_score=0.95,
        typical_action_seconds=3.0,
        lead_in_seconds=3.5,
        cues=(
            "lob pass thrown into the space above the 6-metre area",
            "attacker jumps in from outside with no ball in hand",
            "catch happens in mid-air, over the goal area",
            "catch, shot and landing form one unbroken movement",
        ),
    ),
)

# --- 2. Defensive disruptions & goalkeeping -----------------------------------

_DEFENSE = (
    MomentType(
        code="steal_interception",
        category="defense",
        label="Steal / Interception",
        description=(
            "A defender reads and breaks up the opponent's build-up play by intercepting a "
            "pass, which instantly triggers a transition the other way."
        ),
        base_score=0.74,
        typical_action_seconds=3.0,
        follow_through_seconds=5.0,
        cues=(
            "defender leaves their mark early to attack the passing lane",
            "ball changes possession without a shot",
            "immediate change of direction by both teams",
        ),
    ),
    MomentType(
        code="block",
        category="defense",
        label="The Block",
        description=(
            "Defenders unite to form a wall and use their arms to deflect a high-velocity "
            "long-range shot from the 9-metre line."
        ),
        base_score=0.62,
        typical_action_seconds=2.5,
        cues=(
            "two or more defenders stepping out together with arms raised",
            "back-court shooter winding up from around 9 metres",
            "visible deflection off hands or arms",
        ),
    ),
    MomentType(
        code="double_save",
        category="defense",
        label="Double Save",
        description=(
            "The goalkeeper stops an initial shot and recovers immediately to block the "
            "rapid-fire rebound."
        ),
        base_score=0.88,
        typical_action_seconds=4.0,
        cues=(
            "two distinct saves inside roughly three seconds",
            "keeper down or off balance between the two",
            "rebound falls to an attacker in the goal area",
        ),
    ),
    MomentType(
        code="empty_goal_save",
        category="defense",
        label="Empty-Goal Goal",
        description=(
            "A long-distance throw by a goalkeeper or defender that scores directly into "
            "the opponent's unattended goal during a 7-v-6 extra-player attack."
        ),
        base_score=0.93,
        typical_action_seconds=4.0,
        follow_through_seconds=5.0,
        cues=(
            "the far goal is visibly empty",
            "throw taken from inside the thrower's own half",
            "long flat ball travelling the length of the court",
            "scoreboard increments for the throwing team",
        ),
    ),
)

# --- 3. Transition & speed phases ---------------------------------------------

_TRANSITION = (
    MomentType(
        code="first_wave",
        category="transition",
        label="First Wave (Fast Break)",
        description=(
            "A single player sprints away immediately after a turnover to collect a long "
            "pass and take on the goalkeeper one-on-one, uncontested."
        ),
        base_score=0.84,
        typical_action_seconds=5.0,
        lead_in_seconds=3.0,
        cues=(
            "one attacker clearly ahead of every retreating defender",
            "long outlet pass down the court, often from the goalkeeper",
            "one-on-one duel with the goalkeeper at the end",
        ),
    ),
    MomentType(
        code="second_third_wave",
        category="transition",
        label="Second / Third Wave",
        description=(
            "Organised, high-speed support running by the rest of the team to exploit a "
            "retreating, unaligned defence before it can set."
        ),
        base_score=0.70,
        typical_action_seconds=6.0,
        lead_in_seconds=3.5,
        cues=(
            "several attackers arriving at speed in a staggered line",
            "defence still running back and not yet formed",
            "shot taken before the defensive block is set",
        ),
    ),
    MomentType(
        code="quick_restart",
        category="transition",
        label="Quick Restart",
        description=(
            "The ball is passed from the centre line the instant after conceding, "
            "exploiting opponents who are still celebrating or walking back."
        ),
        base_score=0.79,
        typical_action_seconds=4.0,
        cues=(
            "throw-off taken within a couple of seconds of the restart",
            "opponents still celebrating, turned away, or jogging back",
            "attack arrives with no organised defence in front of it",
        ),
    ),
)

# --- 4. Referee decisions & penalties -----------------------------------------

_OFFICIATING = (
    MomentType(
        code="seven_meter_penalty",
        category="officiating",
        label="7-Metre Penalty",
        description=(
            "A direct penalty shot awarded when a foul destroys a clear chance of scoring "
            "anywhere on the court."
        ),
        base_score=0.82,
        typical_action_seconds=4.0,
        lead_in_seconds=4.0,
        cues=(
            "referee whistles and points to the 7-metre line",
            "court clears except shooter and goalkeeper",
            "shooter set behind the 7-metre mark",
        ),
    ),
    MomentType(
        code="passive_play_warning",
        category="officiating",
        label="Passive Play Warning",
        description=(
            "The referees raise their hands to signal the attacking team is stalling. The "
            "team then has a maximum of four to six passes before it must shoot."
        ),
        base_score=0.45,
        typical_action_seconds=4.0,
        follow_through_seconds=8.0,
        cues=(
            "referee's forearm raised vertically and held",
            "no whistle; play continues",
            "attack visibly speeds up afterwards",
        ),
    ),
    MomentType(
        code="two_minute_suspension",
        category="officiating",
        label="2-Minute Suspension",
        description=(
            "A progressive punishment for a hard foul or unsportsmanlike conduct, forcing "
            "the offending team to play a man down for two minutes."
        ),
        base_score=0.66,
        typical_action_seconds=4.0,
        lead_in_seconds=5.0,
        cues=(
            "referee forms a 'T' with both hands or holds up two fingers",
            "player walks to the bench area",
            "the foul that caused it immediately precedes the signal",
        ),
    ),
    MomentType(
        code="red_blue_card",
        category="officiating",
        label="Red / Blue Card",
        description=(
            "An immediate ejection (red) or an ejection accompanied by a written report for "
            "further disciplinary action (blue), for a dangerous foul."
        ),
        base_score=0.86,
        typical_action_seconds=4.0,
        lead_in_seconds=6.0,
        follow_through_seconds=5.0,
        cues=(
            "referee raises a red or blue card",
            "the dangerous foul precedes the card",
            "reaction from players, bench or crowd",
        ),
    ),
)

# --- 5. Tactical shifts & crucial match situations ----------------------------

_TACTICAL = (
    MomentType(
        code="seven_v_six_attack",
        category="tactical",
        label="7-v-6 Attack",
        description=(
            "The goalkeeper is replaced with an extra court player to create a numerical "
            "advantage, leaving the net completely exposed."
        ),
        base_score=0.71,
        typical_action_seconds=8.0,
        lead_in_seconds=4.0,
        cues=(
            "seven outfield players in the attacking half",
            "own goal visibly unguarded",
            "substitution of the keeper immediately before",
        ),
    ),
    MomentType(
        code="team_timeout",
        category="tactical",
        label="Team Timeout",
        description=(
            "A one-minute tactical break called by a coach to stop an opponent's scoring "
            "run, draw up a final play, or rest players."
        ),
        base_score=0.38,
        typical_action_seconds=6.0,
        follow_through_seconds=4.0,
        cues=(
            "green timeout card placed on the timekeeper's table",
            "players gather around the coach at the bench",
            "match clock stops",
        ),
    ),
    MomentType(
        code="last_second_free_throw",
        category="tactical",
        label="Last-Second Free Throw",
        description=(
            "A direct free throw against a defensive wall, executed after the final buzzer "
            "has sounded — often deciding a tight game."
        ),
        base_score=0.97,
        typical_action_seconds=5.0,
        lead_in_seconds=6.0,
        follow_through_seconds=8.0,
        cues=(
            "match clock at or past 00:00, or the buzzer has sounded",
            "defensive wall set on the 9-metre line",
            "score is level or within one goal",
            "whole-arena reaction on the outcome",
        ),
    ),
)


HANDBALL_CONTEXT = """\
Court and markings you can rely on:
- The court is 40 m x 20 m with a goal at each end.
- The solid arc 6 m from each goal is the goal area line. Only the goalkeeper may
  stand inside it; attackers may fly over it but must release before landing.
- The dashed arc at 9 m is the free-throw line. Long-range shots are taken from
  around here.
- The short mark 7 m out, directly in front of goal, is the penalty mark.

Match structure:
- Two halves of 30 minutes with a running clock that stops only for timeouts and
  major interruptions. Teams field 6 outfield players and a goalkeeper.
- Goalkeepers wear a shirt that clearly differs from both their outfield
  team-mates and the opposition.

Broadcast conventions:
- The score bug usually sits in a top corner and carries both team abbreviations,
  the score, and the match clock. Read it whenever it is legible; the clock is
  the most reliable way to tell a genuine last-second play from an ordinary one.
- Replays are common immediately after a goal or a controversial decision. A
  replay is usually a different camera angle, in slow motion, often with a
  graphical wipe on entry and exit.
- Commentary volume and crowd noise rise sharply on goals, saves and big
  decisions. Treat that rise as supporting evidence, never as the sole evidence.
"""

HANDBALL_EXCLUSIONS = (
    "Replays of an action you have already reported at its live timestamp. Report the live "
    "occurrence and set is_replay on the replay if you report it at all.",
    "Studio segments, pre-match and half-time analysis, panel discussion and interviews.",
    "Advertising breaks, sponsor stings and channel idents.",
    "Ordinary uncontested passing in a set attack with no shot and no defensive event.",
    "Crowd cutaways, bench reaction shots and coach close-ups that are not attached to a "
    "specific play.",
)


HANDBALL_SCOREBOARD = """\
`team1`, `team2`, `score_team1` and `score_team2` come from the on-screen score \
graphic and nowhere else. Copy the names exactly as printed, abbreviations \
included: `team1` is the first or left-hand side, `team2` the second.

- If the bug is not legible in this clip, leave the names empty and the scores \
null. Do not carry a score forward from an earlier moment, and do not work one \
out from the goals you have counted — a score you calculated is a score nobody \
displayed.
- `null` and `0` are different answers. Nil is a real score; unreadable is not a \
score at all.
- `action_team` is the side the action belongs to, named the same way as the bug \
so it matches `team1` or `team2`. If the bug is not legible use the shirt colour. \
Leave it empty when no side owns the action, such as a referee decision.
- Never infer who is playing from the competition, the venue, the kit or the \
commentary's turn of phrase. An empty field is correct; a guessed one is a team \
that never played."""


HANDBALL = register_profile(
    SportProfile(
        sport="handball",
        display_name="Handball",
        moment_types=_OFFENSE + _DEFENSE + _TRANSITION + _OFFICIATING + _TACTICAL,
        context=HANDBALL_CONTEXT,
        exclusions=HANDBALL_EXCLUSIONS,
        action_results=(
            "Goal", "Save", "Miss", "Block", "Foul", "Turnover", "Card",
            "Timeout", "Penalty",
        ),
        participant_roles=(
            "Attacker", "Defender", "Goalkeeper", "Pivot", "Wing", "Back",
            "Referee", "Coach",
        ),
        scoreboard_guidance=HANDBALL_SCOREBOARD,
    )
)
