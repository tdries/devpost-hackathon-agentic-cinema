"""How structural a violation is, and what that rules out."""
from customs import costs, scope


def test_a_detail_a_beat_a_shot_and_a_premise():
    # a 0.8s detail in a 30s film
    assert scope.structural(0.8, 30.0, 0.8, 1) == "frame"
    # part of the action inside one shot
    assert scope.structural(3.0, 30.0, 3.0, 1) == "segment"
    # a long take is still only a segment: duration is not meaning. A wine
    # glass through a seven second shot is a detail in a long shot.
    assert scope.structural(7.0, 30.0, 7.0, 1) == "segment"
    # one finding covering most of the film, though, is the film
    assert scope.structural(20.0, 30.0, 20.0, 1) == "concept"
    # the same rule firing across most of the running time is the film itself
    assert scope.structural(3.0, 30.0, 15.0, 3) == "concept"
    # ...as is a rule that keeps firing shot after shot and holds real screen
    # time between those hits
    assert scope.structural(1.0, 60.0, 14.0, 5) == "concept"
    # but repetition alone is not a premise: four glimpses in a long film is
    # a detail, four times over
    assert scope.structural(1.0, 60.0, 4.0, 4) == "frame"


def test_the_judge_and_the_arithmetic_escalate_but_never_soften():
    # the judge read the film: a short shot can still be the whole premise
    assert scope.merge("concept", "frame") == "concept"
    # the judge underestimated it; coverage overrules
    assert scope.merge("frame", "concept") == "concept"
    assert scope.merge(None, "segment") == "segment"
    assert scope.merge("nonsense", "scene") == "scene"


def test_scene_scope_only_ever_comes_from_the_judge():
    # the arithmetic never claims a shot is the violation...
    assert scope.structural(7.0, 30.0, 7.0, 1) != "scene"
    # ...but the judge saying so is believed and escalates
    assert scope.merge("scene", "segment") == "scene"


def test_scope_closes_methods_that_cannot_reach_it():
    assert scope.allows("frame", "overlay")[0]
    ok, why = scope.allows("scene", "overlay")
    assert not ok and "whole shot" in why
    # a premise whose element cannot be swapped offers nothing at all
    ok, why = scope.allows("concept", "bridge", substitutable=False)
    assert not ok and "premise" in why
    assert all(not o["available"]
               for o in costs.options(3.0, 0.0, "concept", False))
    # a shot-level one offers only regeneration
    scene = {o["key"]: o["available"] for o in costs.options(6.0, 0.0, "scene")}
    assert scene == {"overlay": False, "track": False, "bridge": True}


def test_a_premise_can_still_be_fixable_when_the_element_is_swappable():
    """The slingshot gag is legal; the child in it is not. Putting an adult
    in the same slingshot leaves the joke standing, so this is expensive
    rather than impossible."""
    ok, why = scope.allows("concept", "bridge", substitutable=True)
    assert ok
    ok, why = scope.allows("concept", "overlay", substitutable=True)
    assert not ok and "regenerated" in why
    assert "still works" in scope.verdict("concept", True)

    # an advertisement for alcohol is not an advertisement for alcohol with
    # the alcohol removed: nothing reaches that
    assert not scope.allows("concept", "bridge", substitutable=False)[0]
    assert "different film" in scope.verdict("concept", False)


def test_the_console_prices_a_recast_and_refuses_the_patches(monkeypatch):
    from customs import costs
    opts = {o["key"]: o for o in costs.options(6.0, 0.0, "concept", True)}
    assert opts["bridge"]["available"] and opts["bridge"]["eur"] > 1.0
    assert not opts["overlay"]["available"]
    # and the day's budget still governs it
    broke = {o["key"]: o for o in costs.options(6.0, costs.DAILY_BUDGET_EUR, "concept", True)}
    assert not broke["bridge"]["available"] and "budget" in broke["bridge"]["why_not"]
