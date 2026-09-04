"""Searching the captions: the two-stage filter, and what it refuses to count.

Every one of these is about a wrong answer that is invisible. A search that
matches a rule id instead of a caption returns a plausible number. A search
that counts one frame four times because the run was re-pushed returns a
plausible number. A search for "hare" that finds SHARE returns a plausible
number. Nothing errors; the count is just wrong, and a count is exactly what
somebody asked for.
"""
import json

import pytest

from customs import search


class FakeOps:
    """Loki, with whatever lines the test wants. Records the query it got."""

    def __init__(self, rows):
        self.rows = rows
        self.asked = None

    def loki_lines(self, query, days=30, limit=400, **kw):
        self.asked = query
        return self.rows[:limit]


def line(statement, *, asset="ad", run="run_1", obs="obs_1", dimension="none",
         markets=(), rules=(), frame=True, t=1.0, key="parsed"):
    body = {"run_id": run, "observation_id": obs, "shot_id": "shot_0",
            "t_start": t, "t_end": t + 1, "dimension": dimension,
            "statement": statement, "confidence": 0.9, "has_frame": frame,
            "markets": list(markets), "rules": list(rules),
            "max_severity": 70 if markets else 0}
    return {"ts_ns": "1", "labels": {"app": "customs", "asset": asset},
            "line": json.dumps(body), key: body}


def test_a_rule_id_that_contains_the_word_is_not_a_frame_that_shows_it():
    """Loki's line filter matches the whole JSON line, so a rule named
    RABBIT-01 is a hit on the stream and not a hit on the picture. The
    caption is re-checked here, alone, because "how many frames show a
    rabbit" must not be answered with "the ones a rule was named after"."""
    ops = FakeOps([
        line("An animated rabbit holds a cigar.", obs="obs_1"),
        line("A woman raises a glass.", obs="obs_2", rules=["RABBIT-01"]),
    ])

    found = search.frames(ops, "rabbit")

    assert found["total"] == 1
    assert found["hits"][0]["observation_id"] == "obs_1"


def test_a_word_boundary_stops_hare_finding_share():
    """The trap this actually fell into: "bunn|rabbit|hare" returned frames
    whose captions said SHARE and SMILE. Anchoring the start of the pattern
    and not the end is what keeps "bunn" finding bunnies while "hare" stops
    finding "share"."""
    ops = FakeOps([
        line("The words SHARE and SMILE in chalk.", obs="obs_1"),
        line("Two bunnies in a field.", obs="obs_2"),
    ])

    found = search.frames(ops, "bunn|rabbit|hare")

    assert [h["observation_id"] for h in found["hits"]] == ["obs_2"]


def test_a_pattern_that_anchors_itself_is_left_alone():
    ops = FakeOps([line("Share a bottle.", obs="obs_1")])

    assert search.frames(ops, "hare")["total"] == 0      # boundary applied
    assert search.frames(ops, "(?:)hare")["total"] == 1  # caller's own anchor


def test_the_same_frame_pushed_twice_is_one_result():
    """A re-pushed run writes every observation again and Loki keeps both,
    which is how one film answered a search for "wine" four hundred times."""
    ops = FakeOps([line("A wine glass is visible.")] * 4)

    found = search.frames(ops, "wine")

    assert found["total"] == 1 and found["films"] == 1


def test_the_counts_are_the_answer_to_how_many():
    """"How many frames show a short skirt" is a counting question, so the
    result carries the counts and nobody has to tally rows to answer it."""
    ops = FakeOps([
        line("A model in a short skirt.", obs="o1", asset="catwalk",
             dimension="modesty_dress_body", markets=["AE", "SA"]),
        line("A skirt cut short at the knee.", obs="o2", asset="catwalk",
             dimension="modesty_dress_body", markets=["AE"]),
        line("A skirt below the ankle.", obs="o3", asset="advert",
             dimension="modesty_dress_body"),
    ])

    found = search.frames(ops, "skirt")

    assert found["total"] == 3 and found["films"] == 2
    assert found["flagged"] == 2                       # a market objected
    assert found["by_market"] == {"AE": 2, "SA": 1}
    assert found["by_asset"] == {"catwalk": 2, "advert": 1}
    assert "3 frames across 2 films" in search.summary(found)


def test_two_words_that_are_not_adjacent_need_a_proximity_pattern():
    """The phrase people type finds nothing; the pattern the tool tells the
    agent to use finds both. This is why `text` is a regex."""
    ops = FakeOps([line("A model in a short pleated skirt.", obs="o1")])

    assert search.frames(ops, "short skirt")["total"] == 0
    assert search.frames(ops, "short.{0,30}skirt")["total"] == 1


def test_the_body_is_read_whatever_the_reader_called_it():
    """loki_lines names the parsed body `parsed`; the MCP path names it
    `finding`. Reading only one of them is a silent zero, because every
    caption comes back empty and nothing errors."""
    for key in ("parsed", "finding", "body"):
        ops = FakeOps([line("A rabbit.", key=key)])
        assert search.frames(ops, "rabbit")["total"] == 1, key

    bare = {"labels": {"asset": "ad"}, "line": json.dumps(
        {"statement": "A rabbit.", "run_id": "r", "observation_id": "o"})}
    assert search.frames(FakeOps([bare]), "rabbit")["total"] == 1


def test_the_filters_reach_loki_as_labels_and_the_pattern_as_a_line_filter():
    ops = FakeOps([])
    search.frames(ops, "rabbit", dimension="food_and_animals", flagged="yes")

    assert ops.asked == ('{app="customs", kind="observation", '
                         'dimension="food_and_animals", flagged="yes"} '
                         '|~ "(?i)rabbit"')


def test_a_capped_result_says_so_rather_than_reporting_the_limit_as_a_count():
    ops = FakeOps([line(f"A wine glass. {n}", obs=f"o{n}") for n in range(50)])

    found = search.frames(ops, "wine", limit=50)

    assert found["capped"] and "At least 50 frames" in search.summary(found)


def test_a_broken_pattern_is_a_sentence_not_a_stack_trace():
    with pytest.raises(search.SearchError) as caught:
        search.frames(FakeOps([]), "a(b")
    assert "not a usable pattern" in str(caught.value)

    with pytest.raises(search.SearchError):
        search.frames(FakeOps([]), "x" * 500)
