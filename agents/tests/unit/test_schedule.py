"""Placing the published start list against the video, and what follows from it.

A lower third names some rounds and not others. The start list knows who rode
at 14:32 but not where 14:32 is in an eight-hour file; the named rounds are what
tie the two together. These tests are the rules of that tying, plus the pins
that keep the new fields from being dropped at the Firestore boundary — which
has now happened, or nearly, four times.
"""

import re
from pathlib import Path

from sprtz_agents.tools import grounding, rides

ROOT = Path(__file__).resolve().parents[3]


def ride(order, start, end, rider="", horse="", **kw):
    return {"order": order, "start_sec": start, "end_sec": end, "rider": rider, "horse": horse, **kw}


def slot(number, hhmm, rider, horse):
    return {"startNumber": str(number), "startTime": hhmm, "rider": rider, "horse": horse}


START_LIST = [
    slot(101, "14:00", "Alexander Harrison", "Kickback"),
    slot(102, "14:08", "Nathalie Wahlund", "Cerano Gold"),
    slot(103, "14:16", "Angus Corrie-Deane", "Jack Johnson"),
    slot(104, "14:24", "Bobby Hayler", "Iconic II"),
]


class TestAnchoring:
    def test_a_named_round_places_the_unnamed_one_after_it(self):
        """The camera named the first rider; the schedule names the second."""
        placed, info = rides.align_schedule([
            ride(1, 214, 686, "Alexander Harrison", "Kickback"),  # 14:00 -> 214s in the file
            ride(2, 700, 1160),                                   # nobody named this one
        ], START_LIST)
        assert info["anchors"] == 1 and info["named"] == 1
        assert placed[1]["rider"] == "Nathalie Wahlund"
        assert placed[1]["start_number"] == "102"
        assert placed[1]["identity_source"] == "schedule"
        assert placed[0]["identity_source"] == "observed", "a graphic's name stays a graphic's"

    def test_the_named_round_also_learns_its_head_number(self):
        placed, _ = rides.align_schedule(
            [ride(1, 214, 686, "Alexander Harrison", "Kickback")], START_LIST)
        assert placed[0]["start_number"] == "101"

    def test_the_median_survives_one_misattributed_anchor(self):
        """Three anchors agree on the day's offset; one is wildly off. It loses."""
        placed, info = rides.align_schedule([
            ride(1, 214, 686, "Alexander Harrison", "Kickback"),      # delta 50186
            ride(2, 694, 1160, "Nathalie Wahlund", "Cerano Gold"),    # delta 50186
            ride(3, 1174, 1640, "Angus Corrie-Deane", "Jack Johnson"),  # delta 50186
            ride(4, 5000, 5400, "Bobby Hayler", "Iconic II"),         # a bad anchor
            ride(5, 1654, 2100),                                       # unnamed, at 14:24
        ], START_LIST)
        assert info["anchors"] == 4
        assert placed[4]["rider"] == "Bobby Hayler", "the majority offset placed it"

    def test_a_round_midway_between_two_slots_is_not_an_identification(self):
        """Equidistant is the honest ambiguous case: neither slot is nearer."""
        tight = [slot(1, "14:00", "A", "Horse A"), slot(2, "14:04", "B", "Horse B")]
        placed, info = rides.align_schedule([
            ride(1, 100, 200, "A", "Horse A"),   # 14:00 -> 100s, so the offset is 50300
            ride(2, 220, 400),                    # predicted 14:02:00 -- 120s from each
        ], tight)
        assert info["named"] == 0
        assert placed[1]["rider"] == "", "a guess with a coin is left blank"

    def test_clearly_nearer_beats_a_neighbour_inside_the_window(self):
        """Six seconds from one slot, eight minutes from the next: it is that slot."""
        placed, info = rides.align_schedule([
            ride(1, 214, 686, "Alexander Harrison", "Kickback"),
            ride(2, 700, 1160),
        ], START_LIST)
        assert info["named"] == 1 and placed[1]["rider"] == "Nathalie Wahlund"

    def test_no_anchor_names_nothing(self):
        placed, info = rides.align_schedule([ride(1, 100, 200), ride(2, 300, 400)], START_LIST)
        assert info == {"anchors": 0, "offset_sec": None, "named": 0}
        assert all(r["rider"] == "" for r in placed)

    def test_a_start_list_with_no_times_names_nothing(self):
        no_times = [{"startNumber": "1", "rider": "A", "horse": "H", "startTime": ""}]
        _, info = rides.align_schedule([ride(1, 100, 200, "A", "H"), ride(2, 300, 400)], no_times)
        assert info["named"] == 0

    def test_a_far_off_prediction_is_not_named(self):
        placed, _ = rides.align_schedule([
            ride(1, 214, 686, "Alexander Harrison", "Kickback"),
            ride(2, 9000, 9400),  # predicted ~16:26, nothing scheduled near it
        ], START_LIST)
        assert placed[1]["rider"] == ""


