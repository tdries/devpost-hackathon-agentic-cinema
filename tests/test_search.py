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
    """Loki, with whatever lines the test wants. Records the query it got,
    and pages the way the real one does: newest first, `end` walking back."""

    def __init__(self, rows):
        self.rows = rows
        self.asked = None
        self.queries = []
        self.pages = 0

    def loki_lines(self, query, days=30, limit=400, end=None, **kw):
        self.asked = query
        self.queries.append(query)
        self.pages += 1
        rows = self.rows
        if end is not None:
            rows = [r for r in rows if int(r.get("ts_ns") or 0) <= end * 1e9]
        return rows[:limit]


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

    found = search.frames(ops, "rabbit", mode="literal")

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

    found = search.frames(ops, "bunn|rabbit|hare", mode="literal")

    assert [h["observation_id"] for h in found["hits"]] == ["obs_2"]


def test_a_pattern_that_anchors_itself_is_left_alone():
    ops = FakeOps([line("Share a bottle.", obs="obs_1")])

    assert search.frames(ops, "hare", mode="literal")["total"] == 0      # boundary applied
    assert search.frames(ops, "(?:)hare", mode="literal")["total"] == 1  # caller's own anchor


def test_the_same_frame_pushed_twice_is_one_result():
    """A re-pushed run writes every observation again and Loki keeps both,
    which is how one film answered a search for "wine" four hundred times."""
    ops = FakeOps([line("A wine glass is visible.")] * 4)

    found = search.frames(ops, "wine", mode="literal")

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

    found = search.frames(ops, "skirt", mode="literal")

    assert found["total"] == 3 and found["films"] == 2
    assert found["flagged"] == 2                       # a market objected
    assert found["by_market"] == {"AE": 2, "SA": 1}
    assert found["by_asset"] == {"catwalk": 2, "advert": 1}
    assert "3 frames across 2 films" in search.summary(found)


def test_two_words_that_are_not_adjacent_need_a_proximity_pattern():
    """The phrase people type finds nothing; the pattern the tool tells the
    agent to use finds both. This is why `text` is a regex."""
    ops = FakeOps([line("A model in a short pleated skirt.", obs="o1")])

    assert search.frames(ops, "short skirt", mode="literal")["total"] == 0
    assert search.frames(ops, "short.{0,30}skirt", mode="literal")["total"] == 1


def test_the_body_is_read_whatever_the_reader_called_it():
    """loki_lines names the parsed body `parsed`; the MCP path names it
    `finding`. Reading only one of them is a silent zero, because every
    caption comes back empty and nothing errors."""
    for key in ("parsed", "finding", "body"):
        ops = FakeOps([line("A rabbit.", key=key)])
        assert search.frames(ops, "rabbit", mode="literal")["total"] == 1, key

    bare = {"labels": {"asset": "ad"}, "line": json.dumps(
        {"statement": "A rabbit.", "run_id": "r", "observation_id": "o"})}
    assert search.frames(FakeOps([bare]), "rabbit", mode="literal")["total"] == 1


def test_the_filters_reach_loki_as_labels_and_the_pattern_as_a_line_filter():
    ops = FakeOps([])
    search.frames(ops, "rabbit", dimension="food_and_animals",
                  flagged="yes", mode="literal")

    assert ops.asked == ('{app="customs", kind="observation", '
                         'dimension="food_and_animals", flagged="yes"} '
                         '|~ "(?i)rabbit"')


def test_a_capped_result_says_so_rather_than_reporting_the_limit_as_a_count():
    ops = FakeOps([line(f"A wine glass. {n}", obs=f"o{n}") for n in range(50)])

    found = search.frames(ops, "wine", limit=50, mode="literal")

    assert found["capped"] and "At least 50 frames" in search.summary(found)


def test_a_broken_pattern_is_a_sentence_not_a_stack_trace():
    with pytest.raises(search.SearchError) as caught:
        search.frames(FakeOps([]), "a(b", mode="literal")
    assert "not a usable pattern" in str(caught.value)

    # the length cap is a literal-pattern rule; a long QUESTION is fine
    with pytest.raises(search.SearchError):
        search.frames(FakeOps([]), "x" * 500, mode="literal")


# --- the default: Gemini reads the captions ---------------------------------

