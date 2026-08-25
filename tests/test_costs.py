"""What a fix costs, and what the day's budget allows."""
import pytest
from types import SimpleNamespace

from customs import costs
from customs.store import Store


def test_a_patch_is_cents_and_a_bridge_is_euros():
    assert costs.estimate("overlay", 2.0) == 0.04
    assert costs.estimate("bridge", 2.0) > 1.0
    # Veo will not emit less than four seconds, so a two second span and a
    # four second span cost the same
    assert costs.estimate("bridge", 2.0) == costs.estimate("bridge", 4.0)
    assert costs.estimate("bridge", 8.0) > costs.estimate("bridge", 4.0)
    with pytest.raises(ValueError):
        costs.estimate("teleport", 2.0)


def test_a_long_scene_cannot_be_bridged_at_all():
    ok, why = costs.available("bridge", 20.0, spent_today=0.0)
    assert not ok and "longer than Veo will generate" in why
    ok, _ = costs.available("bridge", 8.0, spent_today=0.0)
    assert ok


def test_the_day_s_budget_closes_the_expensive_option_and_leaves_the_cheap_one():
    price = costs.estimate("bridge", 4.0)
    nearly_spent = costs.DAILY_BUDGET_EUR - price + 0.01
    ok, why = costs.available("bridge", 4.0, spent_today=nearly_spent)
    assert not ok and "budget" in why
    # patching never touches the generation budget
    assert costs.available("overlay", 4.0, spent_today=99.0)[0]


def test_every_option_is_priced_and_says_what_it_is_for():
    opts = costs.options(3.0, spent_today=0.0)
    assert [o["key"] for o in opts] == ["overlay", "track", "bridge"]
    for o in opts:
        assert o["eur"] > 0 and o["length"] and o["complexity"] and o["best_for"]
    # tracking is honestly reported as not built rather than quietly offered
    assert not next(o for o in opts if o["key"] == "track")["available"]


def test_three_concrete_choices_per_finding():
    fallback = [s["label"] for s in costs.suggestions("alcohol_tobacco_drugs")]
    assert len(fallback) == 3
    assert any("Remove" in s for s in fallback)
    # One dimension covers alcohol, tobacco and drugs, so the table that
    # answers for all three may not name one: this offered "swap the drink
    # for a non-alcoholic one" over a lit cigarette.
    assert not any("drink" in s.lower() or "glass" in s.lower()
                   or "alcohol" in s.lower() for s in fallback)
    # an unmapped dimension still offers three real choices
    assert len(costs.suggestions("something_new")) == 3


def test_the_judges_own_remedies_beat_the_table():
    """A remedy written against the frame wins over one written per category."""
    finding = SimpleNamespace(remedies=[
        {"label": "Swap the cigarette for a pen", "directive": "..."},
        {"label": "Erase the rising smoke", "directive": "..."},
        {"label": "Empty the hand", "directive": "..."},
    ])
    picked = costs.suggestions("alcohol_tobacco_drugs", finding)
    assert [s["label"] for s in picked] == [r["label"] for r in finding.remedies]
    # addressed by position: no model-written prose goes through a form field
    assert [s["key"] for s in picked] == ["remedy:0", "remedy:1", "remedy:2"]
    # a finding judged before remedies existed still gets the table
    assert costs.suggestions("alcohol_tobacco_drugs",
                             SimpleNamespace(remedies=[]))[0]["key"] == "swap"


def test_spend_is_recorded_per_utc_day(tmp_path):
    store = Store(tmp_path / "s.db")
    assert store.spent_today() == 0.0
    store.record_spend("bridge", 1.88, "run_a", "fnd_a")
    store.record_spend("bridge", 1.88, "run_a", "fnd_b")
    assert store.spent_today() == pytest.approx(3.76)
    # yesterday's spending does not eat today's budget
    store.record_spend("bridge", 4.0, "run_a", "fnd_old", now=0.0)
    assert store.spent_today() == pytest.approx(3.76)
