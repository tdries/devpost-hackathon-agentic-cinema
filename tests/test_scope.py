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
    ok, why = scope.allows("concept", "bridge")
    assert not ok and "premise" in why
    # a premise-level violation offers nothing at all
    assert all(not o["available"] for o in costs.options(3.0, 0.0, "concept"))
    # a shot-level one offers only regeneration
    scene = {o["key"]: o["available"] for o in costs.options(6.0, 0.0, "scene")}
    assert scene == {"overlay": False, "track": False, "bridge": True}