def test_semantics_finds_the_frame_nobody_could_have_guessed_the_words_for(monkeypatch):
    """The point of the whole thing. The analyst wrote "an animated rabbit"
    months ago; somebody types "bunnies". A keyword search answers "none"
    and is believed. The question and the captions go to the model
    together, so the wording of the caption stops being the caller's
    problem."""
    ops = FakeOps([
        line("An animated rabbit character wearing a tuxedo.", obs="o1"),
        line("A pack of Parliament cigarettes on a table.", obs="o2"),
        line("A carrot lies on a chopping board.", obs="o3"),
    ])
    seen = {}

    def fake_batch(question, batch, model):
        seen["question"] = question
        seen["captions"] = [c for _, c in batch]
        return {n: "" for n, c in batch if "rabbit" in c}

    monkeypatch.setattr(search, "route", lambda *a, **k: [])
    monkeypatch.setattr(search, "_match_batch", fake_batch)

    found = search.frames(ops, "bunnies")

    assert found["total"] == 1
    assert found["hits"][0]["observation_id"] == "o1"
    assert seen["question"] == "bunnies"
    assert len(seen["captions"]) == 3, "the model sees them all, not a prefiltered set"
    assert found["mode"] == "semantic"


def test_the_model_never_sees_the_same_caption_twice(monkeypatch):
    """Nine shots of the same product write the same sentence nine times,
    and a re-pushed run doubles that. Sending each distinct caption once is
    the difference between a small prompt and a bill."""
    ops = FakeOps([line("A wine glass is visible.", obs=f"o{n}") for n in range(9)]
                  + [line("A cigarette burns in an ashtray.", obs="o9")])
    batches = []

    def fake_batch(question, batch, model):
        batches.append([c for _, c in batch])
        return {n: "" for n, c in batch if "wine" in c}

    monkeypatch.setattr(search, "route", lambda *a, **k: [])
    monkeypatch.setattr(search, "_match_batch", fake_batch)

    found = search.frames(ops, "alcohol")

    assert batches == [["A wine glass is visible.",
                        "A cigarette burns in an ashtray."]]
    assert found["total"] == 9, "and every frame that shares the caption still matches"


def test_a_number_the_model_invented_is_not_a_frame(monkeypatch):
    """A hallucinated index would be a search result pointing at nothing.
    Anything outside the batch it was handed is dropped."""
    ops = FakeOps([line("An animated rabbit.", obs="o1")])
    monkeypatch.setattr(search, "generate_json_for_test", None, raising=False)

    def fake_generate(model, parts, schema, thinking_budget=None):
        return {"matches": [0, 77]}

    monkeypatch.setattr(search, "route", lambda *a, **k: [])
    monkeypatch.setattr("customs.genai_client.generate_json", fake_generate)

    found = search.frames(ops, "bunnies")
    assert found["total"] == 1


def test_the_model_being_down_costs_the_reasoning_not_the_search(monkeypatch):
    """Grafana is up, the captions are there, and Vertex is not answering.
    Falling back to the words as typed finds the obvious ones, and the
    result says which of the two answered so nobody reads a degraded search
    as a complete one."""
    ops = FakeOps([
        line("An animated rabbit character.", obs="o1"),
        line("A cigarette pack.", obs="o2"),
    ])

    def explode(question, batch, model):
        raise RuntimeError("vertex is down")

    monkeypatch.setattr(search, "route", lambda *a, **k: [])
    monkeypatch.setattr(search, "_match_batch", explode)

    found = search.frames(ops, "rabbit")

    assert found["total"] == 1 and found["hits"][0]["observation_id"] == "o1"
    assert "semantic failed" in found["mode"]


def test_the_search_pages_until_the_stream_is_out_not_until_the_first_page(monkeypatch):
    """Loki answers newest-first, one page at a time. Reading only the first
    page is how "are there any rabbits" gets a confident no when the rabbits
    are three weeks old and the last fortnight was busy."""
    monkeypatch.setattr(search, "_PAGE", 2)
    rows = [line(f"caption {n}", obs=f"o{n}") for n in range(6)]
    for n, row in enumerate(rows):
        row["ts_ns"] = str((100 - n) * 1_000_000_000)  # newest first
    ops = FakeOps(rows)

    found = search.frames(ops, "", limit=100)

    assert found["total"] == 6, "every page, not just the newest"
    assert ops.pages > 1


def test_the_labels_narrow_the_corpus_before_the_model_reads_it(monkeypatch):
    """Eighteen dimensions, and a question about a rabbit has no business
    being answered by a model reading four thousand captions about
    hemlines. The routing call picks the labels, the selector carries them,
    and only what is left gets read."""
    ops = FakeOps([line("An animated rabbit.", obs="o1")])
    monkeypatch.setattr(search, "route",
                        lambda *a, **k: ["food_and_animals", "humour_irony_satire"])
    monkeypatch.setattr(search, "_match_batch",
                        lambda q, batch, m: {n: "" for n, _ in batch})

    found = search.frames(ops, "bunnies")

    labelled = [q for q in ops.queries if "dimension=~" in q]
    assert labelled and 'dimension=~"food_and_animals|humour_irony_satire"' in labelled[0]
    # ...and the question's own words are asked of everything else, because
    # a dimension says why a frame matters, not what is in it
    assert any("bunnies" in q and "dimension" not in q for q in ops.queries)
    assert found["routed"] == ["food_and_animals", "humour_irony_satire"]


