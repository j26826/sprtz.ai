"""Equestrian moment taxonomy, across ten disciplines.

One sport profile, not ten. "Equestrian" is what an uploader knows and what the
IPTC record says; *which* equestrian is a question about the footage — the tack,
the attire, the obstacles and the way the horse moves — and the analysis is what
answers it. Registering ten profiles would put that question in the upload
panel, where the person filling it in is least able to answer it and most likely
to guess.

So the discipline is detected per job: every segment reports what it sees, the
readings are consensused the same way team names are, and the moment catalogue
is filtered to the discipline that won. A video that could not be placed keeps
the whole catalogue rather than none of it — an unplaced round is still a round.
"""

from __future__ import annotations

from sprtz_agents.schemas import EquestrianSegmentAnalysis
from sprtz_agents.sports.registry import (
    Discipline,
    MomentType,
    SportProfile,
    register_profile,
)

DISCIPLINES = (
    Discipline(
        code="dressage",
        label="Dressage",
        cues=(
            "a flat rectangular arena marked with letters, no obstacles; a rider in "
            "tails or a show coat on a horse working in a collected outline; "
            "movements ridden to a pattern and often to music"
        ),
    ),
    Discipline(
        code="para_dressage",
        label="Para-Dressage",
        cues=(
            "a dressage arena, with adaptive equipment visible: connected or looped "
            "reins, a modified saddle, one or two whips carried as balance aids, "
            "occasionally a caller or live commentary of the test"
        ),
    ),
    Discipline(
        code="jumping",
        label="Jumping",
        cues=(
            "coloured poles in cups on painted standards, a numbered course inside an "
            "arena, a timing clock and a fault count on screen; the horse jumps "
            "against the clock and rails can fall"
        ),
    ),
    Discipline(
        code="hunter",
        label="Hunter",
        cues=(
            "natural-toned fences — brush, rails, flower boxes — in an unhurried round "
            "with no visible clock; conservative tack and attire, and a judge's box "
            "rather than a scoreboard"
        ),
    ),
    Discipline(
        code="eventing",
        label="Eventing",
        cues=(
            "one of three phases: a dressage test, a cross-country round over solid "
            "fixed obstacles in open country with a body protector and a stopwatch, or "
            "a showjumping round over coloured poles"
        ),
    ),
    Discipline(
        code="endurance",
        label="Endurance",
        cues=(
            "long stretches of trail or open terrain, lightweight tack, a numbered bib, "
            "and periodic halts where a vet takes a heart rate and water is thrown over "
            "the horse"
        ),
    ),
    Discipline(
        code="driving",
        label="Driving",
        cues=(
            "one or more horses in harness pulling a carriage with a driver and a groom "
            "aboard; cones with balls on top, or fixed marathon obstacles taken at speed"
        ),
    ),
    Discipline(
        code="vaulting",
        label="Vaulting",
        cues=(
            "a horse cantering a lunge circle under a lunger while an athlete in a "
            "gymnastics suit performs on its back; a surcingle with handles instead of "
            "a saddle"
        ),
    ),
    Discipline(
        code="western",
        label="Western",
        cues=(
            "a western saddle with a horn, split or romal reins held in one hand, a "
            "cowboy hat or helmet, and a deep sand arena worked for sliding"
        ),
    ),
    Discipline(
        code="general_performance",
        label="General Performance",
        cues=(
            "one horse shown across more than one style in the same footage — western "
            "classes and English flatwork, with a visible change of tack or attire "
            "between them"
        ),
    ),
)


# --- Dressage and Para-Dressage: what the horse does on the flat --------------

