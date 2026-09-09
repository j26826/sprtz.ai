"""Grounding a competition day against online.equipe.com.

The rule these protect is the one the fixture grounding already lives by:
a published value lands beside an observed one and never on top of it.
"""

from types import SimpleNamespace

from sprtz_agents.tools import grounding, rides


def ride(rider, horse, total=None, check=""):
    return {"rider": rider, "horse": horse, "total_pct": total,
            "score_check": check, "score_source": "observed" if total is not None else ""}


class TestTheAnswerIsParsedNotTrusted:
    def test_rides_survive_as_a_list(self):
        text = 'Found it. {"show": "LeMieux National Dressage Championships 2025", ' \
               '"className": "Kudos Grand Prix", "rides": [{"rider": "Becky Moody", ' \
               '"horse": "James Bond II", "finalPlace": 1, "totalPct": 74.8}], "notes": ""}'
        out = grounding.parse_show(text)
        assert out["show"].startswith("LeMieux")
        assert out["rides"][0]["finalPlace"] == 1

    def test_garbage_is_an_empty_answer(self):
        assert grounding.parse_show("no json here") == {}
        assert grounding.parse_show('{"rides": "not a list"}') == {}


class TestWhereTheAnswerCameFrom:
    """A prompt can prefer a site; only the citations say it was used."""

    def test_an_equipe_citation_is_recognised(self):
        assert grounding.cites_equipe([{"uri": "https://online.equipe.com/shows/73895"}])

    def test_a_general_web_answer_is_not_credited_to_equipe(self):
        assert not grounding.cites_equipe([{"uri": "https://example.com/results"}])
        assert not grounding.cites_equipe([])

    def test_sources_read_from_grounding_metadata(self):
        chunk = SimpleNamespace(web=SimpleNamespace(uri="https://online.equipe.com/starts/1", title="x"))
        resp = SimpleNamespace(candidates=[SimpleNamespace(
            grounding_metadata=SimpleNamespace(grounding_chunks=[chunk], web_search_queries=["q"]))])
        assert grounding.cites_equipe(grounding.extract_sources(resp))


class TestPublishedResultsLandBesideObservedOnes:
    def test_a_placing_is_attached_by_name(self):
        out = rides.apply_grounding(
            [ride("Becky Moody", "James Bond II", 74.8, "ok")],
            [{"rider": "Becky Moody", "horse": "James Bond II", "finalPlace": 1, "totalPct": 74.8}],
            source="equipe")
        assert out[0]["final_place"] == 1
        assert out[0]["grounded_source"] == "equipe"

    def test_an_observed_total_is_never_overwritten(self):
        out = rides.apply_grounding(
            [ride("Becky Moody", "James Bond II", 74.8, "ok")],
            [{"rider": "Becky Moody", "horse": "James Bond II", "totalPct": 71.2}],
            source="equipe")
        assert out[0]["total_pct"] == 74.8, "the camera's reading stays"
        assert out[0]["grounded_total_pct"] == 71.2, "and the published one sits beside it"
        assert out[0]["score_check"].startswith("equipe disagrees")

    def test_agreement_is_recorded_as_confirmation(self):
        out = rides.apply_grounding(
            [ride("Becky Moody", "James Bond II", 74.8, "ok")],
            [{"rider": "Becky Moody", "horse": "James Bond II", "totalPct": 74.8}],
            source="equipe")
        assert out[0]["score_check"] == "ok, confirmed by equipe"

    def test_an_empty_total_is_filled_and_labelled(self):
        """A round whose graphic never showed still scored something."""
        out = rides.apply_grounding(
            [ride("Becky Moody", "James Bond II")],
            [{"rider": "Becky Moody", "horse": "James Bond II", "totalPct": 74.8}],
            source="equipe")
        assert out[0]["total_pct"] == 74.8
        assert out[0]["score_source"] == "equipe", "filled, so it says by whom"

    def test_a_rider_with_two_horses_gets_the_right_one(self):
        out = rides.apply_grounding(
            [ride("Gareth Hughes", "Lufada MVL"), ride("Gareth Hughes", "Classic Briolinca")],
            [{"rider": "Gareth Hughes", "horse": "Classic Briolinca", "finalPlace": 2},
             {"rider": "Gareth Hughes", "horse": "Lufada MVL", "finalPlace": 5}],
            source="equipe")
        assert [r["final_place"] for r in out] == [5, 2]

    def test_published_spelling_does_not_replace_the_on_screen_one(self):
        out = rides.apply_grounding(
            [ride("A-M Bork Eppers", "Zeilinger Firfod")],
            [{"rider": "Anne-Marie Bork Eppers", "horse": "Zeilinger Firfod", "finalPlace": 4}],
            source="equipe")
        assert out[0]["rider"] == "A-M Bork Eppers"
        assert out[0]["grounded_rider"] == "Anne-Marie Bork Eppers"

    def test_a_ride_the_results_do_not_mention_is_left_alone(self):
        out = rides.apply_grounding([ride("Nobody", "No Horse")],
                                    [{"rider": "Becky Moody", "horse": "James Bond II"}],
                                    source="equipe")
        assert "final_place" not in out[0] and "grounded_source" not in out[0]

    def test_two_empty_names_do_not_match_each_other(self):
        out = rides.apply_grounding([ride("", "")], [{"rider": "", "horse": "", "finalPlace": 1}],
                                    source="equipe")
        assert "final_place" not in out[0]


class TestItIsWired:
    def test_the_pipeline_grounds_a_day_of_rides_as_a_show(self):
        import inspect

        from sprtz_agents.tools import pipeline

        src = inspect.getsource(pipeline._record_game_details)
        assert "identify_show" in src and "apply_grounding" in src
        assert "identify_fixture" in src, "and a match still grounds as a fixture"
