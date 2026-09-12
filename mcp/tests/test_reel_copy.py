"""The copy a reel goes out with.

This is published to a channel under someone's name, which makes it the most
consequential prose on the desk. The rule it inherits from the game record is
that facts are assembled in code and only judgement is generated — so what is
tested here is mostly the boundary between the two.

The keywords matter most. A keyword nobody competed under is a channel
claiming something that did not happen, so they come from the sport's own
taxonomy and the names the analysis read, and never from a model. The digest
matters for the same reason in reverse: everything in it is something the
model might repeat, so it must contain only observations.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catalog_server import reel_copy  # noqa: E402
from catalog_server.reel_copy import (  # noqa: E402
    MAX_DESCRIPTION, ReelCopy, build_digest, collect_keywords, compose, compose_title,
)

REEL = {"title": "Highlights", "durationMs": 28_000, "cuts": [{}, {}, {}]}
MOMENTS = [
    {"label": "Pirouette", "rider": "Jonas Keller", "horse": "Falkenstein",
     "summary": "Tight canter pirouette to the left."},
    {"label": "Passage", "rider": "Jonas Keller", "horse": "Falkenstein",
     "summary": "Cadenced passage along the long side."},
]
DRESSAGE = {"title": "Preview CDI 3*", "discipline": "Dressage",
            "competition": "Preview CDI 3*", "venue": "Lindenhof Arena"}
HANDBALL = {"title": "NOR v RHE", "sport": "handball",
            "competition": "Preview League", "venue": "Nordhalle"}


class TestTheTitleIsComposedNotWritten:
    def test_a_name_an_editor_typed_is_the_name(self):
        # Same rule as a match's title: if a person typed it, it wins.
        reel = {**REEL, "title": "Cup final — the best of it"}
        assert compose_title(reel, [DRESSAGE]) == "Cup final — the best of it"

    def test_one_event_is_named_after_it(self):
        assert compose_title(REEL, [DRESSAGE]) == "Preview CDI 3* — Highlights"

    def test_several_events_of_one_sport_are_named_after_the_sport(self):
        other = {**DRESSAGE, "competition": "Autumn CDI"}
        assert compose_title(REEL, [DRESSAGE, other]) == "Dressage — Highlights"

    def test_several_sports_fall_back_rather_than_pick_one(self):
        # Naming a mixed reel after one of its sports is worse than not naming
        # it: the other half is then mislabelled.
        assert compose_title(REEL, [DRESSAGE, HANDBALL]) == "Highlights"

    def test_the_default_title_is_not_treated_as_a_typed_name(self):
        assert compose_title({**REEL, "title": "Untitled reel"}, [DRESSAGE]) \
            == "Preview CDI 3* — Highlights"

    def test_it_never_exceeds_what_youtube_keeps(self):
        long = {**REEL, "title": "x" * 400}
        assert len(compose_title(long, [DRESSAGE])) <= 100


class TestKeywordsComeFromTheRecord:
    def test_every_keyword_appears_in_the_records(self):
        # The claim this file exists to defend.
        words = collect_keywords(REEL, MOMENTS, [DRESSAGE])
        source = " ".join([
            *(str(v) for m in MOMENTS for v in m.values()),
            *(str(v) for v in DRESSAGE.values()),
        ]).lower()
        for word in words:
            assert word.lower() in source, word

    def test_the_sport_and_the_competition_come_first(self):
        words = collect_keywords(REEL, MOMENTS, [DRESSAGE])
        assert words[0] == "Dressage"
        assert "Preview CDI 3*" in words[:3]

    def test_riders_and_horses_are_kept(self):
        words = collect_keywords(REEL, MOMENTS, [DRESSAGE])
        assert "Jonas Keller" in words
        assert "Falkenstein" in words

    def test_a_repeated_rider_is_named_once(self):
        words = collect_keywords(REEL, MOMENTS, [DRESSAGE])
        assert words.count("Jonas Keller") == 1

    def test_a_reel_with_no_names_still_yields_the_sport(self):
        bare = [{"label": "Double Save"}]
        assert collect_keywords(REEL, bare, [HANDBALL])[0] == "handball"

    def test_there_are_never_more_than_youtube_can_use(self):
        many = [{"label": f"Move {i}", "rider": f"Rider {i}"} for i in range(40)]
        assert len(collect_keywords(REEL, many, [DRESSAGE])) <= reel_copy.MAX_TAGS


class TestTheDigestIsObservationsOnly:
    def test_it_carries_what_was_read(self):
        digest = build_digest(REEL, MOMENTS, [DRESSAGE])
        assert "Jonas Keller" in digest
        assert "Pirouette" in digest
        assert "Lindenhof Arena" in digest

    def test_it_says_how_long_and_how_many(self):
        digest = build_digest(REEL, MOMENTS, [DRESSAGE])
        assert "3 cuts" in digest
        assert "28 seconds" in digest

    def test_it_survives_a_reel_with_nothing_in_it(self):
        assert build_digest({"cuts": []}, [], [])


class TestWhatIsGeneratedAndWhatIsNot:
    def test_without_a_model_the_copy_is_still_publishable(self):
        # A reel that publishes with a plain description is a much better
        # outcome than one that cannot be published.
        out = compose(REEL, MOMENTS, [DRESSAGE], None)
        assert out["description"]
        assert out["tags"]
        assert out["hashtags"]
        assert out["generated"] is False

    def test_a_generated_description_is_used_and_marked(self):
        written = ReelCopy(description="Two rounds worth watching twice.",
                           hashtags=["Dressage", "Falkenstein"])
        out = compose(REEL, MOMENTS, [DRESSAGE], written)
        assert out["description"] == "Two rounds worth watching twice."
        assert out["generated"] is True

    def test_generated_hashtags_lose_the_hash_and_the_spaces(self):
        written = ReelCopy(description="x", hashtags=["#grand prix", "Jonas Keller"])
        out = compose(REEL, MOMENTS, [DRESSAGE], written)
        assert out["hashtags"][:2] == ["GrandPrix", "JonasKeller"]

    def test_duplicate_hashtags_are_dropped_however_they_are_spelled(self):
        written = ReelCopy(description="x", hashtags=["dressage", "Dressage", "#DRESSAGE"])
        assert len(compose(REEL, MOMENTS, [DRESSAGE], written)["hashtags"]) == 1

    def test_keywords_are_never_taken_from_the_model(self):
        # The model writes hashtags; it does not get to write keywords. A tag
        # is what the channel is claiming the video is about.
        written = ReelCopy(description="x", hashtags=["OlympicFinal", "WorldRecord"])
        out = compose(REEL, MOMENTS, [DRESSAGE], written)
        assert "OlympicFinal" not in out["tags"]
        assert "WorldRecord" not in out["tags"]

    def test_a_runaway_description_is_cut(self):
        written = ReelCopy(description="word " * 5000, hashtags=[])
        assert len(compose(REEL, MOMENTS, [DRESSAGE], written)["description"]) <= MAX_DESCRIPTION

    def test_an_empty_generated_description_falls_back_rather_than_publishing_blank(self):
        out = compose(REEL, MOMENTS, [DRESSAGE], ReelCopy(description="   ", hashtags=[]))
        assert out["description"].strip()
        assert out["generated"] is False


class TestThePromptForbidsInvention:
    def test_it_says_so_in_as_many_words(self):
        # The one instruction that keeps a channel from claiming a record that
        # was never set. Worth a test because it is easy to soften by accident.
        prompt = reel_copy.prompt_for("digest")
        for phrase in ("Do not name", "do not state a score", "do not say a moment was the best"):
            assert phrase in prompt
        assert "digest" in prompt