_FLATWORK = (
    MomentType(
        code="piaffe",
        category="flatwork",
        discipline="dressage",
        label="Piaffe",
        description=(
            "A cadenced trot on the spot: the horse lifts each diagonal pair in rhythm "
            "with almost no forward travel, hind legs carrying the weight."
        ),
        base_score=0.82,
        typical_action_seconds=8.0,
        cues=(
            "diagonal pairs lifting in an even two-beat rhythm",
            "little or no ground covered",
            "hindquarters visibly lowered, forehand light",
        ),
    ),
    MomentType(
        code="passage",
        category="flatwork",
        discipline="dressage",
        label="Passage",
        description=(
            "A slow, powerful trot with a long moment of suspension — the horse appears "
            "to hang between each step."
        ),
        base_score=0.84,
        typical_action_seconds=8.0,
        cues=(
            "pronounced hang time between diagonal beats",
            "elevated, deliberate steps travelling forward",
            "unchanging rhythm across the movement",
        ),
    ),
    MomentType(
        code="extended_gait",
        category="flatwork",
        discipline="dressage",
        label="Extended Gait",
        description=(
            "Maximum ground cover across a diagonal or long side, the frame lengthening "
            "while the rhythm stays exactly the same."
        ),
        base_score=0.76,
        typical_action_seconds=7.0,
        cues=(
            "stride visibly longer, tempo unchanged",
            "front foot landing where it points",
            "usually ridden corner to corner across the diagonal",
        ),
    ),
    MomentType(
        code="pirouette",
        category="flatwork",
        discipline="dressage",
        label="Pirouette",
        description=(
            "A turn on the spot in canter, forehand describing a small circle around "
            "hind legs that keep cantering under the body."
        ),
        base_score=0.85,
        typical_action_seconds=6.0,
        cues=(
            "canter maintained throughout the turn",
            "hind feet staying within a small circle",
            "even number of strides around",
        ),
    ),
    MomentType(
        code="flying_change",
        category="flatwork",
        discipline="dressage",
        label="Flying Change",
        description=(
            "The horse swaps canter lead in the moment of suspension, singly or in a "
            "counted sequence down the diagonal."
        ),
        base_score=0.80,
        typical_action_seconds=5.0,
        cues=(
            "lead changing without a break to trot",
            "changes evenly spaced when ridden in sequence",
            "straightness held through the change",
        ),
    ),
    MomentType(
        code="compulsory_movement",
        category="flatwork",
        discipline="para_dressage",
        label="Compulsory Movement",
        description=(
            "A movement required by the rider's grade test, executed to the letter of "
            "the pattern — a halt, a circle, a change of rein."
        ),
        base_score=0.74,
        typical_action_seconds=8.0,
        cues=(
            "movement ridden to a marked point in the arena",
            "accuracy of the figure against the arena letters",
            "adaptive aids used without disturbing the outline",
        ),
    ),
    MomentType(
        code="steady_rhythm",
        category="flatwork",
        discipline="para_dressage",
        label="Sustained Rhythm",
        description=(
            "A long passage where the horse holds one tempo and a relaxed frame, which "
            "is the heart of what this discipline is judged on."
        ),
        base_score=0.70,
        typical_action_seconds=10.0,
        cues=(
            "tempo unchanged over many strides",
            "topline soft, no tension in the neck or tail",
            "horse staying on the aids through a turn or transition",
        ),
    ),
)


# --- Jumping, Hunter and the jumping phases of eventing ----------------------