class TestMomentsLearnTheirRide:
    def test_joined_by_the_peak_time(self):
        out = rides.attach_moments(
            [{"moment_id": "m1", "start_sec": 300, "peak_sec": 320, "end_sec": 330}],
            [ride(1, 214, 686, "Alexander Harrison", "Kickback",
                  start_number="101", identity_source="observed")])
        assert out[0]["rider"] == "Alexander Harrison"
        assert out[0]["start_number"] == "101"
        assert out[0]["ride_order"] == 1
        assert out[0]["identity_source"] == "observed"

    def test_a_moment_outside_every_ride_is_left_alone(self):
        out = rides.attach_moments(
            [{"moment_id": "m1", "start_sec": 5000, "peak_sec": 5010, "end_sec": 5020}],
            [ride(1, 214, 686, "A", "H")])
        assert "rider" not in out[0]


class TestThePublishedAnswer:
    def test_judges_and_start_list_survive_parsing_as_lists(self):
        text = ('{"show": "LeMieux Nationals", "judges": [{"position": "C", "name": "J Smith"}], '
                '"startList": [{"startNumber": "101", "startTime": "14:00", "rider": "A", "horse": "H"}], '
                '"rides": []}')
        out = grounding.parse_show(text)
        assert out["judges"][0]["position"] == "C"
        assert out["startList"][0]["startTime"] == "14:00"

    def test_a_string_where_a_list_belongs_is_dropped(self):
        out = grounding.parse_show('{"show": "x", "judges": "C Smith", "startList": "..."}')
        assert "judges" not in out and "startList" not in out

    def test_a_result_row_carries_the_head_number_and_nation(self):
        out = rides.apply_grounding(
            [ride(1, 100, 200, "Becky Moody", "James Bond II", total_pct=None)],
            [{"rider": "Becky Moody", "horse": "James Bond II", "startNumber": "7",
              "nation": "GBR", "finalPlace": 1}],
            source="equipe")
        assert out[0]["start_number"] == "7" and out[0]["nation"] == "GBR"


class TestTheBoundaryThatDropsFields:
    """Both Firestore writers are field-by-field. Correct everywhere else, absent here."""

    STORE = (ROOT / "mcp" / "catalog_server" / "store.py").read_text()

    def _fn(self, name):
        m = re.search(rf"def {name}\(.*?(?=\ndef )", self.STORE, re.S)
        assert m, name
        return m.group(0)

    def test_moment_identity_is_written(self):
        body = self._fn("upsert_moments")
        for key in ("rider", "horse", "startNumber", "rideOrder", "identitySource"):
            assert f'"{key}"' in body, f"{key} dropped on write"

    def test_moment_identity_is_read_back(self):
        body = self._fn("_moment_out")
        for key in ("startNumber", "rideOrder", "identitySource"):
            assert key in body, f"{key} lost on read"

    def test_game_enrichment_is_written(self):
        body = self._fn("upsert_game")
        for key in ("showTitle", "location", "equipeUrl", "judges", "startList",
                    "scheduleAnchors", "scheduleOffsetSec"):
            assert f'"{key}"' in body, f"{key} dropped on write"

    def test_the_identity_patch_exists_and_is_a_tool(self):
        assert "def update_moment_identity(" in self.STORE
        server = (ROOT / "mcp" / "catalog_server" / "server.py").read_text()
        assert re.search(r"@mcp\.tool\s*\n\s*def update_moment_identity\(", server), (
            "the store function exists but no tool exposes it — the agent cannot call it"
        )


class TestItIsWired:
    def test_the_pipeline_uses_all_of_it(self):
        import inspect

        from sprtz_agents.tools import pipeline

        src = inspect.getsource(pipeline)
        for name in ("align_schedule", "attach_moments", "apply_grounding",
                     "update_moment_identity", "_patch_moment_identities"):
            assert name in src, f"{name} has no call site"