def test_routing_that_fails_reads_everything_rather_than_nothing(monkeypatch):
    """Narrowing is an optimisation. If it breaks, the search gets slower,
    not wrong."""
    ops = FakeOps([line("An animated rabbit.", obs="o1")])

    def explode(*a, **k):
        raise RuntimeError("no model")

    monkeypatch.setattr(search, "route", explode)
    monkeypatch.setattr(search, "_match_batch",
                        lambda q, batch, m: {n: "" for n, _ in batch})

    found = search.frames(ops, "bunnies")

    assert found["total"] == 1 and found["routed"] == []
    assert "dimension" not in ops.asked


# --- the index: retrieval without a reading ---------------------------------

def test_the_index_answers_from_vectors_and_the_model_only_reads_the_shortlist(
        tmp_path, monkeypatch):
    """The point of the index. Eighty captions are read, not four thousand,
    and the eighty come back from a dot product rather than a model."""
    from customs import vectors
    from customs.schema import Observation
    from customs.store import Store

    store = Store(tmp_path / "s.db")
    run = store.create_run(asset_path="runs/uploads/a/toon.mp4", markets=["FR"])
    store.add_observations(run.id, [
        Observation(id="o1", shot_id="shot_0", t_start=1.0, t_end=2.0,
                    dimension="food_and_animals",
                    statement="An animated rabbit in a tuxedo.", confidence=0.9,
                    evidence_frame="/x/a.png"),
        Observation(id="o2", shot_id="shot_1", t_start=3.0, t_end=4.0,
                    dimension="food_and_animals",
                    statement="A carrot on a chopping board.", confidence=0.9,
                    evidence_frame="/x/b.png"),
    ])
    # a stand-in embedder: "rabbit" points one way, everything else the other
    def fake_embed(texts, query=False, model=""):
        out = []
        for text in texts:
            near = 1.0 if ("rabbit" in text.lower() or "bunn" in text.lower()) else 0.0
            out.append(vectors._pack([near, 1.0 - near]))
        return out

    monkeypatch.setattr(vectors, "embed", fake_embed)
    assert vectors.index_run(store, run.id) == 2
    assert vectors.size(store) == 2

    read = {}

    def fake_batch(question, batch, model):
        read["captions"] = [c for _, c in batch]
        return {n: "" for n, c in batch if "rabbit" in c}

    monkeypatch.setattr(search, "_match_batch", fake_batch)

    found = search.indexed(store, "bunnies", k=1)

    assert found["mode"] == "indexed"
    assert found["total"] == 1
    assert found["hits"][0]["observation_id"] == "o1"
    assert read["captions"] == ["An animated rabbit in a tuxedo."], \
        "the model reads the shortlist, not the corpus"


def test_a_second_index_of_the_same_run_embeds_nothing(tmp_path, monkeypatch):
    """Indexing is idempotent, because it runs at the end of every clearance
    and again from the backfill, and embedding is the part that costs."""
    from customs import vectors
    from customs.schema import Observation
    from customs.store import Store

    store = Store(tmp_path / "s.db")
    run = store.create_run(asset_path="runs/uploads/a/toon.mp4", markets=["FR"])
    store.add_observations(run.id, [
        Observation(id="o1", shot_id="shot_0", t_start=1.0, t_end=2.0,
                    dimension="none", statement="A rabbit.", confidence=0.9,
                    evidence_frame="")])
    calls = []
    monkeypatch.setattr(vectors, "embed",
                        lambda texts, **kw: calls.append(texts) or
                        [vectors._pack([1.0, 0.0]) for _ in texts])

    assert vectors.index_run(store, run.id) == 1
    assert vectors.index_run(store, run.id) == 0
    assert len(calls) == 1


def test_an_index_that_cannot_be_built_costs_a_slow_search_not_a_run(
        tmp_path, monkeypatch):
    from customs import vectors
    from customs.schema import Observation
    from customs.store import Store

    store = Store(tmp_path / "s.db")
    run = store.create_run(asset_path="runs/uploads/a/toon.mp4", markets=["FR"])
    store.add_observations(run.id, [
        Observation(id="o1", shot_id="shot_0", t_start=1.0, t_end=2.0,
                    dimension="none", statement="A rabbit.", confidence=0.9,
                    evidence_frame="")])

    def explode(*a, **kw):
        raise RuntimeError("vertex is down")

    monkeypatch.setattr(vectors, "embed", explode)

    assert vectors.index_run(store, run.id) == 0
    assert vectors.size(store) == 0