_OBSTACLE = (
    MomentType(
        code="clear_jump",
        category="obstacle",
        discipline="jumping",
        label="Clear Jump",
        description=(
            "Approach, take-off, clearance and landing over a fence left standing, with "
            "the stride met correctly on the way in."
        ),
        base_score=0.78,
        typical_action_seconds=4.0,
        cues=(
            "take-off point meeting the fence on a good stride",
            "every rail still in its cups after landing",
            "canter picked up cleanly on the landing side",
        ),
    ),
    MomentType(
        code="knocked_rail",
        category="obstacle",
        discipline="jumping",
        label="Knocked Rail",
        description="A pole dislodged from its cups and falling, adding faults to the round.",
        base_score=0.72,
        typical_action_seconds=4.0,
        cues=(
            "pole leaving the cup and hitting the ground",
            "the limb that touched it, fore or hind",
            "fault count on the on-screen graphic changing",
        ),
    ),
    MomentType(
        code="refusal",
        category="obstacle",
        discipline="jumping",
        label="Refusal or Run-Out",
        description=(
            "The horse stops in front of the fence or ducks past its side rather than "
            "jumping it."
        ),
        base_score=0.70,
        typical_action_seconds=5.0,
        cues=(
            "forward movement stopping within a stride of the fence",
            "shoulder dropping out to one side of the standard",
            "the re-approach that follows",
        ),
    ),
    MomentType(
        code="tight_turn",
        category="obstacle",
        discipline="jumping",
        label="Tight Turn",
        description=(
            "An inside turn or rollback taken against the clock, shortening the track "
            "between two fences at the cost of the approach."
        ),
        base_score=0.81,
        typical_action_seconds=4.0,
        cues=(
            "turn taken inside the obvious track",
            "balance recovered before the next take-off",
            "clock visible on screen during the round",
        ),
    ),
    MomentType(
        code="hunter_form",
        category="obstacle",
        discipline="hunter",
        label="Jumping Form",
        description=(
            "A square, even bascule over the fence: knees tucked level and high, back "
            "rounded, the picture this discipline is judged on."
        ),
        base_score=0.79,
        typical_action_seconds=4.0,
        cues=(
            "both knees folded to the same height",
            "rounded outline over the top of the fence",
            "quiet, undisturbed landing",
        ),
    ),
    MomentType(
        code="consistent_pace",
        category="obstacle",
        discipline="hunter",
        label="Consistent Pacing",
        description=(
            "A line ridden in the intended number of strides at one unvarying pace, "
            "fence to fence."
        ),
        base_score=0.70,
        typical_action_seconds=8.0,
        cues=(
            "stride count met without a visible adjustment",
            "no change of speed between the fences",
            "the same rhythm arriving as leaving",
        ),
    ),
    MomentType(
        code="sweeping_movement",
        category="obstacle",
        discipline="hunter",
        label="Sweeping Flat Movement",
        description=(
            "The long, low, flat-kneed way of going on the flat that a hunter is judged "
            "on between the fences."
        ),
        base_score=0.66,
        typical_action_seconds=8.0,
        cues=(
            "knee staying low through the swing of the stride",
            "the stride reaching forward rather than upward",
            "head and neck carried level",
        ),
    ),
    MomentType(
        code="stadium_knockdown",
        category="obstacle",
        discipline="eventing",
        label="Stadium Knockdown",
        description=(
            "A rail down in the showjumping phase, where the round is ridden on a "
            "cross-country horse and the fences fall easily."
        ),
        base_score=0.73,
        typical_action_seconds=4.0,
        cues=(
            "coloured poles in an arena rather than fixed obstacles",
            "pole leaving its cups",
            "penalties added on the on-screen graphic",
        ),
    ),
)


# --- Cross-country -----------------------------------------------------------

_CROSS_COUNTRY = (
    MomentType(
        code="water_complex",
        category="cross_country",
        discipline="eventing",
        label="Water Complex",
        description=(
            "Entry into water and the element that follows it, where the drag of the "
            "water changes the stride and the line."
        ),
        base_score=0.86,
        typical_action_seconds=6.0,
        cues=(
            "drop or step into standing water",
            "spray thrown up on landing",
            "an element jumped in or out of the water",
        ),
    ),
    MomentType(
        code="ditch_or_bank",
        category="cross_country",
        discipline="eventing",
        label="Ditch or Bank",
        description=(
            "A fixed cross-country obstacle taken over open ground: a ditch, a coffin, "
            "a bank up or down, or a step complex."
        ),
        base_score=0.84,
        typical_action_seconds=5.0,
        cues=(
            "solid obstacle that does not fall",
            "a change of level on take-off or landing",
            "the horse committing from a distance out",
        ),
    ),
    MomentType(
        code="cross_country_dressage",
        category="flatwork",
        discipline="eventing",
        label="Eventing Dressage Test",
        description=(
            "The flatwork phase of a three-day event: a test ridden in an arena by a "
            "horse fit for cross-country."
        ),
        base_score=0.62,
        typical_action_seconds=10.0,
        cues=(
            "lettered arena with no obstacles",
            "a test ridden to a pattern",
            "a horse visibly fitter and sharper than a pure dressage horse",
        ),
    ),
)


