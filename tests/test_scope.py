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


def test_scope_advises_but_never_closes_a_method():
    """Scope says what fits. It does not decide what the operator may run.

    It used to: at concept scope every method was refused, which walled off
    the findings people most wanted to try something on. The judgement was
    often right and still not the gate's to make -- the verifier decides
    whether an edit cleared, and it is better at that than a rule of thumb.
    """
    assert scope.allows("frame", "overlay")[0]
    # allows() still reports the mismatch, and the reason still reads well
    fits, why = scope.allows("scene", "overlay")
    assert not fits and "whole shot" in why
    fits, why = scope.allows("concept", "bridge", substitutable=False)
    assert not fits and "premise" in why

    # ...but nothing it says takes an option away
    hopeless = {o["key"]: o for o in costs.options(3.0, 0.0, "concept", False)}
    assert hopeless["overlay"]["available"]
    assert hopeless["bridge"]["available"]
    # the mismatch travels as a caveat the picker shows beside the option
    assert "premise" in hopeless["overlay"]["caveat"]

    scene = {o["key"]: o["available"] for o in costs.options(6.0, 0.0, "scene")}
    # nothing is off any more: scope advises, it does not close doors, and
    # track stopped being a promise once relight propagation landed
    assert scene == {"overlay": True, "track": True, "bridge": True}


def test_a_premise_can_still_be_fixable_when_the_element_is_swappable():
    """The slingshot gag is legal; the child in it is not. Putting an adult
    in the same slingshot leaves the joke standing, so this is expensive
    rather than impossible."""
    ok, why = scope.allows("concept", "bridge", substitutable=True)
    assert ok
    fits, why = scope.allows("concept", "overlay", substitutable=True)
    assert not fits and "regenerated" in why
    # a poor fit is still offered, at its own price
    assert {o["key"] for o in costs.options(6.0, 0.0, "concept", True)
            if o["available"]} == {"overlay", "track", "bridge"}
    assert "still works" in scope.verdict("concept", True)

    # an advertisement for alcohol is not an advertisement for alcohol with
    # the alcohol removed: nothing reaches that
    assert not scope.allows("concept", "bridge", substitutable=False)[0]
    assert "different film" in scope.verdict("concept", False)


def test_the_console_prices_a_recast_and_still_offers_the_patches(monkeypatch):
    from customs import costs
    opts = {o["key"]: o for o in costs.options(6.0, 0.0, "concept", True)}
    assert opts["bridge"]["available"] and opts["bridge"]["eur"] > 1.0
    # the patch is a poor fit at this scope and is offered anyway, flagged
    assert opts["overlay"]["available"] and opts["overlay"]["caveat"]
    # what still governs is money and physics, not shape
    broke = {o["key"]: o for o in costs.options(6.0, costs.DAILY_BUDGET_EUR, "concept", True)}
    assert not broke["bridge"]["available"] and "budget" in broke["bridge"]["why_not"]