# --- Endurance, driving, vaulting, western -----------------------------------

_ENDURANCE = (
    MomentType(
        code="vet_gate",
        category="endurance",
        discipline="endurance",
        label="Vet Gate Inspection",
        description=(
            "A compulsory halt where a vet takes the horse's heart rate and checks it "
            "is fit to continue; the ride's real deciding point."
        ),
        base_score=0.72,
        typical_action_seconds=12.0,
        cues=(
            "stethoscope on the horse's side",
            "crew holding the horse still at a marked area",
            "trot-up in hand for soundness",
        ),
    ),
    MomentType(
        code="cooling_effort",
        category="endurance",
        discipline="endurance",
        label="Cooling and Recovery",
        description=(
            "Water thrown and scraped off between loops, with the horse's respiration "
            "visible in the flank."
        ),
        base_score=0.60,
        typical_action_seconds=10.0,
        cues=(
            "water poured over the neck and quarters",
            "sweat scraper used immediately after",
            "flank movement showing the breathing rate",
        ),
    ),
    MomentType(
        code="sustained_pace",
        category="endurance",
        discipline="endurance",
        label="Sustained Pace",
        description=(
            "A long stretch held at an efficient working pace over changing terrain, "
            "which is what the discipline is actually about."
        ),
        base_score=0.64,
        typical_action_seconds=12.0,
        cues=(
            "one rhythm held over uneven ground",
            "rider out of the saddle in a light seat",
            "terrain changing under the same pace",
        ),
    ),
)

_DRIVING = (
    MomentType(
        code="cone_gate",
        category="driving",
        discipline="driving",
        label="Cone Gate",
        description=(
            "The carriage threaded between paired cones with balls on top, where a "
            "touch drops the ball and costs a penalty."
        ),
        base_score=0.79,
        typical_action_seconds=4.0,
        cues=(
            "cones set barely wider than the wheel track",
            "balls staying on or falling",
            "wheels tracking through without a check of pace",
        ),
    ),
    MomentType(
        code="marathon_obstacle",
        category="driving",
        discipline="driving",
        label="Marathon Obstacle",
        description=(
            "A timed hazard of tight turns between fixed posts, taken at speed with the "
            "groom balancing the carriage."
        ),
        base_score=0.85,
        typical_action_seconds=8.0,
        cues=(
            "sharp direction changes inside a fixed structure",
            "groom leaning hard to keep the carriage down",
            "gates taken in a lettered order",
        ),
    ),
    MomentType(
        code="team_synchronisation",
        category="driving",
        discipline="driving",
        label="Team Synchronisation",
        description=(
            "The horses in harness moving as one — matched stride, matched head "
            "carriage, an even line across the pair or team."
        ),
        base_score=0.68,
        typical_action_seconds=8.0,
        cues=(
            "legs of the pair rising together",
            "even tension in the traces",
            "the line across the team staying square",
        ),
    ),
)

_ACROBATIC = (
    MomentType(
        code="vaulting_mount",
        category="acrobatic",
        discipline="vaulting",
        label="Dynamic Mount",
        description=(
            "The athlete springs from the ground onto a horse that is already "
            "cantering, without breaking its circle."
        ),
        base_score=0.83,
        typical_action_seconds=4.0,
        cues=(
            "run alongside a cantering horse",
            "push off the ground into the vault",
            "canter and circle unbroken",
        ),
    ),
    MomentType(
        code="handstand",
        category="acrobatic",
        discipline="vaulting",
        label="Handstand",
        description="A full handstand on the horse's back while the canter continues underneath.",
        base_score=0.90,
        typical_action_seconds=5.0,
        cues=(
            "both hands on the surcingle or the back, legs vertical",
            "position held for several strides",
            "horse's rhythm unchanged",
        ),
    ),
    MomentType(
        code="shoulder_stand",
        category="acrobatic",
        discipline="vaulting",
        label="Shoulder Stand",
        description="A shoulder stand held on the moving horse, body extended above the back.",
        base_score=0.87,
        typical_action_seconds=5.0,
        cues=(
            "shoulders bearing weight on the horse's back",
            "line of the body held straight",
            "the horse continuing its lunge circle",
        ),
    ),
    MomentType(
        code="dismount",
        category="acrobatic",
        discipline="vaulting",
        label="Artistic Dismount",
        description=(
            "The finish: an athlete leaves the cantering horse in a controlled, shaped "
            "movement and lands on their feet."
        ),
        base_score=0.80,
        typical_action_seconds=3.0,
        cues=(
            "shape held in the air on the way off",
            "landing on both feet, facing the horse",
            "the horse cantering on undisturbed",
        ),
    ),
)

_WESTERN = (
    MomentType(
        code="spin",
        category="western_maneuver",
        discipline="western",
        label="Spin",
        description=(
            "A series of 360-degree turns pivoting on a planted inside hind foot, at "
            "speed and on a loose rein."
        ),
        base_score=0.86,
        typical_action_seconds=5.0,
        cues=(
            "inside hind foot staying planted",
            "several full revolutions without a break",
            "rein hand still and low",
        ),
    ),
    MomentType(
        code="sliding_stop",
        category="western_maneuver",
        discipline="western",
        label="Sliding Stop",
        description=(
            "From a hard gallop the horse drops its hindquarters and slides on its hind "
            "feet while the front legs keep walking."
        ),
        base_score=0.92,
        typical_action_seconds=4.0,
        cues=(
            "hind feet locked and sliding, throwing up sand",
            "front legs continuing to move",
            "a visible slide track left in the arena",
        ),
    ),
    MomentType(
        code="rollback",
        category="western_maneuver",
        discipline="western",
        label="Rollback",
        description=(
            "A stop followed immediately by a 180-degree turn over the hocks and a "
            "departure back the way it came, in one movement."
        ),
        base_score=0.84,
        typical_action_seconds=4.0,
        cues=(
            "turn beginning without a pause after the stop",
            "pivot over the hocks rather than a step round",
            "leaving in the new direction at speed",
        ),
    ),
    MomentType(
        code="neck_rein_response",
        category="western_maneuver",
        discipline="western",
        label="Neck-Rein Response",
        description=(
            "An immediate change of direction from rein pressure on the neck with no "
            "contact on the mouth."
        ),
        base_score=0.70,
        typical_action_seconds=4.0,
        cues=(
            "rein visibly slack throughout",
            "one-handed rein hand moving across the neck",
            "the horse responding within a stride",
        ),
    ),
)

_PERFORMANCE = (
    MomentType(
        code="western_pleasure_pass",
        category="performance",
        discipline="general_performance",
        label="Western Pleasure Pass",
        description=(
            "A pass down the rail at a slow, level western gait, judged on how "
            "unhurried and level the horse looks."
        ),
        base_score=0.62,
        typical_action_seconds=8.0,
        cues=(
            "slow, level gait on a loose rein",
            "travelling along the arena rail",
            "topline flat and quiet",
        ),
    ),
    MomentType(
        code="trail_obstacle",
        category="performance",
        discipline="general_performance",
        label="Trail Obstacle",
        description=(
            "A set obstacle negotiated from the saddle: poles, a gate, a bridge or a "
            "back-through."
        ),
        base_score=0.74,
        typical_action_seconds=8.0,
        cues=(
            "an arranged obstacle rather than a jump",
            "precise foot placement between poles",
            "gate worked without letting go of it",
        ),
    ),
    MomentType(
        code="english_flatwork",
        category="performance",
        discipline="general_performance",
        label="English Flatwork",
        description=(
            "The same horse shown English: an English saddle, contact taken up, and a "
            "rounder frame than its western pass."
        ),
        base_score=0.64,
        typical_action_seconds=8.0,
        cues=(
            "English tack and attire, often after a visible change",
            "contact taken up on both reins",
            "a rounder outline than the western work",
        ),
    ),
)


EQUESTRIAN_CONTEXT = """\
What you are looking at:
- A dressage arena is 20 m x 60 m (or 20 m x 40 m), flat, with single letters on
  boards around the edge. Movements are ridden to and from those letters.
- A jumping course is numbered, and each fence carries a red flag on the right and
  a white flag on the left. Poles sit in shallow cups and fall when struck.
- Cross-country obstacles are solid and do not fall. Water, ditches, banks and
  steps are the ones that decide rounds.
- A vaulting horse is lunged on a circle by a person on the ground and wears a
  surcingle with two handles instead of a saddle.
- A driving turnout is one, two or four horses in harness with a carriage, a
  driver and at least one groom.

Tack and attire tell you the discipline faster than the movement does. A western
saddle has a horn and is ridden one-handed on a slack rein; an English saddle is
ridden with contact in both hands. Adaptive reins, a modified saddle or a rider
carrying two whips indicate para-dressage rather than dressage.

Broadcast conventions:
- Graphics usually show the competitor's number or name, and then a fault count
  and an elapsed time (jumping, eventing), a percentage (dressage), or a heart
  rate and a hold time (endurance). Read whatever is there; it is not a scoreline.
- Rounds are often replayed immediately, in slow motion and from another angle.
- Commentary rises on a clear round, a fall, a knockdown and a big movement.
  Treat it as supporting evidence, never as the only evidence.

The horse is the athlete as much as the human is. Where you describe form,
describe what the horse's body is doing, not only what the rider asked for.
"""

EQUESTRIAN_EXCLUSIONS = (
    "Replays of an action you have already reported at its live timestamp. Report the live "
    "occurrence and set is_replay on the replay if you report it at all.",
    "Warm-up and collecting-ring footage, presentations, prize-givings and interviews.",
    "Studio and punditry segments, advertising breaks and channel idents.",
    "Ordinary travel between obstacles or movements with nothing happening in it.",
    "Crowd, sponsor-board and groom cutaways that are not attached to a specific action.",
    "Any judgement about a horse's welfare, soundness or treatment. Report what is visibly "
    "happening and leave the verdict to the officials who are there.",
)


EQUESTRIAN = register_profile(
    SportProfile(
        sport="equestrian",
        display_name="Equestrian",
        moment_types=(
            _FLATWORK + _OBSTACLE + _CROSS_COUNTRY + _ENDURANCE
            + _DRIVING + _ACROBATIC + _WESTERN + _PERFORMANCE
        ),
        context=EQUESTRIAN_CONTEXT,
        exclusions=EQUESTRIAN_EXCLUSIONS,
        disciplines=DISCIPLINES,
        segment_schema=EquestrianSegmentAnalysis,
        action_results=(
            "Clear", "Knockdown", "Refusal", "Run-out", "Fault", "Time Fault",
            "Eliminated", "Fall", "Completed", "Held", "Passed", "Retired",
        ),
        participant_roles=(
            "Horse-Rider Pair", "Horse-Driver Pair", "Vaulter", "Lunger",
            "Horse", "Groom", "Official",
        ),
        scoreboard_guidance="""\
A round has one competitor, so there is no scoreline to read.

- Put the on-screen graphic's text in `scoreboard` exactly as printed — number, \
name, faults, time, percentage, heart rate, whatever it shows.
- `team1` is the competitor as the graphic names them: the rider, the driver, the \
vaulter, or the horse if that is what is shown. Copy it as printed. Leave `team2` \
empty; there is no opponent on course.
- Leave `score_team1` and `score_team2` null. A fault count and an elapsed time \
are not a score between two sides, and putting them there would make a round look \
like a match.
- `action_team` is the nation or team when the graphic names one, and empty \
otherwise. Never infer it from the flag on a jump or the language of the \
commentary.
- Never write a rider's or a horse's name you did not read on screen or hear said. \
An invented name is worse than an empty field, because an editor will publish it.""",
    )
)
